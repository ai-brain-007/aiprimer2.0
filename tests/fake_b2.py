"""In-memory fake of the `pipeline.storage_b2.B2Backend` protocol, for offline tests.

Objects are kept per key as a list of versions (the latest wins, like B2). File ids are `fid00001`, `fid00002`, …
Bytes are kept in memory and, when `storage_dir` is given, also copied there under the file id."""

from __future__ import annotations

import itertools
from pathlib import Path
from urllib.parse import quote

from pipeline.storage_b2 import MAX_SIGNED_SECONDS, B2Error

_counter = itertools.count(1)


class FakeB2Backend:
    def __init__(self, bucket_name: str = "ai-primer-raw", bucket_type: str = "allPrivate", storage_dir: Path | None = None):
        self.bucket_name = bucket_name
        self.bucket_type = bucket_type
        self.storage_dir = storage_dir
        self.download_url = "https://f000.backblazeb2.com"
        self.versions: dict[str, list[dict]] = {}  # key -> versions, oldest first
        self.blobs: dict[str, bytes] = {}  # file_id -> bytes
        self.calls: list[str] = []
        self.fail_put_with: Exception | None = None
        self.cap_bytes: int | None = None
        self.signed: list[tuple[str, int]] = []

    # -- internals
    def _new_entry(self, key: str, data: bytes, content_type: str, info: dict) -> dict:
        n = next(_counter)
        entry = {"key": key, "file_id": f"fid{n:05d}", "size": len(data), "content_type": content_type, "info": {str(k): str(v) for k, v in info.items()}, "is_folder": False, "uploaded_at": 1_700_000_000_000 + n}
        self.versions.setdefault(key, []).append(entry)
        self.blobs[entry["file_id"]] = data
        if self.storage_dir:
            self.storage_dir.mkdir(parents=True, exist_ok=True)
            (self.storage_dir / entry["file_id"]).write_bytes(data)
        return dict(entry)

    def _current(self) -> dict[str, dict]:
        return {k: v[-1] for k, v in self.versions.items() if v}

    def _find_version(self, file_id: str) -> dict:
        for versions in self.versions.values():
            for v in versions:
                if v["file_id"] == file_id:
                    return v
        raise B2Error(404, "not_found", f"file id {file_id} is not in the bucket")

    def _check_cap(self, incoming: int) -> None:
        if self.cap_bytes is not None and self.total_bytes() + incoming > self.cap_bytes:
            raise B2Error(403, "cap_exceeded", "storage cap exceeded")

    # -- protocol
    def bucket_info(self) -> dict:
        self.calls.append("bucket_info")
        return {"bucket_id": f"bkt-{self.bucket_name}", "bucket_name": self.bucket_name, "bucket_type": self.bucket_type}

    def list_prefix(self, prefix: str, delimiter: str | None = "/") -> list[dict]:
        self.calls.append(f"list:{prefix}:{delimiter or ''}")
        out: list[dict] = []
        seen: set[str] = set()
        current = self._current()
        for key in sorted(current):
            if not key.startswith(prefix):
                continue
            rest = key[len(prefix) :]
            if delimiter and delimiter in rest:
                folder = prefix + rest.split(delimiter, 1)[0] + delimiter
                if folder not in seen:
                    seen.add(folder)
                    out.append({"key": folder, "file_id": "", "size": 0, "content_type": "", "info": {}, "is_folder": True, "uploaded_at": 0})
            else:
                out.append(dict(current[key]))
        return out

    def get_by_key(self, key: str) -> dict | None:
        self.calls.append(f"get:{key}")
        versions = self.versions.get(key)
        return dict(versions[-1]) if versions else None

    def get_by_id(self, file_id: str) -> dict:
        self.calls.append(f"get_id:{file_id}")
        return dict(self._find_version(file_id))

    def put(self, path: Path, key: str, content_type: str, info: dict[str, str]) -> dict:
        self.calls.append(f"put:{key}")
        if self.fail_put_with is not None:
            raise self.fail_put_with
        data = Path(path).read_bytes()
        self._check_cap(len(data))
        return self._new_entry(key, data, content_type, info)

    def copy(self, file_id: str, new_key: str, info: dict[str, str] | None = None, content_type: str | None = None) -> dict:
        src = self._find_version(file_id)
        self.calls.append(f"copy:{src['key']}->{new_key}")
        self._check_cap(src["size"])
        if info is None:  # COPY directive
            return self._new_entry(new_key, self.blobs[file_id], src["content_type"], src["info"])
        return self._new_entry(new_key, self.blobs[file_id], content_type or src["content_type"], info)

    def delete(self, key: str, file_id: str) -> None:
        self.calls.append(f"delete:{key}")
        versions = self.versions.get(key, [])
        for i, v in enumerate(versions):
            if v["file_id"] == file_id:
                versions.pop(i)
                self.blobs.pop(file_id, None)
                break
        else:
            raise B2Error(400, "file_not_present", f"{key} ({file_id}) is not in the bucket")
        if not versions:
            self.versions.pop(key, None)

    def download(self, key: str, dest: Path) -> Path:
        self.calls.append(f"download:{key}")
        versions = self.versions.get(key)
        if not versions:
            raise B2Error(404, "not_found", f"{key} is not in the bucket")
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(self.blobs[versions[-1]["file_id"]])
        return dest

    def public_url(self, key: str) -> str:
        return f"{self.download_url}/file/{self.bucket_name}/{quote(key, safe='/')}"

    def signed_url(self, key: str, seconds: int = MAX_SIGNED_SECONDS) -> str:
        seconds = max(1, min(int(seconds), MAX_SIGNED_SECONDS))
        self.signed.append((key, seconds))
        return f"{self.public_url(key)}?Authorization=fake-download-token"

    def account_email(self) -> str:
        return ""

    # -- test helpers
    def keys(self) -> list[str]:
        return sorted(self._current())

    def read(self, key: str) -> bytes:
        return self.blobs[self.versions[key][-1]["file_id"]]

    def info_of(self, key: str) -> dict:
        return dict(self.versions[key][-1]["info"])

    def total_bytes(self) -> int:
        return sum(v["size"] for versions in self.versions.values() for v in versions)
