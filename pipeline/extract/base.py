"""Shared types and helpers of the extract package.

`Extraction` lives here (not in `__init__`) so the per-type modules can import it without a
circular import; `pipeline.extract` re-exports it.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from pipeline.models import MetadataGuess

log = logging.getLogger(__name__)


@dataclass
class Extraction:
    text: str  # normalized markdown of the whole resource
    metadata: MetadataGuess  # from pipeline.models
    source_type: str  # one of SourceType values
    transcript_kind: str = "text"  # text|manual|auto|ocr|vision|none
    pages: int | None = None
    duration_sec: int | None = None
    language: str = ""
    needs_vision: bool = False  # True when the agent must transcribe by eye (images, low-confidence OCR pages)
    vision_pages: list[int] = field(default_factory=list)  # 1-based page numbers needing vision
    vision_files: list[Path] = field(default_factory=list)  # rendered PNGs (or the image itself) for the agent to Read
    data_exports: list[Path] = field(default_factory=list)  # CSV exports for spreadsheets
    toc: list[dict] = field(default_factory=list)  # [{"level": int, "title": str, "page": int}]
    warnings: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- external tools


def tool_path(name: str) -> str | None:
    """Absolute path of an optional system tool, or None when it is not installed."""
    return shutil.which(name)


def run_tool(
    cmd: list[str], timeout: float = 600, cwd: Path | None = None
) -> subprocess.CompletedProcess[bytes] | None:
    """Run a system tool without raising. Returns None (and logs) when it could not be started or timed out."""
    try:
        return subprocess.run(cmd, capture_output=True, timeout=timeout, cwd=str(cwd) if cwd else None)
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.warning("command failed to run %s: %s", cmd[0], exc)
        return None


# --------------------------------------------------------------------------- text helpers

_TRAILING_WS = re.compile(r"[ \t]+$", re.MULTILINE)
_MANY_BLANK = re.compile(r"\n{3,}")


def normalize_text(text: str) -> str:
    """Unify newlines, strip trailing whitespace and collapse runs of blank lines. Ends with one newline."""
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")
    text = text.lstrip("﻿")
    text = _TRAILING_WS.sub("", text)
    text = _MANY_BLANK.sub("\n\n", text)
    text = text.strip("\n")
    return text + "\n" if text else ""


def safe_name(text: str, max_length: int = 60, default: str = "untitled") -> str:
    """File-system safe ASCII-ish name (used for CSV exports)."""
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", (text or "").strip()).strip("-._")
    cleaned = re.sub(r"-{2,}", "-", cleaned)
    return (cleaned[:max_length].strip("-._") or default)


def truncate(text: str, limit: int) -> str:
    text = text or ""
    if len(text) <= limit:
        return text
    return text[: max(limit - 1, 0)].rstrip() + "…"


def format_timestamp(seconds: float) -> str:
    """`mm:ss`, or `h:mm:ss` from one hour on."""
    total = max(int(seconds), 0)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


_WESTERN_CODECS = ("cp1252", "latin_1", "iso8859_15")


def detect_encoding(raw: bytes) -> str | None:
    """Encoding name for non-UTF-8 text bytes via charset-normalizer, or None when nothing fits.

    On small samples the detector cannot tell the single-byte Latin code pages apart and returns
    near-tied candidates; among those we prefer cp1252 / latin-1, by far the most common ones.
    """
    if not raw:
        return None
    try:
        from charset_normalizer import from_bytes

        matches = from_bytes(raw)
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("charset detection failed: %s", exc)
        return None
    best = matches.best()
    if best is None or not best.encoding:
        return None
    for match in matches:
        if match.chaos > best.chaos + 0.05 or match.coherence < best.coherence - 0.1:
            continue
        candidates = {match.encoding, *(match.could_be_from_charset or [])}
        for codec in _WESTERN_CODECS:
            if codec in candidates:
                return codec
    return best.encoding


def decode_bytes(raw: bytes) -> tuple[str, str, list[str]]:
    """Decode text bytes: strict UTF-8 first, then charset-normalizer, then UTF-8 with replacement.

    Returns (text, encoding_used, warnings).
    """
    warnings: list[str] = []
    if not raw:
        return "", "utf-8", warnings
    try:
        return raw.decode("utf-8"), "utf-8", warnings
    except UnicodeDecodeError:
        pass
    encoding = detect_encoding(raw)
    if encoding:
        try:
            return raw.decode(encoding), encoding, warnings
        except (UnicodeDecodeError, LookupError):
            pass
    warnings.append("could not detect text encoding; decoded as utf-8 with replacement characters")
    return raw.decode("utf-8", errors="replace"), "utf-8", warnings


# --------------------------------------------------------------------------- language heuristic

_STOPWORDS: dict[str, frozenset[str]] = {
    "en": frozenset("the and of to in is that it for with as was on are this be by at from or an have not".split()),
    "es": frozenset("el la de que y en los las del se un una por con para es no como más pero sus".split()),
    "fr": frozenset("le la les de des et en un une du que est pour dans qui pas sur au avec ce".split()),
    "de": frozenset("der die das und ist nicht ein eine den von zu mit sich auf für dem des auch im".split()),
    "it": frozenset("il la di che e le un una per non con del della sono gli nel come anche più".split()),
    "pt": frozenset("o a os as de que e do da em um uma para com não por se mais dos das".split()),
    "nl": frozenset("de het een en van is dat die niet op te zijn voor met ook maar aan er".split()),
}
_WORD_RE = re.compile(r"[^\W\d_]+", re.UNICODE)


def guess_language(text: str, default: str = "", min_words: int = 20, sample_chars: int = 200_000) -> str:
    """Very small stop-word based language guess over Latin-script text.

    Returns the best matching ISO-639-1 code, `default` when there is enough text but no clear
    signal, and "" when there is not enough text to say anything.
    """
    words = _WORD_RE.findall((text or "")[:sample_chars].lower())
    if len(words) < min_words:
        return ""
    scores = {lang: sum(1 for w in words if w in stops) for lang, stops in _STOPWORDS.items()}
    best_lang, best_score = max(scores.items(), key=lambda kv: kv[1])
    if best_score == 0 or best_score / len(words) < 0.02:
        return default
    return best_lang
