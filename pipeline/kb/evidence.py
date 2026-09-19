"""Evidence pack for the independent reviewer: what changed, and the source text behind it."""

from __future__ import annotations

import difflib

from rapidfuzz import fuzz

from pipeline.kb.units import UnitStore
from pipeline.kb.verify import normalize_text
from pipeline.models import Citation, Unit

LOCATE_MIN_SCORE = 60


def locate_quote(quote: str, source: str, context_chars: int = 300) -> dict | None:
    """Find the quote in the (normalized) source; return before/match/after context or None."""
    nq = normalize_text(quote)
    ns = normalize_text(source)
    if not nq or not ns:
        return None
    start = ns.find(nq)
    if start >= 0:
        end = start + len(nq)
        score = 100
    else:
        alignment = fuzz.partial_ratio_alignment(nq, ns)
        if alignment is None or alignment.score < LOCATE_MIN_SCORE:
            return None
        start, end, score = alignment.dest_start, alignment.dest_end, int(round(alignment.score))
    return {
        "before": ns[max(0, start - context_chars) : start],
        "match": ns[start:end],
        "after": ns[end : end + context_chars],
        "score": score,
    }


def _diff(label: str, before: str, after: str) -> list[str]:
    before, after = (before or "").strip(), (after or "").strip()
    if before == after:
        return [f"{label}: unchanged", ""]
    if not before:
        return [f"{label} (new):", "", after or "(empty)", ""]
    diff = difflib.unified_diff(
        before.splitlines(), after.splitlines(), fromfile=f"{label} before", tofile=f"{label} after", lineterm="", n=1
    )
    return [f"{label} diff:", "", "```diff", *diff, "```", ""]


def _citation_key(c: Citation) -> tuple[str, str]:
    return c.resource_id, normalize_text(c.quote)


def build_evidence(
    store: UnitStore,
    changed_unit_ids: list[str],
    before: dict[str, Unit | None],
    resources_text: dict[str, str],
    context_chars: int = 300,
) -> str:
    lines: list[str] = ["# Evidence for review", ""]
    lines.append(f"Units changed in this run: {len(changed_unit_ids)}. For each one: the card's new text, what changed, and the source text around every citation that was added.")
    lines.append("")
    for unit_id in changed_unit_ids:
        after = store.get(unit_id)
        prev = before.get(unit_id)
        if after is None:
            lines += [f"### {unit_id} (DELETED)", "", "The card no longer exists in the store.", ""]
            continue
        label = "NEW" if prev is None else "CHANGED"
        lines += [f"### {unit_id} {after.name} ({label})", ""]
        lines.append(f"Type: {after.type} | status: {after.status} | stages: {', '.join(after.stages) or 'none'}")
        if after.aliases:
            lines.append(f"Aliases: {', '.join(after.aliases)}")
        lines.append("")
        if prev is None:
            lines += ["Description:", "", after.description.strip() or "(empty)", ""]
            lines += ["Details:", "", after.details.strip() or "(empty)", ""]
        else:
            lines += _diff("Description", prev.description, after.description)
            lines += _diff("Details", prev.details, after.details)
            if prev.status != after.status:
                lines += [f"Status: {prev.status} -> {after.status}", ""]
            new_versions = [v for v in after.versions if v not in prev.versions]
            for v in new_versions:
                lines.append(f"Version added: {v.date or 'undated'} ({v.resource_id}) {v.what_changed or v.summary}".rstrip())
            new_contra = [c for c in after.contradictions if c not in prev.contradictions]
            for c in new_contra:
                lines.append(f"Contradiction added ({c.resource_id}): {c.claim} | conflicts with: {c.conflicts_with}")
            if new_versions or new_contra:
                lines.append("")

        known = {_citation_key(c) for c in (prev.citations if prev else [])}
        added = [c for c in after.citations if _citation_key(c) not in known]
        lines.append(f"Citations added: {len(added)}")
        lines.append("")
        for c in added:
            where = f"{c.resource_id} ({c.location})" if c.location else c.resource_id
            lines.append(f"- {where}, score {c.score if c.score is not None else 'n/a'}")
            lines.append(f"  > {c.quote.strip()}")
            source = resources_text.get(c.resource_id)
            if source is None:
                lines.append("  Context: source text not available")
            else:
                found = locate_quote(c.quote, source, context_chars)
                if found is None:
                    lines.append("  Context: quote not located")
                else:
                    lines.append(f"  Context (normalized text, match score {found['score']}):")
                    lines.append(f"  ...{found['before']} [[{found['match']}]] {found['after']}...")
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"
