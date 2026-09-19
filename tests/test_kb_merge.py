from pathlib import Path

import pytest

from pipeline.kb.merge import MergeReport, apply_decisions
from pipeline.kb.units import UnitStore
from pipeline.models import (
    Citation,
    Decision,
    DecisionsFile,
    ExtractedCitation,
    Unit,
    UnitVersion,
    VerifiedUnit,
)

AUTHOR = "A-jane-doe"


def resource(rid="R-2", date="2020-01-01", precision="day", stage="T-stage2"):
    return {
        "resource_id": rid,
        "title": f"Title {rid}",
        "published_date": date,
        "date_precision": precision,
        "stage_id": stage,
        "stage_path": "Body > Yogi > Stage",
        "source_type": "pdf",
        "source_url": "",
        "raw_file_url": "",
    }


def vunit(temp_id, name, description, details, rid="R-2", quotes=("quote one",), aliases=None, notes=""):
    return VerifiedUnit(
        temp_id=temp_id,
        type="technique",
        name=name,
        aliases=aliases or [],
        description=description,
        details=details,
        notes=notes,
        citations=[ExtractedCitation(location="p. 5", quote=q) for q in quotes],
        verified_citations=[Citation(resource_id=rid, location="p. 5", quote=q, verified=True, score=100) for q in quotes],
    )


def existing(store: UnitStore, version_date="2018-06-01", **overrides) -> Unit:
    data = dict(
        id="U-target",
        author_id=AUTHOR,
        type="technique",
        name="Box breathing",
        aliases=["Square breathing"],
        stages=["T-stage1"],
        first_seen=version_date,
        last_seen=version_date,
        citations=[Citation(resource_id="R-1", location="p. 1", quote="old quote", verified=True, score=100)],
        versions=[UnitVersion(date=version_date, resource_id="R-1", summary="Old description")] if version_date else [],
        description="Old description of box breathing.",
        details="Inhale 4, hold 4, exhale 4, hold 4.",
        notes="Old notes.",
    )
    data.update(overrides)
    unit = Unit(**data)
    store.save(unit)
    return unit


@pytest.fixture
def store(tmp_path: Path) -> UnitStore:
    return UnitStore(tmp_path / "jane-doe")


def decisions(*items, rid="R-2") -> DecisionsFile:
    return DecisionsFile(resource_id=rid, decisions=list(items))


def test_new_creates_unit(store):
    vu = vunit("t0", "Nasal breathing", "Breathe through the nose.", "Close the mouth, breathe slowly.", aliases=["Nose breathing"])
    report = apply_decisions(store, decisions(Decision(temp_id="t0", decision="NEW")), {"t0": vu}, resource(), AUTHOR)
    assert isinstance(report, MergeReport)
    assert len(report.new) == 1 and not report.errors
    unit = store.get(report.new[0])
    assert unit.id.startswith("U-") and unit.author_id == AUTHOR
    assert unit.name == "Nasal breathing" and unit.aliases == ["Nose breathing"]
    assert unit.description == "Breathe through the nose." and unit.details == "Close the mouth, breathe slowly."
    assert unit.citations == vu.verified_citations
    assert unit.stages == ["T-stage2"]
    assert unit.first_seen == "2020-01-01" and unit.last_seen == "2020-01-01"
    assert len(unit.versions) == 1
    assert unit.versions[0].date == "2020-01-01" and unit.versions[0].resource_id == "R-2"
    assert unit.versions[0].summary == "Breathe through the nose."
    assert unit.created_at and unit.updated_at and unit.status == "active"


def test_new_with_unknown_date_has_no_version_and_default_stages(store):
    vu = vunit("t0", "Nasal breathing", "Breathe through the nose.", "")
    report = apply_decisions(
        store, decisions(Decision(temp_id="t0", decision="NEW")), {"t0": vu},
        resource(date="", precision="unknown"), AUTHOR, default_stages=["T-given"],
    )
    unit = store.get(report.new[0])
    assert unit.versions == [] and unit.first_seen == "" and unit.last_seen == ""
    assert unit.stages == ["T-given"]


def test_same_adds_citation_alias_stage_and_dates(store):
    existing(store)
    vu = vunit("t0", "Box Breathing", "New wording.", "Inhale 4, hold 4, exhale 4, hold 4.", quotes=("old quote", "new quote"), aliases=["4x4 breath"])
    report = apply_decisions(store, decisions(Decision(temp_id="t0", decision="SAME", target_unit_id="U-target")), {"t0": vu}, resource(), AUTHOR)
    assert report.same == ["U-target"] and not report.errors
    unit = store.get("U-target")
    quotes = [(c.resource_id, c.quote) for c in unit.citations]
    assert quotes == [("R-1", "old quote"), ("R-2", "old quote"), ("R-2", "new quote")]
    # "Box Breathing" normalizes to the card's own name, so only the genuinely new alias is added
    assert unit.aliases == ["Square breathing", "4x4 breath"]
    assert unit.stages == ["T-stage1", "T-stage2"]
    assert unit.first_seen == "2018-06-01" and unit.last_seen == "2020-01-01"
    # body untouched
    assert unit.description == "Old description of box breathing."
    assert len(unit.versions) == 1
    # applying the same decision again does not duplicate anything
    apply_decisions(store, decisions(Decision(temp_id="t0", decision="SAME", target_unit_id="U-target")), {"t0": vu}, resource(), AUTHOR)
    unit = store.get("U-target")
    assert len(unit.citations) == 3 and len(unit.aliases) == 2


def test_evolved_newer_resource_updates_body(store):
    existing(store, version_date="2018-06-01")
    vu = vunit("t0", "Box breathing", "New description.", "Inhale 5, hold 5, exhale 5, hold 5.", quotes=("five count",))
    d = Decision(
        temp_id="t0", decision="EVOLVED", target_unit_id="U-target",
        what_changed="count raised from 4 to 5", merged_details="Merged: inhale 5, hold 5, exhale 5, hold 5 (was 4).",
    )
    report = apply_decisions(store, decisions(d), {"t0": vu}, resource(date="2020-01-01"), AUTHOR)
    assert report.evolved == ["U-target"] and not report.errors
    unit = store.get("U-target")
    assert unit.description == "New description."
    assert unit.details == "Merged: inhale 5, hold 5, exhale 5, hold 5 (was 4)."
    assert "Old description of box breathing." in unit.earlier_versions
    assert "Inhale 4, hold 4, exhale 4, hold 4." in unit.earlier_versions
    assert unit.earlier_versions.startswith("### 2018-06-01")
    assert [v.date for v in unit.versions] == ["2018-06-01", "2020-01-01"]
    assert unit.versions[-1].what_changed == "count raised from 4 to 5"
    assert unit.versions[-1].resource_id == "R-2"
    assert unit.last_seen == "2020-01-01" and unit.first_seen == "2018-06-01"
    assert any(c.quote == "five count" for c in unit.citations)


def test_evolved_older_resource_inserts_version_without_touching_body(store):
    existing(store, version_date="2020-01-01")
    vu = vunit("t0", "Box breathing", "Older description.", "Inhale 3, exhale 3.", rid="R-old", quotes=("three count",))
    d = Decision(temp_id="t0", decision="EVOLVED", target_unit_id="U-target", what_changed="early three-count form")
    report = apply_decisions(store, decisions(d, rid="R-old"), {"t0": vu}, resource(rid="R-old", date="2015"), AUTHOR)
    assert report.evolved == ["U-target"] and not report.errors
    unit = store.get("U-target")
    assert unit.description == "Old description of box breathing."
    assert unit.details == "Inhale 4, hold 4, exhale 4, hold 4."
    assert [v.date for v in unit.versions] == ["2015", "2020-01-01"]
    assert unit.versions[0].resource_id == "R-old" and unit.versions[0].what_changed == "early three-count form"
    assert "Older description." in unit.earlier_versions and "### 2015" in unit.earlier_versions
    assert unit.first_seen == "2015" and unit.last_seen == "2020-01-01"
    assert any(c.resource_id == "R-old" for c in unit.citations)


def test_evolved_without_dates_falls_back_to_same(store):
    existing(store, version_date="2018-06-01")
    vu = vunit("t0", "Box breathing", "Undated description.", "Different details.", quotes=("undated quote",))
    d = Decision(temp_id="t0", decision="EVOLVED", target_unit_id="U-target", what_changed="x")
    report = apply_decisions(store, decisions(d), {"t0": vu}, resource(date="", precision="unknown"), AUTHOR)
    assert report.evolved == [] and report.same == ["U-target"]
    assert any("evolved requires dated sources" in e for e in report.errors)
    unit = store.get("U-target")
    assert unit.description == "Old description of box breathing."
    assert unit.details == "Inhale 4, hold 4, exhale 4, hold 4."
    assert len(unit.versions) == 1 and unit.earlier_versions == ""
    assert any(c.quote == "undated quote" for c in unit.citations)
    # target without any dated version: also SAME
    existing(store, version_date="")
    report = apply_decisions(store, decisions(d), {"t0": vu}, resource(date="2021-01-01"), AUTHOR)
    assert report.same == ["U-target"] and report.errors


def test_contradicts_flags_unit(store):
    existing(store)
    vu = vunit("t0", "Box breathing", "Holds are harmful.", "Never hold the breath.", quotes=("never hold",))
    d = Decision(temp_id="t0", decision="CONTRADICTS", target_unit_id="U-target", conflicting_claim="Breath holds should be avoided.")
    report = apply_decisions(store, decisions(d), {"t0": vu}, resource(), AUTHOR)
    assert report.contradicted == ["U-target"] and not report.errors
    unit = store.get("U-target")
    assert unit.status == "contradicted"
    assert len(unit.contradictions) == 1
    c = unit.contradictions[0]
    assert c.resource_id == "R-2" and c.claim == "Breath holds should be avoided."
    assert c.conflicts_with == "Old description of box breathing."
    assert unit.description == "Old description of box breathing." and unit.details == "Inhale 4, hold 4, exhale 4, hold 4."
    assert any(c.quote == "never hold" for c in unit.citations)
    assert len(unit.versions) == 1


def test_errors_for_unknown_temp_id_and_missing_target(store):
    existing(store)
    vu = vunit("t0", "Box breathing", "d", "x")
    report = apply_decisions(
        store,
        decisions(
            Decision(temp_id="nope", decision="NEW"),
            Decision(temp_id="t0", decision="SAME", target_unit_id="U-missing"),
            Decision(temp_id="t0", decision="EVOLVED", target_unit_id=""),
        ),
        {"t0": vu},
        resource(),
        AUTHOR,
    )
    assert not report.new and not report.same and not report.evolved
    assert len(report.errors) == 3 and len(report.skipped) == 3
    assert any("unknown temp_id" in e for e in report.errors)
    assert any("U-missing" in e for e in report.errors)
    assert [u.id for u in store.load_all()] == ["U-target"]
