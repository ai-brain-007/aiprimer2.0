"""In-memory fake of the Notion API backend (`pipeline.notion.NotionBackend`), for tests.

It stores pages, databases, blocks, comments and uploads with the shapes the API returns, and it enforces
the limits the converter must respect (2000 characters per text item, 100 items per rich text array, 100
blocks per children array, two levels of nesting, one cell per table column), so a converter bug fails a
test instead of a live call.
"""

from __future__ import annotations

import copy
import itertools
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from pipeline.notion import CHILDREN_LIMIT, RICH_TEXT_LIMIT, TEXT_LIMIT, NotionError, block_plain_text, plain_text, property_plain_text

_EPOCH = datetime(2026, 9, 27, 9, 0, 0)


def _bad(message: str) -> NotionError:
    return NotionError(400, "validation_error", message)


class FakeNotionBackend:
    def __init__(self):
        self.pages: dict[str, dict] = {}
        self.databases: dict[str, dict] = {}
        self.blocks: dict[str, dict] = {}
        self.children: dict[str, list[str]] = {}  # parent id -> ordered child block ids
        self.comments: dict[str, dict] = {}
        self.uploads: dict[str, dict] = {}
        self.calls: list[str] = []
        self._ids = itertools.count(1)
        self._ticks = itertools.count(1)

    # -- internals
    @staticmethod
    def _id(value):
        """Like the API, accept an id with or without dashes; everything is stored under the dashed UUID form."""
        if isinstance(value, str) and len(value) == 32 and all(c in "0123456789abcdefABCDEF" for c in value):
            return str(uuid.UUID(value))
        return value

    def _new_id(self) -> str:
        return str(uuid.UUID(int=next(self._ids)))

    def _now(self) -> str:
        return (_EPOCH + timedelta(seconds=next(self._ticks))).strftime("%Y-%m-%dT%H:%M:%S.000Z")

    def _rt(self, items) -> list[dict]:
        """Validate a rich text array the way the API does and add plain_text / href."""
        if not isinstance(items, list):
            raise _bad("rich_text should be an array")
        if len(items) > RICH_TEXT_LIMIT:
            raise _bad(f"rich_text has {len(items)} items; the limit is {RICH_TEXT_LIMIT}")
        out = []
        for item in items:
            if not isinstance(item, dict) or item.get("type", "text") != "text" or "text" not in item:
                raise _bad(f"unsupported rich text item: {item!r}")
            content = item["text"].get("content", "")
            if not isinstance(content, str):
                raise _bad("text.content should be a string")
            if len(content) > TEXT_LIMIT:
                raise _bad(f"text.content has {len(content)} characters; the limit is {TEXT_LIMIT}")
            link = item["text"].get("link") or None
            stored = copy.deepcopy(item)
            stored["type"] = "text"
            stored["plain_text"] = content
            stored["href"] = link.get("url") if isinstance(link, dict) else None
            out.append(stored)
        return out

    def _parent_of(self, parent_id: str) -> dict:
        parent_id = self._id(parent_id)
        if parent_id in self.pages:
            return {"type": "page_id", "page_id": parent_id}
        if parent_id in self.blocks:
            return {"type": "block_id", "block_id": parent_id}
        raise NotionError(404, "object_not_found", f"Could not find block with ID: {parent_id}")

    def _store_block(self, parent_id: str, block: dict, depth: int) -> dict:
        if depth > 2:
            raise _bad("children can be nested at most two levels deep in one request")
        kind = block.get("type")
        if not kind or kind not in block or not isinstance(block[kind], dict):
            raise _bad(f"block without a valid type: {block!r}")
        payload = copy.deepcopy(block[kind])
        kids = payload.pop("children", None) or block.get("children") or []
        if "rich_text" in payload:
            payload["rich_text"] = self._rt(payload["rich_text"])
        if "caption" in payload:
            payload["caption"] = self._rt(payload["caption"])
        if kind == "table_row":
            cells = payload.get("cells")
            if not isinstance(cells, list):
                raise _bad("table_row needs cells")
            payload["cells"] = [self._rt(c) for c in cells]
        if kind == "table":
            if not kids:
                raise _bad("a table needs at least one table_row child")
            for k in kids:
                if k.get("type") != "table_row":
                    raise _bad("table children must be table_row blocks")
                if len((k.get("table_row") or {}).get("cells", [])) != payload.get("table_width"):
                    raise _bad("every table_row needs exactly table_width cells")
        if kind == "code" and "language" not in payload:
            raise _bad("code blocks need a language")
        if kind in ("image", "video") and payload.get("type") not in ("external", "file_upload", "file"):
            raise _bad(f"{kind} needs an external url or a file upload")
        if len(kids) > CHILDREN_LIMIT:
            raise _bad(f"children has {len(kids)} blocks; the limit is {CHILDREN_LIMIT}")
        bid = self._new_id()
        stored = {"object": "block", "id": bid, "type": kind, "parent": self._parent_of(parent_id), "has_children": bool(kids), "archived": False, "created_time": self._now(), kind: payload}
        self.blocks[bid] = stored
        self.children.setdefault(parent_id, []).append(bid)
        for k in kids:
            self._store_block(bid, k, depth + 1)
        return copy.deepcopy(stored)

    def _schema(self, properties: dict, existing: dict | None = None) -> dict:
        schema = copy.deepcopy(existing or {})
        for name, spec in properties.items():
            if spec is None:
                schema.pop(name, None)
                continue
            if not isinstance(spec, dict):
                raise _bad(f"property {name} should be an object")
            if "name" in spec and name in schema and set(spec) <= {"name", "id"}:
                prop = schema.pop(name)
                prop["name"] = spec["name"]
                schema[spec["name"]] = prop
                continue
            kind = next((k for k in spec if k not in ("name", "id")), None)
            if kind is None:
                raise _bad(f"property {name} has no type")
            schema[name] = {"id": name, "name": name, "type": kind, kind: copy.deepcopy(spec[kind] or {})}
        titles = [n for n, p in schema.items() if p["type"] == "title"]
        if len(titles) != 1:
            raise _bad("a database needs exactly one title property")
        return schema

    def _db_page_props(self, db: dict, properties: dict, current: dict | None = None) -> dict:
        schema = db["properties"]
        props = copy.deepcopy(current or {})
        for name, value in properties.items():
            if name not in schema:
                raise _bad(f"{name} is not a property that exists.")
            kind = schema[name]["type"]
            if not isinstance(value, dict) or kind not in value:
                raise _bad(f"{name} is expected to be {kind}.")
            payload = value[kind]
            if kind in ("title", "rich_text"):
                payload = self._rt(payload)
            props[name] = {"id": schema[name]["id"], "type": kind, kind: copy.deepcopy(payload)}
        return props

    def _page_view(self, page: dict) -> dict:
        view = copy.deepcopy(page)
        parent = page["parent"]
        if parent.get("type") == "database_id":
            schema = self.databases[parent["database_id"]]["properties"]
            for name, prop in schema.items():
                if name not in view["properties"]:
                    kind = prop["type"]
                    view["properties"][name] = {"id": prop["id"], "type": kind, kind: [] if kind in ("title", "rich_text") else None}
        return view

    def _matches(self, page: dict, flt: dict | None) -> bool:
        if not flt:
            return True
        if "and" in flt:
            return all(self._matches(page, f) for f in flt["and"])
        if "or" in flt:
            return any(self._matches(page, f) for f in flt["or"])
        name = flt["property"]
        kind = next(k for k in flt if k != "property")
        cond = flt[kind]
        value = property_plain_text(self._page_view(page)["properties"].get(name))
        if "equals" in cond:
            return value == cond["equals"]
        if "contains" in cond:
            return cond["contains"] in value
        if "is_empty" in cond:
            return (value == "") == bool(cond["is_empty"])
        if "is_not_empty" in cond:
            return (value != "") == bool(cond["is_not_empty"])
        raise _bad(f"unsupported filter: {flt!r}")

    def _db_pages(self, database_id: str) -> list[dict]:
        return [p for p in self.pages.values() if p["parent"].get("database_id") == database_id and not p["archived"]]

    # -- blocks
    def list_block_children(self, block_id):
        block_id = self._id(block_id)
        self.calls.append(f"list_children:{block_id}")
        self._parent_of(block_id)
        return [copy.deepcopy(self.blocks[b]) for b in self.children.get(block_id, []) if not self.blocks[b]["archived"]]

    def append_block_children(self, block_id, children):
        block_id = self._id(block_id)
        self.calls.append(f"append_children:{block_id}:{len(children)}")
        self._parent_of(block_id)
        return [self._store_block(block_id, c, 0) for c in children]

    def delete_block(self, block_id):
        block_id = self._id(block_id)
        self.calls.append(f"delete_block:{block_id}")
        block = self.blocks.get(block_id)
        if block is None:
            raise NotionError(404, "object_not_found", f"Could not find block with ID: {block_id}")
        block["archived"] = True
        if block_id in self.pages:
            self.pages[block_id]["archived"] = True
        if block_id in self.databases:
            self.databases[block_id]["archived"] = True

    # -- databases
    def create_database(self, parent_page_id, title, properties):
        parent_page_id = self._id(parent_page_id)
        self.calls.append(f"create_database:{title}")
        if parent_page_id not in self.pages:
            raise NotionError(404, "object_not_found", f"Could not find page with ID: {parent_page_id}")
        did = self._new_id()
        db = {
            "object": "database",
            "id": did,
            "title": self._rt([{"type": "text", "text": {"content": title, "link": None}}]),
            "parent": {"type": "page_id", "page_id": parent_page_id},
            "properties": self._schema(properties),
            "archived": False,
            "url": f"https://www.notion.so/{did.replace('-', '')}",
        }
        self.databases[did] = db
        self.blocks[did] = {"object": "block", "id": did, "type": "child_database", "parent": {"type": "page_id", "page_id": parent_page_id}, "has_children": False, "archived": False, "created_time": self._now(), "child_database": {"title": title}}
        self.children.setdefault(parent_page_id, []).append(did)
        return copy.deepcopy(db)

    def update_database(self, database_id, properties):
        database_id = self._id(database_id)
        self.calls.append(f"update_database:{database_id}")
        db = self.retrieve_database(database_id)
        self.databases[database_id]["properties"] = self._schema(properties, db["properties"])
        return copy.deepcopy(self.databases[database_id])

    def retrieve_database(self, database_id):
        database_id = self._id(database_id)
        self.calls.append(f"retrieve_database:{database_id}")
        db = self.databases.get(database_id)
        if db is None:
            raise NotionError(404, "object_not_found", f"Could not find database with ID: {database_id}")
        return copy.deepcopy(db)

    def query_database(self, database_id, filter=None):
        database_id = self._id(database_id)
        self.calls.append(f"query:{database_id}")
        if database_id not in self.databases:
            raise NotionError(404, "object_not_found", f"Could not find database with ID: {database_id}")
        return [self._page_view(p) for p in self._db_pages(database_id) if self._matches(p, filter)]

    # -- pages
    def create_page(self, parent, properties, children=None, icon=None):
        parent = {k: self._id(v) for k, v in parent.items()}
        self.calls.append("create_page")
        if "database_id" in parent:
            db = self.databases.get(parent["database_id"])
            if db is None:
                raise NotionError(404, "object_not_found", f"Could not find database with ID: {parent['database_id']}")
            props = self._db_page_props(db, properties)
            parent_obj = {"type": "database_id", "database_id": parent["database_id"]}
        elif "page_id" in parent:
            if parent["page_id"] not in self.pages:
                raise NotionError(404, "object_not_found", f"Could not find page with ID: {parent['page_id']}")
            title = (properties.get("title") or {}).get("title", [])
            props = {"title": {"id": "title", "type": "title", "title": self._rt(title)}}
            parent_obj = {"type": "page_id", "page_id": parent["page_id"]}
        else:
            raise _bad("parent should be a database_id or a page_id")
        if children and len(children) > CHILDREN_LIMIT:
            raise _bad(f"children has {len(children)} blocks; the limit is {CHILDREN_LIMIT}")
        pid = self._new_id()
        page = {"object": "page", "id": pid, "parent": parent_obj, "archived": False, "properties": props, "icon": icon, "created_time": self._now(), "url": self.page_url(pid)}
        self.pages[pid] = page
        if parent_obj["type"] == "page_id":  # a page under a page also shows as a child_page block
            self.blocks[pid] = {"object": "block", "id": pid, "type": "child_page", "parent": parent_obj, "has_children": bool(children), "archived": False, "created_time": page["created_time"], "child_page": {"title": plain_text(props["title"]["title"])}}
            self.children.setdefault(parent_obj["page_id"], []).append(pid)
        for c in children or []:
            self._store_block(pid, c, 0)
        return self._page_view(page)

    def update_page(self, page_id, properties=None, archived=None):
        page_id = self._id(page_id)
        self.calls.append(f"update_page:{page_id}")
        page = self.pages.get(page_id)
        if page is None:
            raise NotionError(404, "object_not_found", f"Could not find page with ID: {page_id}")
        if properties:
            if page["parent"]["type"] == "database_id":
                page["properties"] = self._db_page_props(self.databases[page["parent"]["database_id"]], properties, page["properties"])
            else:
                title = (properties.get("title") or {}).get("title")
                if title is not None:
                    page["properties"]["title"]["title"] = self._rt(title)
                    if page_id in self.blocks:
                        self.blocks[page_id]["child_page"]["title"] = plain_text(page["properties"]["title"]["title"])
        if archived is not None:
            page["archived"] = bool(archived)
            if page_id in self.blocks:
                self.blocks[page_id]["archived"] = bool(archived)
        return self._page_view(page)

    def move_page(self, page_id, parent_page_id):
        page_id, parent_page_id = self._id(page_id), self._id(parent_page_id)
        self.calls.append(f"move_page:{page_id}->{parent_page_id}")
        page = self.pages.get(page_id)
        if page is None or page_id in self.databases:
            raise NotionError(404, "object_not_found", f"Could not find page with ID: {page_id}")
        if parent_page_id not in self.pages:
            raise NotionError(404, "object_not_found", f"Could not find page with ID: {parent_page_id}")
        old = page["parent"]
        if old.get("type") == "page_id":
            self.children[old["page_id"]] = [b for b in self.children.get(old["page_id"], []) if b != page_id]
        page["parent"] = {"type": "page_id", "page_id": parent_page_id}
        block = self.blocks.get(page_id) or {"object": "block", "id": page_id, "type": "child_page", "has_children": bool(self.children.get(page_id)), "archived": False, "created_time": page["created_time"], "child_page": {"title": plain_text(page["properties"]["title"]["title"])}}
        block["parent"] = page["parent"]
        self.blocks[page_id] = block
        self.children.setdefault(parent_page_id, []).append(page_id)
        return self._page_view(page)

    def retrieve_page(self, page_id):
        page_id = self._id(page_id)
        self.calls.append(f"retrieve_page:{page_id}")
        page = self.pages.get(page_id)
        if page is None:
            raise NotionError(404, "object_not_found", f"Could not find page with ID: {page_id}")
        return self._page_view(page)

    # -- comments
    def _comment_parent_id(self, comment: dict) -> str:
        parent = comment["parent"]
        return parent.get("page_id") or parent.get("block_id") or ""

    def list_comments(self, block_id):
        block_id = self._id(block_id)
        self.calls.append(f"list_comments:{block_id}")
        self._parent_of(block_id)
        return [copy.deepcopy(c) for c in self.comments.values() if self._comment_parent_id(c) == block_id and not c.get("_resolved")]

    def create_comment(self, *, page_id=None, discussion_id=None, text):
        page_id = self._id(page_id)
        self.calls.append("create_comment")
        return self._add_comment(text, {"object": "user", "id": "bot-ai-primer", "name": "AI Primer pipeline", "type": "bot"}, page_id=page_id, discussion_id=discussion_id)

    def _add_comment(self, text, created_by, *, page_id=None, discussion_id=None, block_id=None):
        if discussion_id:
            first = next((c for c in self.comments.values() if c["discussion_id"] == discussion_id), None)
            if first is None:
                raise NotionError(404, "object_not_found", f"Could not find discussion with ID: {discussion_id}")
            parent = copy.deepcopy(first["parent"])
        elif block_id:
            if block_id not in self.blocks:
                raise NotionError(404, "object_not_found", f"Could not find block with ID: {block_id}")
            parent = {"type": "block_id", "block_id": block_id}
            discussion_id = self._new_id()
        elif page_id:
            if page_id not in self.pages:
                raise NotionError(404, "object_not_found", f"Could not find page with ID: {page_id}")
            parent = {"type": "page_id", "page_id": page_id}
            discussion_id = self._new_id()
        else:
            raise _bad("a comment needs a page, a block or a discussion")
        cid = self._new_id()
        comment = {
            "object": "comment",
            "id": cid,
            "discussion_id": discussion_id,
            "parent": parent,
            "rich_text": self._rt([{"type": "text", "text": {"content": text, "link": None}}]),
            "created_time": self._now(),
            "created_by": created_by,
        }
        self.comments[cid] = comment
        return copy.deepcopy(comment)

    # -- files
    def upload_file(self, path, content_type):
        path = Path(path)
        self.calls.append(f"upload_file:{path.name}")
        if not path.is_file():
            raise FileNotFoundError(str(path))
        fid = self._new_id()
        self.uploads[fid] = {"id": fid, "filename": path.name, "content_type": content_type, "size": path.stat().st_size}
        return fid

    def page_url(self, page_id):
        return f"https://www.notion.so/{page_id.replace('-', '')}"

    # -- test helpers
    def add_page(self, title: str) -> str:
        """A top-level page (workspace parent) to act as the "AI Primer" page."""
        pid = self._new_id()
        self.pages[pid] = {"object": "page", "id": pid, "parent": {"type": "workspace", "workspace": True}, "archived": False, "properties": {"title": {"id": "title", "type": "title", "title": self._rt([{"type": "text", "text": {"content": title, "link": None}}])}}, "icon": None, "created_time": self._now(), "url": self.page_url(pid)}
        return pid

    def child_databases(self, page_id: str) -> dict[str, str]:
        return {self.blocks[b]["child_database"]["title"]: b for b in self.children.get(page_id, []) if self.blocks[b]["type"] == "child_database" and not self.blocks[b]["archived"]}

    def rows(self, database_id: str) -> list[dict[str, str]]:
        return [{name: property_plain_text(prop) for name, prop in self._page_view(p)["properties"].items()} for p in self._db_pages(database_id)]

    def _flatten(self, parent_id: str, out: list[str]) -> None:
        for bid in self.children.get(parent_id, []):
            block = self.blocks[bid]
            if block["archived"]:
                continue
            if block["type"] != "table":
                out.append(block_plain_text(block))
            if block["type"] != "child_page":  # a child page's body belongs to that page
                self._flatten(bid, out)

    def page_plain_text(self, page_id: str) -> str:
        """All blocks of a page flattened, one line per block, table rows as `a | b`."""
        lines: list[str] = []
        self._flatten(page_id, lines)
        return "\n".join(line for line in lines if line)

    def _block_ids_under(self, parent_id: str) -> list[str]:
        out = []
        for bid in self.children.get(parent_id, []):
            out.append(bid)
            if self.blocks[bid]["type"] != "child_page":
                out.extend(self._block_ids_under(bid))
        return out

    def comments_for(self, page_id: str) -> list[dict]:
        """Every comment on the page or on one of its blocks."""
        ids = {page_id, *self._block_ids_under(page_id)}
        return [copy.deepcopy(c) for c in self.comments.values() if self._comment_parent_id(c) in ids]

    def add_comment(self, page_id: str, text: str, author: str = "Jules", block_id: str | None = None, discussion_id: str | None = None) -> dict:
        """Simulate a human comment: on the page, on one of its blocks, or as a reply in a discussion."""
        who = {"object": "user", "id": f"user-{author.lower()}", "name": author, "type": "person"}
        return self._add_comment(text, who, page_id=None if (block_id or discussion_id) else page_id, discussion_id=discussion_id, block_id=block_id)
