"""Citation verification: every quote must really appear in the source text.

Matching is done on normalized text (lowercase, NFKC, straight quotes, markdown markers and
page/time markers removed, whitespace collapsed) so that punctuation, line breaks and
formatting noise in the extracted source do not reject an honest quote.
"""

from __future__ import annotations

import re
import unicodedata

from rapidfuzz import fuzz

from pipeline.models import Citation, ExtractionOutput, UNIT_TYPES, VerifiedUnit

# Above this many normalized characters the source is scanned in sliding windows.
LARGE_SOURCE_CHARS = 200_000
WINDOW_CHARS = 50_000
WINDOW_OVERLAP_CHARS = 1_000

_TIME_MARKER_RE = re.compile(r"\[(?:\d{1,2}:)?\d{1,2}:\d{2}\]")
_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_HYPHEN_BREAK_RE = re.compile(r"(\w)-\n[ \t]*(\w)")
_HEADING_RE = re.compile(r"^[ \t]*#{1,6}[ \t]+", re.MULTILINE)
_BLOCKQUOTE_RE = re.compile(r"^[ \t]*>[ \t]?", re.MULTILINE)
_BULLET_RE = re.compile(r"^[ \t]*(?:[-*+]|\d+[.)])[ \t]+", re.MULTILINE)
_EMPHASIS_RE = re.compile(r"[*_`~]+")
_WS_RE = re.compile(r"\s+")

_CHAR_MAP = str.maketrans(
    {
        "‘": "'",
        "’": "'",
        "‚": "'",
        "‛": "'",
        "′": "'",
        "‹": "'",
        "›": "'",
        "“": '"',
        "”": '"',
        "„": '"',
        "‟": '"',
        "″": '"',
        "«": '"',
        "»": '"',
        "‐": "-",
        "‑": "-",
        "‒": "-",
        "–": "-",
        "—": "-",
        "−": "-",
        " ": " ",
    }
)


def normalize_text(s: str) -> str:
    """Lowercase, NFKC, straight quotes, no markdown/page/time markers, single spaces."""
    if not s:
        return ""
    s = unicodedata.normalize("NFKC", s)
    s = s.translate(_CHAR_MAP)
    s = _HTML_COMMENT_RE.sub(" ", s)  # <!-- page N --> and other comments
    s = _TIME_MARKER_RE.sub(" ", s)  # [mm:ss] / [h:mm:ss]
    s = _HYPHEN_BREAK_RE.sub(r"\1\2", s)  # "infor-\nmation" -> "information"
    s = _HEADING_RE.sub("", s)
    s = _BLOCKQUOTE_RE.sub("", s)
    s = _BULLET_RE.sub("", s)
    s = _EMPHASIS_RE.sub("", s)
    s = s.lower()
    return _WS_RE.sub(" ", s).strip()


def _best_partial(needle: str, haystack: str) -> float:
    if len(haystack) <= LARGE_SOURCE_CHARS:
        return float(fuzz.partial_ratio(needle, haystack))
    best = 0.0
    step = WINDOW_CHARS - WINDOW_OVERLAP_CHARS
    start = 0
    while start < len(haystack):
        window = haystack[start : start + WINDOW_CHARS]
        score = float(fuzz.partial_ratio(needle, window))
        if score > best:
            best = score
            if best >= 100:
                break
        start += step
    return best


def _match_normalized(nq: str, ns: str, threshold: int) -> tuple[bool, int]:
    if not nq or not ns:
        return False, 0
    if nq in ns:
        return True, 100
    score = int(round(_best_partial(nq, ns)))
    return score >= threshold, score


def quote_in_source(quote: str, source: str, threshold: int) -> tuple[bool, int]:
    """Return (accepted, score 0-100). Exact normalized substring scores 100."""
    nq = normalize_text(quote)
    if not nq:
        return False, 0
    return _match_normalized(nq, normalize_text(source), threshold)


def _check_citation(quote: str, chunk_norm: str, full_norm: str, threshold: int) -> tuple[bool, int]:
    """Check an already-normalized chunk first, then the normalized full text; keep the best."""
    nq = normalize_text(quote)
    if not nq:
        return False, 0
    best_ok, best = False, 0
    if chunk_norm:
        best_ok, best = _match_normalized(nq, chunk_norm, threshold)
        if best_ok:
            return True, best
    if full_norm:
        ok, score = _match_normalized(nq, full_norm, threshold)
        if score > best:
            best_ok, best = ok, score
    return best_ok, best


def verify_outputs(
    outputs: list[ExtractionOutput],
    chunk_texts: dict[str, str],
    full_text: str,
    threshold: int,
) -> tuple[list[VerifiedUnit], list[dict]]:
    """Check every citation of every extracted unit.

    Each unit gets temp_id "<resource_id>:<chunk_id>:<i>". A citation is checked first against
    its own chunk text, then against the full source text. Units with no verified citation, a
    name shorter than 2 characters, an empty description or an unknown type are rejected.
    """
    verified: list[VerifiedUnit] = []
    rejects: list[dict] = []
    full_norm = normalize_text(full_text)
    chunk_norm_cache: dict[str, str] = {}
    for output in outputs:
        chunk_norm = ""
        if output.chunk_id:
            if output.chunk_id not in chunk_norm_cache:
                chunk_norm_cache[output.chunk_id] = normalize_text(chunk_texts.get(output.chunk_id, ""))
            chunk_norm = chunk_norm_cache[output.chunk_id]
        for i, eu in enumerate(output.units):
            temp_id = f"{output.resource_id}:{output.chunk_id}:{i}"
            name = (eu.name or "").strip()
            reason = ""
            if len(name) < 2:
                reason = "name shorter than 2 characters"
            elif not (eu.description or "").strip():
                reason = "empty description"
            elif eu.type not in UNIT_TYPES:
                reason = f"unknown unit type '{eu.type}'"
            if reason:
                rejects.append({"temp_id": temp_id, "name": name, "reason": reason, "best_score": 0})
                continue

            citations: list[Citation] = []
            best_score = 0
            for ec in eu.citations:
                ok, score = _check_citation(ec.quote, chunk_norm, full_norm, threshold)
                best_score = max(best_score, score)
                if ok:
                    citations.append(
                        Citation(
                            resource_id=output.resource_id,
                            location=ec.location or "",
                            quote=ec.quote,
                            verified=True,
                            score=score,
                        )
                    )
            if not citations:
                rejects.append(
                    {
                        "temp_id": temp_id,
                        "name": name,
                        "reason": f"no citation quote matched the source (best score {best_score}, threshold {threshold})",
                        "best_score": best_score,
                    }
                )
                continue
            verified.append(VerifiedUnit(**eu.model_dump(), temp_id=temp_id, verified_citations=citations))
    return verified, rejects


def _names_of(unit: VerifiedUnit) -> list[str]:
    names = [normalize_text(unit.name)] + [normalize_text(a) for a in unit.aliases]
    return [n for n in names if n]


def dedupe_within_resource(units: list[VerifiedUnit], name_threshold: int = 92) -> list[VerifiedUnit]:
    """Merge units of one resource whose names are near-identical.

    The first unit seen is kept; a merged duplicate contributes the longer details, its
    aliases (and its name, when different) and all distinct citations.
    """
    kept: list[VerifiedUnit] = []
    for unit in units:
        query = normalize_text(unit.name)
        target: VerifiedUnit | None = None
        if query:
            for k in kept:
                if any(fuzz.token_set_ratio(query, n) >= name_threshold for n in _names_of(k)):
                    target = k
                    break
        if target is None:
            kept.append(unit.model_copy(deep=True))
            continue

        if len(unit.details or "") > len(target.details or ""):
            target.details = unit.details
        if not (target.notes or "") and unit.notes:
            target.notes = unit.notes

        known = set(_names_of(target))
        for alias in [unit.name, *unit.aliases]:
            na = normalize_text(alias)
            if na and na not in known:
                target.aliases.append(alias)
                known.add(na)

        seen_raw = {(c.location, normalize_text(c.quote)) for c in target.citations}
        for c in unit.citations:
            key = (c.location, normalize_text(c.quote))
            if key not in seen_raw:
                target.citations.append(c)
                seen_raw.add(key)
        seen_ver = {(c.resource_id, c.location, normalize_text(c.quote)) for c in target.verified_citations}
        for c in unit.verified_citations:
            key = (c.resource_id, c.location, normalize_text(c.quote))
            if key not in seen_ver:
                target.verified_citations.append(c)
                seen_ver.add(key)
    return kept
