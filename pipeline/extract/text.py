"""Plain text / markdown files."""

from __future__ import annotations

import re
from pathlib import Path

from pipeline.config import Settings
from pipeline.models import MetadataGuess

from .base import Extraction, decode_bytes, guess_language, normalize_text, truncate

_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$", re.MULTILINE)
MAX_TITLE_CHARS = 120


def guess_title(text: str) -> tuple[str, str]:
    """(title, evidence): first markdown heading, else the first non-empty line, capped at 120 chars."""
    m = _HEADING.search(text)
    if m:
        return truncate(m.group(1).strip(), MAX_TITLE_CHARS), "title from first markdown heading"
    for line in text.splitlines():
        line = line.strip()
        if line:
            return truncate(line.strip("#*_> ").strip(), MAX_TITLE_CHARS), "title from first non-empty line"
    return "", ""


def extract_text(path: Path, settings: Settings, workdir: Path) -> Extraction:
    path = Path(path)
    raw = path.read_bytes()
    decoded, encoding, warnings = decode_bytes(raw)
    text = normalize_text(decoded)
    title, evidence = guess_title(text)
    evidence_lines = [evidence] if evidence else []
    confidence = 0.4 if evidence else 0.2
    if not title:
        title = path.stem
        evidence_lines.append("title from file name")
    if encoding.lower().replace("_", "-") not in {"utf-8", "utf8", "ascii"}:
        evidence_lines.append(f"decoded as {encoding}")
    language = guess_language(text)
    metadata = MetadataGuess(title=title, confidence=confidence, evidence=evidence_lines, language=language)
    if not text.strip():
        warnings.append("file is empty")
    return Extraction(
        text=text,
        metadata=metadata,
        source_type="txt",
        transcript_kind="text",
        language=language,
        warnings=warnings,
    )
