"""Duplicate detection: deterministic natural keys plus a soft title warning."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from rapidfuzz import fuzz

from .ids import canonical_url, resource_id_for_sha256, resource_id_for_url, resource_id_for_youtube, sha256_file, youtube_video_id
from .models import Resource
from .registry import Registry


@dataclass
class NaturalKey:
    kind: str          # youtube | file | url
    key: str           # video id, sha256 hex, canonical url
    resource_id: str
    natural_key: str   # what is stored in Resources.natural_key


def natural_key_for_url(url: str) -> NaturalKey:
    vid = youtube_video_id(url)
    if vid:
        return NaturalKey("youtube", vid, resource_id_for_youtube(vid), f"yt:{vid}")
    cu = canonical_url(url)
    return NaturalKey("url", cu, resource_id_for_url(cu), f"url:{cu}")


def natural_key_for_file(path: Path) -> NaturalKey:
    digest = sha256_file(path)
    return NaturalKey("file", digest, resource_id_for_sha256(digest), f"sha256:{digest}")


def find_existing(registry: Registry, nk: NaturalKey) -> Resource | None:
    existing = registry.resource(nk.resource_id)
    if existing:
        return existing
    return registry.find_resource_by_natural_key(nk.natural_key)


def near_duplicates(registry: Registry, title: str, author_id: str = "", threshold: int = 90, limit: int = 3) -> list[dict]:
    """Soft warning only: same author (when known) and very similar title."""
    if not title:
        return []
    out = []
    for r in registry.resources():
        if author_id and r.author_id and r.author_id != author_id:
            continue
        score = fuzz.token_set_ratio(title.lower(), r.title.lower())
        if score >= threshold:
            out.append({"resource_id": r.resource_id, "title": r.title, "score": int(score), "stage_path": r.stage_path})
    out.sort(key=lambda d: -d["score"])
    return out[:limit]
