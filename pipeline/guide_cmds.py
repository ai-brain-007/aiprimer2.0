"""Reference pages for the owner in Notion, generated from `docs/notion/*.md`.

`python -m pipeline doc guide` publishes every markdown file of `docs/notion/` as a page inside
"LAYER 0 - CONFIG" under the "AI Primer" page (`pipeline.layout.guide_home`). The page title is the file's first
`# ` heading (the file name without its number otherwise). A page that already exists under that title is
refreshed in place, so its link never changes; a copy left directly under "AI Primer" by an earlier version is
moved, not duplicated. Deterministic: no model is called; the agent edits the markdown, the script publishes it.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .context import AppContext
from .notion import CONTAINER_BLOCK_TYPES, NotionPublisher, child_pages

__all__ = ["GUIDE_DIR", "child_pages", "guide_files", "publish_guide", "split_title"]

GUIDE_DIR = Path("docs") / "notion"
_HEADING = re.compile(r"^#\s+(.+?)\s*$")
_NUMBER_PREFIX = re.compile(r"^\d+[-_ ]*")


def guide_files(repo_root: Path) -> list[Path]:
    """The markdown files to publish, in file-name order (a number prefix such as `10-` fixes the order)."""
    return sorted(p for p in (repo_root / GUIDE_DIR).glob("*.md") if p.is_file())


def split_title(md_text: str, fallback: str) -> tuple[str, str]:
    """(title, body): the first `# ` heading becomes the page title and leaves the body, so the title is
    not repeated as the first line of the page."""
    lines = md_text.splitlines()
    for i, line in enumerate(lines):
        if not line.strip():
            continue
        m = _HEADING.match(line)
        if m:
            return m.group(1), "\n".join(lines[i + 1 :]).strip() + "\n"
        break
    return fallback, md_text


def publish_guide(ctx: AppContext, only: str | None = None) -> dict[str, Any]:
    """Create or refresh the reference pages under the "AI Primer" page. `only` limits to one file name part."""
    backend = ctx.notion
    parent_id = ctx.settings.notion_parent_page_id
    if backend is None or not parent_id:
        raise RuntimeError("Notion mode is off: AIPRIMER_NOTION_PAGE_ID is not set (see README.md, Setup)")
    files = [p for p in guide_files(ctx.settings.repo_root) if not only or only.lower() in p.name.lower()]
    if not files:
        raise RuntimeError(f"no markdown file to publish in {GUIDE_DIR}/" + (f" matching {only!r}" if only else ""))
    from .layout import GUIDES_SECTION, guide_home

    home = guide_home(ctx)
    publisher = NotionPublisher(backend, scan_blocks_for_comments=0)
    existing = child_pages(backend, home)
    stray = child_pages(backend, parent_id)  # guides published before the layer layout sat directly under "AI Primer"
    pages: list[dict[str, Any]] = []
    for path in files:
        title, body = split_title(path.read_text(encoding="utf-8"), _NUMBER_PREFIX.sub("", path.stem).replace("-", " ").strip() or path.stem)
        page_id = existing.get(title)
        moved = False
        if page_id is None and title in stray:
            backend.move_page(stray[title], home)
            page_id = existing[title] = stray[title]
            moved = True
        info = publisher.publish_markdown(body, title, home, page_id)
        pages.append({"title": title, "file": str(path.relative_to(ctx.settings.repo_root)), "url": info["url"], "page_id": info["doc_id"], "created": info["created"], "moved": moved})
    return {"parent_url": backend.page_url(parent_id), "home": GUIDES_SECTION, "home_url": backend.page_url(home), "pages": pages, "kept_block_types": list(CONTAINER_BLOCK_TYPES)}
