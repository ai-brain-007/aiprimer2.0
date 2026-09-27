"""v2 end to end, offline: Backblaze storage (fake) + Notion control panel and pages (fake).

Covers: bootstrap of storage accounts from the environment, setup (databases, bucket prefixes, health with
write test), ingestion into the bucket with the Notion Resources row, free-tier rollover to the next account,
taxonomy rename without touching storage, moving a resource (keys change), publishing an author page in place,
and reading comments back.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from pipeline.context import AppContext, b2_account_numbers, default_storage_accounts
from pipeline.ingest import Ingestor
from pipeline.notion import NotionRepo
from pipeline.registry import Registry
from pipeline.resources import move
from pipeline.setup_cmds import auth_check, import_taxonomy, init_drive, init_sheet, status
from pipeline.storage_b2 import B2StorageClient
from pipeline.summarize_cmds import _publish_to_notion, doc_comments
from pipeline.taxonomy import load_taxonomy, rename_node
from tests.fake_b2 import FakeB2Backend
from tests.fake_notion import FakeNotionBackend

SEED = Path(__file__).resolve().parent.parent / "config" / "taxonomy.seed.yaml"
STAGE = "Body / Immortal Yogi / Rest Body"


@pytest.fixture
def v2(settings, fake_apify, monkeypatch, tmp_path):
    monkeypatch.setenv("AIPRIMER_NOTION_PAGE_ID", "0123456789abcdef0123456789abcdef")
    monkeypatch.setenv("B2_KEY_ID_0001", "kid1")
    monkeypatch.setenv("B2_APPLICATION_KEY_0001", "k1")
    for v in ("B2_KEY_ID_0002", "B2_APPLICATION_KEY_0002", "B2_MEDIA_BUCKET_0001", "B2_MEDIA_BUCKET_0002", "GOOGLE_SERVICE_ACCOUNT_JSON", "AIPRIMER_CONTROL_SHEET_ID"):
        monkeypatch.delenv(v, raising=False)
    notion = FakeNotionBackend()
    parent = notion.add_page("AI Primer")
    repo = NotionRepo(notion, parent)
    reg = Registry(repo, settings)
    buckets: dict[str, FakeB2Backend] = {}

    def bucket(name: str, public: bool = False) -> FakeB2Backend:
        if name not in buckets:
            buckets[name] = FakeB2Backend(bucket_name=name, bucket_type="allPublic" if public else "allPrivate", storage_dir=tmp_path / "blobs" / name)
        return buckets[name]

    def factory(account):
        if account.backend != "b2":
            return None
        return B2StorageClient(bucket(account.bucket, public=account.account_id.endswith(":media")))

    ctx = AppContext(settings, registry=reg, drive_factory=factory, apify_runner=fake_apify, notion_backend=notion)
    return ctx, reg, notion, bucket, parent


def test_storage_accounts_come_from_the_environment(settings, monkeypatch):
    monkeypatch.setenv("B2_APPLICATION_KEY_0001", "k1")
    monkeypatch.setenv("B2_MEDIA_BUCKET_0001", "aiprimer-media-0001")
    monkeypatch.setenv("B2_APPLICATION_KEY_0003", "k3")
    monkeypatch.setenv("B2_BUCKET_0003", "my-other-bucket")
    monkeypatch.delenv("B2_MEDIA_BUCKET_0003", raising=False)
    assert b2_account_numbers() == ["0001", "0003"]
    rows = default_storage_accounts(settings)
    assert [r["account_id"] for r in rows] == ["b2-0001", "b2-0003"]
    assert rows[0]["bucket"] == "ai-primer-raw-0001" and rows[0]["media_bucket"] == "aiprimer-media-0001"
    assert rows[1]["bucket"] == "my-other-bucket" and rows[1]["media_bucket"] == "" and rows[1]["priority"] == 2
    assert rows[0]["quota_bytes"] == 10_000_000_000 and rows[0]["key_id_env_var"] == "B2_KEY_ID_0001"


def test_setup_creates_databases_prefixes_and_health(v2):
    ctx, reg, notion, bucket, parent = v2
    sheet = init_sheet(ctx)
    assert all(reg.repo.database_id(t) for t in ("Accounts", "Taxonomy", "Folders", "Resources", "Authors", "Summaries", "Jobs"))
    layers = {b["child_page"]["title"]: b["id"] for b in notion.list_block_children(parent) if b["type"] == "child_page"}
    assert set(notion.child_databases(layers["LAYER 0 - CONFIG"])) == {"Accounts", "Taxonomy", "Folders", "Jobs"}  # tables live in their layer pages
    assert notion.child_databases(parent) == {}
    assert sheet["accounts"] == ["b2-0001"]
    acc = reg.account("b2-0001")
    assert acc.backend == "b2" and acc.bucket == "ai-primer-raw-0001" and acc.quota_bytes == 10_000_000_000
    drive = init_drive(ctx)
    entry = drive["accounts"][0]
    assert entry["ok"] and entry["root"].endswith("raw/") and entry["inbox"].endswith("raw/_Inbox/")
    keys = bucket("ai-primer-raw-0001").keys()
    assert any(k.startswith("raw/") for k in keys) and any(k.startswith("raw/_Inbox/") for k in keys)
    health = auth_check(ctx)
    assert health["mode"] == {"control_panel": "notion", "storage": "b2"}
    assert health["control_panel"]["ok"]
    b2 = health["accounts"][0]
    assert b2["ok"] and b2["write_test"]["can_write"] and b2["writes_into"]["reachable"] and b2["cap_gb"] == 9.31
    assert b2["media"]["configured"] is False  # the public media bucket is optional
    assert not [k for k in bucket("ai-primer-raw-0001").keys() if "write-test" in k]
    st = status(ctx)
    assert st["mode"]["storage"] == "b2" and st["accounts"][0]["backend"] == "b2"
    # second run changes nothing
    assert init_sheet(ctx)["tabs"]["created"] == [] and init_drive(ctx)["accounts"][0]["ok"]


def _bootstrap(ctx):
    init_sheet(ctx)
    init_drive(ctx)
    import_taxonomy(ctx)


def test_ingest_file_into_bucket_and_notion(v2, tmp_path):
    ctx, reg, notion, bucket, parent = v2
    _bootstrap(ctx)
    src = tmp_path / "notes.txt"
    src.write_text("Sleep before you optimise anything else. " * 40, encoding="utf-8")
    out = Ingestor(ctx).run(str(src), STAGE, author="Jane Doe", date_text="2021-06-10")
    assert out["status"] == "ingested" and out["account_id"] == "b2-0001"
    row = reg.resource(out["resource_id"])
    tax = load_taxonomy(reg)
    stage = tax.resolve(STAGE)
    # keys: raw/<domain id>/<primer id>/<stage id>/<readable filename>
    assert row.raw_file_id.startswith("raw/") and f"/{stage.node_id}/" in row.raw_file_id and row.raw_file_id.endswith(".txt")
    assert row.text_file_id.startswith("raw/") and f"/{stage.node_id}/" in row.text_file_id and row.text_file_id.endswith(".extracted.md")
    assert all(part.startswith("T-") for part in row.raw_file_id.split("/")[1:-1])
    assert row.raw_file_url.startswith("b2://ai-primer-raw-0001/raw/")
    assert row.drive_path == "AI Primer Raw / Body / Immortal Yogi / Rest Body" and row.stored_at and row.resource_kind == "text / notes"
    keys = bucket("ai-primer-raw-0001").keys()
    assert row.raw_file_id in keys and row.text_file_id in keys
    # the Notion Resources database holds the row
    db = reg.repo.database_id("Resources")
    rows = notion.rows(db)
    assert any(r["resource_id"] == row.resource_id and r["status"] == "ingested" for r in rows)
    # duplicate is skipped
    again = Ingestor(ctx).run(str(src), STAGE, author="Jane Doe")
    assert again["status"] == "skipped"
    # a job was logged
    jobs = notion.rows(reg.repo.database_id("Jobs"))
    assert isinstance(jobs, list)


def test_free_tier_rollover_to_next_account(v2, tmp_path, monkeypatch):
    ctx, reg, notion, bucket, parent = v2
    monkeypatch.setenv("B2_KEY_ID_0002", "kid2")
    monkeypatch.setenv("B2_APPLICATION_KEY_0002", "k2")
    _bootstrap(ctx)
    assert [a.account_id for a in reg.raw_accounts()] == ["b2-0001", "b2-0002"]
    bucket("ai-primer-raw-0001").cap_bytes = 200  # the free 10 GB, in miniature
    src = tmp_path / "big.txt"
    src.write_text("x" * 5000, encoding="utf-8")
    first = Ingestor(ctx).run(str(src), STAGE, author="Jane Doe")
    assert first["status"] == "retry" and "cap" in first["reason"].lower()
    assert reg.account("b2-0001").status == "full"
    second = Ingestor(ctx).run(str(src), STAGE, author="Jane Doe")
    assert second["status"] == "ingested" and second["account_id"] == "b2-0002"
    assert reg.resource(second["resource_id"]).raw_file_id in bucket("ai-primer-raw-0002").keys()


def test_rename_stage_touches_no_file_and_move_changes_keys(v2, tmp_path):
    ctx, reg, notion, bucket, parent = v2
    _bootstrap(ctx)
    src = tmp_path / "a.txt"
    src.write_text("hello " * 100, encoding="utf-8")
    out = Ingestor(ctx).run(str(src), STAGE, author="Jane Doe")
    row = reg.resource(out["resource_id"])
    before = set(bucket("ai-primer-raw-0001").keys())
    tax = load_taxonomy(reg)
    report = rename_node(reg, tax.resolve(STAGE).node_id, "Sleep", ctx.drive_for)
    assert report.errors == [] and set(bucket("ai-primer-raw-0001").keys()) == before
    assert reg.resource(row.resource_id).stage_path == "Body / Immortal Yogi / Sleep"
    moved = move(ctx, row.resource_id, "Body / Immortal Yogi / Regulate Body")
    new_row = reg.resource(row.resource_id)
    target = load_taxonomy(reg).resolve("Body / Immortal Yogi / Regulate Body")
    assert f"/{target.node_id}/" in new_row.raw_file_id and f"/{target.node_id}/" in new_row.text_file_id
    keys = set(bucket("ai-primer-raw-0001").keys())
    assert new_row.raw_file_id in keys and row.raw_file_id not in keys and moved["to"] == "Body / Immortal Yogi / Regulate Body"


def test_author_page_is_published_in_place_and_comments_read(v2):
    ctx, reg, notion, bucket, parent = v2
    _bootstrap(ctx)
    from pipeline.metadata import ensure_author

    author = ensure_author(reg, "Jane Doe")
    meta: dict = {}
    files = {"summary.md": "<!-- generated -->\n\n# Summary: Jane Doe\n\n## Author profile\n\n- Sources: 1\n\n## Cards: Body\n\n#### Sleep first\n\nGo to bed early.\n"}
    info = _publish_to_notion(ctx, author, files, meta)
    page_id = info["doc_id"]
    assert page_id == reg.repo.page_id("Authors", author.author_id) and info["url"]
    text = notion.page_plain_text(page_id)
    assert "Author profile" in text and "Sleep first" in text and "Go to bed early." in text
    files["summary.md"] = files["summary.md"].replace("Go to bed early.", "Go to bed before eleven.")
    info2 = _publish_to_notion(ctx, author, files, meta)
    assert info2["doc_id"] == page_id
    text2 = notion.page_plain_text(page_id)
    assert "before eleven" in text2 and "bed early" not in text2
    # feedback loop
    author.summary_doc_id, author.summary_doc_url = page_id, info["url"]
    reg.upsert_author(author)
    notion.add_comment(page_id, "This is wrong, he says ten.", author="Jules")
    got = doc_comments(ctx, author.author_id)
    assert got["comments"] and got["comments"][0]["comment"].startswith("This is wrong")
    res = doc_comments(ctx, author.author_id, resolve=got["comments"][0]["comment_id"])
    assert res["resolved"]
