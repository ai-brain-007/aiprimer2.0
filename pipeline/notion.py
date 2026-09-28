"""Notion as the v2 control panel and page host.

Three layers, mirroring `pipeline/sheets.py` and `pipeline/docs.py`:

- `NotionBackend` is the small set of raw API operations we need. `RealNotionBackend` implements it over
  HTTPS (throttled to Notion's ~3 requests per second, retrying 429 and 5xx); tests use
  `tests/fake_notion.py`.
- `NotionRepo` offers the same typed, header-driven interface as `SheetsRepo`. Every "tab" is a database
  under the "AI Primer" page: the model's key field is the database title property and every other column
  a rich-text property with the same name. One query per tab per command (cached), one call per row written.
- `markdown_to_blocks` converts the rendered summary markdown into Notion blocks; `NotionPublisher` writes
  them into a page (create once, then refresh in place so the link never changes) and reads the comments
  people leave on it.

Scripts stay deterministic: nothing here calls a model. The token never appears in an error message.
"""

from __future__ import annotations

import mimetypes
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Callable, Protocol

from .models import TAB_MODELS, TabRow
from .sheets import README_TEXT

NOTION_VERSION = "2022-06-28"
NOTION_BASE_URL = "https://api.notion.com/v1"
TEXT_LIMIT = 2000  # characters per rich text item
RICH_TEXT_LIMIT = 100  # items per rich_text array
CHILDREN_LIMIT = 100  # blocks per children array and per append call
REQUEST_BLOCK_LIMIT = 1000  # blocks in one request, nested ones included
TABLE_ROWS_PER_BLOCK = 90  # body rows per table block (a table block holds at most 100 rows)
MAX_ATTEMPTS = 6
CONTAINER_BLOCK_TYPES = ("child_database", "child_page")
MOVE_PAGE_VERSION = "2026-03-11"  # POST /pages/{id}/move is not in the pinned 2022-06-28 version

# What each database is for, in the words of the v1 Readme tab (the Notion API cannot set a database
# description through the create call we use, so /setup prints these instead).
TAB_DESCRIPTIONS: dict[str, str] = {row[0]: row[1] for row in README_TEXT if len(row) == 2}


class NotionError(RuntimeError):
    """An error reported by the Notion API (or the transport). Never carries the token."""

    def __init__(self, status: int, code: str, message: str):
        self.status = status
        self.code = code
        self.message = message
        super().__init__(f"Notion API error {status} ({code}): {message}")


# --------------------------------------------------------------------------- rich text helpers

_DEFAULT_ANNOTATIONS = {"bold": False, "italic": False, "strikethrough": False, "underline": False, "code": False, "color": "default"}


def split_text(text: str, limit: int = TEXT_LIMIT) -> list[str]:
    """Cut a string into pieces of at most `limit` characters (Notion's per-item limit)."""
    return [text[i : i + limit] for i in range(0, len(text), limit)]


def rich_text(text: str, *, link: str | None = None, **annotations: bool) -> list[dict]:
    """Plain string -> list of Notion rich text objects (several when longer than 2000 characters)."""
    flags = {k: v for k, v in annotations.items() if v}
    items = []
    for piece in split_text(text or ""):
        item: dict[str, Any] = {"type": "text", "text": {"content": piece, "link": {"url": link} if link else None}}
        if flags:
            item["annotations"] = {**_DEFAULT_ANNOTATIONS, **flags}
        items.append(item)
    return items


def plain_text(items: list[dict] | None) -> str:
    """Join the text of a rich text array (what the API returns as `plain_text`)."""
    out = []
    for item in items or []:
        pt = item.get("plain_text")
        if pt is None:
            pt = (item.get("text") or {}).get("content", "")
        out.append(pt or "")
    return "".join(out)


def property_plain_text(prop: dict | None) -> str:
    """Any page property value -> the text we keep in a registry cell."""
    if not prop:
        return ""
    kind = prop.get("type")
    value = prop.get(kind) if kind else None
    if kind in ("title", "rich_text"):
        return plain_text(value or [])
    if kind == "number":
        return "" if value is None else str(value)
    if kind == "select" or kind == "status":
        return (value or {}).get("name", "") if isinstance(value, dict) else ""
    if kind == "multi_select":
        return "|".join(v.get("name", "") for v in (value or []))
    if kind in ("url", "email", "phone_number"):
        return value or ""
    if kind == "checkbox":
        return "Y" if value else "N"
    if kind == "date":
        return (value or {}).get("start", "") if isinstance(value, dict) else ""
    if kind == "formula" and isinstance(value, dict):
        inner = value.get(value.get("type", ""), "")
        return "" if inner is None else str(inner)
    return ""


def _capped(items: list[dict]) -> list[dict]:
    return items[:RICH_TEXT_LIMIT]


def block_plain_text(block: dict) -> str:
    """The visible text of one block (without its children)."""
    kind = block.get("type") or ""
    payload = block.get(kind) or {}
    if kind == "table_row":
        return " | ".join(plain_text(cell) for cell in payload.get("cells", []))
    if "rich_text" in payload:
        return plain_text(payload.get("rich_text"))
    if kind in ("image", "video", "file", "pdf", "embed", "bookmark"):
        src = (payload.get("external") or {}).get("url") or (payload.get("file") or {}).get("url") or payload.get("url") or ""
        return f"[{kind}] {src}".strip()
    if kind in CONTAINER_BLOCK_TYPES:
        return payload.get("title", "")
    return ""


def _count_blocks(block: dict) -> int:
    kind = block.get("type") or ""
    payload = block.get(kind) or {}
    kids = payload.get("children") or block.get("children") or []
    return 1 + sum(_count_blocks(k) for k in kids)


def chunk_children(children: list[dict]) -> list[list[dict]]:
    """Split a children list into append-sized calls: at most 100 top-level blocks and under 1000 blocks
    in total (nested ones included) per call."""
    chunks: list[list[dict]] = []
    current: list[dict] = []
    total = 0
    for block in children:
        n = _count_blocks(block)
        if current and (len(current) >= CHILDREN_LIMIT or total + n > REQUEST_BLOCK_LIMIT):
            chunks.append(current)
            current, total = [], 0
        current.append(block)
        total += n
    if current:
        chunks.append(current)
    return chunks


def child_pages(backend: "NotionBackend", parent_id: str) -> dict[str, str]:
    """Title -> page id of the pages directly under `parent_id` (a child page's block id is its page id).
    The first page of a title wins."""
    out: dict[str, str] = {}
    for block in backend.list_block_children(parent_id):
        if block.get("type") == "child_page":
            title = ((block.get("child_page") or {}).get("title") or "").strip()
            if title and title not in out:
                out[title] = block["id"]
    return out


def page_url(page_id: str) -> str:
    return f"https://www.notion.so/{page_id.replace('-', '')}"


# --------------------------------------------------------------------------- backend protocol


class NotionBackend(Protocol):
    def list_block_children(self, block_id: str) -> list[dict]: ...  # all pages
    def append_block_children(self, block_id: str, children: list[dict], after: str | None = None) -> list[dict]: ...  # chunks by 100; `after` = insert after that child
    def delete_block(self, block_id: str) -> None: ...
    def create_database(self, parent_page_id: str, title: str, properties: dict) -> dict: ...
    def update_database(self, database_id: str, properties: dict) -> dict: ...
    def retrieve_database(self, database_id: str) -> dict: ...
    def query_database(self, database_id: str, filter: dict | None = None) -> list[dict]: ...  # all pages
    def create_page(self, parent: dict, properties: dict, children: list[dict] | None = None, icon: dict | None = None) -> dict: ...
    def update_page(self, page_id: str, properties: dict | None = None, archived: bool | None = None) -> dict: ...
    def retrieve_page(self, page_id: str) -> dict: ...
    def list_comments(self, block_id: str) -> list[dict]: ...
    def create_comment(self, *, page_id: str | None = None, discussion_id: str | None = None, text: str) -> dict: ...
    def upload_file(self, path: Path, content_type: str) -> str: ...  # returns the file_upload id
    def move_page(self, page_id: str, parent_page_id: str) -> dict: ...  # pages only, never a database
    def page_url(self, page_id: str) -> str: ...


class RealNotionBackend:
    """The Notion REST API over `requests`, throttled and retried.

    `token=None` sends no Authorization header: in the cloud environment the proxy attaches the token for
    api.notion.com (API credentials box). `session`, `sleep` and `clock` are injectable for tests.
    """

    def __init__(
        self,
        token: str | None = None,
        *,
        base_url: str = NOTION_BASE_URL,
        version: str = NOTION_VERSION,
        session: Any | None = None,
        min_interval: float = 0.34,
        timeout: float = 60.0,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._token = token or None
        self.base_url = base_url.rstrip("/")
        self.version = version
        self.session = session
        self.min_interval = min_interval
        self.timeout = timeout
        self._sleep = sleep
        self._clock = clock
        self._last_request: float | None = None

    @classmethod
    def from_env(cls, **kwargs: Any) -> "RealNotionBackend":
        """Token from NOTION_TOKEN when set; otherwise the proxy is expected to attach it."""
        return cls(os.environ.get("NOTION_TOKEN") or None, **kwargs)

    # -- plumbing
    def _http(self) -> Any:
        if self.session is None:
            import requests  # local import: tests inject a stub session and never need the library

            self.session = requests.Session()
        return self.session

    def _headers(self, version: str | None = None) -> dict[str, str]:
        headers = {"Notion-Version": version or self.version, "Accept": "application/json"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    def _throttle(self) -> None:
        if self._last_request is not None:
            wait = self._last_request + self.min_interval - self._clock()
            if wait > 0:
                self._sleep(wait)

    @staticmethod
    def _retry_after(resp: Any) -> float:
        try:
            return max(float((getattr(resp, "headers", None) or {}).get("Retry-After", 1)), 0.0)
        except (TypeError, ValueError):
            return 1.0

    @staticmethod
    def _json(resp: Any) -> dict:
        try:
            body = resp.json()
        except ValueError:
            return {}
        return body if isinstance(body, dict) else {}

    def _request(self, method: str, path: str, *, params: dict | None = None, json: dict | None = None, files: dict | None = None, version: str | None = None) -> dict:
        url = path if path.startswith("http") else f"{self.base_url}{path}"
        backoff = 1.0
        for attempt in range(1, MAX_ATTEMPTS + 1):
            self._throttle()
            try:
                resp = self._http().request(method, url, headers=self._headers(version), params=params, json=json, files=files, timeout=self.timeout)
            except OSError as exc:  # requests' exceptions derive from IOError; no header ever appears in them
                self._last_request = self._clock()
                if attempt == MAX_ATTEMPTS:
                    raise NotionError(0, "transport_error", f"{type(exc).__name__}: {exc}") from None
                self._sleep(backoff)
                backoff = min(backoff * 2, 4.0)
                continue
            self._last_request = self._clock()
            status = int(getattr(resp, "status_code", 0) or 0)
            if status == 429 and attempt < MAX_ATTEMPTS:
                self._sleep(self._retry_after(resp))
                continue
            if status >= 500 and attempt < MAX_ATTEMPTS:
                self._sleep(backoff)
                backoff = min(backoff * 2, 4.0)
                continue
            if status >= 400:
                body = self._json(resp)
                message = body.get("message") or (getattr(resp, "text", "") or "")[:300]
                raise NotionError(status, str(body.get("code") or "http_error"), str(message))
            return self._json(resp)
        raise NotionError(0, "retries_exhausted", f"{method} {path}")  # not reachable

    def _paginate(self, method: str, path: str, *, params: dict | None = None, json: dict | None = None) -> list[dict]:
        out: list[dict] = []
        cursor: str | None = None
        while True:
            if method == "GET":
                q = {**(params or {}), "page_size": 100}
                if cursor:
                    q["start_cursor"] = cursor
                body = self._request("GET", path, params=q)
            else:
                b = {**(json or {}), "page_size": 100}
                if cursor:
                    b["start_cursor"] = cursor
                body = self._request("POST", path, json=b)
            out.extend(body.get("results") or [])
            cursor = body.get("next_cursor")
            if not body.get("has_more") or not cursor:
                return out

    # -- blocks
    def list_block_children(self, block_id: str) -> list[dict]:
        return self._paginate("GET", f"/blocks/{block_id}/children")

    def append_block_children(self, block_id: str, children: list[dict], after: str | None = None) -> list[dict]:
        created: list[dict] = []
        anchor = after
        for chunk in chunk_children(children):
            payload: dict[str, Any] = {"children": chunk}
            if anchor:
                payload["after"] = anchor
            results = self._request("PATCH", f"/blocks/{block_id}/children", json=payload).get("results") or []
            created.extend(results)
            if anchor and results:
                anchor = results[-1]["id"]  # keep the next chunk in order, right behind this one
        return created

    def delete_block(self, block_id: str) -> None:
        self._request("DELETE", f"/blocks/{block_id}")

    # -- databases
    def create_database(self, parent_page_id: str, title: str, properties: dict) -> dict:
        body = {"parent": {"type": "page_id", "page_id": parent_page_id}, "title": rich_text(title), "properties": properties}
        return self._request("POST", "/databases", json=body)

    def update_database(self, database_id: str, properties: dict) -> dict:
        return self._request("PATCH", f"/databases/{database_id}", json={"properties": properties})

    def retrieve_database(self, database_id: str) -> dict:
        return self._request("GET", f"/databases/{database_id}")

    def query_database(self, database_id: str, filter: dict | None = None) -> list[dict]:
        body = {"filter": filter} if filter else {}
        return self._paginate("POST", f"/databases/{database_id}/query", json=body)

    # -- pages
    def create_page(self, parent: dict, properties: dict, children: list[dict] | None = None, icon: dict | None = None) -> dict:
        body: dict[str, Any] = {"parent": parent, "properties": properties}
        if children:
            body["children"] = children
        if icon:
            body["icon"] = icon
        return self._request("POST", "/pages", json=body)

    def update_page(self, page_id: str, properties: dict | None = None, archived: bool | None = None) -> dict:
        body: dict[str, Any] = {}
        if properties is not None:
            body["properties"] = properties
        if archived is not None:
            body["archived"] = archived
        return self._request("PATCH", f"/pages/{page_id}", json=body)

    def move_page(self, page_id: str, parent_page_id: str) -> dict:
        """Move a page (never a database) under another page; its id and link stay the same. The endpoint exists
        only in the newer API versions, so this one call is sent with MOVE_PAGE_VERSION."""
        return self._request("POST", f"/pages/{page_id}/move", json={"parent": {"type": "page_id", "page_id": parent_page_id}}, version=MOVE_PAGE_VERSION)

    def retrieve_page(self, page_id: str) -> dict:
        return self._request("GET", f"/pages/{page_id}")

    # -- comments
    def list_comments(self, block_id: str) -> list[dict]:
        return self._paginate("GET", "/comments", params={"block_id": block_id})

    def create_comment(self, *, page_id: str | None = None, discussion_id: str | None = None, text: str) -> dict:
        if discussion_id:
            body: dict[str, Any] = {"discussion_id": discussion_id, "rich_text": _capped(rich_text(text))}
        elif page_id:
            body = {"parent": {"page_id": page_id}, "rich_text": _capped(rich_text(text))}
        else:
            raise ValueError("create_comment needs page_id or discussion_id")
        return self._request("POST", "/comments", json=body)

    # -- files
    def upload_file(self, path: Path, content_type: str) -> str:
        path = Path(path)
        created = self._request("POST", "/file_uploads", json={"mode": "single_part", "filename": path.name, "content_type": content_type})
        upload_id = created["id"]
        data = path.read_bytes()  # bytes, not a handle, so a retried send starts from the beginning
        self._request("POST", f"/file_uploads/{upload_id}/send", files={"file": (path.name, data, content_type)})
        return upload_id

    def page_url(self, page_id: str) -> str:
        return page_url(page_id)


# --------------------------------------------------------------------------- registry repo


class NotionRepo:
    """Header-driven typed access to the registry databases under one Notion page (same interface as
    SheetsRepo). Cache per tab: headers, rows as text cells, key -> page id."""

    def __init__(self, backend: NotionBackend, parent_page_id: str, tab_models: dict[str, type[TabRow]] | None = None):
        self.backend = backend
        self.parent_page_id = parent_page_id
        self.tab_models = tab_models or TAB_MODELS
        self._db_ids: dict[str, str] | None = None
        self._db_parents: dict[str, str] = {}
        self._prop_ids: dict[str, dict[str, str]] = {}  # database id -> {property name: property id}
        self._cache: dict[str, tuple[list[str], list[dict[str, str]], dict[str, str]]] = {}

    # ---- structure
    def _databases(self, refresh: bool = False) -> dict[str, str]:
        """Title -> database id of the registry databases. They live directly under the parent page or inside
        one of its child pages (the layer pages of `pipeline.layout`); a database inside a layer page wins over
        a same-titled one at the top, so a half-finished move is resumed, not repeated."""
        if self._db_ids is None or refresh:
            found: dict[str, str] = {}
            parents: dict[str, str] = {}
            top = self.backend.list_block_children(self.parent_page_id)
            pages = [b["id"] for b in top if b.get("type") == "child_page"]
            for page_id, blocks in [*((p, None) for p in pages), (self.parent_page_id, top)]:
                for block in blocks if blocks is not None else self.backend.list_block_children(page_id):
                    if block.get("type") != "child_database":
                        continue
                    title = (block.get("child_database") or {}).get("title", "")
                    if title and title not in found:
                        found[title] = block["id"]
                        parents[title] = page_id
            self._db_ids, self._db_parents = found, parents
        return self._db_ids

    def database_parent(self, tab: str) -> str | None:
        """Id of the page the database sits in (the parent page or a layer page); None when not found."""
        self._databases()
        return self._db_parents.get(tab)

    def relocate_tab(self, tab: str, page_id: str) -> dict[str, Any]:
        """Put a registry database under another page. The API cannot move a database, so a new one with the same
        schema is created there, the rows are copied and counted, and only then is the old database archived
        (recoverable from Notion's trash). A count mismatch archives the copy instead and raises."""
        old_id = self._require_db(tab)
        model = self.tab_models[tab]
        headers, rows, _ = self._load_raw(tab, refresh=True)
        new_id = self.backend.create_database(page_id, tab, self._schema(model))["id"]
        for cells in rows:
            self.backend.create_page({"database_id": new_id}, self._properties_of(tab, model.headers(), cells, db_id=new_id))
        copied = [p for p in self.backend.query_database(new_id) if not p.get("archived")]
        if len(copied) != len(rows):
            self.backend.delete_block(new_id)
            raise NotionError(500, "copy_mismatch", f"{tab}: copied {len(copied)} of {len(rows)} rows; the copy was discarded and the database stays where it is")
        self.backend.delete_block(old_id)
        self._db_ids[tab] = new_id  # type: ignore[index]
        self._db_parents[tab] = page_id
        self._cache.pop(tab, None)
        return {"rows": len(rows), "old_id": old_id, "new_id": new_id, "columns_dropped": [h for h in headers if h not in model.headers()]}

    @staticmethod
    def _schema(model: type[TabRow]) -> dict[str, dict]:
        return {h: self_type for h, self_type in ((h, NotionRepo._property_type(model, h)) for h in model.headers())}

    @staticmethod
    def _property_type(model: type[TabRow], header: str) -> dict[str, dict]:
        if header == model.key_field:
            return {"title": {}}
        if header in model.url_fields:
            return {"url": {}}
        return {"rich_text": {}}

    def ensure_tabs(self, homes: dict[str, str] | None = None) -> dict[str, Any]:
        """Create the missing databases (under `homes[tab]` when given, else the parent page) and add missing
        properties to existing ones. Returns what changed."""
        report: dict[str, Any] = {"created": [], "columns_added": {}}
        existing = self._databases(refresh=True)
        for tab, model in self.tab_models.items():
            headers = model.headers()
            if tab not in existing:
                parent = (homes or {}).get(tab) or self.parent_page_id
                db = self.backend.create_database(parent, tab, self._schema(model))
                existing[tab] = db["id"]
                self._db_parents[tab] = parent
                report["created"].append(tab)
                continue
            db_id = existing[tab]
            props = dict((self.backend.retrieve_database(db_id) or {}).get("properties") or {})
            title_name = next((name for name, p in props.items() if (p or {}).get("type") == "title"), None)
            if title_name is not None and title_name != model.key_field:
                self.backend.update_database(db_id, {title_name: {"name": model.key_field}})
                props[model.key_field] = props.pop(title_name)
                report.setdefault("title_renamed", {})[tab] = f"{title_name} -> {model.key_field}"
            missing = [h for h in headers if h not in props]
            if missing:
                self.backend.update_database(db_id, {h: self._property_type(model, h) for h in missing})
                report["columns_added"][tab] = missing
            # A text column the model now declares as a link becomes a clickable `url` property. The API may not
            # carry the cell values across a type change, so they are read first and written back afterwards.
            retype = [h for h in model.url_fields if h in props and (props[h] or {}).get("type") == "rich_text"]
            if retype:
                self._cache.pop(tab, None)
                _headers, rows, page_ids = self._load_raw(tab, refresh=True)
                self.backend.update_database(db_id, {h: {"url": {}} for h in retype})
                self._prop_ids.pop(db_id, None)
                ids = self._property_ids(db_id)
                for row in rows:
                    page_id = page_ids.get(row.get(model.key_field, ""))
                    values = {ids.get(h, h): {"url": row[h]} for h in retype if row.get(h, "").strip()}
                    if page_id and values:
                        self.backend.update_page(page_id, values)
                report.setdefault("columns_retyped", {})[tab] = sorted(retype)
        self._cache.clear()
        self._prop_ids.clear()
        return report

    def database_id(self, tab: str) -> str | None:
        return self._databases().get(tab)

    def _require_db(self, tab: str) -> str:
        db_id = self.database_id(tab)
        if db_id is None:
            raise NotionError(404, "object_not_found", f"database '{tab}' not found under the parent page; run /setup")
        return db_id

    def url(self) -> str:
        return self.backend.page_url(self.parent_page_id)

    def spreadsheet_url(self) -> str:  # old callers
        return self.url()

    # ---- reading
    def _load_raw(self, tab: str, refresh: bool = False) -> tuple[list[str], list[dict[str, str]], dict[str, str]]:
        if tab in self._cache and not refresh:
            return self._cache[tab]
        model = self.tab_models[tab]
        key_field = model.key_field
        db_id = self.database_id(tab)
        pages = self.backend.query_database(db_id) if db_id else []
        # The schema is what the first page carries; a column the database lacks is neither read nor written
        # (same as a sheet whose header row lacks it) until /setup adds it.
        available = set((pages[0].get("properties") or {}).keys()) if pages else None
        headers = [h for h in model.headers() if available is None or h == key_field or h in available]
        rows: list[dict[str, str]] = []
        page_ids: dict[str, str] = {}
        for page in pages:
            if page.get("archived"):
                continue
            props = page.get("properties") or {}
            row = {h: property_plain_text(props.get(h)) for h in headers}
            if not any(v.strip() for v in row.values()):
                continue
            rows.append(row)
            key = row.get(key_field, "")
            if key and key not in page_ids:
                page_ids[key] = page["id"]
        self._cache[tab] = (headers, rows, page_ids)
        return self._cache[tab]

    def preload(self, tabs: list[str]) -> None:
        for tab in tabs:
            if tab not in self._cache:
                self._load_raw(tab)

    @staticmethod
    def _parse_row(model: type[TabRow], row: dict[str, str]) -> TabRow | None:
        """A hand-edited bad cell must not break the whole tab: the offending fields fall back to their
        defaults and the problem is recorded in `notes` (when the model has one)."""
        try:
            return model.from_row(row)
        except Exception as exc:
            errors = getattr(exc, "errors", None)
            details = [(str((e.get("loc") or ["?"])[0]), str(e.get("msg", ""))) for e in (errors() if callable(errors) else [])]
            bad = {field for field, _ in details if field in model.model_fields and field != model.key_field}
            cleaned = {**row, **{field: "" for field in bad}}
            note = "[parse error: " + ("; ".join(f"{f}: {m}" for f, m in details) or f"{type(exc).__name__}: {exc}")[:300] + "]"
            if "notes" in model.model_fields:
                cleaned["notes"] = f"{row.get('notes', '')} {note}".strip()
            try:
                return model.from_row(cleaned)
            except Exception:
                print(f"notion: skipping unreadable {model.__name__} row {row.get(model.key_field, '')!r}: {note}", file=sys.stderr)
                return None

    def load(self, tab: str, refresh: bool = False) -> list[TabRow]:
        model = self.tab_models[tab]
        _headers, rows, _ = self._load_raw(tab, refresh)
        out = []
        for row in rows:
            parsed = self._parse_row(model, row)
            if parsed is not None:
                out.append(parsed)
        return out

    def get(self, tab: str, key: str) -> TabRow | None:
        for row in self.load(tab):
            if row.key() == key:
                return row
        return None

    def page_id(self, tab: str, key: str) -> str | None:
        return self._load_raw(tab)[2].get(key)

    def invalidate(self, tab: str | None = None) -> None:
        if tab is None:
            self._cache.clear()
            self._db_ids = None
            self._prop_ids.clear()
        else:
            self._cache.pop(tab, None)

    # ---- writing
    def _properties(self, tab: str, headers: list[str], row: TabRow) -> dict[str, dict]:
        return self._properties_of(tab, headers, row.to_row())

    def _property_ids(self, db_id: str) -> dict[str, str]:
        """{property name: property id} of a database, fetched once. Rows are written by id: a name is also
        accepted by the API, but it resolves ids first, and every title column has the fixed id "title", so a
        rich-text column *named* "title" (the Resources table) is refused when written by name (seen live)."""
        if db_id not in self._prop_ids:
            props = (self.backend.retrieve_database(db_id) or {}).get("properties") or {}
            self._prop_ids[db_id] = {name: str(p.get("id") or name) for name, p in props.items()}
        return self._prop_ids[db_id]

    def _properties_of(self, tab: str, headers: list[str], cells: dict[str, str], db_id: str | None = None) -> dict[str, dict]:
        model = self.tab_models[tab]
        ids = self._property_ids(db_id or self._require_db(tab))
        props: dict[str, dict] = {}
        for h in headers:
            value = cells.get(h, "")
            if h in model.url_fields:
                props[ids.get(h, h)] = {"url": value or None}
                continue
            items = _capped(rich_text(value))
            props[ids.get(h, h)] = {"title": items} if h == model.key_field else {"rich_text": items}
        return props

    def append(self, tab: str, rows: list[TabRow]) -> None:
        if not rows:
            return
        headers, cached_rows, page_ids = self._load_raw(tab)
        db_id = self._require_db(tab)
        for r in rows:
            page = self.backend.create_page({"database_id": db_id}, self._properties(tab, headers, r))
            cached_rows.append(r.to_row())
            page_ids[r.key()] = page["id"]

    def update(self, tab: str, rows: list[TabRow]) -> None:
        """Rewrite existing rows (found by key); unknown keys are appended."""
        if not rows:
            return
        headers, cached_rows, page_ids = self._load_raw(tab)
        key_field = self.tab_models[tab].key_field
        to_append = []
        for r in rows:
            pid = page_ids.get(r.key())
            if pid is None:
                to_append.append(r)
                continue
            self.backend.update_page(pid, properties=self._properties(tab, headers, r))
            for cached in cached_rows:
                if cached.get(key_field) == r.key():
                    cached.update(r.to_row())
        self.append(tab, to_append)

    def upsert(self, tab: str, row: TabRow) -> None:
        self.update(tab, [row])


# --------------------------------------------------------------------------- markdown -> blocks

_COMMENT_RE = re.compile(r"<!--.*?-->", re.S)
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_LIST_RE = re.compile(r"^(\s*)([-*+]|\d+[.)])\s+(.*)$")
_DIVIDER_RE = re.compile(r"^(?:-{3,}|\*{3,}|_{3,})\s*$")
_IMAGE_RE = re.compile(r"^!\[([^\]]*)\]\(\s*(\S+?)(?:\s+\"[^\"]*\")?\s*\)$")
_EXCALIDRAW_RE = re.compile(r"^https?://(?:www\.)?excalidraw\.com/\S*$")  # a bare board link on its own line -> embed
_YOUTUBE_RE = re.compile(r"^https?://(?:www\.|m\.)?(?:youtube\.com/(?:watch\?\S*v=|embed/|shorts/|live/)[\w-]{6,}\S*|youtu\.be/[\w-]{6,}\S*)$")
_VIDEO_FILE_RE = re.compile(r"^https?://\S+\.(?:mp4|webm|mov|m4v)(?:\?\S*)?$", re.I)
_TABLE_SEP_CELL_RE = re.compile(r"^:?-+:?$")
_TRAILING_PUNCT = ".,;:!?'\")"
_INLINE_RE = re.compile(
    r"(?P<code>`(?P<code_t>[^`\n]+)`)"
    r"|(?P<bold>\*\*(?P<bold_t>(?:[^*\n]|\*(?!\*))+?)\*\*)"
    r"|(?P<link>\[(?P<link_t>[^\]\n]+)\]\((?P<link_u>https?://[^)\s]+)\))"
    r"|(?P<ital>(?<![\w*\\])\*(?P<ital_t>[^*\s][^*\n]*?)\*(?![\w*]))"
    r"|(?P<url>https?://[^\s<>\[\]]+)"
)
_CODE_LANGUAGES = {
    "bash", "c", "c++", "c#", "css", "go", "html", "java", "javascript", "json", "kotlin", "markdown", "php",
    "mermaid", "plain text", "powershell", "python", "ruby", "rust", "shell", "sql", "swift", "typescript", "xml", "yaml",
}  # "mermaid": Notion renders the block as a diagram
_CODE_ALIASES = {"sh": "shell", "zsh": "shell", "py": "python", "js": "javascript", "ts": "typescript", "yml": "yaml", "md": "markdown", "txt": "plain text", "text": "plain text", "": "plain text", "cpp": "c++", "cs": "c#", "console": "shell"}

ImageResolver = Callable[[str], dict | None]


def inline_rich_text(text: str, *, bold: bool = False, italic: bool = False, code: bool = False, link: str | None = None) -> list[dict]:
    """Inline markdown (**bold**, *italic*, `code`, [text](url), bare URLs) -> rich text objects."""
    out: list[dict] = []
    pos = 0
    for m in _INLINE_RE.finditer(text):
        if m.start() > pos:
            out.extend(rich_text(text[pos : m.start()], link=link, bold=bold, italic=italic, code=code))
        if m.group("code") is not None:
            out.extend(rich_text(m.group("code_t"), link=link, bold=bold, italic=italic, code=True))
        elif m.group("bold") is not None:
            out.extend(inline_rich_text(m.group("bold_t"), bold=True, italic=italic, code=code, link=link))
        elif m.group("link") is not None:
            out.extend(inline_rich_text(m.group("link_t"), bold=bold, italic=italic, code=code, link=m.group("link_u")))
        elif m.group("ital") is not None:
            out.extend(inline_rich_text(m.group("ital_t"), bold=bold, italic=True, code=code, link=link))
        else:
            url = m.group("url")
            trail = ""
            while url and url[-1] in _TRAILING_PUNCT and not (url[-1] == ")" and url.count("(") >= url.count(")")):
                trail = url[-1] + trail
                url = url[:-1]
            out.extend(rich_text(url, link=link or url, bold=bold, italic=italic, code=code))
            if trail:
                out.extend(rich_text(trail, link=link, bold=bold, italic=italic, code=code))
        pos = m.end()
    if pos < len(text):
        out.extend(rich_text(text[pos:], link=link, bold=bold, italic=italic, code=code))
    return out


def _rich_chunks(items: list[dict]) -> list[list[dict]]:
    return [items[i : i + RICH_TEXT_LIMIT] for i in range(0, len(items), RICH_TEXT_LIMIT)] or [[]]


def _block(kind: str, payload: dict) -> dict:
    return {"object": "block", "type": kind, kind: payload}


def _text_blocks(kind: str, text: str, **extra: Any) -> list[dict]:
    """One block of `kind`; when the inline runs exceed 100 items, the overflow follows as paragraphs."""
    chunks = _rich_chunks(inline_rich_text(text))
    blocks = [_block(kind, {"rich_text": chunks[0], **extra})]
    blocks.extend(_block("paragraph", {"rich_text": chunk}) for chunk in chunks[1:])
    return blocks


def _paragraph_blocks(text: str) -> list[dict]:
    return [_block("paragraph", {"rich_text": chunk}) for chunk in _rich_chunks(inline_rich_text(text)) if chunk]


def _code_language(lang: str) -> str:
    lang = (lang or "").strip().lower()
    lang = _CODE_ALIASES.get(lang, lang)
    return lang if lang in _CODE_LANGUAGES else "plain text"


def _image_block(src: str, alt: str, resolver: ImageResolver | None) -> dict | None:
    if re.match(r"^https?://", src):
        payload: dict[str, Any] = {"type": "external", "external": {"url": src}}
    elif resolver is None:
        return None
    else:
        resolved = resolver(src)
        if not resolved:
            return None
        payload = dict(resolved)
    if alt:
        payload["caption"] = _capped(rich_text(alt))
    return _block("image", payload)


def _video_block(url: str) -> dict:
    return _block("video", {"type": "external", "external": {"url": url}})


def _table_cells(line: str) -> list[str]:
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    return [c.strip() for c in s.split("|")]


def _is_table_separator(line: str) -> bool:
    s = line.strip()
    if "|" not in s and not _TABLE_SEP_CELL_RE.match(s):
        return False
    cells = _table_cells(s)
    return any(cells) and all(_TABLE_SEP_CELL_RE.match(c) for c in cells if c)


def _table_row(cells: list[str]) -> dict:
    return _block("table_row", {"cells": [_capped(inline_rich_text(c)) if c else [] for c in cells]})


def _parse_table(lines: list[str], i: int) -> tuple[list[dict], int]:
    header = _table_cells(lines[i])
    i += 2  # header + separator
    body: list[list[str]] = []
    while i < len(lines) and lines[i].strip().startswith("|"):
        body.append(_table_cells(lines[i]))
        i += 1
    width = max([len(header)] + [len(r) for r in body])
    pad = lambda row: row + [""] * (width - len(row))  # noqa: E731
    header = pad(header)
    body = [pad(r) for r in body]
    blocks = []
    for start in range(0, max(len(body), 1), TABLE_ROWS_PER_BLOCK):
        rows = [_table_row(header)] + [_table_row(r) for r in body[start : start + TABLE_ROWS_PER_BLOCK]]
        blocks.append(_block("table", {"table_width": width, "has_column_header": True, "has_row_header": False, "children": rows}))
    return blocks, i


def _parse_list(lines: list[str], i: int) -> tuple[list[dict], int]:
    items: list[list[Any]] = []  # [indent, kind, text]
    n = len(lines)
    while i < n:
        line = lines[i]
        if not line.strip():
            j = i
            while j < n and not lines[j].strip():
                j += 1
            if j < n and _LIST_RE.match(lines[j]):  # blank lines between items keep the list together
                i = j
                continue
            break
        m = _LIST_RE.match(line)
        if m:
            indent = len(m.group(1).expandtabs(4))
            kind = "numbered_list_item" if m.group(2)[0].isdigit() else "bulleted_list_item"
            items.append([indent, kind, m.group(3).strip()])
            i += 1
            continue
        if line[:1].isspace() and items:  # indented continuation of the previous item
            items[-1][2] += " " + line.strip()
            i += 1
            continue
        break
    # indent -> tree
    root: list[dict] = []
    stack: list[tuple[int, dict]] = []
    for indent, kind, text in items:
        node = {"kind": kind, "text": text, "children": []}
        while stack and stack[-1][0] >= indent:
            stack.pop()
        (stack[-1][1]["children"] if stack else root).append(node)
        stack.append((indent, node))
    return _list_blocks(root, 0), i


def _list_blocks(nodes: list[dict], depth: int) -> list[dict]:
    """Nodes -> list item blocks; children nest at most two levels deep, deeper ones become siblings."""
    out: list[dict] = []
    for node in nodes:
        blocks = _text_blocks(node["kind"], node["text"])
        item, overflow = blocks[0], blocks[1:]
        trailing: list[dict] = []
        if node["children"]:
            kids = _list_blocks(node["children"], depth + 1)
            if depth < 2:
                item[node["kind"]]["children"] = kids[:CHILDREN_LIMIT]
                trailing = kids[CHILDREN_LIMIT:]
            else:
                trailing = kids
        out.append(item)
        out.extend(overflow)
        out.extend(trailing)
    return out


def markdown_to_blocks(md: str, *, image_resolver: ImageResolver | None = None) -> list[dict]:
    """The pipeline's markdown -> Notion blocks (headings, paragraphs, lists, quotes, dividers, code fences,
    pipe tables, images, video links). HTML comments are dropped. Every limit Notion enforces on one request
    is respected here or in `chunk_children`."""
    lines = _COMMENT_RE.sub("", md or "").splitlines()
    blocks: list[dict] = []
    para: list[str] = []
    n = len(lines)

    def flush() -> None:
        if para:
            blocks.extend(_paragraph_blocks(" ".join(para)))
            para.clear()

    i = 0
    while i < n:
        line = lines[i]
        stripped = line.strip()
        if not stripped:
            flush()
            i += 1
            continue
        if stripped.startswith("```"):
            flush()
            lang = stripped[3:].strip()
            j = i + 1
            code_lines = []
            while j < n and not lines[j].strip().startswith("```"):
                code_lines.append(lines[j])
                j += 1
            blocks.append(_block("code", {"rich_text": _capped(rich_text("\n".join(code_lines))), "language": _code_language(lang)}))
            i = j + 1
            continue
        m = _HEADING_RE.match(stripped)
        if m:
            flush()
            blocks.extend(_text_blocks(f"heading_{min(len(m.group(1)), 3)}", m.group(2).strip()))
            i += 1
            continue
        if _DIVIDER_RE.match(stripped):
            flush()
            blocks.append(_block("divider", {}))
            i += 1
            continue
        m = _IMAGE_RE.match(stripped)
        if m:
            flush()
            image = _image_block(m.group(2), m.group(1).strip(), image_resolver)
            if image:
                blocks.append(image)
            i += 1
            continue
        if _YOUTUBE_RE.match(stripped) or _VIDEO_FILE_RE.match(stripped):
            flush()
            blocks.append(_video_block(stripped))
            i += 1
            continue
        if _EXCALIDRAW_RE.match(stripped):
            flush()
            blocks.append(_block("embed", {"url": stripped}))
            i += 1
            continue
        if stripped.startswith("|") and i + 1 < n and _is_table_separator(lines[i + 1]):
            flush()
            table_blocks, i = _parse_table(lines, i)
            blocks.extend(table_blocks)
            continue
        if stripped.startswith(">"):
            flush()
            quote_lines = []
            while i < n and lines[i].strip().startswith(">"):
                quote_lines.append(lines[i].strip()[1:].strip())
                i += 1
            blocks.extend(_text_blocks("quote", "\n".join(quote_lines).strip()))
            continue
        if _LIST_RE.match(line):
            flush()
            list_blocks, i = _parse_list(lines, i)
            blocks.extend(list_blocks)
            continue
        para.append(stripped)
        i += 1
    flush()
    return blocks


# --------------------------------------------------------------------------- publisher


class NotionPublisher:
    """Publish a markdown summary into a Notion page and read the comments left on it.

    Create once (a child page of `parent_id`), then refresh the same page in place so its link never
    changes. The page may be a database row (an author's row in Authors): its title is never touched, and
    its child databases and child pages survive a refresh.
    """

    RESOLVED_REPLY = "Applied by the AI Primer summary agent."

    def __init__(self, backend: NotionBackend, *, scan_blocks_for_comments: int = 300):
        self.backend = backend
        self.scan_blocks_for_comments = scan_blocks_for_comments

    # -- images
    def _image_resolver(self) -> ImageResolver:
        def resolve(src: str) -> dict | None:
            path = Path(src)
            if not path.is_file():
                return None
            content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            upload_id = self.backend.upload_file(path, content_type)
            return {"type": "file_upload", "file_upload": {"id": upload_id}}

        return resolve

    # -- publishing
    def publish_markdown(self, md_text: str, title: str, parent_id: str, existing_doc_id: str | None = None) -> dict:
        """Create (or refresh) the page from markdown. Returns {doc_id, url, created, format, name}."""
        blocks = markdown_to_blocks(md_text, image_resolver=self._image_resolver())
        if existing_doc_id:
            old = [b for b in self.backend.list_block_children(existing_doc_id) if b.get("type") not in CONTAINER_BLOCK_TYPES]
            # The new content takes the old content's place: inserted right after the first old block, so it stays
            # above any child page or database that follows (a node page's sub-pages, an author's Cards table).
            self.backend.append_block_children(existing_doc_id, blocks, after=old[0]["id"] if old else None)
            for block in old:
                self.backend.delete_block(block["id"])
            return {"doc_id": existing_doc_id, "url": self.backend.page_url(existing_doc_id), "created": False, "format": "notion", "name": title}
        first, rest = blocks[:CHILDREN_LIMIT], blocks[CHILDREN_LIMIT:]
        page = self.backend.create_page({"page_id": parent_id}, {"title": {"title": _capped(rich_text(title))}}, children=first)
        if rest:
            self.backend.append_block_children(page["id"], rest)
        return {"doc_id": page["id"], "url": page.get("url") or self.backend.page_url(page["id"]), "created": True, "format": "notion", "name": title}

    # -- comments
    def _comment_batches(self, doc_id: str):
        """Yield (comments, quoted text): the page-level comments first, then those of the first
        `scan_blocks_for_comments` top-level blocks (one call per block; the API has no page-wide listing)."""
        yield self.backend.list_comments(doc_id), ""
        if self.scan_blocks_for_comments <= 0:
            return
        for block in self.backend.list_block_children(doc_id)[: self.scan_blocks_for_comments]:
            if block.get("type") in CONTAINER_BLOCK_TYPES:
                continue
            comments = self.backend.list_comments(block["id"])
            if comments:
                yield comments, block_plain_text(block)[:200]

    @staticmethod
    def _author(comment: dict) -> str:
        who = comment.get("created_by") or {}
        return who.get("name") or who.get("id") or ""

    def unresolved_comments(self, doc_id: str) -> list[dict]:
        """Open discussions on the page, oldest comment of each first. Notion's API cannot resolve a comment,
        so a discussion whose last reply is the agent's "applied" note counts as resolved and is omitted."""
        discussions: dict[str, dict] = {}
        for comments, quoted in self._comment_batches(doc_id):
            for c in sorted(comments, key=lambda x: x.get("created_time", "")):
                did = c.get("discussion_id") or c.get("id", "")
                text = plain_text(c.get("rich_text"))
                if did not in discussions:
                    discussions[did] = {
                        "comment_id": c.get("id", ""),
                        "discussion_id": did,
                        "comment": text,
                        "author": self._author(c),
                        "created": c.get("created_time", ""),
                        "quoted": quoted,
                        "replies": [],
                    }
                else:
                    discussions[did]["replies"].append(text)
        return [d for d in discussions.values() if not (d["replies"] and d["replies"][-1] == self.RESOLVED_REPLY)]

    def _discussion_for(self, doc_id: str, ref: str) -> str:
        for comments, _quoted in self._comment_batches(doc_id):
            for c in comments:
                if c.get("id") == ref:
                    return c.get("discussion_id") or ref
                if c.get("discussion_id") == ref:
                    return ref
        return ref  # assume a discussion id

    def resolve(self, doc_id: str, comment_id: str) -> None:
        """Reply in the discussion that the feedback was applied (the API cannot mark it resolved)."""
        self.backend.create_comment(discussion_id=self._discussion_for(doc_id, comment_id), text=self.RESOLVED_REPLY)
