"""Google Drive access: a small backend protocol (real + fake) and the client logic on top."""

from __future__ import annotations

import io
import mimetypes
import shutil
import time
from pathlib import Path
from typing import Any, Protocol

FOLDER_MIME = "application/vnd.google-apps.folder"
GDOC_MIME = "application/vnd.google-apps.document"
FILE_FIELDS = "id,name,mimeType,parents,size,appProperties,webViewLink,modifiedTime,md5Checksum,trashed"


def _q_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


def folder_url(folder_id: str) -> str:
    return f"https://drive.google.com/drive/folders/{folder_id}"


def file_url(file_id: str) -> str:
    return f"https://drive.google.com/file/d/{file_id}/view"


def doc_url(doc_id: str) -> str:
    return f"https://docs.google.com/document/d/{doc_id}/edit"


class DriveBackend(Protocol):
    def list_files(self, q: str, page_size: int = 100) -> list[dict]: ...
    def get_file(self, file_id: str) -> dict: ...
    def create_folder(self, name: str, parent_id: str | None) -> dict: ...
    def upload_file(self, path: Path, name: str, parent_id: str, mime_type: str, app_properties: dict[str, str] | None, convert_to: str | None = None) -> dict: ...
    def update_file(self, file_id: str, *, name: str | None = None, add_parents: list[str] | None = None, remove_parents: list[str] | None = None, app_properties: dict[str, str] | None = None, media_path: Path | None = None, media_mime: str | None = None) -> dict: ...
    def download_file(self, file_id: str, dest: Path) -> Path: ...
    def export_file(self, file_id: str, mime_type: str, dest: Path) -> Path: ...
    def about_quota(self) -> dict: ...
    def create_permission(self, file_id: str, role: str, type_: str) -> dict: ...
    def list_comments(self, file_id: str) -> list[dict]: ...
    def resolve_comment(self, file_id: str, comment_id: str) -> None: ...
    def trash_file(self, file_id: str) -> None: ...


class GoogleDriveBackend:
    def __init__(self, service: Any, upload_chunk_mb: int = 8):
        self.service = service
        self.chunk = upload_chunk_mb * 1024 * 1024

    def _retry(self, fn, attempts: int = 5):
        delay = 1.0
        for i in range(attempts):
            try:
                return fn()
            except Exception as exc:
                status = getattr(getattr(exc, "resp", None), "status", None)
                if i == attempts - 1 or status not in (None, 429, 500, 502, 503, 504):
                    raise
                time.sleep(delay)
                delay *= 2

    def list_files(self, q: str, page_size: int = 100) -> list[dict]:
        files: list[dict] = []
        token = None
        while True:
            resp = self._retry(
                lambda: self.service.files()
                .list(q=q, pageSize=page_size, fields=f"nextPageToken,files({FILE_FIELDS})", pageToken=token, spaces="drive")
                .execute()
            )
            files.extend(resp.get("files", []))
            token = resp.get("nextPageToken")
            if not token:
                return files

    def get_file(self, file_id: str) -> dict:
        return self._retry(lambda: self.service.files().get(fileId=file_id, fields=FILE_FIELDS).execute())

    def create_folder(self, name: str, parent_id: str | None) -> dict:
        body: dict[str, Any] = {"name": name, "mimeType": FOLDER_MIME}
        if parent_id:
            body["parents"] = [parent_id]
        return self._retry(lambda: self.service.files().create(body=body, fields=FILE_FIELDS).execute())

    def upload_file(self, path: Path, name: str, parent_id: str, mime_type: str, app_properties: dict[str, str] | None, convert_to: str | None = None) -> dict:
        from googleapiclient.http import MediaFileUpload

        body: dict[str, Any] = {"name": name, "parents": [parent_id]}
        if app_properties:
            body["appProperties"] = app_properties
        if convert_to:
            body["mimeType"] = convert_to
        size = Path(path).stat().st_size
        media = MediaFileUpload(str(path), mimetype=mime_type, resumable=size > self.chunk, chunksize=self.chunk)
        return self._retry(lambda: self.service.files().create(body=body, media_body=media, fields=FILE_FIELDS).execute(num_retries=3))

    def update_file(self, file_id: str, *, name=None, add_parents=None, remove_parents=None, app_properties=None, media_path=None, media_mime=None) -> dict:
        from googleapiclient.http import MediaFileUpload

        body: dict[str, Any] = {}
        if name is not None:
            body["name"] = name
        if app_properties:
            body["appProperties"] = app_properties
        kwargs: dict[str, Any] = {"fileId": file_id, "body": body, "fields": FILE_FIELDS}
        if add_parents:
            kwargs["addParents"] = ",".join(add_parents)
        if remove_parents:
            kwargs["removeParents"] = ",".join(remove_parents)
        if media_path is not None:
            kwargs["media_body"] = MediaFileUpload(str(media_path), mimetype=media_mime, resumable=False)
        return self._retry(lambda: self.service.files().update(**kwargs).execute(num_retries=3))

    def download_file(self, file_id: str, dest: Path) -> Path:
        from googleapiclient.http import MediaIoBaseDownload

        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        request = self.service.files().get_media(fileId=file_id)
        with open(dest, "wb") as fh:
            downloader = MediaIoBaseDownload(fh, request, chunksize=self.chunk)
            done = False
            while not done:
                _status, done = downloader.next_chunk(num_retries=3)
        return dest

    def export_file(self, file_id: str, mime_type: str, dest: Path) -> Path:
        from googleapiclient.http import MediaIoBaseDownload

        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        request = self.service.files().export_media(fileId=file_id, mimeType=mime_type)
        buf = io.BytesIO()
        downloader = MediaIoBaseDownload(buf, request)
        done = False
        while not done:
            _status, done = downloader.next_chunk(num_retries=3)
        dest.write_bytes(buf.getvalue())
        return dest

    def about_quota(self) -> dict:
        resp = self._retry(lambda: self.service.about().get(fields="storageQuota,user").execute())
        quota = resp.get("storageQuota", {})
        return {
            "limit": int(quota["limit"]) if quota.get("limit") else None,
            "usage": int(quota.get("usage", 0) or 0),
            "email": resp.get("user", {}).get("emailAddress", ""),
        }

    def create_permission(self, file_id: str, role: str, type_: str) -> dict:
        body = {"role": role, "type": type_}
        return self._retry(lambda: self.service.permissions().create(fileId=file_id, body=body, fields="id").execute())

    def list_comments(self, file_id: str) -> list[dict]:
        out: list[dict] = []
        token = None
        while True:
            resp = self._retry(
                lambda: self.service.comments()
                .list(fileId=file_id, pageSize=100, pageToken=token, fields="nextPageToken,comments(id,content,quotedFileContent,resolved,author,createdTime,replies(content,author))")
                .execute()
            )
            out.extend(resp.get("comments", []))
            token = resp.get("nextPageToken")
            if not token:
                return out

    def resolve_comment(self, file_id: str, comment_id: str) -> None:
        self._retry(lambda: self.service.replies().create(fileId=file_id, commentId=comment_id, body={"action": "resolve", "content": "Applied by the AI Primer summary agent."}, fields="id").execute())

    def trash_file(self, file_id: str) -> None:
        self._retry(lambda: self.service.files().update(fileId=file_id, body={"trashed": True}).execute())


class DriveClient:
    """Folder-path, idempotent-upload and move helpers on top of a DriveBackend."""

    def __init__(self, backend: DriveBackend):
        self.backend = backend

    # ---- folders
    def list_children(self, parent_id: str, folders_only: bool = False) -> list[dict]:
        q = f"'{_q_escape(parent_id)}' in parents and trashed = false"
        if folders_only:
            q += f" and mimeType = '{FOLDER_MIME}'"
        return self.backend.list_files(q)

    def find_child_folder(self, parent_id: str, name: str) -> dict | None:
        q = f"'{_q_escape(parent_id)}' in parents and name = '{_q_escape(name)}' and mimeType = '{FOLDER_MIME}' and trashed = false"
        found = self.backend.list_files(q)
        return found[0] if found else None

    def find_root_folder(self, name: str) -> dict | None:
        q = f"name = '{_q_escape(name)}' and mimeType = '{FOLDER_MIME}' and 'root' in parents and trashed = false"
        found = self.backend.list_files(q)
        return found[0] if found else None

    def ensure_folder(self, parent_id: str | None, name: str) -> dict:
        existing = self.find_child_folder(parent_id, name) if parent_id else self.find_root_folder(name)
        return existing or self.backend.create_folder(name, parent_id)

    def ensure_path(self, root_id: str, names: list[str]) -> list[dict]:
        out: list[dict] = []
        parent = root_id
        for name in names:
            folder = self.ensure_folder(parent, name)
            out.append(folder)
            parent = folder["id"]
        return out

    def rename(self, file_id: str, name: str) -> dict:
        return self.backend.update_file(file_id, name=name)

    def get(self, file_id: str) -> dict:
        return self.backend.get_file(file_id)

    # ---- files
    def find_by_app_property(self, key: str, value: str, parent_id: str | None = None) -> list[dict]:
        q = f"appProperties has {{ key='{_q_escape(key)}' and value='{_q_escape(value)}' }} and trashed = false"
        if parent_id:
            q += f" and '{_q_escape(parent_id)}' in parents"
        return self.backend.list_files(q)

    def upload(self, path: Path, name: str, parent_id: str, app_properties: dict[str, str] | None = None, mime_type: str | None = None, convert_to: str | None = None) -> dict:
        """Upload, or return the existing file when one with the same resource_id+role label is already in the folder."""
        if app_properties and app_properties.get("resource_id"):
            role = app_properties.get("role", "")
            for f in self.find_by_app_property("resource_id", app_properties["resource_id"], parent_id):
                if (f.get("appProperties") or {}).get("role", "") == role:
                    return f
        mime = mime_type or mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        return self.backend.upload_file(Path(path), name, parent_id, mime, app_properties, convert_to=convert_to)

    def move(self, file_id: str, new_parent_id: str) -> dict:
        current = self.backend.get_file(file_id)
        parents = current.get("parents") or []
        if parents == [new_parent_id]:
            return current
        return self.backend.update_file(file_id, add_parents=[new_parent_id], remove_parents=parents)

    def download(self, file_id: str, dest: Path) -> Path:
        return self.backend.download_file(file_id, Path(dest))

    def quota(self) -> dict:
        return self.backend.about_quota()

    def share_anyone_reader(self, file_id: str) -> None:
        self.backend.create_permission(file_id, "reader", "anyone")


def copy_local(path: Path, dest_dir: Path) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / Path(path).name
    if Path(path).resolve() != dest.resolve():
        shutil.copy2(path, dest)
    return dest
