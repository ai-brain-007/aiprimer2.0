"""Find existing cards that a freshly extracted unit may duplicate, evolve or contradict.

Candidate generation is deterministic (rapidfuzz on normalized names, aliases and
descriptions). Clear cases are auto-decided (SAME / NEW); the rest go to a helper agent as
MatchItems whose candidates carry everything the helper needs inlined.
"""

from __future__ import annotations

from rapidfuzz import fuzz, process

from pipeline.kb.units import unit_details_hash
from pipeline.kb.verify import normalize_text
from pipeline.models import Candidate, MatchItem, Unit, VerifiedUnit

DEFAULTS = {
    "candidate_limit": 5,
    "auto_same_threshold": 95,
    "llm_review_threshold": 70,
    "description_partial_threshold": 80,
}
# Description-only candidates never outrank a strong name match.
DESCRIPTION_SCORE_CAP = 89
# Below this many normalized characters a description is too short for partial matching.
MIN_DESCRIPTION_CHARS = 20
DETAILS_RATIO_SAME = 85


def build_index(units: list[Unit]) -> list[dict]:
    """JSON-serialisable index of the store used by find_candidates."""
    index: list[dict] = []
    for u in units:
        names = [u.name, *u.aliases]
        index.append(
            {
                "unit_id": u.id,
                "name": u.name,
                "aliases": list(u.aliases),
                "type": u.type,
                "stages": list(u.stages),
                "description": u.description,
                "details": u.details,
                "last_seen": u.last_seen,
                "names_norm": sorted({n for n in (normalize_text(x) for x in names) if n}),
            }
        )
    return index


def _cfg(kb_cfg: dict, key: str) -> int:
    value = (kb_cfg or {}).get(key)
    return int(value) if value is not None else DEFAULTS[key]


def _entry_names(entry: dict) -> list[str]:
    if entry.get("names_norm"):
        return list(entry["names_norm"])
    names = [entry.get("name", ""), *(entry.get("aliases") or [])]
    return sorted({n for n in (normalize_text(x) for x in names) if n})


def find_candidates(vunit: VerifiedUnit, index: list[dict], units_by_id: dict[str, Unit], kb_cfg: dict) -> MatchItem:
    candidate_limit = _cfg(kb_cfg, "candidate_limit")
    auto_same = _cfg(kb_cfg, "auto_same_threshold")
    review_min = _cfg(kb_cfg, "llm_review_threshold")
    desc_min = _cfg(kb_cfg, "description_partial_threshold")

    queries = [n for n in (normalize_text(x) for x in [vunit.name, *vunit.aliases]) if n]
    query_name = normalize_text(vunit.name)

    # choices: every name and alias of every indexed unit
    choices: dict[int, str] = {}
    owner: dict[int, str] = {}
    entries_by_id: dict[str, dict] = {}
    for entry in index:
        entries_by_id[entry["unit_id"]] = entry
        for n in _entry_names(entry):
            key = len(choices)
            choices[key] = n
            owner[key] = entry["unit_id"]

    best: dict[str, int] = {}
    if choices:
        for q in queries:
            for _choice, score, key in process.extract(q, choices, scorer=fuzz.token_set_ratio, limit=None):
                uid = owner[key]
                s = int(round(score))
                if s > best.get(uid, -1):
                    best[uid] = s

    # description overlap
    nd = normalize_text(vunit.description)
    if len(nd) >= MIN_DESCRIPTION_CHARS:
        for entry in index:
            ed = normalize_text(entry.get("description") or "")
            if len(ed) < MIN_DESCRIPTION_CHARS:
                continue
            ratio = int(round(fuzz.partial_ratio(nd, ed)))
            if ratio >= desc_min:
                s = min(ratio, DESCRIPTION_SCORE_CAP)
                if s > best.get(entry["unit_id"], -1):
                    best[entry["unit_id"]] = s

    ranked = sorted(best.items(), key=lambda kv: (-kv[1], kv[0]))[: max(candidate_limit, 1)]
    candidates: list[Candidate] = []
    for uid, score in ranked:
        unit = units_by_id.get(uid)
        entry = entries_by_id.get(uid, {})
        candidates.append(
            Candidate(
                unit_id=uid,
                name=unit.name if unit else entry.get("name", ""),
                score=score,
                type=unit.type if unit else entry.get("type", ""),
                description=unit.description if unit else entry.get("description", ""),
                details=unit.details if unit else entry.get("details", ""),
                last_seen=unit.last_seen if unit else entry.get("last_seen", ""),
            )
        )

    auto_decision = ""
    hint = ""
    if candidates:
        top = candidates[0]
        top_unit = units_by_id.get(top.unit_id)
        top_names = set(_entry_names(entries_by_id.get(top.unit_id, {})))
        if top_unit is not None:
            top_names |= {n for n in (normalize_text(x) for x in [top_unit.name, *top_unit.aliases]) if n}
        same_name = bool(query_name) and query_name in top_names
        details_same = False
        if same_name:
            target_details = top_unit.details if top_unit else top.details
            if unit_details_hash(vunit) == unit_details_hash({"details": target_details}):
                details_same = True
            else:
                a, b = normalize_text(vunit.details), normalize_text(target_details)
                details_same = fuzz.ratio(a, b) >= DETAILS_RATIO_SAME
        if top.score >= auto_same and same_name and details_same:
            auto_decision = "SAME"
            hint = f"same name and details as {top.unit_id}"
        elif top.score < review_min:
            auto_decision = "NEW"
            hint = f"no existing card is similar enough (best {top.score} < {review_min})"
        elif same_name:
            hint = f"likely EVOLVED: same name as {top.unit_id}, details differ"
        else:
            hint = f"similar to {top.unit_id} '{top.name}' (score {top.score}); decide SAME, EVOLVED, CONTRADICTS or NEW"
    else:
        auto_decision = "NEW"
        hint = "no existing card is similar"

    return MatchItem(temp_id=vunit.temp_id, unit=vunit, candidates=candidates, auto_decision=auto_decision, hint=hint)
