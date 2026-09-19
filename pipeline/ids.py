"""Identifier helpers.

Rules (see CLAUDE.md):
- Taxonomy nodes, units and jobs get short random base32 IDs (created rarely, never renamed).
- Authors get slug-based IDs so files under knowledge/ are readable.
- Resources get DETERMINISTIC IDs derived from the resource itself, so re-ingesting the
  same thing yields the same ID and becomes an upsert instead of a duplicate.
"""

from __future__ import annotations

import base64
import hashlib
import re
import secrets
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from slugify import slugify as _slugify

_B32_ALPHABET = "abcdefghijklmnopqrstuvwxyz234567"


def slugify(text: str, max_length: int = 60) -> str:
    return _slugify(text or "", max_length=max_length, lowercase=True) or "untitled"


def random_b32(length: int = 6) -> str:
    return "".join(secrets.choice(_B32_ALPHABET) for _ in range(length))


def short_hash_b32(data: bytes | str, length: int = 10) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    digest = hashlib.sha256(data).digest()
    return base64.b32encode(digest).decode("ascii").lower().rstrip("=")[:length]


def sha256_file(path: str | Path, chunk_size: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(chunk_size), b""):
            h.update(chunk)
    return h.hexdigest()


def new_node_id() -> str:
    return f"T-{random_b32(6)}"


def new_unit_id() -> str:
    return f"U-{random_b32(6)}"


def new_job_id() -> str:
    return f"J-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}-{random_b32(4)}"


def author_id_for(name: str) -> str:
    return f"A-{slugify(name, max_length=40)}"


def resource_id_for_youtube(video_id: str) -> str:
    return f"R-YT-{video_id}"


def resource_id_for_sha256(sha256_hex: str) -> str:
    return f"R-F-{short_hash_b32(bytes.fromhex(sha256_hex), 10)}"


def resource_id_for_url(url: str) -> str:
    return f"R-URL-{short_hash_b32(canonical_url(url), 10)}"


_YT_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be", "music.youtube.com"}
_YT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")


def youtube_video_id(url: str) -> str | None:
    """Return the 11-character video id for any common YouTube URL form, else None."""
    try:
        parsed = urlparse(url.strip())
    except ValueError:
        return None
    host = (parsed.hostname or "").lower()
    if host not in _YT_HOSTS:
        return None
    path = parsed.path or ""
    candidate: str | None = None
    if host == "youtu.be":
        candidate = path.strip("/").split("/")[0]
    elif path == "/watch":
        candidate = parse_qs(parsed.query).get("v", [None])[0]
    else:
        m = re.match(r"^/(?:shorts|embed|live|v)/([A-Za-z0-9_-]{11})", path)
        if m:
            candidate = m.group(1)
    if candidate and _YT_ID_RE.match(candidate):
        return candidate
    return None


def is_youtube_channel_url(url: str) -> bool:
    try:
        parsed = urlparse(url.strip())
    except ValueError:
        return False
    host = (parsed.hostname or "").lower()
    if host not in _YT_HOSTS or host == "youtu.be":
        return False
    path = parsed.path or ""
    return bool(re.match(r"^/(@[^/]+|channel/[^/]+|c/[^/]+|user/[^/]+)(/videos|/shorts|/streams)?/?$", path))


def is_youtube_playlist_url(url: str) -> bool:
    try:
        parsed = urlparse(url.strip())
    except ValueError:
        return False
    return (parsed.hostname or "").lower() in _YT_HOSTS and parsed.path == "/playlist" and "list" in parse_qs(parsed.query)


def canonical_url(url: str) -> str:
    """Lower-case host, strip fragments and tracking parameters, normalise YouTube video URLs."""
    url = url.strip()
    vid = youtube_video_id(url)
    if vid:
        return f"https://www.youtube.com/watch?v={vid}"
    parsed = urlparse(url)
    query = "&".join(
        f"{k}={v}"
        for k, v in sorted((k, v) for k, vs in parse_qs(parsed.query, keep_blank_values=True).items() for v in vs)
        if not k.lower().startswith(("utm_", "fbclid", "gclid", "ref"))
    )
    path = parsed.path.rstrip("/") or "/"
    scheme = parsed.scheme or "https"
    return f"{scheme}://{(parsed.hostname or '').lower()}{path}" + (f"?{query}" if query else "")


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def today_iso() -> str:
    return datetime.now(timezone.utc).date().isoformat()
