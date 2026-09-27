"""Backblaze B2 storage: a semantic backend protocol (real + fake), and a client shaped like `drive.DriveClient`.

B2 is a flat object store. This module makes it look like the folder tree the rest of the pipeline expects:

- A **folder** is a key prefix ending in "/" (the root is ""). Its id *is* the prefix. Creating a folder writes a
  zero-byte marker object `<prefix>.aiprimer-folder` so an empty folder can be found; markers are never listed.
- A **file** id is its object key (`<prefix><name>`). Renaming or moving an object changes its key, so
  `rename()`, `move()` and the adapter's `update_file()` return the NEW dict and callers must store the new id.
- Every method returns the same dict shape as the Drive client (`id, name, mimeType, parents, size, appProperties,
  webViewLink, trashed`) plus `key` and `file_id` (the B2 fileId of the current version).
- `appProperties` map to B2 file info (at most 10 lowercase keys).

Scripts stay deterministic: nothing here calls a model.
"""

from __future__ import annotations

import base64
import hashlib
import mimetypes
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Protocol
from urllib.parse import quote

from .drive import FOLDER_MIME

__all__ = [
    "B2Backend",
    "B2DriveAdapter",
    "B2Error",
    "B2StorageClient",
    "FOLDER_MARKER",
    "RealB2Backend",
    "is_cap_error",
]

AUTH_URL = "https://api.backblazeb2.com/b2api/v2/b2_authorize_account"
API_PATH = "b2api/v2"
FOLDER_MARKER = ".aiprimer-folder"
MARKER_INFO = {"aiprimer": "folder"}
MARKER_NAMES = frozenset({FOLDER_MARKER, ".bzEmpty"})  # .bzEmpty is what the Backblaze web UI writes for empty folders
MAX_ATTEMPTS = 5
MAX_INFO_ITEMS = 10
MAX_SIGNED_SECONDS = 7 * 24 * 3600  # 604800: the longest download authorization B2 issues
COPY_LIMIT_BYTES = 5 * 1024**3  # b2_copy_file handles sources up to 5 GB in one call
OCTET = "application/octet-stream"
_REAUTH_CODES = ("expired_auth_token", "bad_auth_token")
_CAP_WORD = re.compile(r"(?<![a-z])cap(?![a-z])")


# --------------------------------------------------------------------------- errors


class B2Error(RuntimeError):
    """An error answered by the B2 API (or raised locally in the same shape)."""

    def __init__(self, status: int, code: str, message: str):
        super().__init__(f"B2 {status} {code}: {message}")
        self.status = int(status)
        self.code = code
        self.message = message


def is_cap_error(exc: BaseException) -> bool:
    """True when the account refused a write because its storage cap is reached (free tier: 10 GB, HTTP 403)."""
    code = str(getattr(exc, "code", "") or "").lower()
    if "cap_exceeded" in code or "storage_cap" in code:
        return True
    text = str(exc).lower()
    return "exceeded" in text and bool(_CAP_WORD.search(text))


# --------------------------------------------------------------------------- protocol


class B2Backend(Protocol):
    """What a B2 bucket must offer. Entries are dicts
    `{"key", "file_id", "size", "content_type", "info", "is_folder", "uploaded_at"}`."""

    bucket_name: str

    def bucket_info(self) -> dict: ...  # {"bucket_id", "bucket_name", "bucket_type": "allPrivate"|"allPublic"}
    def list_prefix(self, prefix: str, delimiter: str | None = "/") -> list[dict]: ...
    def get_by_key(self, key: str) -> dict | None: ...
    def get_by_id(self, file_id: str) -> dict: ...
    def put(self, path: Path, key: str, content_type: str, info: dict[str, str]) -> dict: ...
    def copy(self, file_id: str, new_key: str, info: dict[str, str] | None = None, content_type: str | None = None) -> dict: ...
    def delete(self, key: str, file_id: str) -> None: ...
    def download(self, key: str, dest: Path) -> Path: ...
    def public_url(self, key: str) -> str: ...
    def signed_url(self, key: str, seconds: int = MAX_SIGNED_SECONDS) -> str: ...
    def account_email(self) -> str: ...


# --------------------------------------------------------------------------- helpers


def _norm_prefix(prefix: str | None) -> str:
    p = (prefix or "").strip().lstrip("/")
    if p and not p.endswith("/"):
        p += "/"
    return p


def _parent_of(key: str) -> str:
    """Prefix (ending "/") that holds `key`; "" at the root. Works for file keys and folder prefixes."""
    k = key[:-1] if key.endswith("/") else key
    i = k.rfind("/")
    return k[: i + 1] if i >= 0 else ""


def _basename(key: str) -> str:
    k = key[:-1] if key.endswith("/") else key
    return k.rsplit("/", 1)[-1]


def _is_marker(key: str) -> bool:
    return _basename(key) in MARKER_NAMES


def _check_name(name: str, what: str) -> str:
    name = (name or "").strip()
    if not name or "/" in name or name in (".", ".."):
        raise ValueError(f"invalid {what} name {name!r}: must be non-empty and contain no '/'")
    return name


def _sha1_of(path: Path) -> str:
    h = hashlib.sha1()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _str_info(info: dict[str, Any] | None) -> dict[str, str]:
    return {str(k): str(v) for k, v in (info or {}).items()}


def _tmp_file(content: bytes, suffix: str = ".txt") -> Path:
    with tempfile.NamedTemporaryFile("wb", suffix=suffix, delete=False) as fh:
        fh.write(content)
        return Path(fh.name)


def _entry_from_api(f: dict, is_folder: bool = False) -> dict:
    """Normalise a B2 file object (list, get_file_info, upload, copy, finish_large_file) into an entry dict."""
    size = f.get("contentLength", f.get("size", 0))
    return {
        "key": f.get("fileName", ""),
        "file_id": f.get("fileId") or "",
        "size": int(size or 0),
        "content_type": f.get("contentType") or "",
        "info": dict(f.get("fileInfo") or {}),
        "is_folder": is_folder,
        "uploaded_at": int(f.get("uploadTimestamp") or 0),
    }


def _error_from_response(resp: Any) -> B2Error:
    status = int(getattr(resp, "status_code", 0) or 0)
    code, message = "http_error", ""
    try:
        data = resp.json()
        if isinstance(data, dict):
            code = str(data.get("code") or code)
            message = str(data.get("message") or "")
    except Exception:
        text = getattr(resp, "text", "") or ""
        message = str(text)[:200]
    return B2Error(status, code, message or f"HTTP {status}")


def _retry_after(resp: Any, default: float) -> float:
    try:
        value = (getattr(resp, "headers", None) or {}).get("Retry-After")
        if value:
            return min(float(value), 60.0)
    except Exception:
        pass
    return min(default, 60.0)


# --------------------------------------------------------------------------- real backend


class RealB2Backend:
    """Native B2 API v2 over `requests`. Proxies and CA bundles come from the environment; TLS is left alone.

    Tokens live only in memory. No log line or error message ever contains a header or a token."""

    def __init__(self, key_id: str | None, application_key: str | None, bucket_name: str, *, session: Any = None, large_file_threshold_bytes: int = 100 * 1024 * 1024):
        if session is None:
            import requests  # local import: tests inject a stub session and never need the package

            session = requests.Session()
        self.session = session
        self.key_id = key_id
        self.application_key = application_key
        self.bucket_name = bucket_name
        self.large_file_threshold_bytes = int(large_file_threshold_bytes)
        self.timeout: Any = (30, 600)
        self._auth: dict | None = None
        self._bucket: dict | None = None
        self._sleep: Callable[[float], None] = time.sleep

    @classmethod
    def from_env(cls, key_id_env: str, app_key_env: str, bucket_name: str) -> "RealB2Backend":
        """Build from environment variable names; a missing variable becomes None (a proxy may inject the auth)."""
        key_id = os.environ.get(key_id_env) if key_id_env else None
        app_key = os.environ.get(app_key_env) if app_key_env else None
        return cls(key_id or None, app_key or None, bucket_name)

    # ---- auth + transport
    def authorize(self) -> dict:
        if self._auth is None:
            headers: dict[str, str] = {}
            if self.key_id is not None and self.application_key is not None:
                raw = f"{self.key_id}:{self.application_key}".encode("utf-8")
                headers["Authorization"] = "Basic " + base64.b64encode(raw).decode("ascii")
            data = self._http("GET", AUTH_URL, headers=headers).json()
            self._auth = {
                "accountId": data.get("accountId", ""),
                "authorizationToken": data.get("authorizationToken", ""),
                "apiUrl": (data.get("apiUrl") or "").rstrip("/"),
                "downloadUrl": (data.get("downloadUrl") or "").rstrip("/"),
                "allowed": dict(data.get("allowed") or {}),
                "recommendedPartSize": int(data.get("recommendedPartSize") or 100 * 1024 * 1024),
                "absoluteMinimumPartSize": int(data.get("absoluteMinimumPartSize") or 5 * 1024 * 1024),
            }
        return self._auth

    def _http(self, method: str, url: str, *, headers: dict | None = None, json: Any = None, data: Any = None, stream: bool = False) -> Any:
        """One request with the generic retry: 429 and 5xx sleep (Retry-After or exponential) up to MAX_ATTEMPTS."""
        delay = 1.0
        for attempt in range(MAX_ATTEMPTS):
            resp = self.session.request(method, url, headers=headers or {}, json=json, data=data, stream=stream, timeout=self.timeout)
            status = int(getattr(resp, "status_code", 0) or 0)
            if 200 <= status < 300:
                return resp
            if (status == 429 or status >= 500) and attempt < MAX_ATTEMPTS - 1:
                self._sleep(_retry_after(resp, delay))
                delay *= 2
                continue
            raise _error_from_response(resp)
        raise B2Error(0, "retry_exhausted", f"{method} to B2 failed after {MAX_ATTEMPTS} attempts")

    def _with_auth(self, fn: Callable[[dict], Any]) -> Any:
        """Run `fn(auth)`; on an expired/bad account token re-authorize once and run it again."""
        try:
            return fn(self.authorize())
        except B2Error as exc:
            if exc.status == 401 and exc.code in _REAUTH_CODES:
                self._auth = None
                return fn(self.authorize())
            raise

    def _api(self, name: str, body: dict) -> dict:
        def call(auth: dict) -> Any:
            return self._http("POST", f"{auth['apiUrl']}/{API_PATH}/{name}", headers={"Authorization": auth["authorizationToken"]}, json=body)

        return self._with_auth(call).json()

    # ---- bucket
    def bucket_info(self) -> dict:
        if self._bucket is None:
            auth = self.authorize()
            allowed = auth.get("allowed") or {}
            body: dict[str, Any] = {"accountId": auth["accountId"]}
            if allowed.get("bucketId"):
                body["bucketId"] = allowed["bucketId"]
            else:
                body["bucketName"] = self.bucket_name
            try:
                buckets = self._api("b2_list_buckets", body).get("buckets", [])
            except B2Error as exc:
                if exc.status not in (401, 403) or not allowed.get("bucketId"):
                    raise
                # key restricted to one bucket and not allowed to list: trust what the authorization said
                buckets = [{"bucketId": allowed["bucketId"], "bucketName": allowed.get("bucketName") or self.bucket_name, "bucketType": ""}]
            match = next((b for b in buckets if b.get("bucketName") == self.bucket_name), None)
            if match is None:
                visible = ", ".join(str(b.get("bucketName", "?")) for b in buckets) or "none"
                raise B2Error(404, "bucket_not_found", f"bucket {self.bucket_name!r} is not reachable with this key (visible: {visible})")
            self._bucket = {"bucket_id": match.get("bucketId", ""), "bucket_name": match.get("bucketName", self.bucket_name), "bucket_type": match.get("bucketType") or ""}
        return dict(self._bucket)

    def _bucket_id(self) -> str:
        return self.bucket_info()["bucket_id"]

    # ---- listing + metadata
    def list_prefix(self, prefix: str, delimiter: str | None = "/") -> list[dict]:
        out: list[dict] = []
        start: str | None = None
        while True:
            body: dict[str, Any] = {"bucketId": self._bucket_id(), "prefix": prefix, "maxFileCount": 1000}
            if delimiter:
                body["delimiter"] = delimiter
            if start:
                body["startFileName"] = start
            resp = self._api("b2_list_file_names", body)
            for f in resp.get("files", []):
                action = f.get("action", "upload")
                if action == "hide":
                    continue
                out.append(_entry_from_api(f, is_folder=(action == "folder")))
            start = resp.get("nextFileName")
            if not start:
                return out

    def get_by_key(self, key: str) -> dict | None:
        resp = self._api("b2_list_file_names", {"bucketId": self._bucket_id(), "startFileName": key, "prefix": key, "maxFileCount": 1})
        for f in resp.get("files", []):
            if f.get("fileName") == key and f.get("action", "upload") == "upload":
                return _entry_from_api(f)
        return None

    def get_by_id(self, file_id: str) -> dict:
        return _entry_from_api(self._api("b2_get_file_info", {"fileId": file_id}))

    # ---- upload
    def put(self, path: Path, key: str, content_type: str, info: dict[str, str]) -> dict:
        path = Path(path)
        info = _str_info(info)
        if len(info) > MAX_INFO_ITEMS:
            raise ValueError(f"B2 file info holds at most {MAX_INFO_ITEMS} items; got {len(info)}")
        size = path.stat().st_size
        if size > self.large_file_threshold_bytes:
            return self._put_large(path, key, content_type or OCTET, info, size)
        return self._put_small(path, key, content_type or OCTET, info, size)

    @staticmethod
    def _info_headers(info: dict[str, str]) -> dict[str, str]:
        return {f"X-Bz-Info-{k.lower()}": quote(v) for k, v in info.items()}

    def _upload_attempts(self, fetch_target: Callable[[], dict], make_headers: Callable[[str], dict], open_body: Callable[[], Any]) -> dict:
        """POST to an upload URL. On 408/429/5xx, an expired upload token or a connection error, fetch a fresh
        upload URL and retry with exponential backoff (up to MAX_ATTEMPTS)."""
        delay = 1.0
        last_error: Exception | None = None
        for attempt in range(MAX_ATTEMPTS):
            target = fetch_target()
            headers = make_headers(target["authorizationToken"])
            body = open_body()
            resp = None
            try:
                resp = self.session.request("POST", target["uploadUrl"], headers=headers, data=body, timeout=self.timeout)
            except OSError as exc:  # requests' ConnectionError / Timeout are OSErrors
                last_error = exc
            finally:
                if hasattr(body, "close"):
                    body.close()
            if resp is not None:
                status = int(getattr(resp, "status_code", 0) or 0)
                if 200 <= status < 300:
                    return resp.json()
                last_error = _error_from_response(resp)
                retryable = status in (408, 429) or status >= 500 or (status == 401 and last_error.code in _REAUTH_CODES)
                if not retryable:
                    raise last_error
            if attempt < MAX_ATTEMPTS - 1:
                self._sleep(min(delay, 60.0))
                delay *= 2
        assert last_error is not None
        raise last_error

    def _put_small(self, path: Path, key: str, content_type: str, info: dict[str, str], size: int) -> dict:
        sha1 = _sha1_of(path)
        base_headers = {"X-Bz-File-Name": quote(key, safe="/"), "Content-Type": content_type, "Content-Length": str(size), "X-Bz-Content-Sha1": sha1, **self._info_headers(info)}
        # A zero-byte body goes as bytes: for an empty *file object* `requests` cannot size the stream and adds
        # `Transfer-Encoding: chunked` next to our `Content-Length: 0`, which Backblaze's nginx rejects with an
        # HTML 400 (seen live with the empty folder markers).
        open_body: Callable[[], Any] = (lambda: open(path, "rb")) if size else (lambda: b"")
        result = self._upload_attempts(
            lambda: self._api("b2_get_upload_url", {"bucketId": self._bucket_id()}),
            lambda token: {"Authorization": token, **base_headers},
            open_body,
        )
        return _entry_from_api(result)

    def _put_large(self, path: Path, key: str, content_type: str, info: dict[str, str], size: int) -> dict:
        auth = self.authorize()
        part_size = max(int(auth["recommendedPartSize"]), int(auth["absoluteMinimumPartSize"]))
        started = self._api("b2_start_large_file", {"bucketId": self._bucket_id(), "fileName": key, "contentType": content_type, "fileInfo": info})
        file_id = started["fileId"]
        sha1s: list[str] = []
        try:
            with open(path, "rb") as fh:
                part_no = 1
                while True:
                    chunk = fh.read(part_size)
                    if not chunk:
                        break
                    sha1 = hashlib.sha1(chunk).hexdigest()
                    self._upload_attempts(
                        lambda: self._api("b2_get_upload_part_url", {"fileId": file_id}),
                        lambda token, n=part_no, s=sha1, c=chunk: {"Authorization": token, "X-Bz-Part-Number": str(n), "Content-Length": str(len(c)), "X-Bz-Content-Sha1": s},
                        lambda c=chunk: c,
                    )
                    sha1s.append(sha1)
                    part_no += 1
            return _entry_from_api(self._api("b2_finish_large_file", {"fileId": file_id, "partSha1Array": sha1s}))
        except Exception:
            try:
                self._api("b2_cancel_large_file", {"fileId": file_id})
            except Exception:
                pass
            raise

    # ---- copy / delete / download
    def copy(self, file_id: str, new_key: str, info: dict[str, str] | None = None, content_type: str | None = None) -> dict:
        source = self.get_by_id(file_id)
        if source["size"] > COPY_LIMIT_BYTES:
            raise B2Error(400, "too_large_to_copy", f"{source['key']!r} is {source['size']} bytes; b2_copy_file takes sources up to 5 GB (part copies are not implemented)")
        body: dict[str, Any] = {"sourceFileId": file_id, "fileName": new_key}
        if info is None:
            body["metadataDirective"] = "COPY"
        else:
            body["metadataDirective"] = "REPLACE"
            body["contentType"] = content_type or source["content_type"] or OCTET
            body["fileInfo"] = _str_info(info)
        return _entry_from_api(self._api("b2_copy_file", body))

    def delete(self, key: str, file_id: str) -> None:
        self._api("b2_delete_file_version", {"fileName": key, "fileId": file_id})

    def download(self, key: str, dest: Path) -> Path:
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)

        def call(auth: dict) -> Any:
            url = f"{auth['downloadUrl']}/file/{self.bucket_name}/{quote(key, safe='/')}"
            return self._http("GET", url, headers={"Authorization": auth["authorizationToken"]}, stream=True)

        resp = self._with_auth(call)
        try:
            with open(dest, "wb") as fh:
                if hasattr(resp, "iter_content"):
                    for chunk in resp.iter_content(1024 * 1024):
                        if chunk:
                            fh.write(chunk)
                else:
                    fh.write(resp.content)
        finally:
            close = getattr(resp, "close", None)
            if close:
                close()
        return dest

    # ---- links
    def public_url(self, key: str) -> str:
        return f"{self.authorize()['downloadUrl']}/file/{self.bucket_name}/{quote(key, safe='/')}"

    def signed_url(self, key: str, seconds: int = MAX_SIGNED_SECONDS) -> str:
        seconds = max(1, min(int(seconds), MAX_SIGNED_SECONDS))
        resp = self._api("b2_get_download_authorization", {"bucketId": self._bucket_id(), "fileNamePrefix": key, "validDurationInSeconds": seconds})
        return f"{self.public_url(key)}?Authorization={resp.get('authorizationToken', '')}"

    def account_email(self) -> str:
        return ""  # the B2 API does not expose the account's email


# --------------------------------------------------------------------------- client


class B2StorageClient:
    """Folder-path, idempotent-upload and move helpers on a B2 bucket, with the method names and return shapes of
    `drive.DriveClient`, so ingest/taxonomy/resources/accounts/setup code runs unchanged.

    Ids: folders are prefixes ending "/" (root ""), files are object keys. `rename()` and `move()` of a file
    return the new dict with a NEW id. With `id_folders=True` (default) folders are named by a permanent id and
    `rename()` of a folder is a no-op; with `id_folders=False` the objects under the prefix are copied to the new
    prefix and the old versions deleted. The bucket's own name is accepted as an alias of the root prefix.
    `convert_to` (Google Docs conversion) is accepted and ignored."""

    def __init__(self, backend: B2Backend, *, id_folders: bool = True):
        self.b2 = backend
        self.id_folders = id_folders
        self.backend = B2DriveAdapter(self)
        self._bucket: dict | None = None

    # ---- prefix helpers
    def _prefix(self, parent_id: str | None) -> str:
        p = _norm_prefix(parent_id)
        if p.rstrip("/") == self.b2.bucket_name:
            return ""
        return p

    def _bucket_info(self) -> dict:
        if self._bucket is None:
            self._bucket = self.b2.bucket_info()
        return self._bucket

    def _is_public(self) -> bool:
        try:
            return self._bucket_info().get("bucket_type") == "allPublic"
        except Exception:
            return False

    def _link(self, key: str) -> str:
        if self._is_public():
            return self.b2.public_url(key)
        return f"b2://{self.b2.bucket_name}/{key}"

    def _folder_dict(self, prefix: str) -> dict:
        prefix = _norm_prefix(prefix)
        return {
            "id": prefix,
            "name": _basename(prefix),
            "mimeType": FOLDER_MIME,
            "parents": [_parent_of(prefix)] if prefix else [],
            "size": "0",
            "appProperties": {},
            "webViewLink": f"b2://{self.b2.bucket_name}/{prefix}",
            "trashed": False,
            "key": prefix,
            "file_id": "",
        }

    def _file_dict(self, entry: dict) -> dict:
        key = entry["key"]
        return {
            "id": key,
            "name": _basename(key),
            "mimeType": entry.get("content_type") or OCTET,
            "parents": [_parent_of(key)],
            "size": str(int(entry.get("size", 0) or 0)),
            "appProperties": dict(entry.get("info") or {}),
            "webViewLink": self._link(key),
            "trashed": False,
            "key": key,
            "file_id": entry.get("file_id", ""),
        }

    def _folder_exists(self, prefix: str) -> bool:
        if prefix == "":
            return True
        if self.b2.get_by_key(prefix + FOLDER_MARKER) is not None:
            return True
        return bool(self.b2.list_prefix(prefix, "/"))

    def _files_under(self, prefix: str, recursive: bool) -> list[dict]:
        entries = self.b2.list_prefix(prefix, None if recursive else "/")
        return [e for e in entries if not e.get("is_folder") and not _is_marker(e["key"])]

    def _relocate_prefix(self, old: str, new: str) -> dict:
        """Copy every object under `old` to `new` and delete the originals (markers included)."""
        if old == new:
            return self._folder_dict(new)
        if new.startswith(old):
            raise ValueError(f"cannot move folder {old!r} into itself ({new!r})")
        for e in self.b2.list_prefix(old, None):
            if e.get("is_folder"):
                continue
            self.b2.copy(e["file_id"], new + e["key"][len(old) :])
            self.b2.delete(e["key"], e["file_id"])
        return self._folder_dict(new)

    # ---- folders
    def list_children(self, parent_id: str, folders_only: bool = False) -> list[dict]:
        prefix = self._prefix(parent_id)
        out: list[dict] = []
        for e in self.b2.list_prefix(prefix, "/"):
            if e.get("is_folder"):
                out.append(self._folder_dict(e["key"]))
            elif not folders_only and not _is_marker(e["key"]):
                out.append(self._file_dict(e))
        return out

    def find_child_folder(self, parent_id: str | None, name: str) -> dict | None:
        prefix = f"{self._prefix(parent_id)}{_check_name(name, 'folder')}/"
        return self._folder_dict(prefix) if self._folder_exists(prefix) else None

    def find_root_folder(self, name: str) -> dict | None:
        return self.find_child_folder("", name)

    def ensure_folder(self, parent_id: str | None, name: str) -> dict:
        """Find or create the folder `<parent><name>/`; creation writes the zero-byte marker object."""
        existing = self.find_child_folder(parent_id, name)
        if existing:
            return existing
        prefix = f"{self._prefix(parent_id)}{_check_name(name, 'folder')}/"
        marker = _tmp_file(b"")
        try:
            self.b2.put(marker, prefix + FOLDER_MARKER, OCTET, dict(MARKER_INFO))
        finally:
            marker.unlink(missing_ok=True)
        return self._folder_dict(prefix)

    def shared_drive(self, drive_id: str) -> dict | None:
        return None  # no such thing on B2

    def container(self, container_id: str) -> dict | None:
        """The place this account writes into: always the bucket, when `container_id` is "", the bucket name
        or an existing prefix; None otherwise (or when the bucket cannot be reached)."""
        bucket = self.b2.bucket_name
        try:
            prefix = self._prefix(container_id)
            if prefix and not self._folder_exists(prefix):
                return None
            self._bucket_info()
        except Exception:
            return None
        return {"kind": "bucket", "id": bucket, "name": bucket}

    def write_test(self, parent_id: str) -> dict:
        """Prove whether this key can write under `parent_id` (puts a 1-byte object, then deletes it)."""
        key = f"{self._prefix(parent_id)}aiprimer-write-test.txt"
        path = _tmp_file(b"x")
        try:
            entry = self.b2.put(path, key, "text/plain", {"aiprimer": "write-test"})
        except Exception as exc:
            return {"can_write": False, "error": str(exc)}
        finally:
            path.unlink(missing_ok=True)
        try:
            self.b2.delete(entry["key"], entry["file_id"])
        except Exception:
            pass
        return {"can_write": True, "file_id": entry.get("file_id", "")}

    def ensure_path(self, root_id: str, names: list[str]) -> list[dict]:
        out: list[dict] = []
        parent = root_id
        for name in names:
            folder = self.ensure_folder(parent, name)
            out.append(folder)
            parent = folder["id"]
        return out

    def rename(self, file_id: str, name: str) -> dict:
        """File: server-side copy to `<parent><name>` (metadata kept) then delete the old version; returns the new
        dict, whose id is the new key. Folder: unchanged with id_folders (the id is the name), else relocated."""
        meta = self.get(file_id)
        if meta["mimeType"] == FOLDER_MIME:
            if self.id_folders or not meta["id"]:
                return meta
            return self._relocate_prefix(meta["id"], f"{meta['parents'][0]}{_check_name(name, 'folder')}/")
        new_key = meta["parents"][0] + _check_name(name, "file")
        if new_key == meta["id"]:
            return meta
        entry = self.b2.copy(meta["file_id"], new_key)
        self.b2.delete(meta["id"], meta["file_id"])
        return self._file_dict(entry)

    def get(self, file_id: str) -> dict:
        """Metadata of a file key or a folder prefix (with or without the trailing "/"); "" is the root."""
        fid = (file_id or "").strip().lstrip("/")
        if fid == "" or fid.endswith("/") or fid == self.b2.bucket_name:
            prefix = self._prefix(fid)
            if self._folder_exists(prefix):
                return self._folder_dict(prefix)
            raise B2Error(404, "not_found", f"no folder {prefix!r} in bucket {self.b2.bucket_name}")
        entry = self.b2.get_by_key(fid)
        if entry is not None:
            return self._file_dict(entry)
        if self._folder_exists(fid + "/"):
            return self._folder_dict(fid + "/")
        raise B2Error(404, "not_found", f"no object or folder {fid!r} in bucket {self.b2.bucket_name}")

    # ---- files
    def find_by_app_property(self, key: str, value: str, parent_id: str | None = None) -> list[dict]:
        """Files whose info[key] == value: direct children of `parent_id`, or the whole bucket when it is empty."""
        if parent_id:
            entries = self._files_under(self._prefix(parent_id), recursive=False)
        else:
            entries = self._files_under("", recursive=True)
        return [self._file_dict(e) for e in entries if (e.get("info") or {}).get(key) == value]

    def upload(self, path: Path, name: str, parent_id: str, app_properties: dict[str, str] | None = None, mime_type: str | None = None, convert_to: str | None = None) -> dict:
        """Upload, or return the existing file when one with the same resource_id+role info is already in the folder."""
        if app_properties and app_properties.get("resource_id"):
            role = app_properties.get("role", "")
            for f in self.find_by_app_property("resource_id", app_properties["resource_id"], parent_id):
                if (f.get("appProperties") or {}).get("role", "") == role:
                    return f
        mime = mime_type or mimetypes.guess_type(str(path))[0] or OCTET
        key = self._prefix(parent_id) + _check_name(name, "file")
        return self._file_dict(self.b2.put(Path(path), key, mime, _str_info(app_properties)))

    def move(self, file_id: str, new_parent_id: str) -> dict:
        """Copy to `<new parent><basename>` and delete the original; returns the new dict (new id). No-op when
        already there. Folders are relocated object by object."""
        meta = self.get(file_id)
        new_prefix = self._prefix(new_parent_id)
        if meta["mimeType"] == FOLDER_MIME:
            if not meta["id"] or meta["parents"] == [new_prefix]:
                return meta
            return self._relocate_prefix(meta["id"], f"{new_prefix}{meta['name']}/")
        if meta["parents"] == [new_prefix]:
            return meta
        entry = self.b2.copy(meta["file_id"], new_prefix + meta["name"])
        self.b2.delete(meta["id"], meta["file_id"])
        return self._file_dict(entry)

    def download(self, file_id: str, dest: Path) -> Path:
        return self.b2.download(str(file_id), Path(dest))

    def quota(self) -> dict:
        """B2 has no per-bucket limit to read: `limit` is None; `usage` sums every object in the bucket."""
        usage = sum(int(e.get("size", 0) or 0) for e in self.b2.list_prefix("", None) if not e.get("is_folder"))
        return {"limit": None, "usage": usage, "email": self.b2.account_email()}

    def share_anyone_reader(self, file_id: str) -> None:
        return None  # visibility is a bucket setting (allPublic); nothing to do per object

    def signed_url(self, file_id: str, seconds: int = MAX_SIGNED_SECONDS) -> str:
        """Time-limited download link for an object in a private bucket."""
        return self.b2.signed_url(str(file_id), seconds)


class B2DriveAdapter:
    """The Drive-backend-shaped methods some callers use through `client.backend.…`, mapped onto the B2 client.
    `update_file` returns the NEW dict when the key changes (name or parent)."""

    def __init__(self, client: B2StorageClient):
        self.client = client

    @property
    def b2(self) -> B2Backend:
        return self.client.b2

    @property
    def bucket_name(self) -> str:
        return self.client.b2.bucket_name

    def get_file(self, file_id: str) -> dict:
        return self.client.get(file_id)

    def create_folder(self, name: str, parent_id: str | None) -> dict:
        return self.client.ensure_folder(parent_id, name)

    def upload_file(self, path: Path, name: str, parent_id: str, mime_type: str, app_properties: dict[str, str] | None, convert_to: str | None = None) -> dict:
        key = self.client._prefix(parent_id) + _check_name(name, "file")
        return self.client._file_dict(self.b2.put(Path(path), key, mime_type or OCTET, _str_info(app_properties)))

    def update_file(self, file_id: str, *, name: str | None = None, add_parents: list[str] | None = None, remove_parents: list[str] | None = None, app_properties: dict[str, str] | None = None, media_path: Path | None = None, media_mime: str | None = None) -> dict:
        client = self.client
        meta = client.get(file_id)
        if meta["mimeType"] == FOLDER_MIME:
            if add_parents:
                meta = client.move(meta["id"], add_parents[0])
            if name is not None:
                meta = client.rename(meta["id"], name)
            return meta
        parent = client._prefix(add_parents[0]) if add_parents else meta["parents"][0]
        new_key = parent + (_check_name(name, "file") if name is not None else meta["name"])
        new_info = {**meta["appProperties"], **_str_info(app_properties)} if app_properties else None
        if media_path is not None:
            entry = self.b2.put(Path(media_path), new_key, media_mime or meta["mimeType"], new_info if new_info is not None else meta["appProperties"])
            self.b2.delete(meta["id"], meta["file_id"])  # the previous version (same or other key)
            return client._file_dict(entry)
        if new_key == meta["id"] and new_info is None:
            return meta
        entry = self.b2.copy(meta["file_id"], new_key, info=new_info, content_type=meta["mimeType"] if new_info is not None else None)
        self.b2.delete(meta["id"], meta["file_id"])
        return client._file_dict(entry)

    def download_file(self, file_id: str, dest: Path) -> Path:
        return self.client.download(file_id, dest)

    def about_quota(self) -> dict:
        return self.client.quota()

    def create_permission(self, file_id: str, role: str, type_: str) -> dict:
        return {"id": ""}  # no per-object permissions on B2

    def trash_file(self, file_id: str) -> None:
        """B2 has no trash: a file is deleted (its current version); a folder loses every object under it."""
        meta = self.client.get(file_id)
        if meta["mimeType"] == FOLDER_MIME:
            for e in self.b2.list_prefix(meta["id"], None):
                if not e.get("is_folder"):
                    self.b2.delete(e["key"], e["file_id"])
            return
        self.b2.delete(meta["id"], meta["file_id"])
