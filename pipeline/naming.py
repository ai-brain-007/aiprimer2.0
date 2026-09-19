"""File naming in Drive and the front matter of `.extracted.md` files."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import frontmatter
import yaml

from .models import Resource

UNDATED = "undated"
_BAD_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def clean_component(text: str, max_chars: int) -> str:
    text = _BAD_CHARS.sub(" ", text or "").strip()
    text = re.sub(r"\s+", " ", text)
    if len(text) > max_chars:
        text = text[: max_chars - 1].rstrip() + "…"
    return text or "Untitled"


def date_prefix(resource: Resource) -> str:
    d = (resource.published_date or "").strip()
    if not d or resource.date_precision == "unknown":
        return UNDATED
    return d


def drive_filename(resource: Resource, ext: str, author_name: str = "", max_title_chars: int = 80) -> str:
    ext = ext if ext.startswith(".") or ext == "" else f".{ext}"
    author = clean_component(author_name or resource.author_raw or "Unknown author", 60)
    title = clean_component(resource.title or resource.resource_id, max_title_chars)
    return f"{date_prefix(resource)} - {author} - {title} [{resource.resource_id}]{ext}"


def extracted_filename(resource: Resource, author_name: str = "", max_title_chars: int = 80) -> str:
    base = drive_filename(resource, "", author_name, max_title_chars)
    return f"{base}.extracted.md"


def data_export_filename(resource: Resource, sheet_name: str, author_name: str = "", max_title_chars: int = 60) -> str:
    base = drive_filename(resource, "", author_name, max_title_chars)
    return f"{base}.{clean_component(sheet_name, 40)}.csv"


def write_extracted_markdown(path: Path, resource: Resource, extra: dict[str, Any], text: str) -> Path:
    meta: dict[str, Any] = {k: v for k, v in resource.to_row().items() if v != ""}
    meta.update({k: v for k, v in extra.items() if v not in (None, "", [], {})})
    post = frontmatter.Post(text, **meta)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(frontmatter.dumps(post), encoding="utf-8")
    return path


def read_extracted_markdown(path: Path) -> tuple[dict[str, Any], str]:
    post = frontmatter.load(str(path))
    return dict(post.metadata), post.content


def dump_yaml(data: Any) -> str:
    return yaml.safe_dump(data, sort_keys=False, allow_unicode=True)
