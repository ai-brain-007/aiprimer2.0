"""End-to-end summary flow with fakes: prepare -> (simulated helper outputs) -> verify -> match -> apply ->
review-prep -> (simulated review) -> finalize (Doc published to the fake Drive, no git commit)."""

from __future__ import annotations

import json
from pathlib import Path

import yaml

from pipeline import summarize_cmds as sc
from pipeline.context import AppContext
from pipeline.drive import DriveClient
from pipeline.models import Account, Author, Resource
from pipeline.registry import Registry
from pipeline.sheets import SheetsRepo
from pipeline.taxonomy import import_seed, load_taxonomy

SEED = Path(__file__).resolve().parent.parent / "config" / "taxonomy.seed.yaml"

TEXT_2015 = """---
resource_id: R-YT-aaaaaaaaaaa
---
[00:00] Welcome. Today we talk about the shoulder roll. The shoulder roll is a defensive move where you turn the lead shoulder inward to deflect the punch.
[01:00] Keep the rear hand high by the chin at all times. Practice the roll slowly in front of a mirror for five minutes every day.
[02:00] A common mistake is dropping the rear hand while rolling, which leaves the chin open.
"""

TEXT_2021 = """---
resource_id: R-YT-bbbbbbbbbbb
---
[00:00] The shoulder roll has changed in my teaching. I now tell fighters to roll and immediately counter with the rear hand instead of only deflecting.
[01:00] The jab is the most important punch: it measures distance and sets up everything else.
"""


def build(settings, fake_sheets, fake_drive, fake_apify, tmp_path, monkeypatch):
    # knowledge dir inside tmp so the repo is untouched
    monkeypatch.setattr(type(settings), "knowledge_dir", property(lambda self: tmp_path / "knowledge"))
    repo = SheetsRepo(fake_sheets)
    repo.ensure_tabs()
    reg = Registry(repo, settings)
    import_seed(reg, yaml.safe_load(SEED.read_text()))
    drive = DriveClient(fake_drive)
    root = drive.ensure_folder(None, "AI Primer Raw")
    summaries = drive.ensure_folder(None, "AI Primer Summaries")
    reg.upsert_account(Account(account_id="raw01", root_folder_id=root["id"], token_env_var="GOOGLE_REFRESH_TOKEN_RAW01"))
    reg.upsert_account(Account(account_id="summary01", role="summary", summaries_folder_id=summaries["id"], token_env_var="GOOGLE_REFRESH_TOKEN_SUMMARY01"))
    reg.upsert_author(Author(author_id="A-teddy-atlas", canonical_name="Teddy Atlas", type="channel"))
    stage = load_taxonomy(reg).resolve("Body / Olympic Spartan / Boxing")
    for rid, text, date in (("R-YT-aaaaaaaaaaa", TEXT_2015, "2015-03-01"), ("R-YT-bbbbbbbbbbb", TEXT_2021, "2021-06-10")):
        p = tmp_path / f"{rid}.md"
        p.write_text(text, encoding="utf-8")
        meta = drive.upload(p, f"{rid}.extracted.md", root["id"], {"resource_id": rid, "role": "text"})
        reg.upsert_resource(Resource(resource_id=rid, status="ingested", title=f"Video {rid[-3:]}", author_id="A-teddy-atlas", published_date=date, date_precision="day", source_type="youtube", stage_id=stage.node_id, stage_path=stage.path, account_id="raw01", text_file_id=meta["id"], extracted_chars=len(text)))
    ctx = AppContext(settings, registry=reg, drive_factory=lambda a: drive, apify_runner=fake_apify)
    return ctx, reg, fake_drive


def helper_extract(chunk_path: Path, output_path: Path, resource_id: str, chunk_id: str, units: list[dict]) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps({"resource_id": resource_id, "chunk_id": chunk_id, "units": units}), encoding="utf-8")


def test_full_summary_flow(settings, fake_sheets, fake_drive, fake_apify, tmp_path, monkeypatch):
    ctx, reg, fd = build(settings, fake_sheets, fake_drive, fake_apify, tmp_path, monkeypatch)

    p = sc.plan(ctx, "Teddy Atlas")
    assert [x["resource_id"] for x in p["pending"]] == ["R-YT-aaaaaaaaaaa", "R-YT-bbbbbbbbbbb"] and p["proposed"] == ["R-YT-aaaaaaaaaaa", "R-YT-bbbbbbbbbbb"]

    # ---- resource 1 (2015)
    prep = sc.prepare(ctx, "A-teddy-atlas", ["R-YT-aaaaaaaaaaa"])
    r1 = prep["resources"][0]
    assert len(r1["chunks"]) == 1 and Path(r1["chunks"][0]["path"]).exists()
    helper_extract(
        Path(r1["chunks"][0]["path"]), Path(r1["chunks"][0]["output_path"]), "R-YT-aaaaaaaaaaa", r1["chunks"][0]["chunk_id"],
        [
            {"type": "technique", "name": "Shoulder roll", "aliases": ["Philly shell roll"], "description": "A defensive move: turn the lead shoulder inward to deflect the punch.", "details": "Turn the lead shoulder in; keep the rear hand high by the chin.", "citations": [{"location": "00:00", "quote": "The shoulder roll is a defensive move where you turn the lead shoulder inward to deflect the punch."}]},
            {"type": "drill", "name": "Mirror roll drill", "description": "Practice the roll slowly in front of a mirror.", "details": "Five minutes every day.", "citations": [{"location": "01:00", "quote": "Practice the roll slowly in front of a mirror for five minutes every day."}]},
            {"type": "mistake", "name": "Dropping the rear hand", "description": "Dropping the rear hand while rolling leaves the chin open.", "citations": [{"location": "02:00", "quote": "dropping the rear hand while rolling, which leaves the chin open"}]},
            {"type": "claim", "name": "Invented claim", "description": "Something not in the source.", "citations": [{"location": "02:00", "quote": "this sentence does not exist anywhere in the transcript at all"}]},
        ],
    )
    v = sc.verify(ctx, "A-teddy-atlas", "R-YT-aaaaaaaaaaa")
    assert v["extracted"] == 4 and v["verified"] == 3 and v["rejected"] == 1
    m = sc.match(ctx, "A-teddy-atlas", "R-YT-aaaaaaaaaaa")
    assert m["units"] == 3 and m["auto_new"] == 3 and m["needs_helper"] == 0
    a = sc.apply(ctx, "A-teddy-atlas", "R-YT-aaaaaaaaaaa")
    assert a["new"] == 3 and a["errors"] == []
    rp = sc.review_prep(ctx, "A-teddy-atlas")
    assert rp["changed_units"] == 3 and Path(rp["evidence_path"]).exists()
    fin = sc.finalize(ctx, "A-teddy-atlas", note="first pass", publish_doc=True, commit=False)
    assert fin["version"] == 1 and fin["units_total"] == 3 and fin["doc"]["created"] and fin["doc"]["url"].startswith("https://docs.google.com/document/d/")
    assert reg.resource("R-YT-aaaaaaaaaaa").status == "summarized" and reg.author("A-teddy-atlas").unit_count == 3
    assert reg.summaries()[0].version == 1 and reg.summaries()[0].units_new == 3
    summary_md = (tmp_path / "knowledge" / "teddy-atlas" / "summary.md").read_text(encoding="utf-8")
    assert "Shoulder roll" in summary_md and "Mirror roll drill" in summary_md
    rejected_dir = tmp_path / "knowledge" / "teddy-atlas" / "_rejected"
    assert rejected_dir.exists() and any(rejected_dir.iterdir())

    # ---- resource 2 (2021): the shoulder roll evolved, plus a new concept
    prep2 = sc.prepare(ctx, "A-teddy-atlas", ["R-YT-bbbbbbbbbbb"])
    r2 = prep2["resources"][0]
    helper_extract(
        Path(r2["chunks"][0]["path"]), Path(r2["chunks"][0]["output_path"]), "R-YT-bbbbbbbbbbb", r2["chunks"][0]["chunk_id"],
        [
            {"type": "technique", "name": "Shoulder roll", "description": "Roll the lead shoulder and immediately counter with the rear hand instead of only deflecting.", "details": "Roll then counter with the rear hand.", "citations": [{"location": "00:00", "quote": "roll and immediately counter with the rear hand instead of only deflecting"}]},
            {"type": "principle", "name": "The jab measures distance", "description": "The jab is the most important punch because it measures distance and sets up everything else.", "citations": [{"location": "01:00", "quote": "The jab is the most important punch: it measures distance and sets up everything else."}]},
        ],
    )
    v2 = sc.verify(ctx, "A-teddy-atlas", "R-YT-bbbbbbbbbbb")
    assert v2["verified"] == 2
    m2 = sc.match(ctx, "A-teddy-atlas", "R-YT-bbbbbbbbbbb")
    cand = json.loads(Path(m2["candidates_path"]).read_text(encoding="utf-8"))
    roll_item = next(i for i in cand["items"] if i["unit"]["name"] == "Shoulder roll")
    assert roll_item["candidates"] and roll_item["auto_decision"] == ""  # same name, different details -> helper decides
    target = roll_item["candidates"][0]["unit_id"]
    decisions = {"resource_id": "R-YT-bbbbbbbbbbb", "decisions": [{"temp_id": roll_item["temp_id"], "decision": "EVOLVED", "target_unit_id": target, "what_changed": "Now rolls and counters with the rear hand instead of only deflecting.", "rationale": "same technique taught differently"}]}
    Path(m2["decisions_path"]).write_text(json.dumps(decisions), encoding="utf-8")
    a2 = sc.apply(ctx, "A-teddy-atlas", "R-YT-bbbbbbbbbbb")
    assert a2["evolved"] == 1 and a2["new"] == 1 and a2["errors"] == []
    sc.review_prep(ctx, "A-teddy-atlas")
    ws = sc._ws(ctx, reg.author("A-teddy-atlas"))
    ws.review_path().write_text(json.dumps({"author_id": "A-teddy-atlas", "overall": "pass", "items": [{"unit_id": target, "verdict": "accept", "reason": "supported"}]}), encoding="utf-8")
    fin2 = sc.finalize(ctx, "A-teddy-atlas", publish_doc=True, commit=False)
    assert fin2["version"] == 2 and fin2["evolved"] == 1 and fin2["doc"]["created"] is False and fin2["doc"]["doc_id"] == fin["doc"]["doc_id"]
    a = reg.author("A-teddy-atlas")
    assert a.summary_version == 2 and a.summary_doc_created_at and a.summary_folder_url.startswith("https://drive.google.com/drive/folders/")
    assert a.kb_url.endswith("/knowledge/teddy-atlas") and "github.com" in a.kb_url
    assert reg.summaries()[-1].resources_count == 2
    unit_files = list((tmp_path / "knowledge" / "teddy-atlas" / "units").glob("*.md"))
    assert len(unit_files) == 4
    roll = next(f.read_text(encoding="utf-8") for f in unit_files if "Shoulder roll" in f.read_text(encoding="utf-8"))
    assert "2015-03-01" in roll and "2021-06-10" in roll and "counter with the rear hand" in roll
    summary_md = (tmp_path / "knowledge" / "teddy-atlas" / "summary.md").read_text(encoding="utf-8")
    assert "Evolution" in summary_md and "The jab measures distance" in summary_md
    st = sc.status(ctx, "A-teddy-atlas")
    assert st["units"] == 4 and st["version"] == 2 and st["pending"] == []
    # third run: nothing pending
    assert sc.plan(ctx, "A-teddy-atlas")["proposed"] == []
