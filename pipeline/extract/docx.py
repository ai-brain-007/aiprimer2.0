"""Word documents: pandoc (gfm) when installed, python-docx fallback."""

from __future__ import annotations

import re
from datetime import date, datetime
from pathlib import Path

from pipeline.config import Settings
from pipeline.models import MetadataGuess

from .base import Extraction, normalize_text, run_tool, tool_path

_HEADING_STYLE = re.compile(r"^heading\s*(\d)", re.IGNORECASE)


def _md_cell(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).replace("|", "\\|").strip()


def _table_to_markdown(table) -> str:
    rows: list[list[str]] = []
    for row in table.rows:
        cells = [_md_cell(c.text) for c in row.cells]
        # merged cells repeat the same cell object; collapse exact consecutive duplicates
        rows.append(cells)
    if not rows:
        return ""
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    header, body = rows[0], rows[1:]
    if not any(header):
        header = [f"col{i + 1}" for i in range(width)]
    lines = ["| " + " | ".join(header) + " |", "|" + "|".join([" --- "] * width) + "|"]
    lines += ["| " + " | ".join(r) + " |" for r in body]
    return "\n".join(lines)


def _paragraph_to_markdown(paragraph) -> str:
    text = paragraph.text.strip()
    if not text:
        return ""
    style = (paragraph.style.name if paragraph.style is not None else "") or ""
    m = _HEADING_STYLE.match(style)
    if m:
        level = min(max(int(m.group(1)), 1), 6)
        return "#" * level + " " + re.sub(r"\s+", " ", text)
    if style.lower() == "title":
        return "# " + re.sub(r"\s+", " ", text)
    if style.lower() == "subtitle":
        return "## " + re.sub(r"\s+", " ", text)
    if "list" in style.lower():
        if "number" in style.lower():
            return "1. " + text
        return "- " + text
    if "quote" in style.lower():
        return "> " + text
    return text


def docx_to_markdown_python(path: Path) -> str:
    """Fallback renderer with python-docx: headings, paragraphs and tables in document order."""
    import docx  # python-docx

    document = docx.Document(str(path))
    chunks: list[str] = []
    iterator = document.iter_inner_content() if hasattr(document, "iter_inner_content") else document.paragraphs
    for block in iterator:
        if block.__class__.__name__ == "Table":
            md = _table_to_markdown(block)
        else:
            md = _paragraph_to_markdown(block)
        if md:
            chunks.append(md)
    return "\n\n".join(chunks)


def docx_to_markdown_pandoc(path: Path) -> tuple[str | None, str]:
    """(markdown, warning): markdown is None when pandoc is missing or failed."""
    pandoc = tool_path("pandoc")
    if not pandoc:
        return None, "pandoc not found; used python-docx fallback"
    proc = run_tool([pandoc, str(path), "-f", "docx", "-t", "gfm", "--wrap=none"], timeout=600)
    if proc is None or proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", "replace").strip().splitlines() if proc is not None else ["could not run"]
        return None, f"pandoc failed ({detail[-1] if detail else 'unknown error'}); used python-docx fallback"
    return proc.stdout.decode("utf-8", "replace"), ""


def _core_properties(path: Path) -> tuple[dict, list[str]]:
    try:
        import docx

        props = docx.Document(str(path)).core_properties
    except Exception as exc:
        return {}, [f"could not read docx core properties: {exc}"]
    created = props.created
    created_iso = ""
    if isinstance(created, (datetime, date)):
        created_iso = created.date().isoformat() if isinstance(created, datetime) else created.isoformat()
    return {"title": (props.title or "").strip(), "author": (props.author or "").strip(), "created": created_iso}, []


def _first_heading(markdown: str) -> str:
    m = re.search(r"^#{1,6}\s+(.+?)\s*$", markdown, re.MULTILINE)
    return m.group(1).strip() if m else ""


def extract_docx(path: Path, settings: Settings, workdir: Path) -> Extraction:
    path = Path(path)
    warnings: list[str] = []
    markdown, warning = docx_to_markdown_pandoc(path)
    if markdown is None:
        warnings.append(warning)
        try:
            markdown = docx_to_markdown_python(path)
        except Exception as exc:
            raise ValueError(f"cannot read docx {path.name}: {exc}") from exc
    text = normalize_text(markdown)

    props, prop_warnings = _core_properties(path)
    warnings += prop_warnings
    evidence: list[str] = []
    found = 0
    title = props.get("title", "")
    if title:
        evidence.append("title from docx core properties")
        found += 1
    else:
        title = _first_heading(text)
        if title:
            evidence.append("title from first heading")
        else:
            title = path.stem
            evidence.append("title from file name")
    author = props.get("author", "")
    if author:
        evidence.append("author from docx core properties")
        found += 1
    published, precision = "", "unknown"
    if props.get("created"):
        published, precision = props["created"], "day"
        evidence.append("date from docx core properties (created)")
        found += 1
    confidence = min(0.3 + 0.15 * found, 0.75)
    metadata = MetadataGuess(
        title=title,
        author_raw=author,
        published_date=published,
        date_precision=precision,  # type: ignore[arg-type]
        confidence=confidence,
        evidence=evidence,
    )
    if not text.strip():
        warnings.append("document has no extractable text")
    return Extraction(
        text=text,
        metadata=metadata,
        source_type="docx",
        transcript_kind="text",
        language="",
        warnings=warnings,
    )
