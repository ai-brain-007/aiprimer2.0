"""Video and audio files: probe duration/tags with ffprobe (optional); transcription happens elsewhere."""

from __future__ import annotations

import json
import re
from pathlib import Path

from pipeline.config import Settings
from pipeline.models import MetadataGuess

from .base import Extraction, run_tool, tool_path

MEDIA_STUB = "<!-- media: needs transcription -->\n"


def probe_media(path: Path) -> tuple[dict, list[str]]:
    """Return ffprobe's JSON (`format` + `streams`) for the file, or {} with a warning when unavailable."""
    ffprobe = tool_path("ffprobe")
    if not ffprobe:
        return {}, ["ffprobe not found; duration unknown"]
    proc = run_tool(
        [ffprobe, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
        timeout=120,
    )
    if proc is None or proc.returncode != 0:
        detail = (proc.stderr.decode("utf-8", "replace").strip() if proc is not None else "could not run").splitlines()
        return {}, [f"ffprobe failed: {detail[-1] if detail else 'unknown error'}"]
    try:
        return json.loads(proc.stdout.decode("utf-8", "replace") or "{}"), []
    except json.JSONDecodeError:
        return {}, ["ffprobe returned unreadable output"]


def _tag(info: dict, *names: str) -> str:
    tags = {str(k).lower(): v for k, v in (info.get("format", {}).get("tags") or {}).items()}
    for stream in info.get("streams") or []:
        for k, v in (stream.get("tags") or {}).items():
            tags.setdefault(str(k).lower(), v)
    for n in names:
        v = tags.get(n.lower())
        if v:
            return str(v).strip()
    return ""


def extract_media(path: Path, settings: Settings | None = None, workdir: Path | None = None, kind: str = "video") -> Extraction:
    path = Path(path)
    source_type = "audio" if kind == "audio" else "video"
    info, warnings = probe_media(path)
    duration: int | None = None
    try:
        raw = (info.get("format") or {}).get("duration")
        if raw is not None:
            duration = int(round(float(raw)))
    except (TypeError, ValueError):
        pass
    if duration is None and info:
        for stream in info.get("streams") or []:
            try:
                duration = int(round(float(stream["duration"])))
                break
            except (KeyError, TypeError, ValueError):
                continue

    evidence: list[str] = []
    title = _tag(info, "title")
    if title:
        evidence.append("title from media tags")
    else:
        title = path.stem
        evidence.append("title from file name")
    author = _tag(info, "artist", "album_artist", "author", "composer")
    if author:
        evidence.append("author from media tags")
    published, precision = "", "unknown"
    created = _tag(info, "creation_time", "date", "year")
    m = re.match(r"^(\d{4})(?:-(\d{2}))?(?:-(\d{2}))?", created) if created else None
    if m:
        if m.group(3):
            published, precision = f"{m.group(1)}-{m.group(2)}-{m.group(3)}", "day"
        elif m.group(2):
            published, precision = f"{m.group(1)}-{m.group(2)}", "month"
        else:
            published, precision = m.group(1), "year"
        evidence.append("date from media tags")
    confidence = min(0.2 + 0.15 * sum(bool(x) for x in (title != path.stem, author, published)), 0.6)
    metadata = MetadataGuess(
        title=title,
        author_raw=author,
        published_date=published,
        date_precision=precision,  # type: ignore[arg-type]
        confidence=confidence,
        evidence=evidence,
        duration_sec=duration,
    )
    warnings.append("needs transcription")
    return Extraction(
        text=MEDIA_STUB,
        metadata=metadata,
        source_type=source_type,
        transcript_kind="none",
        duration_sec=duration,
        warnings=warnings,
    )
