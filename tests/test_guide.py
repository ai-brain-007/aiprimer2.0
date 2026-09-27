"""`doc guide`: docs/notion/*.md -> reference pages under the "AI Primer" page, refreshed in place."""

from __future__ import annotations

from pathlib import Path

import pytest

from pipeline.config import Settings
from pipeline.context import AppContext
from pipeline.guide_cmds import GUIDE_DIR, child_pages, publish_guide, split_title
from pipeline.notion import markdown_to_blocks, plain_text
from tests.fake_notion import FakeNotionBackend

REPO_ROOT = Path(__file__).resolve().parent.parent


def _ctx(tmp_path, fake, parent, monkeypatch):
    monkeypatch.setenv("AIPRIMER_NOTION_PAGE_ID", parent)
    return AppContext(Settings(repo_root=tmp_path, config={}), notion_backend=fake)


def test_split_title_takes_the_first_heading_out_of_the_body():
    assert split_title("# Command guide\n\nHow you talk.\n", "x") == ("Command guide", "How you talk.\n")
    assert split_title("\n\n# T \n## Sub\n", "x") == ("T", "## Sub\n")
    assert split_title("No heading first\n# T\n", "fallback") == ("fallback", "No heading first\n# T\n")


def test_publish_guide_creates_pages_then_refreshes_them_in_place(tmp_path, monkeypatch):
    (tmp_path / GUIDE_DIR).mkdir(parents=True)
    (tmp_path / GUIDE_DIR / "10-command-guide.md").write_text("# Command guide\n\nType `/ingest`.\n", encoding="utf-8")
    (tmp_path / GUIDE_DIR / "20-how-it-works.md").write_text("# How it works\n\n## Layers\n\n- one\n- two\n", encoding="utf-8")
    fake = FakeNotionBackend()
    parent = fake.add_page("AI Primer")
    fake.create_database(parent, "Authors", {"Name": {"title": {}}})  # a table under the page must survive untouched
    ctx = _ctx(tmp_path, fake, parent, monkeypatch)

    first = publish_guide(ctx)
    assert [p["title"] for p in first["pages"]] == ["Command guide", "How it works"]
    assert all(p["created"] for p in first["pages"]) and first["parent_url"] == fake.page_url(parent)
    assert first["pages"][0]["file"] == "docs/notion/10-command-guide.md"
    ids = {p["title"]: p["page_id"] for p in first["pages"]}
    assert child_pages(fake, parent) == ids
    assert fake.page_plain_text(ids["Command guide"]) == "Type /ingest."  # the title is not repeated in the body
    assert plain_text(fake.pages[ids["Command guide"]]["properties"]["title"]["title"]) == "Command guide"

    (tmp_path / GUIDE_DIR / "10-command-guide.md").write_text("# Command guide\n\nType `/ingest` or `/summarize`.\n", encoding="utf-8")
    second = publish_guide(ctx)
    assert {p["title"]: p["page_id"] for p in second["pages"]} == ids and not any(p["created"] for p in second["pages"])
    assert fake.page_plain_text(ids["Command guide"]) == "Type /ingest or /summarize."
    assert set(fake.child_databases(parent)) == {"Authors"} and len(child_pages(fake, parent)) == 2

    only = publish_guide(ctx, only="how-it")
    assert [p["title"] for p in only["pages"]] == ["How it works"]
    with pytest.raises(RuntimeError, match="no markdown file"):
        publish_guide(ctx, only="nothing-like-this")


def test_publish_guide_needs_notion_mode(tmp_path, monkeypatch):
    monkeypatch.delenv("AIPRIMER_NOTION_PAGE_ID", raising=False)
    with pytest.raises(RuntimeError, match="Notion mode is off"):
        publish_guide(AppContext(Settings(repo_root=tmp_path, config={})))


def test_the_real_reference_documents_publish_cleanly(tmp_path, monkeypatch):
    """The committed docs/notion files must convert to Notion blocks and publish without hitting a limit."""
    files = sorted((REPO_ROOT / GUIDE_DIR).glob("*.md"))
    assert [f.name for f in files] == ["10-command-guide.md", "20-how-it-works.md"]
    fake = FakeNotionBackend()
    parent = fake.add_page("AI Primer")
    ctx = _ctx(REPO_ROOT, fake, parent, monkeypatch)
    result = publish_guide(ctx)
    assert [p["title"] for p in result["pages"]] == ["Command guide", "How the pipeline works"]
    for path, page in zip(files, result["pages"]):
        title, body = split_title(path.read_text(encoding="utf-8"), path.stem)
        types = {b["type"] for b in markdown_to_blocks(body)}
        assert {"heading_2", "table"} <= types, path.name
        text = fake.page_plain_text(page["page_id"])
        assert "—" not in body, "style: no em dashes in owner-facing text"
        assert "ingest" in text.lower() and "notion" in text.lower()
