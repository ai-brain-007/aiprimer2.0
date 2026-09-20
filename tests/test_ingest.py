from __future__ import annotations

import json
from pathlib import Path

import pymupdf
import yaml

from pipeline.context import AppContext
from pipeline.drive import DriveClient
from pipeline.ingest import Ingestor, _apply_ocr_patch
from pipeline.models import Account
from pipeline.registry import Registry
from pipeline.sheets import SheetsRepo
from pipeline.taxonomy import import_seed, load_taxonomy

SEED = Path(__file__).resolve().parent.parent / "config" / "taxonomy.seed.yaml"


def build_ctx(settings, fake_sheets, fake_drive, fake_apify):
    repo = SheetsRepo(fake_sheets)
    repo.ensure_tabs()
    reg = Registry(repo, settings)
    import_seed(reg, yaml.safe_load(SEED.read_text()))
    drive = DriveClient(fake_drive)
    root = drive.ensure_folder(None, "AI Primer Raw")
    inbox = drive.ensure_folder(root["id"], "_Inbox")
    reg.upsert_account(Account(account_id="raw01", root_folder_id=root["id"], inbox_folder_id=inbox["id"], token_env_var="GOOGLE_REFRESH_TOKEN_RAW01", quota_bytes=10**9, used_bytes=0))
    reg.upsert_account(Account(account_id="summary01", role="summary", token_env_var="GOOGLE_REFRESH_TOKEN_SUMMARY01"))
    ctx = AppContext(settings, registry=reg, drive_factory=lambda a: drive, apify_runner=fake_apify)
    return ctx, reg, drive, root, inbox


def make_pdf(path: Path, pages: int = 2) -> Path:
    doc = pymupdf.open()
    for i in range(pages):
        page = doc.new_page()
        if i == 0:
            page.insert_text((72, 90), "The Art of Footwork", fontsize=24)
            page.insert_text((72, 130), "by Teddy Atlas", fontsize=12)
            page.insert_text((72, 160), "Copyright 2019 Some Press", fontsize=10)
        # enough visible text per page that the PDF is not mistaken for a scan (lines must stay inside the page)
        for k in range(12):
            page.insert_text((72, 220 + 16 * k), f"Page {i + 1}, line {k + 1}: pivot on the ball of the foot and keep the rear heel light.", fontsize=11)
    doc.save(str(path))
    return path


def test_ingest_youtube_and_pdf_end_to_end(settings, fake_sheets, fake_drive, fake_apify, tmp_path):
    fake_apify.responses["apidojo/youtube-scraper"] = [{"id": "abcdefghijk", "title": "Footwork basics", "channelName": "Teddy Atlas", "channelUrl": "https://youtube.com/@teddy", "date": "2021-06-10T00:00:00Z", "duration": "10:00", "description": "boxing footwork"}]
    fake_apify.responses["supreme_coder/youtube-transcript-scraper"] = [{"videoId": "abcdefghijk", "transcript": [{"start": 0, "dur": 3, "text": "Keep your feet under you."}, {"start": 65, "dur": 3, "text": "Pivot on the ball of the foot."}], "language": "en"}]
    ctx, reg, drive, root, inbox = build_ctx(settings, fake_sheets, fake_drive, fake_apify)
    ing = Ingestor(ctx)

    probes = ing.probe(["https://www.youtube.com/watch?v=abcdefghijk"])
    p = probes[0]
    assert p.status == "new" and p.resource_id == "R-YT-abcdefghijk" and p.metadata["author_raw"] == "Teddy Atlas"
    assert p.author_match["is_new"] and p.stage_hints

    r = ing.run("https://www.youtube.com/watch?v=abcdefghijk", "Body / Olympic Spartan / Boxing")
    assert r["status"] == "ingested" and r["author_id"] == "A-teddy-atlas" and r["published_date"] == "2021-06-10"
    row = reg.resource("R-YT-abcdefghijk")
    assert row.status == "ingested" and row.stage_path == "Body / Olympic Spartan / Boxing" and row.raw_file_id and row.text_file_id
    assert fake_drive.path_of(row.text_file_id).startswith("AI Primer Raw/Body/Olympic Spartan/Boxing/2021-06-10 - Teddy Atlas - Footwork basics [R-YT-abcdefghijk]")
    assert reg.author("A-teddy-atlas").resource_count == 1 and reg.author("A-teddy-atlas").type == "channel"
    text = fake_drive.blobs[row.text_file_id].read_text(encoding="utf-8")
    assert "[01:05]" in text and "Pivot on the ball" in text and "resource_id: R-YT-abcdefghijk" in text
    # storage / provenance columns
    assert row.drive_path == "AI Primer Raw / Body / Olympic Spartan / Boxing" and row.folder_url.startswith("https://drive.google.com/drive/folders/")
    assert row.text_file_url and row.stored_at and row.resource_kind == "YouTube video (transcript)"
    assert row.extraction_method.startswith("apify transcript") and row.apify_cost_usd == 0.02  # metadata run + transcript run

    # second run: skipped as duplicate, no new Drive files
    n_files = len(fake_drive.files)
    r2 = ing.run("https://www.youtube.com/watch?v=abcdefghijk", "Body / Olympic Spartan / Boxing")
    assert r2["status"] == "skipped" and len(fake_drive.files) == n_files
    assert ing.probe(["https://youtu.be/abcdefghijk"])[0].status == "duplicate"

    # a PDF book, author given by the user as the existing author id
    pdf = make_pdf(tmp_path / "book.pdf", 3)
    pr = ing.probe([str(pdf)])[0]
    assert pr.status == "new" and pr.kind == "pdf" and pr.metadata["title"] == "The Art of Footwork"
    r3 = ing.run(str(pdf), "Body / Olympic Spartan / Boxing", author="A-teddy-atlas", date_text="2019")
    assert r3["status"] == "ingested" and r3["resource_id"].startswith("R-F-") and r3["published_date"] == "2019"
    row3 = reg.resource(r3["resource_id"])
    assert row3.pages == 3 and row3.author_override and row3.source_type == "pdf"
    assert row3.resource_kind == "document (PDF)" and row3.extraction_method == "pdf text" and row3.file_size_bytes and row3.apify_cost_usd is None
    assert fake_drive.files[row3.raw_file_id]["name"].endswith(".pdf")
    assert fake_drive.files[row3.raw_file_id]["appProperties"]["resource_id"] == row3.resource_id
    assert reg.author("A-teddy-atlas").resource_count == 2
    assert reg.resource(r3["resource_id"]).stage_id == load_taxonomy(reg).resolve("Boxing").node_id


def test_ingest_image_needs_vision_then_completes(settings, fake_sheets, fake_drive, fake_apify, tmp_path):
    ctx, reg, drive, root, inbox = build_ctx(settings, fake_sheets, fake_drive, fake_apify)
    ing = Ingestor(ctx)
    img = tmp_path / "shot.png"
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 40, 40), False)
    pix.save(str(img))
    r = ing.run(str(img), "Mind / Prodigy Autodidact / Validate", author="Jane Doe", title="Screenshot of article")
    assert r["status"] == "needs_vision" and r["vision_files"]
    row = reg.resource(r["resource_id"])
    assert row.status == "uploaded" and row.raw_file_id
    md = tmp_path / "shot.md"
    md.write_text("# Article title\nThe claim is supported by two studies.", encoding="utf-8")
    r2 = ing.run(str(img), "Mind / Prodigy Autodidact / Validate", author="Jane Doe", title="Screenshot of article", extracted_file=md)
    assert r2["status"] == "ingested"
    assert reg.resource(r["resource_id"]).status == "ingested" and reg.resource(r["resource_id"]).transcript_kind == "vision"


def test_channel_list_and_run(settings, fake_sheets, fake_drive, fake_apify, tmp_path):
    fake_apify.responses["apidojo/youtube-scraper"] = lambda inp: (
        [{"id": f"chanvid{i:04d}", "title": f"Video {i}", "channelName": "Chan", "date": f"2022-01-{i + 1:02d}", "duration": 600} for i in range(3)]
        if "@chan" in inp["startUrls"][0]
        else [{"id": u.rsplit("=", 1)[-1], "title": "Video", "channelName": "Chan", "date": "2022-01-01", "duration": 600} for u in inp["startUrls"]]
    )
    fake_apify.responses["supreme_coder/youtube-transcript-scraper"] = lambda inp: [{"videoId": u.rsplit("=", 1)[-1], "transcript": [{"start": 0, "dur": 2, "text": "content"}]} for u in inp["urls"]]
    ctx, reg, *_ = build_ctx(settings, fake_sheets, fake_drive, fake_apify)
    ing = Ingestor(ctx)
    listing = ing.channel_list("https://www.youtube.com/@chan/videos")
    assert listing["count"] == 3 and listing["pending"] == 3 and listing["total_hours"] == 0.5 and listing["estimated_cost_usd"] > 0
    res = ing.channel_run("https://www.youtube.com/@chan/videos", ["chanvid0000", "chanvid0001"], "Body / Olympic Spartan / Boxing")
    assert len(res["ingested"]) == 2 and not res["failed"]
    # listing run (0.01 over 3 videos) + batch transcript run (0.01 over 2 videos), attributed per video
    assert reg.resource("R-YT-chanvid0000").apify_cost_usd == round(0.01 / 3 + 0.01 / 2, 4)
    listing2 = ing.channel_list("https://www.youtube.com/@chan/videos")
    assert listing2["already_ingested"] == 2 and listing2["pending"] == 1


def test_inbox_flow_moves_file(settings, fake_sheets, fake_drive, fake_apify, tmp_path):
    ctx, reg, drive, root, inbox = build_ctx(settings, fake_sheets, fake_drive, fake_apify)
    src = tmp_path / "notes.txt"
    src.write_text("# Sleep notes\nDim the lights two hours before bed.", encoding="utf-8")
    dropped = drive.upload(src, "notes.txt", inbox["id"])
    ing = Ingestor(ctx)
    files = ing.inbox_list()
    assert files and files[0]["file_id"] == dropped["id"]
    r = ing.inbox_file(dropped["id"], "Body / Immortal Yogi / Rest Body", author="Someone")
    assert r["status"] == "ingested"
    meta = fake_drive.files[dropped["id"]]
    assert fake_drive.path_of(dropped["id"]).startswith("AI Primer Raw/Body/Immortal Yogi/Rest Body/") and meta["name"].endswith(".txt")
    assert ing.inbox_list() == []


def test_account_full_rollover(settings, fake_sheets, fake_drive, fake_apify, tmp_path):
    ctx, reg, drive, root, inbox = build_ctx(settings, fake_sheets, fake_drive, fake_apify)
    reg.upsert_account(Account(account_id="raw02", root_folder_id=root["id"], token_env_var="GOOGLE_REFRESH_TOKEN_RAW02", priority=2, quota_bytes=10**9, used_bytes=0))
    fake_drive.fail_upload_with = RuntimeError("The user's Drive storage quota has been exceeded (storageQuotaExceeded)")
    src = tmp_path / "a.txt"
    src.write_text("hello world " * 100)
    ing = Ingestor(ctx)
    r = ing.run(str(src), "Body / Immortal Yogi / Rest Body", author="X")
    assert r["status"] == "retry" and reg.account("raw01").status == "full"
    fake_drive.fail_upload_with = None
    r2 = ing.run(str(src), "Body / Immortal Yogi / Rest Body", author="X")
    assert r2["status"] == "ingested" and r2["account_id"] == "raw02"


def test_ocr_patch():
    text = "<!-- page 1 -->\nold one\n<!-- page 2 -->\nold two\n<!-- page 3 -->\nold three\n"
    out = _apply_ocr_patch(text, {"2": "new two", 4: "new four"})
    assert "old one" in out and "new two" in out and "old two" not in out and "old three" in out and "<!-- page 4 -->\nnew four" in out
