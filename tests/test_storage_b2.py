"""Offline tests for pipeline.storage_b2: the client on the fake backend, and the real backend on a stub session."""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
from urllib.parse import quote

import pytest

from pipeline.drive import FOLDER_MIME
from pipeline.storage_b2 import AUTH_URL, FOLDER_MARKER, MAX_SIGNED_SECONDS, B2Error, B2StorageClient, RealB2Backend, is_cap_error
from tests.fake_b2 import FakeB2Backend


@pytest.fixture
def fake_b2(tmp_path):
    return FakeB2Backend(storage_dir=tmp_path / "b2_blobs")


@pytest.fixture
def client(fake_b2):
    return B2StorageClient(fake_b2)


def _file(tmp_path: Path, name: str, content: str = "hello") -> Path:
    p = tmp_path / name
    p.write_text(content)
    return p


# --------------------------------------------------------------------------- folders


def test_ensure_folder_writes_marker_once_and_lists_it_as_folder(client, fake_b2):
    raw = client.ensure_folder("", "raw")
    assert raw["id"] == "raw/" and raw["name"] == "raw" and raw["mimeType"] == FOLDER_MIME
    assert raw["parents"] == [""] and raw["size"] == "0" and raw["file_id"] == ""
    assert fake_b2.keys() == [f"raw/{FOLDER_MARKER}"]
    assert fake_b2.info_of(f"raw/{FOLDER_MARKER}") == {"aiprimer": "folder"}
    puts_before = fake_b2.calls.count(f"put:raw/{FOLDER_MARKER}")
    assert client.ensure_folder(None, "raw")["id"] == "raw/"
    assert fake_b2.calls.count(f"put:raw/{FOLDER_MARKER}") == puts_before
    assert [c["id"] for c in client.list_children("", folders_only=True)] == ["raw/"]
    assert client.list_children("raw/") == []  # the marker is never listed as a file
    assert client.find_child_folder("", "raw")["id"] == "raw/"
    assert client.find_child_folder("", "nope") is None
    assert client.find_root_folder("raw")["id"] == "raw/"


def test_ensure_path_is_idempotent_and_nested(client, fake_b2, tmp_path):
    chain = client.ensure_path("raw/", ["T-aaa111", "T-bbb222"])
    assert [c["id"] for c in chain] == ["raw/T-aaa111/", "raw/T-aaa111/T-bbb222/"]
    assert chain[1]["parents"] == ["raw/T-aaa111/"] and chain[1]["name"] == "T-bbb222"
    again = client.ensure_path("raw", ["T-aaa111", "T-bbb222"])  # parent without the trailing slash
    assert [c["id"] for c in again] == [c["id"] for c in chain]
    client.upload(_file(tmp_path, "a.txt"), "a.txt", chain[1]["id"])
    kids = client.list_children("raw/T-aaa111/")
    assert [k["id"] for k in kids] == ["raw/T-aaa111/T-bbb222/"]
    assert [k["name"] for k in client.list_children(chain[1]["id"])] == ["a.txt"]
    assert client.list_children(chain[1]["id"], folders_only=True) == []
    with pytest.raises(ValueError):
        client.ensure_folder("raw/", "a/b")


def test_get_resolves_root_folders_and_files(client, fake_b2, tmp_path):
    root = client.get("")
    assert root["id"] == "" and root["mimeType"] == FOLDER_MIME and root["parents"] == []
    assert client.get("ai-primer-raw")["id"] == ""  # the bucket name is an alias of the root
    folder = client.ensure_folder("", "raw")
    assert client.get("raw")["id"] == "raw/" and client.get("raw/") == folder
    up = client.upload(_file(tmp_path, "a.txt"), "a.txt", "raw/")
    assert client.get(up["id"]) == up
    with pytest.raises(B2Error) as ei:
        client.get("raw/missing.txt")
    assert ei.value.status == 404
    with pytest.raises(B2Error):
        client.get("nothing-here/")


# --------------------------------------------------------------------------- files


def test_upload_sets_info_and_is_idempotent_per_resource_and_role(client, fake_b2, tmp_path):
    folder = client.ensure_folder("raw/", "T-aaa111")
    path = _file(tmp_path, "a.txt")
    up1 = client.upload(path, "2021-06-10 - Author - Title [R-YT-x].txt", folder["id"], {"resource_id": "R-YT-x", "role": "raw", "aiprimer": "1"})
    assert up1["id"] == "raw/T-aaa111/2021-06-10 - Author - Title [R-YT-x].txt"
    assert up1["key"] == up1["id"] and up1["name"] == "2021-06-10 - Author - Title [R-YT-x].txt"
    assert up1["parents"] == ["raw/T-aaa111/"] and up1["mimeType"] == "text/plain" and up1["size"] == "5"
    assert up1["appProperties"] == {"resource_id": "R-YT-x", "role": "raw", "aiprimer": "1"}
    assert up1["file_id"].startswith("fid") and up1["trashed"] is False
    assert up1["webViewLink"] == "b2://ai-primer-raw/" + up1["id"]
    up2 = client.upload(path, "different name.txt", folder["id"], {"resource_id": "R-YT-x", "role": "raw"})
    assert up2["id"] == up1["id"]  # same resource + role: the existing object is returned
    up3 = client.upload(path, "a.extracted.md", folder["id"], {"resource_id": "R-YT-x", "role": "text"}, mime_type="text/markdown")
    assert up3["id"] != up1["id"] and up3["mimeType"] == "text/markdown"
    assert fake_b2.calls.count("put:" + up1["id"]) == 1
    forced = client.upload(path, "b.txt", folder["id"], {"kind": "other"}, convert_to="application/vnd.google-apps.document")
    assert forced["mimeType"] == "text/plain"  # convert_to is ignored on B2


def test_find_by_app_property_direct_children_or_whole_bucket(client, tmp_path):
    a = client.ensure_folder("raw/", "T-a")
    b = client.ensure_folder(a["id"], "T-b")
    path = _file(tmp_path, "x.txt")
    f1 = client.upload(path, "x.txt", a["id"], {"resource_id": "R-1", "role": "raw"})
    f2 = client.upload(path, "y.txt", b["id"], {"resource_id": "R-1", "role": "text"})
    assert [f["id"] for f in client.find_by_app_property("resource_id", "R-1", a["id"])] == [f1["id"]]
    assert sorted(f["id"] for f in client.find_by_app_property("resource_id", "R-1")) == sorted([f1["id"], f2["id"]])
    assert client.find_by_app_property("role", "text", a["id"]) == []
    assert [f["id"] for f in client.find_by_app_property("role", "text")] == [f2["id"]]


def test_move_returns_new_id_and_old_key_is_gone(client, fake_b2, tmp_path):
    src = client.ensure_folder("raw/", "T-a")
    dst = client.ensure_folder("raw/", "T-b")
    up = client.upload(_file(tmp_path, "a.txt", "content"), "a.txt", src["id"], {"resource_id": "R-1", "role": "raw"})
    moved = client.move(up["id"], dst["id"])
    assert moved["id"] == "raw/T-b/a.txt" and moved["parents"] == [dst["id"]]
    assert moved["appProperties"] == up["appProperties"] and moved["file_id"] != up["file_id"]
    assert up["id"] not in fake_b2.keys() and fake_b2.read(moved["id"]) == b"content"
    copies = len([c for c in fake_b2.calls if c.startswith("copy:")])
    assert client.move(moved["id"], "raw/T-b") == moved  # already there: no copy
    assert len([c for c in fake_b2.calls if c.startswith("copy:")]) == copies
    with pytest.raises(B2Error):
        client.get(up["id"])


def test_rename_file_keeps_content_and_info_and_changes_id(client, fake_b2, tmp_path):
    folder = client.ensure_folder("raw/", "T-a")
    up = client.upload(_file(tmp_path, "a.txt", "content"), "old name.txt", folder["id"], {"resource_id": "R-1", "role": "raw"})
    renamed = client.rename(up["id"], "new name.txt")
    assert renamed["id"] == "raw/T-a/new name.txt" and renamed["name"] == "new name.txt"
    assert renamed["appProperties"] == {"resource_id": "R-1", "role": "raw"} and renamed["mimeType"] == "text/plain"
    assert fake_b2.read(renamed["id"]) == b"content" and up["id"] not in fake_b2.keys()
    assert client.rename(renamed["id"], "new name.txt") == renamed  # same name: nothing happens


def test_rename_folder_is_a_noop_with_id_folders(client, fake_b2, tmp_path):
    folder = client.ensure_folder("raw/", "T-a")
    client.upload(_file(tmp_path, "a.txt"), "a.txt", folder["id"])
    before = fake_b2.keys()
    assert client.rename(folder["id"], "Boxing") == folder
    assert fake_b2.keys() == before
    assert client.get(folder["id"])["name"] == "T-a"


def test_rename_and_move_folder_relocate_objects_without_id_folders(fake_b2, tmp_path):
    client = B2StorageClient(fake_b2, id_folders=False)
    assert client.id_folders is False
    folder = client.ensure_folder("raw/", "Old")
    sub = client.ensure_folder(folder["id"], "Sub")
    client.upload(_file(tmp_path, "a.txt", "aa"), "a.txt", sub["id"], {"resource_id": "R-1"})
    renamed = client.rename(folder["id"], "New")
    assert renamed["id"] == "raw/New/"
    assert fake_b2.keys() == [f"raw/New/{FOLDER_MARKER}", f"raw/New/Sub/{FOLDER_MARKER}", "raw/New/Sub/a.txt"]
    assert fake_b2.info_of("raw/New/Sub/a.txt") == {"resource_id": "R-1"}
    moved = client.move("raw/New/Sub/", "raw/")
    assert moved["id"] == "raw/Sub/" and "raw/Sub/a.txt" in fake_b2.keys()
    with pytest.raises(ValueError):
        client.move("raw/", "raw/Sub/")


def test_download_round_trip(client, tmp_path):
    folder = client.ensure_folder("raw/", "T-a")
    up = client.upload(_file(tmp_path, "a.txt", "round trip"), "a.txt", folder["id"])
    dest = client.download(up["id"], tmp_path / "out" / "copy.txt")
    assert dest.read_text() == "round trip"
    assert client.backend.download_file(up["id"], tmp_path / "copy2.txt").read_text() == "round trip"


def test_quota_sums_every_object(client, fake_b2, tmp_path):
    a = client.ensure_folder("raw/", "T-a")
    client.upload(_file(tmp_path, "a.txt", "12345"), "a.txt", a["id"])
    client.upload(_file(tmp_path, "b.txt", "1234567"), "b.txt", client.ensure_folder(a["id"], "T-b")["id"])
    assert client.quota() == {"limit": None, "usage": 12, "email": ""}
    assert client.backend.about_quota()["usage"] == 12


def test_write_test_success_and_failure(client, fake_b2):
    ok = client.write_test("raw/")
    assert ok["can_write"] is True and ok["file_id"].startswith("fid")
    assert "raw/aiprimer-write-test.txt" not in fake_b2.keys()
    assert "put:raw/aiprimer-write-test.txt" in fake_b2.calls and "delete:raw/aiprimer-write-test.txt" in fake_b2.calls
    fake_b2.fail_put_with = B2Error(401, "unauthorized", "key lacks writeFiles")
    bad = client.write_test("raw/")
    assert bad == {"can_write": False, "error": "B2 401 unauthorized: key lacks writeFiles"}


def test_cap_error_is_raised_and_recognised(client, fake_b2, tmp_path):
    fake_b2.cap_bytes = 8
    folder = client.ensure_folder("raw/", "T-a")
    client.upload(_file(tmp_path, "a.txt", "1234"), "a.txt", folder["id"])
    with pytest.raises(B2Error) as ei:
        client.upload(_file(tmp_path, "b.txt", "123456"), "b.txt", folder["id"])
    exc = ei.value
    assert exc.status == 403 and exc.code == "cap_exceeded" and str(exc) == "B2 403 cap_exceeded: storage cap exceeded"
    assert is_cap_error(exc)
    assert is_cap_error(B2Error(403, "storage_cap_exceeded", "x"))
    assert is_cap_error(RuntimeError("Storage cap exceeded for this account"))
    assert not is_cap_error(RuntimeError("capacity exceeded"))
    assert not is_cap_error(B2Error(401, "unauthorized", "no cap here"))
    assert "b.txt" not in " ".join(fake_b2.keys())


def test_web_view_link_depends_on_bucket_type(tmp_path):
    path = _file(tmp_path, "a.txt")
    private = B2StorageClient(FakeB2Backend(bucket_type="allPrivate"))
    up = private.upload(path, "my file.txt", "raw/")
    assert up["webViewLink"] == "b2://ai-primer-raw/raw/my file.txt"
    public = B2StorageClient(FakeB2Backend(bucket_name="ai-primer-media", bucket_type="allPublic"))
    up = public.upload(path, "my file.txt", "media/")
    assert up["webViewLink"] == "https://f000.backblazeb2.com/file/ai-primer-media/media/my%20file.txt"
    public.share_anyone_reader(up["id"])  # no-op
    assert public.signed_url(up["id"], 10**9).endswith("?Authorization=fake-download-token")
    assert public.b2.signed[-1] == (up["id"], MAX_SIGNED_SECONDS)


def test_container_and_shared_drive(client):
    bucket = {"kind": "bucket", "id": "ai-primer-raw", "name": "ai-primer-raw"}
    assert client.container("") == bucket and client.container("ai-primer-raw") == bucket
    assert client.container("raw/") is None
    client.ensure_folder("", "raw")
    assert client.container("raw/") == bucket and client.container("raw") == bucket
    assert client.shared_drive("anything") is None


# --------------------------------------------------------------------------- adapter (Drive-style backend calls)


def test_adapter_update_file_and_trash(client, fake_b2, tmp_path):
    folder = client.ensure_folder("raw/", "T-a")
    other = client.ensure_folder("raw/", "T-b")
    up = client.upload(_file(tmp_path, "a.txt", "content"), "a.txt", folder["id"], {"resource_id": "R-1", "role": "raw"})
    assert client.backend.get_file(up["id"]) == up
    assert client.backend is not client.b2 and client.backend.b2 is fake_b2
    renamed = client.backend.update_file(up["id"], name="renamed.txt", app_properties={"natural_key": "nk", "role": "raw"})
    assert renamed["id"] == "raw/T-a/renamed.txt" and up["id"] not in fake_b2.keys()
    assert renamed["appProperties"] == {"resource_id": "R-1", "role": "raw", "natural_key": "nk"}
    assert fake_b2.read(renamed["id"]) == b"content"
    same = client.backend.update_file(renamed["id"])
    assert same == renamed
    tagged = client.backend.update_file(renamed["id"], app_properties={"x": "1"})
    assert tagged["id"] == renamed["id"] and tagged["appProperties"]["x"] == "1" and tagged["file_id"] != renamed["file_id"]
    assert len(fake_b2.versions[tagged["id"]]) == 1  # the superseded version was deleted
    moved = client.backend.update_file(tagged["id"], add_parents=[other["id"]], remove_parents=[folder["id"]])
    assert moved["id"] == "raw/T-b/renamed.txt" and moved["parents"] == [other["id"]]
    replaced = client.backend.update_file(moved["id"], media_path=_file(tmp_path, "new.txt", "new bytes"), media_mime="text/markdown")
    assert replaced["id"] == moved["id"] and replaced["mimeType"] == "text/markdown"
    assert fake_b2.read(replaced["id"]) == b"new bytes" and len(fake_b2.versions[replaced["id"]]) == 1
    client.backend.trash_file(replaced["id"])
    assert replaced["id"] not in fake_b2.keys()
    extra = client.backend.upload_file(_file(tmp_path, "z.txt"), "z.txt", other["id"], "text/plain", {"role": "raw"})
    assert extra["id"] == "raw/T-b/z.txt" and extra["appProperties"] == {"role": "raw"}
    client.backend.trash_file(other["id"])  # a folder: everything under it goes
    assert [k for k in fake_b2.keys() if k.startswith("raw/T-b/")] == []
    assert client.backend.create_permission(extra["id"], "reader", "anyone") == {"id": ""}


# --------------------------------------------------------------------------- real backend on a stub session


class Resp:
    def __init__(self, status: int = 200, body=None, headers: dict | None = None, content: bytes = b""):
        self.status_code = status
        self._body = body
        self.headers = headers or {}
        self.content = content

    def json(self):
        if self._body is None:
            raise ValueError("no json body")
        return self._body

    @property
    def text(self):
        return json.dumps(self._body) if self._body is not None else self.content.decode("utf-8", "replace")

    def iter_content(self, chunk_size):
        yield self.content

    def close(self):
        pass


AUTH_BODY = {
    "accountId": "acc1",
    "apiUrl": "https://api001.backblazeb2.com",
    "downloadUrl": "https://f001.backblazeb2.com",
    "allowed": {"bucketId": "bkt1", "bucketName": "ai-primer-raw", "capabilities": ["listFiles", "readFiles", "writeFiles"]},
    "recommendedPartSize": 100_000_000,
    "absoluteMinimumPartSize": 5_000_000,
}
UPLOAD_URL = "https://pod-000.backblaze.com/b2api/v2/b2_upload_file/bkt1/c001"
PART_URL = "https://pod-000.backblaze.com/b2api/v2/b2_upload_part/4_zbig/c002"


def file_body(key: str, size: int = 5, info: dict | None = None, file_id: str = "4_zabc") -> dict:
    return {"fileName": key, "fileId": file_id, "action": "upload", "contentLength": size, "contentType": "text/plain", "fileInfo": info or {}, "uploadTimestamp": 1700000000000}


class StubSession:
    """Minimal `requests.Session` stand-in: records every call, answers from `routes` or the built-in defaults."""

    def __init__(self, auth_body: dict | None = None):
        self.calls: list[dict] = []
        self.routes: dict[str, object] = {}  # api name -> Resp | [Resp, ...] | callable(rec) -> Resp
        self.auth_calls = 0
        self.auth_body = auth_body or AUTH_BODY

    def request(self, method, url, **kw):
        data = kw.get("data")
        body = data.read() if hasattr(data, "read") else data
        rec = {"method": method, "url": url, "headers": dict(kw.get("headers") or {}), "json": kw.get("json"), "body": body, "name": self._name(url)}
        self.calls.append(rec)
        route = self.routes.get(rec["name"])
        if route is not None:
            if callable(route):
                return route(rec)
            if isinstance(route, list):
                return route.pop(0)
            return route
        return self._default(rec)

    @staticmethod
    def _name(url: str) -> str:
        if "/b2_upload_file/" in url:
            return "upload"
        if "/b2_upload_part/" in url:
            return "upload_part"
        if "/file/" in url:
            return "download"
        return url.rsplit("/", 1)[-1]

    def _default(self, rec: dict) -> Resp:
        name = rec["name"]
        if name == "b2_authorize_account":
            self.auth_calls += 1
            return Resp(200, {**self.auth_body, "authorizationToken": f"TOKEN{self.auth_calls}"})
        if name == "b2_list_buckets":
            return Resp(200, {"buckets": [{"bucketId": "bkt1", "bucketName": "ai-primer-raw", "bucketType": "allPrivate"}]})
        if name == "b2_get_upload_url":
            return Resp(200, {"uploadUrl": UPLOAD_URL, "authorizationToken": "UPTOKEN"})
        if name == "b2_get_upload_part_url":
            return Resp(200, {"uploadUrl": PART_URL, "authorizationToken": "PARTTOKEN"})
        raise AssertionError(f"unexpected B2 call {rec['method']} {rec['url']}")

    def named(self, name: str) -> list[dict]:
        return [c for c in self.calls if c["name"] == name]


def make_backend(session: StubSession, key_id="kid", app_key="sec", **kw) -> RealB2Backend:
    backend = RealB2Backend(key_id, app_key, "ai-primer-raw", session=session, **kw)
    backend._sleep = lambda s: None
    return backend


def test_authorize_sends_basic_auth_only_when_keys_are_given():
    s = StubSession()
    auth = make_backend(s).authorize()
    call = s.calls[0]
    assert call["method"] == "GET" and call["url"] == AUTH_URL
    assert call["headers"]["Authorization"] == "Basic " + base64.b64encode(b"kid:sec").decode()
    assert auth["authorizationToken"] == "TOKEN1" and auth["apiUrl"] == "https://api001.backblazeb2.com"
    s2 = StubSession()
    make_backend(s2, key_id=None, app_key=None).authorize()
    assert "Authorization" not in s2.calls[0]["headers"]
    assert make_backend(s2).authorize() is not None and s2.auth_calls == 2


def test_from_env_reads_variables_or_none(monkeypatch):
    monkeypatch.setenv("B2_KEY_ID_1", "id1")
    monkeypatch.delenv("B2_APP_KEY_1", raising=False)
    backend = RealB2Backend.from_env("B2_KEY_ID_1", "B2_APP_KEY_1", "ai-primer-raw")
    assert backend.key_id == "id1" and backend.application_key is None and backend.bucket_name == "ai-primer-raw"
    assert backend.large_file_threshold_bytes == 100 * 1024 * 1024


def test_list_prefix_paginates_and_skips_hidden_files():
    s = StubSession()
    page1 = {"files": [file_body("raw/T-a/a.txt", 5, {"resource_id": "R-1"}), {"fileName": "raw/T-a/sub/", "fileId": None, "action": "folder", "contentLength": 0}, {**file_body("raw/T-a/gone.txt"), "action": "hide"}], "nextFileName": "raw/T-a/m"}
    page2 = {"files": [file_body("raw/T-a/z.txt", 7, file_id="4_zzz")], "nextFileName": None}
    s.routes["b2_list_file_names"] = [Resp(200, page1), Resp(200, page2)]
    entries = make_backend(s).list_prefix("raw/T-a/", "/")
    assert [e["key"] for e in entries] == ["raw/T-a/a.txt", "raw/T-a/sub/", "raw/T-a/z.txt"]
    assert entries[0]["size"] == 5 and entries[0]["info"] == {"resource_id": "R-1"} and entries[0]["file_id"] == "4_zabc"
    assert entries[1]["is_folder"] is True and entries[2]["size"] == 7
    lists = s.named("b2_list_file_names")
    assert lists[0]["json"] == {"bucketId": "bkt1", "prefix": "raw/T-a/", "maxFileCount": 1000, "delimiter": "/"}
    assert lists[1]["json"]["startFileName"] == "raw/T-a/m"
    assert all(c["headers"]["Authorization"] == "TOKEN1" for c in lists)
    assert lists[0]["url"] == "https://api001.backblazeb2.com/b2api/v2/b2_list_file_names"
    assert s.named("b2_list_buckets")[0]["json"] == {"accountId": "acc1", "bucketId": "bkt1"}


def test_small_upload_headers_and_body(tmp_path):
    s = StubSession()
    key = "raw/T-a/2021-06-10 - Author - Title [R-YT-x].pdf"
    data = b"%PDF-1.4 fake"
    path = tmp_path / "f.pdf"
    path.write_bytes(data)
    s.routes["upload"] = lambda rec: Resp(200, file_body(key, len(data), {"resource_id": "R-YT-x", "role": "raw"}))
    entry = make_backend(s).put(path, key, "application/pdf", {"resource_id": "R-YT-x", "role": "raw", "source_url": "https://youtu.be/x?a=1"})
    assert entry["key"] == key and entry["file_id"] == "4_zabc" and entry["size"] == len(data)
    up = s.named("upload")[0]
    h = up["headers"]
    assert up["method"] == "POST" and up["url"] == UPLOAD_URL and up["body"] == data
    assert h["Authorization"] == "UPTOKEN"
    assert h["X-Bz-File-Name"] == quote(key, safe="/") and "%20" in h["X-Bz-File-Name"] and "%5B" in h["X-Bz-File-Name"]
    assert h["X-Bz-Content-Sha1"] == hashlib.sha1(data).hexdigest()
    assert h["Content-Length"] == str(len(data)) and h["Content-Type"] == "application/pdf"
    assert h["X-Bz-Info-resource_id"] == "R-YT-x" and h["X-Bz-Info-role"] == "raw"
    assert h["X-Bz-Info-source_url"] == quote("https://youtu.be/x?a=1")
    assert s.named("b2_get_upload_url")[0]["json"] == {"bucketId": "bkt1"}


def test_upload_retries_with_fresh_url_on_503_and_gives_up_on_400(tmp_path):
    path = tmp_path / "a.txt"
    path.write_text("abc")
    s = StubSession()
    s.routes["upload"] = [Resp(503, {"status": 503, "code": "service_unavailable", "message": "busy"}), Resp(200, file_body("k", 3))]
    assert make_backend(s).put(path, "k", "text/plain", {})["key"] == "k"
    assert len(s.named("b2_get_upload_url")) == 2 and len(s.named("upload")) == 2
    s2 = StubSession()
    s2.routes["upload"] = Resp(400, {"status": 400, "code": "bad_request", "message": "Sha1 did not match"})
    with pytest.raises(B2Error) as ei:
        make_backend(s2).put(path, "k", "text/plain", {})
    assert ei.value.code == "bad_request" and len(s2.named("upload")) == 1
    with pytest.raises(ValueError):
        make_backend(StubSession()).put(path, "k", "text/plain", {f"k{i}": "v" for i in range(11)})


def test_large_upload_splits_into_parts(tmp_path):
    s = StubSession(auth_body={**AUTH_BODY, "recommendedPartSize": 4, "absoluteMinimumPartSize": 4})
    data = b"0123456789"
    path = tmp_path / "big.bin"
    path.write_bytes(data)
    s.routes["b2_start_large_file"] = Resp(200, {"fileId": "4_zbig"})
    s.routes["upload_part"] = lambda rec: Resp(200, {"partNumber": rec["headers"]["X-Bz-Part-Number"]})
    s.routes["b2_finish_large_file"] = Resp(200, {**file_body("raw/big.bin", 10, file_id="4_zbig"), "contentType": "video/mp4"})
    backend = make_backend(s, large_file_threshold_bytes=8)
    entry = backend.put(path, "raw/big.bin", "video/mp4", {"resource_id": "R-1"})
    assert entry["file_id"] == "4_zbig" and entry["size"] == 10
    assert s.named("b2_start_large_file")[0]["json"] == {"bucketId": "bkt1", "fileName": "raw/big.bin", "contentType": "video/mp4", "fileInfo": {"resource_id": "R-1"}}
    parts = s.named("upload_part")
    assert [p["headers"]["X-Bz-Part-Number"] for p in parts] == ["1", "2", "3"]
    assert [p["body"] for p in parts] == [b"0123", b"4567", b"89"]
    assert all(p["headers"]["Authorization"] == "PARTTOKEN" and p["headers"]["Content-Length"] == str(len(p["body"])) for p in parts)
    shas = [hashlib.sha1(chunk).hexdigest() for chunk in (b"0123", b"4567", b"89")]
    assert [p["headers"]["X-Bz-Content-Sha1"] for p in parts] == shas
    assert s.named("b2_finish_large_file")[0]["json"] == {"fileId": "4_zbig", "partSha1Array": shas}
    assert s.named("b2_get_upload_part_url")[0]["json"] == {"fileId": "4_zbig"}
    # a failing part cancels the large file
    s2 = StubSession(auth_body={**AUTH_BODY, "recommendedPartSize": 4, "absoluteMinimumPartSize": 4})
    s2.routes["b2_start_large_file"] = Resp(200, {"fileId": "4_zbig"})
    s2.routes["upload_part"] = Resp(400, {"status": 400, "code": "bad_request", "message": "no"})
    s2.routes["b2_cancel_large_file"] = Resp(200, {"fileId": "4_zbig"})
    with pytest.raises(B2Error):
        make_backend(s2, large_file_threshold_bytes=8).put(path, "raw/big.bin", "video/mp4", {})
    assert s2.named("b2_cancel_large_file")[0]["json"] == {"fileId": "4_zbig"}


def test_expired_token_reauthorizes_once():
    s = StubSession()
    expired = Resp(401, {"status": 401, "code": "expired_auth_token", "message": "expired"})
    s.routes["b2_get_file_info"] = [expired, Resp(200, file_body("raw/a.txt"))]
    backend = make_backend(s)
    assert backend.get_by_id("4_zabc")["key"] == "raw/a.txt"
    infos = s.named("b2_get_file_info")
    assert [c["headers"]["Authorization"] for c in infos] == ["TOKEN1", "TOKEN2"] and s.auth_calls == 2
    s2 = StubSession()
    s2.routes["b2_get_file_info"] = [Resp(401, {"status": 401, "code": "bad_auth_token", "message": "bad"})] * 3
    with pytest.raises(B2Error) as ei:
        make_backend(s2).get_by_id("4_zabc")
    assert ei.value.status == 401 and s2.auth_calls == 2 and len(s2.named("b2_get_file_info")) == 2
    s3 = StubSession()
    s3.routes["b2_get_file_info"] = Resp(401, {"status": 401, "code": "unauthorized", "message": "capability missing"})
    with pytest.raises(B2Error):
        make_backend(s3).get_by_id("4_zabc")
    assert s3.auth_calls == 1  # a plain unauthorized is not retried


def test_cap_error_maps_to_b2error_without_retry(tmp_path):
    path = tmp_path / "a.txt"
    path.write_text("abc")
    s = StubSession()
    s.routes["upload"] = Resp(403, {"status": 403, "code": "cap_exceeded", "message": "Cap exceeded for storage"})
    with pytest.raises(B2Error) as ei:
        make_backend(s).put(path, "raw/a.txt", "text/plain", {})
    assert ei.value.status == 403 and ei.value.code == "cap_exceeded" and is_cap_error(ei.value)
    assert str(ei.value) == "B2 403 cap_exceeded: Cap exceeded for storage"
    assert len(s.named("upload")) == 1


def test_generic_retry_honours_retry_after_then_raises():
    s = StubSession()
    slept: list[float] = []
    s.routes["b2_get_file_info"] = [Resp(429, {"status": 429, "code": "too_many_requests", "message": "slow"}, headers={"Retry-After": "3"}), Resp(200, file_body("raw/a.txt"))]
    backend = make_backend(s)
    backend._sleep = slept.append
    assert backend.get_by_id("x")["key"] == "raw/a.txt" and slept == [3.0]
    s2 = StubSession()
    s2.routes["b2_get_file_info"] = [Resp(500, {"status": 500, "code": "internal_error", "message": "x"})] * 5
    with pytest.raises(B2Error) as ei:
        make_backend(s2).get_by_id("x")
    assert ei.value.status == 500 and len(s2.named("b2_get_file_info")) == 5


def test_copy_delete_get_by_key_download_and_signed_url(tmp_path):
    s = StubSession()
    s.routes["b2_get_file_info"] = Resp(200, file_body("raw/a.txt", 3, {"role": "raw"}))
    s.routes["b2_copy_file"] = lambda rec: Resp(200, file_body(rec["json"]["fileName"], 3, rec["json"].get("fileInfo", {"role": "raw"}), file_id="4_znew"))
    s.routes["b2_delete_file_version"] = Resp(200, {"fileName": "raw/a.txt", "fileId": "4_zabc"})
    s.routes["b2_list_file_names"] = lambda rec: Resp(200, {"files": [file_body("raw/a.txt", 3)] if rec["json"]["startFileName"] == "raw/a.txt" else [file_body("raw/a.txt.bak")], "nextFileName": None})
    s.routes["download"] = lambda rec: Resp(200, content=b"abc")
    s.routes["b2_get_download_authorization"] = Resp(200, {"authorizationToken": "DLTOKEN"})
    backend = make_backend(s)
    copied = backend.copy("4_zabc", "raw/b.txt")
    assert copied["key"] == "raw/b.txt" and copied["file_id"] == "4_znew"
    assert s.named("b2_copy_file")[0]["json"] == {"sourceFileId": "4_zabc", "fileName": "raw/b.txt", "metadataDirective": "COPY"}
    backend.copy("4_zabc", "raw/c.txt", info={"role": "text"})
    assert s.named("b2_copy_file")[1]["json"] == {"sourceFileId": "4_zabc", "fileName": "raw/c.txt", "metadataDirective": "REPLACE", "contentType": "text/plain", "fileInfo": {"role": "text"}}
    backend.delete("raw/a.txt", "4_zabc")
    assert s.named("b2_delete_file_version")[0]["json"] == {"fileName": "raw/a.txt", "fileId": "4_zabc"}
    assert backend.get_by_key("raw/a.txt")["key"] == "raw/a.txt"
    assert backend.get_by_key("raw/a.txt.") is None
    assert s.named("b2_list_file_names")[0]["json"] == {"bucketId": "bkt1", "startFileName": "raw/a.txt", "prefix": "raw/a.txt", "maxFileCount": 1}
    dest = backend.download("raw/a b.txt", tmp_path / "dl" / "a.txt")
    assert dest.read_bytes() == b"abc"
    dl = s.named("download")[0]
    assert dl["url"] == "https://f001.backblazeb2.com/file/ai-primer-raw/raw/a%20b.txt" and dl["headers"]["Authorization"] == "TOKEN1"
    assert backend.public_url("raw/a b.txt") == "https://f001.backblazeb2.com/file/ai-primer-raw/raw/a%20b.txt"
    assert backend.signed_url("raw/a b.txt", 10**9) == "https://f001.backblazeb2.com/file/ai-primer-raw/raw/a%20b.txt?Authorization=DLTOKEN"
    assert s.named("b2_get_download_authorization")[0]["json"] == {"bucketId": "bkt1", "fileNamePrefix": "raw/a b.txt", "validDurationInSeconds": MAX_SIGNED_SECONDS}
    assert backend.account_email() == ""
    s.routes["b2_get_file_info"] = Resp(200, file_body("raw/huge.bin", 6 * 1024**3))
    with pytest.raises(B2Error) as ei:
        backend.copy("4_zhuge", "raw/huge2.bin")
    assert ei.value.code == "too_large_to_copy"


def test_bucket_info_falls_back_to_allowed_bucket_for_restricted_key():
    s = StubSession()
    s.routes["b2_list_buckets"] = Resp(401, {"status": 401, "code": "unauthorized", "message": "not allowed"})
    info = make_backend(s).bucket_info()
    assert info == {"bucket_id": "bkt1", "bucket_name": "ai-primer-raw", "bucket_type": ""}
    s2 = StubSession(auth_body={**AUTH_BODY, "allowed": {"capabilities": ["listBuckets"]}})
    s2.routes["b2_list_buckets"] = Resp(200, {"buckets": []})
    with pytest.raises(B2Error) as ei:
        make_backend(s2).bucket_info()
    assert ei.value.code == "bucket_not_found"
    assert s2.named("b2_list_buckets")[0]["json"] == {"accountId": "acc1", "bucketName": "ai-primer-raw"}


def test_client_on_real_backend_builds_public_links():
    s = StubSession()
    s.routes["b2_list_buckets"] = Resp(200, {"buckets": [{"bucketId": "bkt1", "bucketName": "ai-primer-raw", "bucketType": "allPublic"}]})
    s.routes["b2_list_file_names"] = Resp(200, {"files": [file_body("raw/T-a/a b.txt", 3, {"resource_id": "R-1"})], "nextFileName": None})
    client = B2StorageClient(make_backend(s))
    kids = client.list_children("raw/T-a/")
    assert kids[0]["webViewLink"] == "https://f001.backblazeb2.com/file/ai-primer-raw/raw/T-a/a%20b.txt"
    assert kids[0]["id"] == "raw/T-a/a b.txt" and kids[0]["file_id"] == "4_zabc"
    assert client.container("") == {"kind": "bucket", "id": "ai-primer-raw", "name": "ai-primer-raw"}


def test_zero_byte_upload_sends_bytes_not_an_empty_stream(tmp_path):
    """Folder markers are empty files. Handing `requests` an empty *file object* makes it add
    `Transfer-Encoding: chunked` next to `Content-Length: 0`, which Backblaze's nginx rejects with an HTML 400
    (seen live in `setup all`). The body must therefore go as plain bytes."""
    kinds: list[type] = []

    class TypedSession(StubSession):
        def request(self, method, url, **kw):
            if self._name(url) == "upload":
                kinds.append(type(kw.get("data")))
            return super().request(method, url, **kw)

    s = TypedSession()
    key = "raw/_Inbox/" + FOLDER_MARKER
    path = tmp_path / "marker"
    path.write_bytes(b"")
    s.routes["upload"] = lambda rec: Resp(200, file_body(key, 0, {"aiprimer": "folder"}))
    entry = make_backend(s).put(path, key, "application/octet-stream", {"aiprimer": "folder"})
    assert entry["key"] == key and entry["size"] == 0
    assert kinds == [bytes]
    h = s.named("upload")[0]["headers"]
    assert h["Content-Length"] == "0" and h["X-Bz-Content-Sha1"] == hashlib.sha1(b"").hexdigest()
    # a non-empty file still streams from disk
    kinds.clear()
    (tmp_path / "one").write_bytes(b"x")
    s.routes["upload"] = lambda rec: Resp(200, file_body("raw/one", 1))
    make_backend(s).put(tmp_path / "one", "raw/one", "text/plain", {})
    assert kinds and kinds[0] is not bytes
