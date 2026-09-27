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


def _has_video_stream(info: dict) -> bool:
    return any((s.get("codec_type") == "video") for s in (info.get("streams") or []))


def derive_audio_and_frames(path: Path, workdir: Path, duration: int | None, settings: Settings | None = None) -> tuple[list[tuple[str, Path, str]], list[str]]:
    """Free-tier saver for video files: the mono audio track (AAC) and a few evenly spaced key frames (JPEG).
    A two-hour video shrinks from gigabytes to roughly 100 MB while keeping everything the knowledge layer needs
    (speech for the transcript, pictures for the pages). Returns ([(role, path, mime)], warnings)."""
    ffmpeg = tool_path("ffmpeg")
    if not ffmpeg:
        return [], ["ffmpeg not found; the full video will be stored"]
    cfg = (settings.extract if settings else {}) or {}
    kbps = int(cfg.get("video_audio_kbps", 48))
    n_frames = int(cfg.get("video_key_frames", 6))
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    out: list[tuple[str, Path, str]] = []
    warnings: list[str] = []
    audio = workdir / f"{path.stem}.audio.m4a"
    proc = run_tool([ffmpeg, "-y", "-v", "error", "-i", str(path), "-vn", "-ac", "1", "-ar", "22050", "-c:a", "aac", "-b:a", f"{kbps}k", str(audio)], timeout=3600)
    if proc is not None and proc.returncode == 0 and audio.exists() and audio.stat().st_size > 0:
        out.append(("audio", audio, "audio/mp4"))
    else:
        detail = (proc.stderr.decode("utf-8", "replace").strip().splitlines() or ["unknown error"])[-1] if proc is not None else "could not run"
        warnings.append(f"audio extraction failed ({detail}); the full video will be stored")
        return [], warnings
    total = float(duration or 0)
    times = [total * (i + 0.5) / n_frames for i in range(n_frames)] if total > 0 and n_frames > 0 else [1.0]
    for i, t in enumerate(times, start=1):
        frame = workdir / f"{path.stem}.frame-{i:02d}.jpg"
        proc = run_tool([ffmpeg, "-y", "-v", "error", "-ss", f"{t:.2f}", "-i", str(path), "-frames:v", "1", "-vf", "scale=640:-2", "-q:v", "4", str(frame)], timeout=300)
        if proc is not None and proc.returncode == 0 and frame.exists() and frame.stat().st_size > 0:
            out.append((f"frame:{i:02d}", frame, "image/jpeg"))
    if len(out) == 1:
        warnings.append("no key frame could be extracted")
    return out, warnings


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
    derived: list[tuple[str, Path, str]] = []
    store = str(((settings.extract if settings else {}) or {}).get("video_store", "audio_frames"))
    if kind == "video" and workdir is not None and store == "audio_frames" and _has_video_stream(info):
        derived, more = derive_audio_and_frames(path, Path(workdir) / "derived", duration, settings)
        warnings.extend(more)
    return Extraction(
        text=MEDIA_STUB,
        metadata=metadata,
        source_type=source_type,
        transcript_kind="none",
        duration_sec=duration,
        warnings=warnings,
        derived_files=derived,
    )
