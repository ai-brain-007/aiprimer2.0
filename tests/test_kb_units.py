from pathlib import Path

import yaml

from pipeline.kb.units import UnitStore, parse_unit_file, render_unit_file, unit_details_hash
from pipeline.models import Citation, Contradiction, ExtractedCitation, ExtractedUnit, Unit, UnitVersion


def make_unit(**overrides) -> Unit:
    data = dict(
        id="U-abc123",
        author_id="A-jane-doe",
        type="technique",
        name="Box breathing: the 4-4-4-4 pattern",
        aliases=["Square breathing", "4x4 breath"],
        stages=["T-stage1", "T-stage2"],
        status="active",
        first_seen="2019",
        last_seen="2023-05-01",
        citations=[
            Citation(resource_id="R-1", location="p. 12", quote='She said "breathe in for four"', verified=True, score=100),
            Citation(resource_id="R-2", location="12:30", quote="It's a 4-4-4-4 pattern", verified=True, score=None),
        ],
        versions=[
            UnitVersion(date="2019", resource_id="R-1", summary="first form"),
            UnitVersion(date="2023-05-01", resource_id="R-2", summary="second form", what_changed="hold added"),
        ],
        contradictions=[Contradiction(resource_id="R-3", claim="No hold is needed", conflicts_with="hold added")],
        created_at="2024-01-01T00:00:00Z",
        updated_at="2024-01-02T00:00:00Z",
        description="A breathing pattern: inhale, hold, exhale, hold.\n\nUsed to calm down.",
        details="Steps:\n\n1. Inhale 4 s\n2. Hold 4 s\n\n### Sub heading inside details\n\n- keep the shoulders down\n- `count` silently",
        notes="Works best seated.",
        earlier_versions="### 2019 (R-1)\n\nThe original three-part version.",
    )
    data.update(overrides)
    return Unit(**data)


def test_round_trip_is_lossless():
    unit = make_unit()
    text = render_unit_file(unit)
    assert text.startswith("---\n")
    assert "## Description" in text and "## Details" in text and "## Notes" in text and "## Earlier version" in text
    assert "description:" not in text.split("---")[1]  # body fields are not in the front matter
    parsed = parse_unit_file(text)
    assert parsed == unit
    assert parsed.first_seen == "2019"  # not coerced to an int/date


def test_round_trip_survives_rules_and_yaml_looking_text():
    unit = make_unit(
        details="Step A\n\n---\n\nStep B: yes\n\n- key: value\n- 2020-01-01 is a date",
        description="---\nstarts with a rule",
        notes="",
        earlier_versions="",
    )
    assert parse_unit_file(render_unit_file(unit)) == unit


def test_earlier_version_section_only_when_non_empty():
    unit = make_unit(earlier_versions="", notes="")
    text = render_unit_file(unit)
    assert "## Earlier version" not in text
    assert "## Notes" in text
    assert parse_unit_file(text) == unit


def test_store_save_get_load_delete(tmp_path: Path):
    store = UnitStore(tmp_path / "jane-doe")
    unit = make_unit(created_at="", updated_at="")
    path = store.save(unit)
    assert path == tmp_path / "jane-doe" / "units" / "U-abc123.md"
    assert path.is_file()
    assert unit.created_at and unit.updated_at
    loaded = store.get("U-abc123")
    assert loaded == unit
    assert store.get("U-missing") is None
    store.save(make_unit(id="U-def456", name="Cold exposure"))
    assert [u.id for u in store.load_all()] == ["U-abc123", "U-def456"]
    store.delete("U-abc123")
    assert store.get("U-abc123") is None
    assert [u.id for u in store.load_all()] == ["U-def456"]


def test_reject_writes_audit_file(tmp_path: Path):
    store = UnitStore(tmp_path / "jane-doe")
    item = ExtractedUnit(
        type="claim", name="Unsupported claim", description="Something", citations=[ExtractedCitation(quote="nope")]
    )
    path = store.reject(item, "no citation matched", temp_id="R-1:R-1#00:3")
    assert path.parent == tmp_path / "jane-doe" / "_rejected"
    assert path.suffix == ".md" and "R-1-R-1-00-3" in path.name
    text = path.read_text(encoding="utf-8")
    meta = yaml.safe_load(text.split("---")[1])
    assert meta["reason"] == "no citation matched"
    assert meta["temp_id"] == "R-1:R-1#00:3"
    assert meta["name"] == "Unsupported claim"
    assert "## Description" in text and "Something" in text
    # a dict item and a second reject in the same second get distinct files
    path2 = store.reject({"name": "Other", "description": "x"}, "bad", temp_id="R-1:R-1#00:3")
    assert path2 != path and path2.is_file()


def test_index_shape(tmp_path: Path):
    store = UnitStore(tmp_path / "jane-doe")
    assert store.index() == []
    store.save(make_unit())
    idx = store.index()
    assert len(idx) == 1
    assert set(idx[0]) == {"unit_id", "name", "aliases", "type", "stages", "description", "last_seen"}
    assert idx[0]["unit_id"] == "U-abc123" and idx[0]["aliases"] == ["Square breathing", "4x4 breath"]


def test_author_meta_round_trip(tmp_path: Path):
    store = UnitStore(tmp_path / "jane-doe")
    assert store.load_author_meta() == {}
    meta = {
        "author_id": "A-jane-doe",
        "canonical_name": "Jane Doe",
        "aliases": ["J. Doe"],
        "type": "person",
        "summary_doc_id": "",
        "summary_doc_url": "",
        "version": 3,
    }
    path = store.save_author_meta(meta)
    assert path == tmp_path / "jane-doe" / "author.yaml"
    assert store.load_author_meta() == meta


def test_unit_details_hash_ignores_formatting_noise():
    a = make_unit(details="Inhale **4**, hold 4,\nexhale 4.")
    b = make_unit(details="inhale 4, hold 4, exhale 4.")
    c = make_unit(details="inhale 5, hold 5, exhale 5.")
    assert unit_details_hash(a) == unit_details_hash(b)
    assert unit_details_hash(a) != unit_details_hash(c)
    assert unit_details_hash({"details": "Inhale 4, hold 4, exhale 4."}) == unit_details_hash(b)
