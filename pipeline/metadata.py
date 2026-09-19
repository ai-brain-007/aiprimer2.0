"""Title / author / date handling: date parsing, manual overrides and author resolution."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from rapidfuzz import fuzz

from .ids import author_id_for, now_iso
from .models import Author, MetadataGuess
from .registry import Registry

_MONTHS = {m.lower(): i for i, m in enumerate(["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"], start=1)}
_MONTHS.update({k[:3]: v for k, v in list(_MONTHS.items())})
_MONTHS["sept"] = 9


def parse_date(text: str | None, today: date | None = None) -> tuple[str, str]:
    """Return (iso_date, precision). Accepts ISO, 'Mar 3, 2021', '3 March 2021', '2021-03', '2021',
    'Premiered Mar 3, 2021', 'Streamed live on ...', relative '2 years ago' / '3 months ago' (lower precision)."""
    today = today or date.today()
    if text is None:
        return "", "unknown"
    s = str(text).strip()
    if not s:
        return "", "unknown"
    s = re.sub(r"^(premiered|streamed live on|published on|published|uploaded on|uploaded|first published(?: in)?|released)\s*[:\-]?\s*", "", s, flags=re.I)
    s = s.replace(" ", " ").strip()

    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        y, mo, d = map(int, m.groups())
        try:
            return date(y, mo, d).isoformat(), "day"
        except ValueError:
            return f"{y:04d}", "year"
    m = re.match(r"^(\d{4})-(\d{2})$", s)
    if m:
        return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}", "month"
    m = re.match(r"^(\d{4})$", s)
    if m:
        return m.group(1), "year"
    m = re.match(r"^(\d{1,2})[/.](\d{1,2})[/.](\d{4})$", s)  # d/m/yyyy (EU) is assumed
    if m:
        d, mo, y = map(int, m.groups())
        try:
            return date(y, mo, d).isoformat(), "day"
        except ValueError:
            pass
    m = re.match(r"^([A-Za-z]+)\.?\s+(\d{1,2}),?\s+(\d{4})$", s)  # Mar 3, 2021
    if m and m.group(1).lower() in _MONTHS:
        try:
            return date(int(m.group(3)), _MONTHS[m.group(1).lower()], int(m.group(2))).isoformat(), "day"
        except ValueError:
            pass
    m = re.match(r"^(\d{1,2})\s+([A-Za-z]+)\.?,?\s+(\d{4})$", s)  # 3 March 2021
    if m and m.group(2).lower() in _MONTHS:
        try:
            return date(int(m.group(3)), _MONTHS[m.group(2).lower()], int(m.group(1))).isoformat(), "day"
        except ValueError:
            pass
    m = re.match(r"^([A-Za-z]+)\.?,?\s+(\d{4})$", s)  # March 2021
    if m and m.group(1).lower() in _MONTHS:
        return f"{int(m.group(2)):04d}-{_MONTHS[m.group(1).lower()]:02d}", "month"
    m = re.match(r"^(\d+)\s+(second|minute|hour|day|week|month|year)s?\s+ago$", s, flags=re.I)
    if m:
        n, unit = int(m.group(1)), m.group(2).lower()
        if unit in ("second", "minute", "hour"):
            return today.isoformat(), "day"
        if unit == "day":
            return (today - timedelta(days=n)).isoformat(), "day" if n <= 7 else "month"
        if unit == "week":
            return (today - timedelta(weeks=n)).strftime("%Y-%m"), "month"
        if unit == "month":
            y, mo = today.year, today.month - n
            while mo <= 0:
                mo += 12
                y -= 1
            return f"{y:04d}-{mo:02d}", "month"
        return f"{today.year - n:04d}", "year"
    m = re.search(r"(19|20)\d{2}", s)
    if m:
        return m.group(0), "year"
    try:
        return datetime.fromisoformat(s).date().isoformat(), "day"
    except ValueError:
        return "", "unknown"


def apply_overrides(guess: MetadataGuess, title: str | None = None, author: str | None = None, date_text: str | None = None, date_precision: str | None = None) -> MetadataGuess:
    g = guess.model_copy()
    if title:
        g.title = title.strip()
        g.evidence.append("title set by user")
    if author:
        g.author_raw = author.strip()
        g.evidence.append("author set by user")
    if date_text:
        iso, precision = parse_date(date_text)
        if iso:
            g.published_date = iso
            g.date_precision = date_precision or precision  # type: ignore[assignment]
            g.evidence.append("date set by user")
    elif date_precision and g.published_date:
        g.date_precision = date_precision  # type: ignore[assignment]
    if title or author or date_text:
        g.confidence = max(g.confidence, 0.9)
    return g


@dataclass
class AuthorResolution:
    author_id: str
    canonical_name: str
    is_new: bool
    matched_by: str = ""                      # exact | alias | fuzzy | created
    near_matches: list[dict] = field(default_factory=list)


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9 ]", " ", (s or "").lower()).strip()


def resolve_author(registry: Registry, name: str, fuzzy_threshold: int = 88, near_threshold: int = 75) -> AuthorResolution:
    """Match a raw author/channel name against the Authors tab. Does not create anything."""
    name = (name or "").strip()
    authors = registry.authors()
    if not name:
        return AuthorResolution("", "", True, "", [])
    n = _norm(name)
    for a in authors:
        if _norm(a.canonical_name) == n:
            return AuthorResolution(a.author_id, a.canonical_name, False, "exact")
    for a in authors:
        if any(_norm(al) == n for al in a.aliases):
            return AuthorResolution(a.author_id, a.canonical_name, False, "alias")
    scored = []
    for a in authors:
        best = max([fuzz.token_set_ratio(n, _norm(a.canonical_name))] + [fuzz.token_set_ratio(n, _norm(al)) for al in a.aliases])
        if best >= near_threshold:
            scored.append({"author_id": a.author_id, "canonical_name": a.canonical_name, "score": int(best)})
    scored.sort(key=lambda d: -d["score"])
    if scored and scored[0]["score"] >= fuzzy_threshold:
        top = scored[0]
        return AuthorResolution(top["author_id"], top["canonical_name"], False, "fuzzy", scored[:3])
    return AuthorResolution(author_id_for(name), name, True, "", scored[:3])


def ensure_author(registry: Registry, name: str, author_type: str = "person", channel_url: str = "", alias_of: str | None = None) -> Author:
    """Get or create an author. `alias_of` = an existing author_id to which `name` is added as an alias."""
    name = name.strip()
    if alias_of:
        existing = registry.author(alias_of)
        if existing is None:
            raise KeyError(alias_of)
        if _norm(name) != _norm(existing.canonical_name) and all(_norm(a) != _norm(name) for a in existing.aliases):
            existing.aliases = [*existing.aliases, name]
        if channel_url and not existing.channel_url:
            existing.channel_url = channel_url
        registry.upsert_author(existing)
        return existing
    res = resolve_author(registry, name)
    if not res.is_new:
        author = registry.author(res.author_id)
        assert author is not None
        if res.matched_by == "fuzzy" and all(_norm(a) != _norm(name) for a in [author.canonical_name, *author.aliases]):
            author.aliases = [*author.aliases, name]
        if channel_url and not author.channel_url:
            author.channel_url = channel_url
        registry.upsert_author(author)
        return author
    author_id = res.author_id
    taken = {a.author_id for a in registry.authors()}
    base, n = author_id, 2
    while author_id in taken:
        author_id = f"{base}-{n}"
        n += 1
    author = Author(
        author_id=author_id,
        canonical_name=name,
        type=author_type,  # type: ignore[arg-type]
        channel_url=channel_url,
        resource_count=0,
        unit_count=0,
        kb_path=f"knowledge/{author_id[2:]}",
        notes=f"created {now_iso()}",
    )
    registry.upsert_author(author)
    return author
