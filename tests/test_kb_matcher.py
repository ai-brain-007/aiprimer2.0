from pipeline.kb.matcher import build_index, find_candidates
from pipeline.models import Citation, ExtractedCitation, MatchItem, Unit, VerifiedUnit

KB_CFG = {
    "candidate_limit": 5,
    "auto_same_threshold": 95,
    "llm_review_threshold": 70,
    "description_partial_threshold": 80,
}

SLEEP_DESC = (
    "A fixed evening routine that dims the lights, cools the room and stops screens ninety minutes "
    "before bed so that falling asleep is automatic rather than effortful."
)


def unit(id_, name, details, description=None, aliases=None, type_="technique"):
    return Unit(
        id=id_,
        author_id="A-x",
        type=type_,
        name=name,
        aliases=aliases or [],
        description=description or f"{name}: what it is.",
        details=details,
        last_seen="2022-01-01",
        citations=[Citation(resource_id="R-0", location="p. 1", quote="x", verified=True, score=100)],
    )


UNITS = [
    unit("U-1", "Box breathing", "Inhale 4, hold 4, exhale 4, hold 4. Repeat for five minutes.", aliases=["Square breathing"]),
    unit("U-2", "Cold exposure", "Two minutes in cold water after training.", type_="drill"),
    unit("U-3", "Sleep hygiene", "Dim lights, cool room, no screens.", description=SLEEP_DESC, type_="procedure"),
]
UNITS_BY_ID = {u.id: u for u in UNITS}


def vunit(name, details, description=None, aliases=None):
    return VerifiedUnit(
        temp_id="R-9:R-9#00:0",
        type="technique",
        name=name,
        aliases=aliases or [],
        description=description or f"{name}: what it is.",
        details=details,
        citations=[ExtractedCitation(location="p. 2", quote="y")],
        verified_citations=[Citation(resource_id="R-9", location="p. 2", quote="y", verified=True, score=100)],
    )


def test_build_index_shape():
    idx = build_index(UNITS)
    assert [e["unit_id"] for e in idx] == ["U-1", "U-2", "U-3"]
    assert {"unit_id", "name", "aliases", "type", "stages", "description", "details", "last_seen"} <= set(idx[0])
    assert "square breathing" in idx[0]["names_norm"]


def test_auto_same_when_name_and_details_match():
    idx = build_index(UNITS)
    item = find_candidates(vunit("Box Breathing", "Inhale 4, hold 4, exhale 4, hold 4. Repeat for 5 minutes."), idx, UNITS_BY_ID, KB_CFG)
    assert isinstance(item, MatchItem)
    assert item.auto_decision == "SAME"
    assert item.candidates[0].unit_id == "U-1" and item.candidates[0].score >= 95
    assert "same name and details" in item.hint
    # matching through an alias also counts as the same name
    item = find_candidates(vunit("Square breathing", "Inhale 4, hold 4, exhale 4, hold 4. Repeat for five minutes."), idx, UNITS_BY_ID, KB_CFG)
    assert item.auto_decision == "SAME"


def test_auto_new_when_nothing_similar():
    idx = build_index(UNITS)
    item = find_candidates(vunit("Deadlift setup", "Bar over midfoot, hips high, brace."), idx, UNITS_BY_ID, KB_CFG)
    assert item.auto_decision == "NEW"
    assert all(c.score < 70 for c in item.candidates)
    assert find_candidates(vunit("Anything", "x"), [], {}, KB_CFG).auto_decision == "NEW"


def test_ambiguous_same_name_different_details():
    idx = build_index(UNITS)
    item = find_candidates(vunit("Box breathing", "Inhale 6, exhale 6, no holds, ten minutes lying down."), idx, UNITS_BY_ID, KB_CFG)
    assert item.auto_decision == ""
    assert "EVOLVED" in item.hint
    top = item.candidates[0]
    assert top.unit_id == "U-1"
    # candidates carry the target's text inline
    assert top.name == "Box breathing" and top.type == "technique"
    assert top.details.startswith("Inhale 4") and top.description and top.last_seen == "2022-01-01"
    assert item.temp_id == "R-9:R-9#00:0" and item.unit.name == "Box breathing"


def test_description_candidate_added():
    idx = build_index(UNITS)
    item = find_candidates(
        vunit("Wind-down routine", "Dim the lights and cool the room.", description=SLEEP_DESC[:110]),
        idx,
        UNITS_BY_ID,
        KB_CFG,
    )
    ids = [c.unit_id for c in item.candidates]
    assert "U-3" in ids
    u3 = next(c for c in item.candidates if c.unit_id == "U-3")
    assert 80 <= u3.score <= 89
    assert item.auto_decision == ""  # above review threshold, not an auto SAME


def test_candidate_limit_respected():
    many = [unit(f"U-{i}", f"Breathing pattern {i}", f"details {i}") for i in range(10)]
    idx = build_index(many)
    item = find_candidates(vunit("Breathing pattern", "details"), idx, {u.id: u for u in many}, {**KB_CFG, "candidate_limit": 3})
    assert len(item.candidates) == 3
