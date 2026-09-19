"""Extraction: raw resources -> normalized markdown text + metadata guess.

Public API:
    Extraction            result dataclass
    detect_kind(source)   'youtube' | 'youtube_channel' | 'youtube_playlist' | 'web' for URLs,
                          'pdf' | 'docx' | 'xlsx' | 'csv' | 'txt' | 'image' | 'video' | 'audio' for files
    extract_file(path, kind, settings, workdir)
    render_pdf_pages(path, pages, dpi, outdir)

System tools (tesseract, pdftoppm, pandoc, ffprobe) are optional; extractors degrade with a warning.
"""

from __future__ import annotations

import re
import zipfile
from pathlib import Path

from pipeline.config import Settings
from pipeline.ids import is_youtube_channel_url, is_youtube_playlist_url, youtube_video_id

from .base import Extraction, guess_language, normalize_text
from .docx import extract_docx
from .image import extract_image
from .pdf import extract_pdf, render_pdf_pages
from .tabular import extract_tabular
from .text import extract_text
from .video import extract_media
from .youtube import normalize_segments, transcript_extraction, transcript_to_markdown

__all__ = [
    "Extraction",
    "detect_kind",
    "extract_file",
    "render_pdf_pages",
    "extract_pdf",
    "extract_docx",
    "extract_tabular",
    "extract_text",
    "extract_image",
    "extract_media",
    "normalize_segments",
    "transcript_to_markdown",
    "transcript_extraction",
    "guess_language",
    "normalize_text",
]

_EXTENSION_KINDS: dict[str, str] = {
    ".pdf": "pdf",
    ".docx": "docx",
    ".xlsx": "xlsx",
    ".xlsm": "xlsx",
    ".csv": "csv",
    ".tsv": "csv",
    ".txt": "txt",
    ".md": "txt",
    ".markdown": "txt",
    ".png": "image",
    ".jpg": "image",
    ".jpeg": "image",
    ".webp": "image",
    ".gif": "image",
    ".mp4": "video",
    ".mov": "video",
    ".mkv": "video",
    ".webm": "video",
    ".avi": "video",
    ".mp3": "audio",
    ".m4a": "audio",
    ".wav": "audio",
    ".ogg": "audio",
    ".flac": "audio",
}
_URL_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*://")


def _is_url(source: str) -> bool:
    s = source.strip()
    return bool(_URL_RE.match(s)) or s.lower().startswith("www.")


def _sniff_magic(path: Path) -> str | None:
    """'pdf' / 'docx' / 'xlsx' from the first bytes (zip members), else None."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(8)
    except OSError:
        return None
    if head.startswith(b"%PDF"):
        return "pdf"
    if head.startswith(b"PK\x03\x04"):
        try:
            with zipfile.ZipFile(path) as zf:
                names = set(zf.namelist())
        except (zipfile.BadZipFile, OSError):
            return None
        if "word/document.xml" in names:
            return "docx"
        if "xl/workbook.xml" in names:
            return "xlsx"
    return None


def _decodes_as_text(path: Path, sample_bytes: int = 65536) -> bool:
    try:
        with open(path, "rb") as fh:
            sample = fh.read(sample_bytes)
    except OSError:
        return False
    if not sample:
        return True
    if b"\x00" in sample:
        return False
    try:
        sample.decode("utf-8")
        return True
    except UnicodeDecodeError:
        pass
    try:
        from charset_normalizer import from_bytes

        best = from_bytes(sample).best()
    except Exception:  # pragma: no cover - defensive
        return False
    return best is not None and bool(best.encoding)


def detect_kind(source: str) -> str:
    """Classify a URL or local path.

    URLs: 'youtube' (video), 'youtube_channel', 'youtube_playlist', else 'web'.
    Files: by extension, with light magic sniffing of the first bytes (PDF header, zip with
    Word/Excel members). Unknown extension -> 'txt' when the file decodes as text, else ValueError.
    """
    if not source or not str(source).strip():
        raise ValueError("empty source")
    source = str(source).strip()
    if _is_url(source):
        url = source if _URL_RE.match(source) else "https://" + source
        if youtube_video_id(url):
            return "youtube"
        if is_youtube_playlist_url(url):
            return "youtube_playlist"
        if is_youtube_channel_url(url):
            return "youtube_channel"
        return "web"

    path = Path(source).expanduser()
    magic = _sniff_magic(path) if path.is_file() else None
    if magic:
        return magic
    kind = _EXTENSION_KINDS.get(path.suffix.lower())
    if kind:
        return kind
    if path.is_file() and _decodes_as_text(path):
        return "txt"
    if not path.exists():
        raise ValueError(f"cannot determine the kind of {source!r}: unknown extension and file not found")
    raise ValueError(f"unsupported file type: {source!r}")


def extract_file(path: Path, kind: str, settings: Settings, workdir: Path) -> Extraction:
    """Dispatch to the per-type extractor. `workdir` receives rendered pages / CSV exports (created)."""
    path = Path(path)
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    if not path.is_file():
        raise FileNotFoundError(str(path))
    if kind == "pdf":
        return extract_pdf(path, settings, workdir)
    if kind == "docx":
        return extract_docx(path, settings, workdir)
    if kind in ("xlsx", "csv"):
        return extract_tabular(path, settings, workdir)
    if kind == "txt":
        return extract_text(path, settings, workdir)
    if kind == "image":
        return extract_image(path, settings, workdir)
    if kind in ("video", "audio"):
        return extract_media(path, settings, workdir, kind=kind)
    raise ValueError(f"no extractor for kind {kind!r}")
