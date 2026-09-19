"""Apply match decisions (NEW / SAME / EVOLVED / CONTRADICTS) to the unit store."""

from __future__ import annotations

from dataclasses import dataclass, field

from pipeline.ids import new_unit_id
from pipeline.kb.units import UnitStore
from pipeline.kb.verify import normalize_text
from pipeline.models import Citation, Contradiction, DecisionsFile, Unit, UnitVersion, VerifiedUnit

SUMMARY_CHARS = 200


@dataclass
class MergeReport:
    new: list[str] = field(default_factory=list)
    same: list[str] = field(default_factory=list)
    evolved: list[str] = field(default_factory=list)
    contradicted: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "new": list(self.new),
            "same": list(self.same),
            "evolved": list(self.evolved),
            "contradicted": list(self.contradicted),
            "skipped": list(self.skipped),
            "errors": list(self.errors),
        }


# --------------------------------------------------------------------------- helpers


def resource_date(resource: dict) -> str:
    """The resource date, or "" when its precision is unknown."""
    if (resource.get("date_precision") or "unknown") == "unknown":
        return ""
    return str(resource.get("published_date") or "")


def _summary(text: str) -> str:
    return (text or "").strip()[:SUMMARY_CHARS]


def _add_citations(target: Unit, citations: list[Citation]) -> int:
    seen = {(c.resource_id, normalize_text(c.quote)) for c in target.citations}
    added = 0
    for c in citations:
        key = (c.resource_id, normalize_text(c.quote))
        if key in seen:
            continue
        target.citations.append(c.model_copy())
        seen.add(key)
        added += 1
    return added


def _add_aliases(target: Unit, vu: VerifiedUnit) -> None:
    known = {n for n in (normalize_text(x) for x in [target.name, *target.aliases]) if n}
    for alias in [vu.name, *vu.aliases]:
        na = normalize_text(alias)
        if na and na not in known:
            target.aliases.append(alias.strip())
            known.add(na)


def _touch_dates(target: Unit, date: str) -> None:
    if not date:
        return
    if not target.first_seen or date < target.first_seen:
        target.first_seen = date
    if not target.last_seen or date > target.last_seen:
        target.last_seen = date


def _add_stages(target: Unit, stages: list[str]) -> None:
    for s in stages:
        if s and s not in target.stages:
            target.stages.append(s)


def _earlier_block(date: str, resource_id: str, description: str, details: str) -> str:
    lines = [f"### {date or 'undated'} ({resource_id})", ""]
    if description.strip():
        lines += [description.strip(), ""]
    if details.strip():
        lines += [details.strip(), ""]
    return "\n".join(lines).strip()


def _append_earlier(target: Unit, block: str) -> None:
    target.earlier_versions = f"{target.earlier_versions.strip()}\n\n{block}".strip() if target.earlier_versions.strip() else block


def _latest_version(target: Unit) -> UnitVersion | None:
    dated = [v for v in target.versions if v.date]
    if not dated:
        return None
    return max(dated, key=lambda v: v.date)


def _sort_versions(target: Unit) -> None:
    target.versions.sort(key=lambda v: v.date)


# --------------------------------------------------------------------------- decision handlers


def _apply_new(vu: VerifiedUnit, resource: dict, author_id: str, date: str, stages: list[str]) -> Unit:
    versions = []
    if date:
        versions.append(UnitVersion(date=date, resource_id=resource["resource_id"], summary=_summary(vu.description)))
    return Unit(
        id=new_unit_id(),
        author_id=author_id,
        type=vu.type,
        name=vu.name.strip(),
        aliases=[a.strip() for a in vu.aliases if a.strip()],
        stages=list(stages),
        status="active",
        first_seen=date,
        last_seen=date,
        citations=[c.model_copy() for c in vu.verified_citations],
        versions=versions,
        description=vu.description.strip(),
        details=(vu.details or "").strip(),
        notes=(vu.notes or "").strip(),
    )


def _apply_same(target: Unit, vu: VerifiedUnit, date: str, stages: list[str]) -> None:
    _add_citations(target, vu.verified_citations)
    _add_aliases(target, vu)
    _touch_dates(target, date)
    _add_stages(target, stages)
    if not target.details.strip() and (vu.details or "").strip():
        target.details = vu.details.strip()


def _apply_evolved(target: Unit, vu: VerifiedUnit, decision, resource: dict, date: str, stages: list[str]) -> None:
    latest = _latest_version(target)
    assert latest is not None and date
    rid = resource["resource_id"]
    what_changed = (decision.what_changed or decision.rationale or "").strip()
    if date >= latest.date:
        # newer teaching: archive the current body, install the new one
        _append_earlier(target, _earlier_block(latest.date, latest.resource_id, target.description, target.details))
        target.description = (decision.merged_description or vu.description).strip()
        target.details = (decision.merged_details or vu.details or "").strip()
        if (vu.notes or "").strip() and vu.notes.strip() not in target.notes:
            target.notes = f"{target.notes.strip()}\n\n{vu.notes.strip()}".strip()
        target.versions.append(
            UnitVersion(date=date, resource_id=rid, summary=_summary(target.description), what_changed=what_changed)
        )
    else:
        # older teaching surfaced later: record the version, keep the current body
        target.versions.append(
            UnitVersion(date=date, resource_id=rid, summary=_summary(vu.description), what_changed=what_changed)
        )
        _append_earlier(target, _earlier_block(date, rid, vu.description, vu.details or ""))
    _sort_versions(target)
    _add_citations(target, vu.verified_citations)
    _add_aliases(target, vu)
    _touch_dates(target, date)
    _add_stages(target, stages)


def _apply_contradicts(target: Unit, vu: VerifiedUnit, decision, resource: dict, date: str, stages: list[str]) -> None:
    target.contradictions.append(
        Contradiction(
            resource_id=resource["resource_id"],
            claim=(decision.conflicting_claim or vu.description).strip(),
            conflicts_with=_summary(target.description),
        )
    )
    target.status = "contradicted"
    _add_citations(target, vu.verified_citations)
    _touch_dates(target, date)
    _add_stages(target, stages)


# --------------------------------------------------------------------------- entry point


def apply_decisions(
    store: UnitStore,
    decisions: DecisionsFile,
    verified: dict[str, VerifiedUnit],
    resource: dict,
    author_id: str,
    default_stages: list[str] | None = None,
) -> MergeReport:
    report = MergeReport()
    date = resource_date(resource)
    stages = list(default_stages) if default_stages else ([resource["stage_id"]] if resource.get("stage_id") else [])
    resource = dict(resource)
    resource.setdefault("resource_id", decisions.resource_id)

    for d in decisions.decisions:
        vu = verified.get(d.temp_id)
        if vu is None:
            report.errors.append(f"{d.temp_id}: unknown temp_id")
            report.skipped.append(d.temp_id)
            continue
        try:
            if d.decision == "NEW":
                unit = _apply_new(vu, resource, author_id, date, stages)
                store.save(unit)
                report.new.append(unit.id)
                continue

            target = store.get(d.target_unit_id) if d.target_unit_id else None
            if target is None:
                report.errors.append(f"{d.temp_id}: {d.decision} target '{d.target_unit_id}' not found")
                report.skipped.append(d.temp_id)
                continue

            if d.decision == "SAME":
                _apply_same(target, vu, date, stages)
                store.save(target)
                report.same.append(target.id)
            elif d.decision == "EVOLVED":
                latest = _latest_version(target)
                if not date or latest is None:
                    _apply_same(target, vu, date, stages)
                    store.save(target)
                    report.same.append(target.id)
                    report.errors.append(f"{d.temp_id}: evolved requires dated sources; applied as SAME to {target.id}")
                else:
                    _apply_evolved(target, vu, d, resource, date, stages)
                    store.save(target)
                    report.evolved.append(target.id)
            elif d.decision == "CONTRADICTS":
                _apply_contradicts(target, vu, d, resource, date, stages)
                store.save(target)
                report.contradicted.append(target.id)
            else:
                report.errors.append(f"{d.temp_id}: unknown decision '{d.decision}'")
                report.skipped.append(d.temp_id)
        except Exception as exc:  # keep going: one bad decision must not lose the others
            report.errors.append(f"{d.temp_id}: {type(exc).__name__}: {exc}")
            report.skipped.append(d.temp_id)
    return report
