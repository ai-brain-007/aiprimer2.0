"""In-memory fakes of the Google Sheets / Drive backends and the Apify runner, for tests."""

from __future__ import annotations

import itertools
import re
import shutil
from pathlib import Path

from pipeline.drive import FOLDER_MIME, GDOC_MIME

_counter = itertools.count(1)


class FakeSheetsBackend:
    def __init__(self):
        self.tabs: dict[str, list[list[str]]] = {}
        self.frozen: set[str] = set()
        self.calls: list[str] = []

    # -- helpers
    @staticmethod
    def _parse_range(range_a1: str):
        m = re.match(r"^'?([^'!]+)'?!([A-Z]+)(\d+)(?::([A-Z]+)(\d+))?$", range_a1)
        if not m:
            raise ValueError(range_a1)
        tab, c1, r1, c2, r2 = m.groups()
        return tab, FakeSheetsBackend._col(c1), int(r1), FakeSheetsBackend._col(c2) if c2 else None, int(r2) if r2 else None

    @staticmethod
    def _col(letters: str) -> int:
        n = 0
        for ch in letters:
            n = n * 26 + (ord(ch) - 64)
        return n - 1

    def sheet_titles(self):
        self.calls.append("titles")
        return list(self.tabs)

    def add_sheet(self, title):
        self.calls.append(f"add:{title}")
        self.tabs.setdefault(title, [])

    def get_values(self, range_a1):
        self.calls.append(f"get:{range_a1}")
        tab, c1, r1, c2, r2 = self._parse_range(range_a1)
        rows = self.tabs.get(tab, [])
        out = []
        for row in rows[r1 - 1 : (r2 if r2 else len(rows))]:
            end = (c2 + 1) if c2 is not None else len(row)
            out.append(row[c1:end])
        while out and not any(out[-1]):
            out.pop()
        return out

    def batch_get(self, ranges):
        self.calls.append(f"batch_get:{len(ranges)}")
        return {r: self.get_values(r) for r in ranges}

    def append(self, range_a1, values):
        self.calls.append(f"append:{range_a1}:{len(values)}")
        tab, *_ = self._parse_range(range_a1)
        self.tabs.setdefault(tab, []).extend([list(v) for v in values])

    def batch_update_values(self, data):
        self.calls.append(f"batch_update:{len(data)}")
        for range_a1, values in data:
            tab, c1, r1, _c2, _r2 = self._parse_range(range_a1)
            rows = self.tabs.setdefault(tab, [])
            for i, vals in enumerate(values):
                r = r1 - 1 + i
                while len(rows) <= r:
                    rows.append([])
                row = rows[r]
                while len(row) < c1 + len(vals):
                    row.append("")
                for j, v in enumerate(vals):
                    row[c1 + j] = v

    def freeze_header(self, title):
        self.frozen.add(title)

    def spreadsheet_url(self):
        return "https://docs.google.com/spreadsheets/d/FAKE/edit"


class FakeDriveBackend:
    def __init__(self, storage_dir: Path | None = None, email: str = "fake@example.com", limit: int = 15 * 1024**3):
        self.files: dict[str, dict] = {}
        self.blobs: dict[str, Path] = {}
        self.storage_dir = storage_dir
        self.email = email
        self.limit = limit
        self.permissions: list[tuple[str, str, str]] = []
        self.comments: dict[str, list[dict]] = {}
        self.calls: list[str] = []
        self.fail_upload_with: Exception | None = None

    def _new(self, **fields) -> dict:
        fid = f"f{next(_counter):05d}"
        meta = {"id": fid, "trashed": False, "appProperties": {}, "parents": [], "webViewLink": f"https://drive.google.com/x/{fid}", **fields}
        self.files[fid] = meta
        return dict(meta)

    def _match(self, meta: dict, q: str) -> bool:
        if meta.get("trashed"):
            return False
        for m in re.finditer(r"'([^']+)' in parents", q):
            if m.group(1) not in (meta.get("parents") or []):
                return False
        m = re.search(r"name = '((?:[^'\\]|\\.)*)'", q)
        if m and meta.get("name") != m.group(1).replace("\\'", "'"):
            return False
        m = re.search(r"mimeType = '([^']+)'", q)
        if m and meta.get("mimeType") != m.group(1):
            return False
        for m in re.finditer(r"appProperties has \{ key='([^']+)' and value='([^']+)' \}", q):
            if (meta.get("appProperties") or {}).get(m.group(1)) != m.group(2):
                return False
        if "'root' in parents" in q and meta.get("parents"):
            return False
        return True

    def list_files(self, q, page_size=100):
        self.calls.append(f"list:{q}")
        return [dict(m) for m in self.files.values() if self._match(m, q)]

    def get_file(self, file_id):
        if file_id not in self.files:
            raise KeyError(file_id)
        return dict(self.files[file_id])

    def create_folder(self, name, parent_id):
        self.calls.append(f"mkdir:{name}")
        return self._new(name=name, mimeType=FOLDER_MIME, parents=[parent_id] if parent_id else [])

    def upload_file(self, path, name, parent_id, mime_type, app_properties, convert_to=None):
        self.calls.append(f"upload:{name}")
        if self.fail_upload_with:
            raise self.fail_upload_with
        size = Path(path).stat().st_size
        meta = self._new(name=name, mimeType=convert_to or mime_type, parents=[parent_id], size=str(size), appProperties=dict(app_properties or {}))
        if self.storage_dir:
            dest = self.storage_dir / meta["id"]
            self.storage_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, dest)
            self.blobs[meta["id"]] = dest
        return meta

    def update_file(self, file_id, *, name=None, add_parents=None, remove_parents=None, app_properties=None, media_path=None, media_mime=None):
        self.calls.append(f"update:{file_id}")
        meta = self.files[file_id]
        if name is not None:
            meta["name"] = name
        if remove_parents:
            meta["parents"] = [p for p in meta["parents"] if p not in remove_parents]
        if add_parents:
            meta["parents"] = meta["parents"] + [p for p in add_parents if p not in meta["parents"]]
        if app_properties:
            meta["appProperties"].update(app_properties)
        if media_path is not None and self.storage_dir:
            dest = self.storage_dir / file_id
            shutil.copy2(media_path, dest)
            self.blobs[file_id] = dest
        return dict(meta)

    def download_file(self, file_id, dest):
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(self.blobs[file_id], dest)
        return dest

    def export_file(self, file_id, mime_type, dest):
        return self.download_file(file_id, dest)

    def about_quota(self):
        usage = sum(int(m.get("size", 0) or 0) for m in self.files.values() if not m.get("trashed"))
        return {"limit": self.limit, "usage": usage, "email": self.email}

    def create_permission(self, file_id, role, type_):
        self.permissions.append((file_id, role, type_))
        return {"id": "perm"}

    def list_comments(self, file_id):
        return list(self.comments.get(file_id, []))

    def resolve_comment(self, file_id, comment_id):
        for c in self.comments.get(file_id, []):
            if c["id"] == comment_id:
                c["resolved"] = True

    def trash_file(self, file_id):
        self.files[file_id]["trashed"] = True

    def get_drive(self, drive_id):
        if drive_id in self.shared_drives:
            return {"id": drive_id, "name": self.shared_drives[drive_id]}
        raise KeyError(drive_id)

    def list_drives(self):
        return [{"id": k, "name": v} for k, v in self.shared_drives.items()]

    # -- test helpers
    shared_drives: dict[str, str] = {}

    def add_shared_drive(self, drive_id: str, name: str):
        """A shared drive behaves like a parent folder whose id is the drive id."""
        self.shared_drives = {**self.shared_drives, drive_id: name}
        self.files[drive_id] = {"id": drive_id, "name": name, "mimeType": FOLDER_MIME, "parents": [], "trashed": False, "appProperties": {}, "webViewLink": f"https://drive.google.com/drive/folders/{drive_id}", "is_drive": True}
    def children(self, parent_id):
        return [m for m in self.files.values() if parent_id in m["parents"] and not m["trashed"]]

    def path_of(self, file_id) -> str:
        parts = []
        cur = self.files[file_id]
        while True:
            parts.append(cur["name"])
            if not cur["parents"]:
                break
            cur = self.files[cur["parents"][0]]
        return "/".join(reversed(parts))


class FakeApifyRunner:
    """Stands in for pipeline.apify_yt.ApifyRunner: returns canned dataset items per actor."""

    def __init__(self):
        self.responses: dict[str, list[dict]] = {}
        self.calls: list[tuple[str, dict]] = []
        self.usage_usd = 0.01
        self.schemas: dict[str, dict] = {}
        self.stored_runs: dict[str, list[dict]] = {}

    def run(self, actor_id, run_input, timeout_secs=1800):
        self.calls.append((actor_id, run_input))
        run_id = f"run{len(self.calls):04d}"
        items = self.responses.get(actor_id, [])
        if callable(items):
            items = items(run_input)
        self.stored_runs[run_id] = items
        return {"run_id": run_id, "items": items, "usage_usd": self.usage_usd, "status": "SUCCEEDED"}

    def dataset_items(self, run_id):
        return self.stored_runs.get(run_id, [])

    def actor_input_schema(self, actor_id):
        return self.schemas.get(actor_id, {})
