from __future__ import annotations

from datetime import date

from pipeline.dedup import find_existing, natural_key_for_file, natural_key_for_url, near_duplicates
from pipeline.metadata import apply_overrides, ensure_author, parse_date, resolve_author
from pipeline.models import MetadataGuess, Resource
from pipeline.naming import drive_filename, extracted_filename, read_extracted_markdown, write_extracted_markdown
from pipeline.registry import Registry
from pipeline.sheets import SheetsRepo


def make_registry(fake_sheets, settings):
    repo = SheetsRepo(fake_sheets)
    repo.ensure_tabs()
    return Registry(repo, settings)


def test_filenames():
    r = Resource(resource_id="R-YT-abc", title='Footwork: the "shuffle"/pivot basics', published_date="2021-06-10", date_precision="day", author_raw="Teddy Atlas")
    assert drive_filename(r, "pdf") == "2021-06-10 - Teddy Atlas - Footwork the shuffle pivot basics [R-YT-abc].pdf"
    r2 = Resource(resource_id="R-F-x", title="x" * 200)
    name = drive_filename(r2, ".md", author_name="Someone")
    assert name.startswith("undated - Someone - ") and name.endswith(" [R-F-x].md") and len(name) < 140
    assert extracted_filename(r).endswith("[R-YT-abc].extracted.md")


def test_extracted_markdown_roundtrip(tmp_path):
    r = Resource(resource_id="R-1", title="T", source_type="pdf", pages=3)
    p = write_extracted_markdown(tmp_path / "x.extracted.md", r, {"toc": [{"level": 1, "title": "A", "page": 1}], "empty": ""}, "<!-- page 1 -->\nHello")
    meta, text = read_extracted_markdown(p)
    assert meta["resource_id"] == "R-1" and meta["toc"][0]["title"] == "A" and "empty" not in meta
    assert text.startswith("<!-- page 1 -->")


def test_natural_keys(tmp_path):
    nk = natural_key_for_url("https://youtu.be/dQw4w9WgXcQ?t=3")
    assert nk.kind == "youtube" and nk.resource_id == "R-YT-dQw4w9WgXcQ" and nk.natural_key == "yt:dQw4w9WgXcQ"
    nk2 = natural_key_for_url("https://Example.com/a/b/?utm_source=x&z=1#frag")
    assert nk2.kind == "url" and nk2.key == "https://example.com/a/b?z=1"
    f = tmp_path / "f.bin"
    f.write_bytes(b"abc")
    nk3 = natural_key_for_file(f)
    assert nk3.resource_id.startswith("R-F-") and nk3.natural_key.startswith("sha256:")
    assert natural_key_for_file(f).resource_id == nk3.resource_id


def test_find_existing_and_near_duplicates(fake_sheets, settings):
    reg = make_registry(fake_sheets, settings)
    reg.upsert_resource(Resource(resource_id="R-YT-abcdefghijk", title="Boxing footwork basics", natural_key="yt:abcdefghijk", author_id="A-t"))
    nk = natural_key_for_url("https://www.youtube.com/watch?v=abcdefghijk")
    assert find_existing(reg, nk).resource_id == "R-YT-abcdefghijk"
    assert find_existing(reg, natural_key_for_url("https://www.youtube.com/watch?v=zzzzzzzzzzz")) is None
    near = near_duplicates(reg, "Boxing Footwork Basics (full)", "A-t")
    assert near and near[0]["resource_id"] == "R-YT-abcdefghijk"
    assert near_duplicates(reg, "Boxing Footwork Basics", "A-other") == []


def test_parse_date():
    today = date(2026, 9, 19)
    assert parse_date("2021-06-10T12:00:00Z") == ("2021-06-10", "day")
    assert parse_date("2021-06") == ("2021-06", "month")
    assert parse_date("2021") == ("2021", "year")
    assert parse_date("Mar 3, 2021") == ("2021-03-03", "day")
    assert parse_date("3 March 2021") == ("2021-03-03", "day")
    assert parse_date("Premiered Sep 1, 2020") == ("2020-09-01", "day")
    assert parse_date("March 2021") == ("2021-03", "month")
    assert parse_date("2 years ago", today) == ("2024", "year")
    assert parse_date("3 months ago", today) == ("2026-06", "month")
    assert parse_date("5 days ago", today) == ("2026-09-14", "day")
    assert parse_date("First published in 1998 by X") == ("1998", "year")
    assert parse_date("") == ("", "unknown")
    assert parse_date("no date here") == ("", "unknown")


def test_apply_overrides():
    g = MetadataGuess(title="A", author_raw="B", confidence=0.3)
    g2 = apply_overrides(g, author="Teddy Atlas", date_text="June 2021")
    assert g2.author_raw == "Teddy Atlas" and g2.published_date == "2021-06" and g2.date_precision == "month" and g2.confidence >= 0.9
    assert g.author_raw == "B"  # original untouched


def test_author_resolution(fake_sheets, settings):
    reg = make_registry(fake_sheets, settings)
    a = ensure_author(reg, "Teddy Atlas", "person")
    assert a.author_id == "A-teddy-atlas" and a.kb_path == "knowledge/teddy-atlas"
    assert ensure_author(reg, "teddy atlas").author_id == a.author_id
    res = resolve_author(reg, "THE FIGHT with Teddy Atlas")
    assert res.is_new is False or res.near_matches  # fuzzy match or at least a near match
    b = ensure_author(reg, "THE FIGHT with Teddy Atlas", "channel", alias_of=a.author_id)
    assert "THE FIGHT with Teddy Atlas" in b.aliases
    assert resolve_author(reg, "the fight with teddy atlas").matched_by == "alias"
    new = resolve_author(reg, "Jane Nobody")
    assert new.is_new and new.author_id == "A-jane-nobody"
    c = ensure_author(reg, "Jane Nobody")
    assert c.author_id == "A-jane-nobody" and len(reg.authors()) == 2
