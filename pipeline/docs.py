"""Publish a markdown summary as a Google Doc (create once, then refresh in place; the link never changes)."""

from __future__ import annotations

import html
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from .drive import GDOC_MIME, DriveClient, doc_url


def markdown_to_html(md: str) -> str:
    """Minimal, dependency-free markdown -> HTML good enough for Google Docs import (pandoc is used when present)."""
    if shutil.which("pandoc"):
        try:
            out = subprocess.run(
                ["pandoc", "-f", "gfm", "-t", "html", "--wrap=none"], input=md, capture_output=True, text=True, check=True, timeout=120
            )
            return f"<html><body>{out.stdout}</body></html>"
        except Exception:
            pass
    lines = md.splitlines()
    out: list[str] = ["<html><body>"]
    in_list = False
    in_table = False
    for line in lines:
        stripped = line.rstrip()
        if in_table and not stripped.startswith("|"):
            out.append("</table>")
            in_table = False
        if in_list and not re.match(r"^\s*[-*] ", stripped):
            out.append("</ul>")
            in_list = False
        m = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        if m:
            level = len(m.group(1))
            out.append(f"<h{level}>{_inline(m.group(2))}</h{level}>")
            continue
        if re.match(r"^\s*[-*] ", stripped):
            if not in_list:
                out.append("<ul>")
                in_list = True
            item_text = re.sub(r"^\s*[-*] ", "", stripped)
            out.append(f"<li>{_inline(item_text)}</li>")
            continue
        if stripped.startswith("|"):
            cells = [c.strip() for c in stripped.strip("|").split("|")]
            if all(re.match(r"^:?-{2,}:?$", c) for c in cells if c):
                continue
            if not in_table:
                out.append("<table border='1'>")
                in_table = True
            out.append("<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in cells) + "</tr>")
            continue
        if not stripped:
            continue
        out.append(f"<p>{_inline(stripped)}</p>")
    if in_list:
        out.append("</ul>")
    if in_table:
        out.append("</table>")
    out.append("</body></html>")
    return "\n".join(out)


def _inline(text: str) -> str:
    text = html.escape(text, quote=False)
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"(?<!\*)\*(?!\*)(.+?)\*", r"<i>\1</i>", text)
    text = re.sub(r"`(.+?)`", r"<code>\1</code>", text)
    text = re.sub(r"\[(.+?)\]\((https?://[^)]+)\)", r'<a href="\2">\1</a>', text)
    return text


class DocsPublisher:
    def __init__(self, drive: DriveClient, sharing: str = "anyone_with_link"):
        self.drive = drive
        self.sharing = sharing

    def publish_markdown(self, md_text: str, title: str, parent_id: str, existing_doc_id: str | None = None) -> dict:
        """Create (or refresh) a Google Doc from markdown. Returns {doc_id, url, created}."""
        with tempfile.TemporaryDirectory() as tmp:
            md_path = Path(tmp) / "summary.md"
            md_path.write_text(md_text, encoding="utf-8")
            html_path = Path(tmp) / "summary.html"
            attempts: list[tuple[Path, str]] = [(md_path, "text/markdown")]
            html_path.write_text(markdown_to_html(md_text), encoding="utf-8")
            attempts.append((html_path, "text/html"))
            last_exc: Exception | None = None
            for path, mime in attempts:
                try:
                    if existing_doc_id:
                        meta = self.drive.backend.update_file(existing_doc_id, name=title, media_path=path, media_mime=mime)
                        return {"doc_id": existing_doc_id, "url": doc_url(existing_doc_id), "created": False, "format": mime, "name": meta.get("name", title)}
                    meta = self.drive.backend.upload_file(path, title, parent_id, mime, {"aiprimer": "summary"}, convert_to=GDOC_MIME)
                    doc_id = meta["id"]
                    if self.sharing == "anyone_with_link":
                        try:
                            self.drive.share_anyone_reader(doc_id)
                        except Exception:
                            pass
                    return {"doc_id": doc_id, "url": doc_url(doc_id), "created": True, "format": mime, "name": meta.get("name", title)}
                except Exception as exc:  # try the next format
                    last_exc = exc
            raise RuntimeError(f"could not publish Google Doc: {last_exc}")

    def unresolved_comments(self, doc_id: str) -> list[dict]:
        out = []
        for c in self.drive.backend.list_comments(doc_id):
            if c.get("resolved"):
                continue
            out.append(
                {
                    "comment_id": c.get("id"),
                    "quoted": (c.get("quotedFileContent") or {}).get("value", ""),
                    "comment": c.get("content", ""),
                    "author": (c.get("author") or {}).get("displayName", ""),
                    "created": c.get("createdTime", ""),
                    "replies": [r.get("content", "") for r in c.get("replies", [])],
                }
            )
        return out

    def resolve(self, doc_id: str, comment_id: str) -> None:
        self.drive.backend.resolve_comment(doc_id, comment_id)
