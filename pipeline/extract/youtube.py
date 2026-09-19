"""YouTube transcript normalisation: Apify actor output -> segments -> timestamped markdown.

The Apify API calls themselves live elsewhere; this module only shapes their results.
"""

from __future__ import annotations

import html
import re
import statistics
from typing import Any

from pipeline.models import MetadataGuess

from .base import Extraction, format_timestamp

_LIST_KEYS = ("transcript", "segments", "captions", "data", "items", "results", "subtitles", "cues")
_TEXT_KEYS = ("text", "content", "caption", "utf8", "snippet")
_START_KEYS = ("start", "offset", "startTime", "start_time", "from", "begin", "tStart", "startSeconds")
_START_MS_KEYS = ("startMs", "start_ms", "offsetMs", "offset_ms", "tStartMs", "startTimeMs", "startMilliseconds")
_DUR_KEYS = ("dur", "duration", "length")
_DUR_MS_KEYS = ("durationMs", "duration_ms", "durMs", "dDurationMs", "durationMilliseconds")
_END_KEYS = ("end", "endTime", "end_time", "to", "tEnd", "endSeconds")
_END_MS_KEYS = ("endMs", "end_ms", "tEndMs", "endTimeMs", "endMilliseconds")

_WS = re.compile(r"\s+")


def _num(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return None
        try:
            return float(value)
        except ValueError:
            m = re.match(r"^(\d+):(\d{2})(?::(\d{2}))?(?:[.,](\d+))?$", value)
            if m:  # "h:mm:ss" / "mm:ss" strings
                parts = [int(p) for p in m.groups()[:3] if p is not None]
                secs = 0
                for p in parts:
                    secs = secs * 60 + p
                frac = m.group(4)
                return secs + (float("0." + frac) if frac else 0.0)
    return None


def _first(d: dict, keys: tuple[str, ...]) -> Any:
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return None


def _text_of(item: dict) -> str:
    raw = _first(item, _TEXT_KEYS)
    if isinstance(raw, dict):  # youtube "snippet": {"text": ...} or {"runs": [{"text": ...}]}
        if isinstance(raw.get("runs"), list):
            raw = "".join(str(r.get("text", "")) for r in raw["runs"] if isinstance(r, dict))
        else:
            raw = raw.get("text", "")
    if raw is None:
        return ""
    text = html.unescape(str(raw))
    return _WS.sub(" ", text).strip()


def _unwrap(raw: Any) -> list[Any]:
    """Reduce the container shapes to a flat list of segment items (dicts or strings)."""
    if raw is None:
        return []
    if isinstance(raw, str):
        return [raw] if raw.strip() else []
    if isinstance(raw, dict):
        for key in _LIST_KEYS:
            if isinstance(raw.get(key), (list, str)):
                return _unwrap(raw[key])
        for value in raw.values():
            if isinstance(value, list) and value and all(isinstance(v, dict) for v in value):
                return _unwrap(value)
        return [raw]  # a single segment
    if isinstance(raw, list):
        out: list[Any] = []
        for item in raw:
            if isinstance(item, dict) and _first(item, _TEXT_KEYS) is None:
                nested = [item[k] for k in _LIST_KEYS if isinstance(item.get(k), (list, str))]
                if nested:
                    for n in nested:
                        out.extend(_unwrap(n))
                    continue
            out.append(item)
        return out
    return []


def normalize_segments(raw: list[dict] | dict | str) -> list[dict]:
    """Accept the transcript shapes Apify actors return and produce
    `[{"start": float seconds, "duration": float seconds, "text": str}]`, ordered by start.

    Handled keys: start/dur/text, start/duration/text, offset/duration/text (values in ms are
    recognised: explicit `*Ms` keys are always milliseconds; ambiguous integer values are treated
    as milliseconds when the durations or the maximum start exceed a plausible seconds count),
    startMs/endMs/text, and a plain string (one segment at 0). HTML entities are unescaped and
    whitespace collapsed. Empty segments are dropped.
    """
    items = _unwrap(raw)
    parsed: list[dict] = []
    for item in items:
        if isinstance(item, str):
            text = _WS.sub(" ", html.unescape(item)).strip()
            if text:
                parsed.append({"start": 0.0, "duration": None, "end": None, "text": text, "ms": True})
            continue
        if not isinstance(item, dict):
            continue
        text = _text_of(item)
        if not text:
            continue
        start = _num(_first(item, _START_MS_KEYS))
        explicit_ms = start is not None
        if start is not None:
            start /= 1000.0
        else:
            start = _num(_first(item, _START_KEYS))
        dur = _num(_first(item, _DUR_MS_KEYS))
        dur_ms = dur is not None
        if dur is not None:
            dur /= 1000.0
        else:
            dur = _num(_first(item, _DUR_KEYS))
        end = _num(_first(item, _END_MS_KEYS))
        end_ms = end is not None
        if end is not None:
            end /= 1000.0
        else:
            end = _num(_first(item, _END_KEYS))
        parsed.append(
            {
                "start": start,
                "duration": dur,
                "end": end,
                "text": text,
                # per-field flags: True when the value is already in seconds (explicit ms handled)
                "start_ok": explicit_ms or start is None,
                "dur_ok": dur_ms or dur is None,
                "end_ok": end_ms or end is None,
            }
        )
    if not parsed:
        return []

    # Decide whether the ambiguous (non-*Ms) numeric fields are milliseconds.
    amb_starts = [p["start"] for p in parsed if p["start"] is not None and not p.get("start_ok")]
    amb_durs = [p["duration"] for p in parsed if p["duration"] is not None and not p.get("dur_ok")]
    amb_ends = [p["end"] for p in parsed if p["end"] is not None and not p.get("end_ok")]
    treat_as_ms = _looks_like_ms(amb_starts, amb_durs, amb_ends)
    if treat_as_ms:
        for p in parsed:
            if p["start"] is not None and not p.get("start_ok"):
                p["start"] /= 1000.0
            if p["duration"] is not None and not p.get("dur_ok"):
                p["duration"] /= 1000.0
            if p["end"] is not None and not p.get("end_ok"):
                p["end"] /= 1000.0

    # Fill missing starts (sequentially), order by start, then fill missing durations
    # (from end, else from the next segment's start).
    last_start = 0.0
    for p in parsed:
        if p["start"] is None:
            p["start"] = last_start
        last_start = p["start"]
    parsed.sort(key=lambda p: p["start"])
    for i, p in enumerate(parsed):
        if p["duration"] is None:
            if p["end"] is not None and p["end"] >= p["start"]:
                p["duration"] = p["end"] - p["start"]
            elif i + 1 < len(parsed) and parsed[i + 1]["start"] is not None:
                p["duration"] = max(parsed[i + 1]["start"] - p["start"], 0.0)
            else:
                p["duration"] = 0.0
    return [
        {"start": round(float(p["start"]), 3), "duration": round(max(float(p["duration"]), 0.0), 3), "text": p["text"]}
        for p in parsed
    ]


def _looks_like_ms(starts: list[float], durs: list[float], ends: list[float]) -> bool:
    values = starts + durs + ends
    if not values:
        return False
    if any(abs(v - round(v)) > 1e-9 for v in values):
        return False  # fractional values are seconds (ms payloads are integers)
    nonzero_durs = [d for d in durs if d > 0]
    if nonzero_durs:
        return statistics.median(nonzero_durs) > 100  # a caption cue rarely lasts > 100 s
    if starts and ends and len(starts) == len(ends):
        gaps = [e - s for s, e in zip(starts, ends) if e > s]
        if gaps:
            return statistics.median(gaps) > 100
    return max(starts + ends, default=0.0) > 10000  # > ~2.8 h expressed in seconds is implausible


def transcript_to_markdown(segments: list[dict], marker_every: int = 60) -> str:
    """Paragraphs of joined segment text, each opened by a `[mm:ss]` (or `[h:mm:ss]` above one hour)
    marker, starting a new paragraph whenever a segment falls into a new ~`marker_every`-second window.
    """
    marker_every = max(int(marker_every or 60), 1)
    paragraphs: list[str] = []
    current: list[str] = []
    current_window = None
    current_start = 0.0
    for seg in segments:
        text = _WS.sub(" ", str(seg.get("text", ""))).strip()
        if not text:
            continue
        start = float(seg.get("start") or 0.0)
        window = int(start // marker_every)
        if current_window is None or window != current_window:
            if current:
                paragraphs.append(f"[{format_timestamp(current_start)}] " + " ".join(current))
            current = []
            current_window = window
            current_start = start
        current.append(text)
    if current:
        paragraphs.append(f"[{format_timestamp(current_start)}] " + " ".join(current))
    return "\n\n".join(paragraphs) + ("\n" if paragraphs else "")


def transcript_extraction(
    segments_raw: list[dict] | dict | str,
    marker_every: int,
    language: str = "",
    transcript_kind: str = "auto",
    duration_sec: int | None = None,
) -> Extraction:
    """Build an Extraction for a YouTube transcript. Title/author come from the metadata actor
    elsewhere, so the metadata guess here only carries language and duration."""
    segments = normalize_segments(segments_raw)
    text = transcript_to_markdown(segments, marker_every=marker_every)
    warnings: list[str] = []
    if duration_sec is None and segments:
        last = segments[-1]
        duration_sec = int(round(last["start"] + last["duration"]))
    kind = transcript_kind if segments else "none"
    if not segments:
        warnings.append("empty transcript")
    metadata = MetadataGuess(language=language or "", duration_sec=duration_sec, confidence=0.0)
    return Extraction(
        text=text,
        metadata=metadata,
        source_type="youtube",
        transcript_kind=kind,
        duration_sec=duration_sec,
        language=language or "",
        warnings=warnings,
    )
