"""PDF files: text layer with pymupdf, metadata guess, scanned detection and optional OCR."""

from __future__ import annotations

import csv
import io
import logging
import re
import statistics
import tempfile
from datetime import date
from pathlib import Path

import pymupdf

from pipeline.config import Settings
from pipeline.models import MetadataGuess

from .base import Extraction, guess_language, normalize_text, run_tool, tool_path, truncate

log = logging.getLogger(__name__)

VISION_FILE_CAP = 40  # rendered pages when no OCR tool is available
PAGE_MARKER = "<!-- page {n} -->"

_YEAR_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"First published (?:in )?(\d{4})", re.IGNORECASE), "first published line"),
    (re.compile(r"Published (?:in )?(\d{4})", re.IGNORECASE), "published line"),
    (re.compile(r"©\s*(\d{4})"), "copyright line"),
    (re.compile(r"Copyright\s*(?:©)?\s*(\d{4})", re.IGNORECASE), "copyright line"),
]
_ISBN_RE = re.compile(r"ISBN[-\s:]*([\d\- ]{10,17}X?)", re.IGNORECASE)
_BY_RE = re.compile(r"^\s*by\s+(.+?)\s*$", re.IGNORECASE | re.MULTILINE)
_JUNK_AUTHORS = {"unknown", "user", "admin", "administrator", "owner", "microsoft office user", "windows user", "author"}
_JUNK_TITLES = {"untitled", "document", "untitled document", "title", "microsoft word", "pdf"}


# --------------------------------------------------------------------------- page text


def page_block(n: int, text: str) -> str:
    body = normalize_text(text).rstrip("\n")
    return PAGE_MARKER.format(n=n) + ("\n" + body if body else "")


def join_pages(page_texts: list[str]) -> str:
    return "\n\n".join(page_block(i, t) for i, t in enumerate(page_texts, 1)) + ("\n" if page_texts else "")


def is_scanned(page_texts: list[str], sample_pages: int, threshold: int) -> bool:
    sample = [len(t.strip()) for t in page_texts[: max(int(sample_pages), 1)]]
    if not sample:
        return False
    return statistics.median(sample) < threshold


# --------------------------------------------------------------------------- metadata guess


def _looks_like_filename(text: str) -> bool:
    t = text.strip()
    if not t or t.lower() in _JUNK_TITLES:
        return True
    if re.search(r"\.(pdf|docx?|pptx?|tex|indd|ai|qxd|pages|odt|rtf|txt|md|htm|html|xlsx?)$", t, re.IGNORECASE):
        return True
    if re.match(r"^microsoft (word|powerpoint|excel)\b", t, re.IGNORECASE):
        return True
    if not re.search(r"\s", t) and ("_" in t or re.fullmatch(r"[\w.-]*\d[\w.-]*", t)):
        return True
    return False


def largest_font_line(page: pymupdf.Page) -> str:
    """Text of the largest-font line(s) on the page (consecutive lines sharing the max size are joined)."""
    lines: list[tuple[float, str]] = []
    try:
        info = page.get_text("dict")
    except Exception:  # pragma: no cover - defensive
        return ""
    for block in info.get("blocks", []):
        for line in block.get("lines", []):
            spans = line.get("spans", [])
            text = "".join(s.get("text", "") for s in spans).strip()
            if len(re.findall(r"[^\W\d_]", text)) < 3:
                continue
            size = max((float(s.get("size", 0)) for s in spans), default=0.0)
            lines.append((size, re.sub(r"\s+", " ", text)))
    if not lines:
        return ""
    max_size = max(size for size, _ in lines)
    start = next(i for i, (size, _) in enumerate(lines) if abs(size - max_size) < 0.5)
    parts: list[str] = []
    for size, text in lines[start:]:
        if abs(size - max_size) >= 0.5:
            break
        parts.append(text)
    return truncate(" ".join(parts).strip(), 200)


def guess_pdf_metadata(doc: pymupdf.Document, page_texts: list[str], path: Path) -> MetadataGuess:
    meta = doc.metadata or {}
    evidence: list[str] = []
    found = 0

    title = (meta.get("title") or "").strip()
    if title and not _looks_like_filename(title):
        evidence.append("title from PDF metadata")
        found += 1
    else:
        title = largest_font_line(doc[0]) if doc.page_count else ""
        if title:
            evidence.append("title from largest font line on p.1")
            found += 1
        else:
            title = path.stem
            evidence.append("title from file name")

    author = (meta.get("author") or "").strip()
    if author and author.lower() not in _JUNK_AUTHORS:
        evidence.append("author from PDF metadata")
        found += 1
    else:
        author = ""
        for i, text in enumerate(page_texts[:3], 1):
            m = _BY_RE.search(text)
            if m:
                author = re.sub(r"\s+", " ", m.group(1)).strip(" .,;:-")
                evidence.append(f"author from 'by' line p.{i}")
                found += 1
                break

    year = ""
    current_year = date.today().year
    for pattern, label in _YEAR_PATTERNS:
        for i, text in enumerate(page_texts[:5], 1):
            m = pattern.search(text)
            if m and 1400 <= int(m.group(1)) <= current_year + 1:
                year = m.group(1)
                evidence.append(f"year from {label} p.{i}")
                found += 1
                break
        if year:
            break

    for i, text in enumerate(page_texts[:5], 1):
        m = _ISBN_RE.search(text)
        if m:
            digits = re.sub(r"[^\dXx]", "", m.group(1)).upper()
            if len(digits) in (10, 13):
                evidence.append(f"isbn {digits} p.{i}")
                found += 1
                break

    confidence = round(min(0.3 + 0.5 * (found / 4), 0.8), 3)
    return MetadataGuess(
        title=title,
        author_raw=author,
        published_date=year,
        date_precision="year" if year else "unknown",
        confidence=confidence,
        evidence=evidence,
        pages=doc.page_count,
    )


# --------------------------------------------------------------------------- OCR


def render_pdf_pages(path: Path, pages: list[int], dpi: int, outdir: Path) -> list[Path]:
    """Render the given 1-based pages to PNG with pymupdf; returns paths named like page-0007.png."""
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    out: list[Path] = []
    with pymupdf.open(str(path)) as doc:
        for n in pages:
            if n < 1 or n > doc.page_count:
                continue
            target = outdir / f"page-{n:04d}.png"
            doc[n - 1].get_pixmap(dpi=int(dpi)).save(str(target))
            out.append(target)
    return out


def _tsv_mean_confidence(tsv_text: str) -> float:
    confs: list[float] = []
    reader = csv.reader(io.StringIO(tsv_text), delimiter="\t", quoting=csv.QUOTE_NONE)
    header = next(reader, None)
    if not header or "conf" not in header:
        return 0.0
    ci, ti = header.index("conf"), header.index("text") if "text" in header else -1
    for row in reader:
        if len(row) <= ci:
            continue
        try:
            conf = float(row[ci])
        except ValueError:
            continue
        if conf > -1 and (ti < 0 or len(row) <= ti or row[ti].strip()):
            confs.append(conf)
    return statistics.fmean(confs) if confs else 0.0


def ocr_page(pdf_path: Path, page_no: int, dpi: int, tmpdir: Path, pdftoppm: str, tesseract: str) -> tuple[str, float, str]:
    """OCR one page. Returns (text, mean_confidence, warning)."""
    page_dir = tmpdir / f"p{page_no}"
    page_dir.mkdir(parents=True, exist_ok=True)
    proc = run_tool(
        [pdftoppm, "-r", str(dpi), "-f", str(page_no), "-l", str(page_no), "-png", str(pdf_path), str(page_dir / "page")],
        timeout=300,
    )
    images = sorted(page_dir.glob("page*.png"))
    if proc is None or proc.returncode != 0 or not images:
        return "", 0.0, f"pdftoppm failed on page {page_no}"
    base = page_dir / "ocr"
    proc = run_tool([tesseract, str(images[0]), str(base), "--psm", "1", "txt", "tsv"], timeout=600)
    txt_file, tsv_file = base.with_suffix(".txt"), base.with_suffix(".tsv")
    if proc is None or proc.returncode != 0 or not txt_file.exists():
        return "", 0.0, f"tesseract failed on page {page_no}"
    text = txt_file.read_text(encoding="utf-8", errors="replace")
    conf = _tsv_mean_confidence(tsv_file.read_text(encoding="utf-8", errors="replace")) if tsv_file.exists() else 0.0
    return text, conf, ""


# --------------------------------------------------------------------------- entry point


def extract_pdf(path: Path, settings: Settings, workdir: Path) -> Extraction:
    path = Path(path)
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    cfg = settings.extract if settings is not None else {}
    threshold = int(cfg.get("scanned_median_chars_threshold", 200))
    sample_pages = int(cfg.get("scanned_sample_pages", 10))
    ocr_dpi = int(cfg.get("ocr_dpi", 300))
    ocr_low = float(cfg.get("ocr_low_confidence", 60))
    vision_dpi = int(cfg.get("vision_render_dpi", 150))
    warnings: list[str] = []

    try:
        doc = pymupdf.open(str(path))
    except Exception as exc:
        raise ValueError(f"cannot open PDF {path.name}: {exc}") from exc
    with doc:
        if doc.is_encrypted and not doc.authenticate(""):
            raise ValueError(f"PDF {path.name} is password protected")
        n_pages = doc.page_count
        page_texts = [page.get_text("text") for page in doc]
        toc = [{"level": int(lvl), "title": str(title).strip(), "page": int(pg)} for lvl, title, pg in (doc.get_toc() or [])]
        metadata = guess_pdf_metadata(doc, page_texts, path)
    if n_pages == 0:
        warnings.append("PDF has no pages")

    scanned = n_pages > 0 and is_scanned(page_texts, sample_pages, threshold)
    vision_pages: list[int] = []
    vision_files: list[Path] = []
    transcript_kind = "text"

    if scanned:
        pdftoppm, tesseract = tool_path("pdftoppm"), tool_path("tesseract")
        if pdftoppm and tesseract:
            transcript_kind = "ocr"
            ocr_texts: list[str] = []
            with tempfile.TemporaryDirectory(prefix="ocr-", dir=str(workdir)) as tmp:
                for n in range(1, n_pages + 1):
                    text, conf, warning = ocr_page(path, n, ocr_dpi, Path(tmp), pdftoppm, tesseract)
                    if warning:
                        warnings.append(warning)
                    ocr_texts.append(text)
                    if conf < ocr_low:
                        vision_pages.append(n)
            page_texts = ocr_texts
            if vision_pages:
                warnings.append(
                    f"{len(vision_pages)} page(s) below OCR confidence {ocr_low:g}: {_page_list(vision_pages)}"
                )
                vision_files = render_pdf_pages(path, vision_pages, vision_dpi, workdir / "vision")
        else:
            transcript_kind = "vision"
            missing = [t for t, p in (("pdftoppm", pdftoppm), ("tesseract", tesseract)) if not p]
            warnings.append(f"scanned PDF but {' and '.join(missing)} not installed; all pages need vision transcription")
            page_texts = []
            vision_pages = list(range(1, n_pages + 1))
            to_render = vision_pages[:VISION_FILE_CAP]
            if len(vision_pages) > VISION_FILE_CAP:
                warnings.append(
                    f"rendered only the first {VISION_FILE_CAP} of {len(vision_pages)} pages for vision transcription"
                )
            vision_files = render_pdf_pages(path, to_render, vision_dpi, workdir / "vision")

    text = join_pages(page_texts) if page_texts else ""
    language = guess_language("\n".join(page_texts[:20]), default="en") if page_texts else ""
    metadata.language = language
    metadata.pages = n_pages
    return Extraction(
        text=text,
        metadata=metadata,
        source_type="pdf",
        transcript_kind=transcript_kind,
        pages=n_pages,
        language=language,
        needs_vision=bool(vision_pages),
        vision_pages=vision_pages,
        vision_files=vision_files,
        toc=toc,
        warnings=warnings,
    )


def _page_list(pages: list[int], limit: int = 20) -> str:
    shown = ", ".join(str(p) for p in pages[:limit])
    return shown + (f", ... (+{len(pages) - limit})" if len(pages) > limit else "")
