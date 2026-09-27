"""Offline tests of pipeline/notion.py against the in-memory FakeNotionBackend and a stub HTTP session."""

from __future__ import annotations

import json

import pytest

from pipeline.kb.render import render_summary
from pipeline.models import TAB_MODELS, Account, Citation, Job, Resource, Unit
from pipeline.notion import (
    NotionError,
    NotionPublisher,
    NotionRepo,
    RealNotionBackend,
    chunk_children,
    inline_rich_text,
    markdown_to_blocks,
    plain_text,
    property_plain_text,
)
from pipeline.registry import Registry
from tests.fake_notion import FakeNotionBackend


@pytest.fixture
def fake_notion():
    return FakeNotionBackend()


@pytest.fixture
def parent_page(fake_notion):
    return fake_notion.add_page("AI Primer")


def _texts(block):
    kind = block["type"]
    return plain_text(block[kind].get("rich_text", []))


def _types(blocks):
    return [b["type"] for b in blocks]


# --------------------------------------------------------------------------- NotionRepo


def test_ensure_tabs_creates_the_seven_databases(fake_notion, parent_page):
    repo = NotionRepo(fake_notion, parent_page)
    report = repo.ensure_tabs()
    assert report["created"] == list(TAB_MODELS) and report["columns_added"] == {}
    dbs = fake_notion.child_databases(parent_page)
    assert set(dbs) == set(TAB_MODELS)
    for tab, model in TAB_MODELS.items():
        schema = fake_notion.databases[dbs[tab]]["properties"]
        assert set(schema) == set(model.headers())
        assert schema[model.key_field]["type"] == "title"
        assert all(p["type"] == "rich_text" for name, p in schema.items() if name != model.key_field)
        assert repo.database_id(tab) == dbs[tab]
    # second run creates nothing
    report2 = repo.ensure_tabs()
    assert report2["created"] == [] and report2["columns_added"] == {}
    assert set(fake_notion.child_databases(parent_page)) == set(TAB_MODELS)


def test_ensure_tabs_adds_missing_columns_and_renames_title(fake_notion, parent_page):
    fake_notion.create_database(parent_page, "Accounts", {"account_id": {"title": {}}, "email": {"rich_text": {}}})
    fake_notion.create_database(parent_page, "Jobs", {"Name": {"title": {}}, "command": {"rich_text": {}}})
    repo = NotionRepo(fake_notion, parent_page)
    report = repo.ensure_tabs()
    assert "Accounts" not in report["created"] and "Jobs" not in report["created"]
    assert report["columns_added"]["Accounts"][0] == "role"
    assert set(report["columns_added"]["Accounts"]) == set(Account.headers()) - {"account_id", "email"}
    assert report["title_renamed"]["Jobs"] == "Name -> job_id"
    jobs_schema = fake_notion.databases[repo.database_id("Jobs")]["properties"]
    assert jobs_schema["job_id"]["type"] == "title" and set(jobs_schema) == set(Job.headers())
    # a row written before the columns existed still loads with defaults
    fake_notion.create_page({"database_id": repo.database_id("Accounts")}, {"account_id": {"title": [{"type": "text", "text": {"content": "raw01"}}]}})
    rows = repo.load("Accounts")
    assert rows[0].account_id == "raw01" and rows[0].status == "active"


def test_append_and_load_round_trip_with_lists_and_long_notes(fake_notion, parent_page):
    repo = NotionRepo(fake_notion, parent_page)
    repo.ensure_tabs()
    notes = "".join(chr(ord("a") + i % 26) for i in range(5000))
    r = Resource(resource_id="R-YT-abc", title="Footwork", data_file_ids=["f1", "f2"], warnings=["w1"], notes=notes, extracted_chars=1234, apify_cost_usd=0.02, author_override=True)
    repo.append("Resources", [r])
    page = fake_notion.pages[repo.page_id("Resources", "R-YT-abc")]
    assert page["parent"]["database_id"] == repo.database_id("Resources")
    assert [len(p["text"]["content"]) for p in page["properties"]["notes"]["rich_text"]] == [2000, 2000, 1000]
    assert property_plain_text(page["properties"]["data_file_ids"]) == "f1|f2"
    got = repo.get("Resources", "R-YT-abc")
    assert got.notes == notes and got.data_file_ids == ["f1", "f2"] and got.warnings == ["w1"] and got.author_override is True
    # a fresh repo (new command) reads the same data back from the database
    fresh = NotionRepo(fake_notion, parent_page)
    again = fresh.get("Resources", "R-YT-abc")
    assert again.notes == notes and again.extracted_chars == 1234 and again.apify_cost_usd == 0.02 and again.title == "Footwork"
    assert fake_notion.rows(repo.database_id("Resources"))[0]["resource_id"] == "R-YT-abc"


def test_update_by_key_patches_the_same_page(fake_notion, parent_page):
    repo = NotionRepo(fake_notion, parent_page)
    repo.ensure_tabs()
    repo.upsert("Accounts", Account(account_id="b2-0001", used_bytes=1))
    pid = repo.page_id("Accounts", "b2-0001")
    repo.upsert("Accounts", Account(account_id="b2-0001", used_bytes=42, status="full"))
    assert repo.page_id("Accounts", "b2-0001") == pid
    assert len(fake_notion.rows(repo.database_id("Accounts"))) == 1
    assert fake_notion.rows(repo.database_id("Accounts"))[0]["used_bytes"] == "42"
    assert repo.get("Accounts", "b2-0001").status == "full"
    # unknown keys are appended by update
    repo.update("Accounts", [Account(account_id="b2-0001", used_bytes=43), Account(account_id="b2-0002")])
    assert [a.account_id for a in repo.load("Accounts")] == ["b2-0001", "b2-0002"]
    assert fake_notion.calls.count("create_page") == 2
    assert sum(c.startswith("update_page:") for c in fake_notion.calls) == 2


def test_get_page_id_and_invalidate(fake_notion, parent_page):
    repo = NotionRepo(fake_notion, parent_page)
    repo.ensure_tabs()
    repo.append("Resources", [Resource(resource_id="R-1", title="One")])
    assert repo.get("Resources", "R-1").title == "One" and repo.get("Resources", "R-9") is None
    assert repo.page_id("Resources", "R-9") is None
    # someone edits the row in Notion: the cache is stale until invalidated
    fake_notion.update_page(repo.page_id("Resources", "R-1"), {"title": {"rich_text": [{"type": "text", "text": {"content": "Edited"}}]}})
    assert repo.get("Resources", "R-1").title == "One"
    repo.invalidate("Resources")
    assert repo.get("Resources", "R-1").title == "Edited"
    repo.invalidate()
    assert repo.load("Resources", refresh=True)[0].title == "Edited"


def test_one_query_per_tab_per_command(fake_notion, parent_page):
    NotionRepo(fake_notion, parent_page).ensure_tabs()
    repo = NotionRepo(fake_notion, parent_page)
    fake_notion.calls.clear()
    repo.load("Resources")
    repo.load("Resources")
    repo.get("Resources", "x")
    assert [c for c in fake_notion.calls if c.startswith("query:")].__len__() == 1
    assert sum(c.startswith("list_children:") for c in fake_notion.calls) == 1
    repo.preload(list(TAB_MODELS))
    assert sum(c.startswith("query:") for c in fake_notion.calls) == len(TAB_MODELS)
    repo.preload(list(TAB_MODELS))
    assert sum(c.startswith("query:") for c in fake_notion.calls) == len(TAB_MODELS)


def test_registry_runs_on_notion_repo(fake_notion, parent_page, settings):
    repo = NotionRepo(fake_notion, parent_page)
    repo.ensure_tabs()
    reg = Registry(repo, settings)
    reg.preload()
    reg.upsert_account(Account(account_id="b2-0001", role="raw", backend="b2", quota_bytes=100, used_bytes=10))
    assert reg.account("b2-0001").free_bytes == 90
    reg.upsert_resource(Resource(resource_id="R-YT-abc", title="Boxing footwork basics", natural_key="yt:abc", author_id="A-x"))
    assert reg.find_resource_by_natural_key("yt:abc").resource_id == "R-YT-abc"
    assert reg.resources_for_author("A-x")[0].updated_at
    import time

    job = reg.log_job("ingest run", {"source": "x"}, "ok", time.time(), resources=["R-YT-abc"])
    assert fake_notion.rows(repo.database_id("Jobs"))[0]["job_id"] == job.job_id
    assert repo.url() == fake_notion.page_url(parent_page) == repo.spreadsheet_url()


def test_bad_rows_and_missing_databases_are_guarded(fake_notion, parent_page):
    repo = NotionRepo(fake_notion, parent_page)
    assert repo.load("Resources") == []  # no database yet: nothing to read
    with pytest.raises(NotionError) as exc:
        repo.append("Resources", [Resource(resource_id="R-1")])
    assert "Resources" in str(exc.value) and exc.value.status == 404
    repo.ensure_tabs()
    fake_notion.create_page(
        {"database_id": repo.database_id("Resources")},
        {"resource_id": {"title": [{"type": "text", "text": {"content": "R-bad"}}]}, "status": {"rich_text": [{"type": "text", "text": {"content": "bogus"}}]}},
    )
    rows = repo.load("Resources")
    assert rows[0].resource_id == "R-bad" and "parse error" in rows[0].notes


def test_property_plain_text_handles_other_column_types():
    assert property_plain_text({"type": "number", "number": 3}) == "3"
    assert property_plain_text({"type": "select", "select": {"name": "raw"}}) == "raw"
    assert property_plain_text({"type": "multi_select", "multi_select": [{"name": "a"}, {"name": "b"}]}) == "a|b"
    assert property_plain_text({"type": "checkbox", "checkbox": True}) == "Y"
    assert property_plain_text({"type": "url", "url": None}) == ""
    assert property_plain_text(None) == ""


# --------------------------------------------------------------------------- markdown_to_blocks


def test_headings_and_paragraph_joining():
    blocks = markdown_to_blocks("# A\n## B\n### C\n#### D\n###### F\n\nline one\nline two\n\nsecond para\n")
    assert _types(blocks) == ["heading_1", "heading_2", "heading_3", "heading_3", "heading_3", "paragraph", "paragraph"]
    assert [_texts(b) for b in blocks[:5]] == ["A", "B", "C", "D", "F"]
    assert _texts(blocks[5]) == "line one line two" and _texts(blocks[6]) == "second para"


def test_nested_bullets_clamp_at_two_levels():
    md = "- a\n  - b\n    - c\n      - d\n- e\n\n- f after a blank line\n"
    blocks = markdown_to_blocks(md)
    assert _types(blocks) == ["bulleted_list_item"] * 3
    a = blocks[0]["bulleted_list_item"]
    assert plain_text(a["rich_text"]) == "a" and len(a["children"]) == 1
    b = a["children"][0]["bulleted_list_item"]
    assert plain_text(b["rich_text"]) == "b"
    assert [plain_text(k["bulleted_list_item"]["rich_text"]) for k in b["children"]] == ["c", "d"]  # d clamped next to c
    assert "children" not in b["children"][0]["bulleted_list_item"]
    assert _texts(blocks[2]) == "f after a blank line"
    four = markdown_to_blocks("- a\n    - b\n")
    assert plain_text(four[0]["bulleted_list_item"]["children"][0]["bulleted_list_item"]["rich_text"]) == "b"


def test_numbered_list_quote_and_divider():
    blocks = markdown_to_blocks("1. one\n2. two\n3) three\n\n> quoted\n> text\n\n---\n* star\n")
    assert _types(blocks) == ["numbered_list_item"] * 3 + ["quote", "divider", "bulleted_list_item"]
    assert _texts(blocks[3]) == "quoted\ntext" and _texts(blocks[5]) == "star"


def test_table_ragged_rows_and_split():
    md = "| Date | Title | Type |\n| --- | --- | --- |\n| 2010 | Old |\n| 2011 | New | pdf | extra |\n"
    (table,) = markdown_to_blocks(md)
    t = table["table"]
    assert t["table_width"] == 4 and t["has_column_header"] is True and t["has_row_header"] is False
    rows = t["children"]
    assert len(rows) == 3 and all(len(r["table_row"]["cells"]) == 4 for r in rows)
    assert plain_text(rows[0]["table_row"]["cells"][0]) == "Date" and rows[0]["table_row"]["cells"][3] == []
    assert plain_text(rows[2]["table_row"]["cells"][3]) == "extra"
    big = "| a | b |\n| --- | --- |\n" + "".join(f"| r{i} | v{i} |\n" for i in range(200))
    tables = markdown_to_blocks(big)
    assert _types(tables) == ["table"] * 3
    assert [len(tb["table"]["children"]) for tb in tables] == [91, 91, 21]
    assert all(plain_text(tb["table"]["children"][0]["table_row"]["cells"][0]) == "a" for tb in tables)
    assert plain_text(tables[1]["table"]["children"][1]["table_row"]["cells"][0]) == "r90"


def test_code_fence_and_html_comments():
    md = "<!-- banner: do not edit -->\n# T\n```python\nprint(1)\nx = 2\n```\n```weird\nraw\n```\n<!-- multi\nline -->\ntail\n"
    blocks = markdown_to_blocks(md)
    assert _types(blocks) == ["heading_1", "code", "code", "paragraph"]
    assert blocks[1]["code"]["language"] == "python" and plain_text(blocks[1]["code"]["rich_text"]) == "print(1)\nx = 2"
    assert blocks[2]["code"]["language"] == "plain text"
    assert _texts(blocks[3]) == "tail"
    assert markdown_to_blocks("Generated by the agent. Do not edit.\n\n# T") [0]["type"] == "paragraph"


def test_images_external_resolved_or_skipped(tmp_path):
    md = "![Alt text](https://img.example/x.png)\n![local](pics/a.png)\n"
    blocks = markdown_to_blocks(md)
    assert _types(blocks) == ["image"] and blocks[0]["image"]["external"]["url"] == "https://img.example/x.png"
    assert plain_text(blocks[0]["image"]["caption"]) == "Alt text"
    seen = []

    def resolver(src):
        seen.append(src)
        return {"type": "file_upload", "file_upload": {"id": "up-1"}}

    blocks = markdown_to_blocks(md, image_resolver=resolver)
    assert seen == ["pics/a.png"] and blocks[1]["image"]["file_upload"]["id"] == "up-1"
    assert markdown_to_blocks(md, image_resolver=lambda src: None) == blocks[:1]


def test_video_lines():
    blocks = markdown_to_blocks("https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=42\nhttps://youtu.be/dQw4w9WgXcQ\n\nhttps://cdn.example/clip.mp4\n\nsee https://youtu.be/dQw4w9WgXcQ here\n")
    assert _types(blocks) == ["video", "video", "video", "paragraph"]
    assert blocks[0]["video"]["external"]["url"].endswith("t=42")
    assert blocks[3]["paragraph"]["rich_text"][1]["text"]["link"]["url"] == "https://youtu.be/dQw4w9WgXcQ"


def test_inline_markup():
    rt = inline_rich_text("**bold** and [link](https://x.y/z) and `code` and *it* and https://a.b/c. end")
    by_text = {r["text"]["content"]: r for r in rt}
    assert by_text["bold"]["annotations"]["bold"] is True and "annotations" not in by_text[" and "]
    assert by_text["link"]["text"]["link"] == {"url": "https://x.y/z"}
    assert by_text["code"]["annotations"]["code"] is True
    assert by_text["it"]["annotations"]["italic"] is True
    assert by_text["https://a.b/c"]["text"]["link"]["url"] == "https://a.b/c" and "." in by_text
    assert plain_text(rt) == "bold and link and code and it and https://a.b/c. end"
    nested = inline_rich_text("**see [the doc](https://d.e)**")
    assert nested[1]["annotations"]["bold"] is True and nested[1]["text"]["link"]["url"] == "https://d.e"
    assert plain_text(inline_rich_text("2 * 3 * 4")) == "2 * 3 * 4"
    assert plain_text(inline_rich_text("Sources: 3")) == "Sources: 3"


def test_long_text_split_into_2000_char_pieces_and_100_item_arrays():
    (para,) = markdown_to_blocks("a" * 4500)
    assert [len(r["text"]["content"]) for r in para["paragraph"]["rich_text"]] == [2000, 2000, 500]
    many = markdown_to_blocks(" ".join("**w**" for _ in range(150)))
    assert all(b["type"] == "paragraph" and len(b["paragraph"]["rich_text"]) <= 100 for b in many)
    assert len(many) >= 3 and sum(len(b["paragraph"]["rich_text"]) for b in many) == 299
    heading = markdown_to_blocks("# " + " ".join("**w**" for _ in range(60)))
    assert _types(heading) == ["heading_1", "paragraph"] and len(heading[0]["heading_1"]["rich_text"]) == 100


def test_chunk_children_respects_both_limits():
    flat = [{"object": "block", "type": "paragraph", "paragraph": {"rich_text": []}} for _ in range(250)]
    assert [len(c) for c in chunk_children(flat)] == [100, 100, 50]
    row = {"object": "block", "type": "table_row", "table_row": {"cells": [[]]}}
    tables = [{"object": "block", "type": "table", "table": {"table_width": 1, "children": [row] * 91}} for _ in range(20)]
    chunks = chunk_children(tables)
    assert [len(c) for c in chunks] == [10, 10]  # 92 blocks per table, under 1000 per request


def test_rendered_summary_converts_and_publishes(fake_notion, parent_page):
    units = [
        Unit(id="U-1", author_id="A-j", type="principle", name="Sleep first", stages=["T-rest"], aliases=["Rest first"], description="Sleep **matters**.", details="Line one.\nLine two.", notes="Key idea.", citations=[Citation(resource_id="R-old", location="12:00", quote="q")]),
        Unit(id="U-2", author_id="A-j", type="glossary", name="Circadian rhythm", stages=[], description="The body's 24 hour cycle.", citations=[Citation(resource_id="R-new", location="p. 9", quote="q")]),
    ]
    resources = [
        {"resource_id": "R-old", "title": "Old talk", "published_date": "2010", "date_precision": "year", "source_type": "youtube", "source_url": "https://youtu.be/old"},
        {"resource_id": "R-new", "title": "New book", "published_date": "2024-02-10", "date_precision": "day", "source_type": "pdf", "source_url": "", "raw_file_url": "https://files/new"},
    ]
    taxonomy = {"T-rest": {"name": "Rest", "path": "Body > Yogi > Rest", "level": "stage", "domain": "Body"}}
    md = render_summary({"author_id": "A-j", "canonical_name": "Jane"}, units, resources, taxonomy, {}, 2, [{"version": 2, "date": "2026-09-27", "note": "second"}], "2026-09-27T10:00:00Z")["summary.md"]
    blocks = markdown_to_blocks(md)
    assert blocks[0]["type"] == "paragraph" and _texts(blocks[0]).startswith("Generated by the AI Primer summary agent")
    assert "table" in _types(blocks) and "heading_3" in _types(blocks) and "bulleted_list_item" in _types(blocks)
    publisher = NotionPublisher(fake_notion)
    info = publisher.publish_markdown(md, "Summary — Jane", parent_page)
    text = fake_notion.page_plain_text(info["doc_id"])
    assert "Summary: Jane" in text and "2010 | Old talk | youtube | https://youtu.be/old" in text
    assert "Sleep first" in text and "Circadian rhythm: The body's 24 hour cycle." in text and "| 2 | 2026-09-27 | second" in text.replace("2 | 2026", "| 2 | 2026")


# --------------------------------------------------------------------------- NotionPublisher


def test_publish_creates_then_refreshes_in_place(fake_notion, parent_page):
    publisher = NotionPublisher(fake_notion)
    first = publisher.publish_markdown("# Hello\n\nfirst body", "Summary — Jane", parent_page)
    assert first["created"] is True and first["format"] == "notion" and first["name"] == "Summary — Jane"
    assert first["url"] == fake_notion.page_url(first["doc_id"])
    page = fake_notion.pages[first["doc_id"]]
    assert page["parent"]["page_id"] == parent_page and plain_text(page["properties"]["title"]["title"]) == "Summary — Jane"
    assert fake_notion.page_plain_text(first["doc_id"]) == "Hello\nfirst body"
    second = publisher.publish_markdown("# Hello again\n\nsecond body", "Renamed", parent_page, first["doc_id"])
    assert second["doc_id"] == first["doc_id"] and second["created"] is False
    assert fake_notion.page_plain_text(first["doc_id"]) == "Hello again\nsecond body"
    assert plain_text(fake_notion.pages[first["doc_id"]]["properties"]["title"]["title"]) == "Summary — Jane"  # title untouched
    assert sum(c.startswith("delete_block:") for c in fake_notion.calls) == 2


def test_mermaid_blocks_and_excalidraw_links_convert_for_notion():
    blocks = markdown_to_blocks("Drawing:\n\n```mermaid\ngraph TD\n    a[\"Body\"] --> b[\"Boxing\"]\n```\n\nhttps://excalidraw.com/#room=abc,def\n\nAfter.")
    assert [b["type"] for b in blocks] == ["paragraph", "code", "embed", "paragraph"]
    assert blocks[1]["code"]["language"] == "mermaid" and 'a["Body"] --> b["Boxing"]' in _texts(blocks[1])
    assert blocks[2]["embed"] == {"url": "https://excalidraw.com/#room=abc,def"}
    assert markdown_to_blocks("see https://excalidraw.com/ now")[0]["type"] == "paragraph"  # only a bare link on its own line embeds


def test_append_after_inserts_behind_the_anchor(fake_notion, parent_page):
    page = fake_notion.create_page({"page_id": parent_page}, {"title": {"title": [{"type": "text", "text": {"content": "P"}}]}}, children=markdown_to_blocks("one\n\ntwo"))["id"]
    first = fake_notion.list_block_children(page)[0]["id"]
    fake_notion.append_block_children(page, markdown_to_blocks("between"), after=first)
    assert fake_notion.page_plain_text(page) == "one\nbetween\ntwo"
    with pytest.raises(NotionError):
        fake_notion.append_block_children(page, markdown_to_blocks("x"), after=parent_page)


def test_refresh_keeps_the_text_above_child_pages(fake_notion, parent_page):
    publisher = NotionPublisher(fake_notion)
    node = publisher.publish_markdown("# Body\n\nold text", "Body", parent_page)["doc_id"]
    sub = fake_notion.create_page({"page_id": node}, {"title": {"title": [{"type": "text", "text": {"content": "Olympic Spartan"}}]}})["id"]
    publisher.publish_markdown("# Body\n\nnew text\n\n```mermaid\ngraph TD\n    a --> b\n```", "Body", parent_page, node)
    kinds = [b["type"] for b in fake_notion.list_block_children(node)]
    assert kinds == ["heading_1", "paragraph", "code", "child_page"], kinds
    assert fake_notion.page_plain_text(node).startswith("Body\nnew text") and sub in fake_notion.children[node]


def test_refresh_keeps_child_databases_and_child_pages(fake_notion, parent_page):
    publisher = NotionPublisher(fake_notion)
    author_page = fake_notion.create_page({"page_id": parent_page}, {"title": {"title": [{"type": "text", "text": {"content": "Jane"}}]}})["id"]
    db_id = fake_notion.create_database(author_page, "Cards", {"name": {"title": {}}})["id"]
    part = publisher.publish_markdown("# Part\n\nbody", "Summary — Jane — Body", author_page)
    publisher.publish_markdown("# Main v1\n\nold", "Summary — Jane", author_page, author_page)
    publisher.publish_markdown("# Main v2\n\nnew", "Summary — Jane", author_page, author_page)
    kinds = [b["type"] for b in fake_notion.list_block_children(author_page)]
    assert kinds.count("child_database") == 1 and kinds.count("child_page") == 1
    assert db_id in fake_notion.children[author_page] and part["doc_id"] in fake_notion.children[author_page]
    assert fake_notion.page_plain_text(author_page) == "Cards\nSummary — Jane — Body\nMain v2\nnew"
    # the part page refreshes in place too
    again = publisher.publish_markdown("# Part 2\n\nbody 2", "Summary — Jane — Body", author_page, part["doc_id"])
    assert again["doc_id"] == part["doc_id"] and fake_notion.page_plain_text(part["doc_id"]) == "Part 2\nbody 2"


def test_publish_more_than_100_blocks(fake_notion, parent_page):
    md = "\n\n".join(f"para {i}" for i in range(150))
    info = NotionPublisher(fake_notion).publish_markdown(md, "Long", parent_page)
    assert len(fake_notion.list_block_children(info["doc_id"])) == 150
    assert f"append_children:{info['doc_id']}:50" in fake_notion.calls
    assert fake_notion.page_plain_text(info["doc_id"]).splitlines()[-1] == "para 149"


def test_publish_uploads_local_images(fake_notion, parent_page, tmp_path):
    pic = tmp_path / "frame.png"
    pic.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 32)
    md = f"# Frames\n\n![Key frame]({pic})\n\n![missing]({tmp_path / 'nope.png'})\n\n![web](https://img.example/a.jpg)\n"
    info = NotionPublisher(fake_notion).publish_markdown(md, "Frames", parent_page)
    blocks = fake_notion.list_block_children(info["doc_id"])
    assert _types(blocks) == ["heading_1", "image", "image"]
    upload_id = blocks[1]["image"]["file_upload"]["id"]
    assert fake_notion.uploads[upload_id]["content_type"] == "image/png" and fake_notion.uploads[upload_id]["filename"] == "frame.png"
    assert plain_text(blocks[1]["image"]["caption"]) == "Key frame"
    assert blocks[2]["image"]["external"]["url"] == "https://img.example/a.jpg"


def test_comments_page_level_block_level_and_resolve(fake_notion, parent_page):
    publisher = NotionPublisher(fake_notion)
    info = publisher.publish_markdown("# Title\n\nThe jab is a straight punch.\n\n- item one\n", "Doc", parent_page)
    doc_id = info["doc_id"]
    blocks = fake_notion.list_block_children(doc_id)
    page_comment = fake_notion.add_comment(doc_id, "Please add a section on footwork.")
    block_comment = fake_notion.add_comment(doc_id, "Not straight, it snaps.", author="Coach", block_id=blocks[1]["id"])
    fake_notion.add_comment(doc_id, "Agreed.", author="Jules", discussion_id=block_comment["discussion_id"])
    items = publisher.unresolved_comments(doc_id)
    assert [i["comment"] for i in items] == ["Please add a section on footwork.", "Not straight, it snaps."]
    assert items[0]["author"] == "Jules" and items[0]["quoted"] == "" and items[0]["replies"] == []
    assert items[0]["comment_id"] == page_comment["id"] and items[0]["discussion_id"] == page_comment["discussion_id"]
    assert items[1]["quoted"] == "The jab is a straight punch." and items[1]["author"] == "Coach" and items[1]["replies"] == ["Agreed."]
    assert items[1]["created"] and items[1]["comment_id"] == block_comment["id"]
    # resolve by comment id: a reply lands in that discussion and the discussion drops out of the list
    publisher.resolve(doc_id, block_comment["id"])
    thread = [c for c in fake_notion.comments_for(doc_id) if c["discussion_id"] == block_comment["discussion_id"]]
    assert plain_text(thread[-1]["rich_text"]) == NotionPublisher.RESOLVED_REPLY and thread[-1]["parent"]["block_id"] == blocks[1]["id"]
    assert [i["comment"] for i in publisher.unresolved_comments(doc_id)] == ["Please add a section on footwork."]
    # resolve by discussion id works too, and a later human reply reopens the thread
    publisher.resolve(doc_id, page_comment["discussion_id"])
    assert publisher.unresolved_comments(doc_id) == []
    fake_notion.add_comment(doc_id, "Still missing.", discussion_id=page_comment["discussion_id"])
    reopened = publisher.unresolved_comments(doc_id)
    assert len(reopened) == 1 and reopened[0]["replies"][-1] == "Still missing."


def test_comment_scan_limit(fake_notion, parent_page):
    info = NotionPublisher(fake_notion).publish_markdown("one\n\ntwo\n\nthree\n", "Doc", parent_page)
    blocks = fake_notion.list_block_children(info["doc_id"])
    fake_notion.add_comment(info["doc_id"], "late", block_id=blocks[2]["id"])
    assert NotionPublisher(fake_notion, scan_blocks_for_comments=2).unresolved_comments(info["doc_id"]) == []
    fake_notion.calls.clear()
    assert len(NotionPublisher(fake_notion, scan_blocks_for_comments=300).unresolved_comments(info["doc_id"])) == 1
    assert sum(c.startswith("list_comments:") for c in fake_notion.calls) == 4  # the page + 3 blocks


# --------------------------------------------------------------------------- RealNotionBackend


class StubResponse:
    def __init__(self, status_code, body=None, headers=None):
        self.status_code = status_code
        self._body = body if body is not None else {}
        self.headers = headers or {}

    def json(self):
        return self._body

    @property
    def text(self):
        return json.dumps(self._body)


class StubSession:
    def __init__(self, responses=None):
        self.responses = list(responses or [])
        self.requests: list[tuple[str, str, dict]] = []

    def request(self, method, url, **kw):
        self.requests.append((method, url, kw))
        return self.responses.pop(0) if self.responses else StubResponse(200, {"results": [], "has_more": False})


def _backend(session, token=None, **kw):
    sleeps: list[float] = []
    backend = RealNotionBackend(token, session=session, min_interval=0, sleep=sleeps.append, **kw)
    return backend, sleeps


def test_headers_with_and_without_token():
    session = StubSession([StubResponse(200, {"id": "p1"})])
    backend, _ = _backend(session, token="secret_token_value")
    backend.retrieve_page("p1")
    method, url, kw = session.requests[0]
    assert (method, url) == ("GET", "https://api.notion.com/v1/pages/p1")
    assert kw["headers"]["Authorization"] == "Bearer secret_token_value" and kw["headers"]["Notion-Version"] == "2022-06-28"
    session = StubSession([StubResponse(200, {"id": "p1"})])
    backend, _ = _backend(session, token=None, version="2025-09-03")
    backend.retrieve_page("p1")
    headers = session.requests[0][2]["headers"]
    assert "Authorization" not in headers and headers["Notion-Version"] == "2025-09-03"


def test_from_env_reads_token(monkeypatch):
    monkeypatch.setenv("NOTION_TOKEN", "from-env")
    session = StubSession([StubResponse(200, {"id": "p1"})])
    RealNotionBackend.from_env(session=session, min_interval=0).retrieve_page("p1")
    assert session.requests[0][2]["headers"]["Authorization"] == "Bearer from-env"
    monkeypatch.delenv("NOTION_TOKEN")
    assert RealNotionBackend.from_env(session=session)._token is None


def test_query_database_paginates():
    session = StubSession([
        StubResponse(200, {"results": [{"id": "a"}], "has_more": True, "next_cursor": "cur-1"}),
        StubResponse(200, {"results": [{"id": "b"}], "has_more": False, "next_cursor": None}),
    ])
    backend, _ = _backend(session)
    pages = backend.query_database("db1", filter={"property": "resource_id", "title": {"equals": "R-1"}})
    assert [p["id"] for p in pages] == ["a", "b"]
    first, second = session.requests
    assert first[0] == "POST" and first[1].endswith("/databases/db1/query")
    assert first[2]["json"] == {"filter": {"property": "resource_id", "title": {"equals": "R-1"}}, "page_size": 100}
    assert second[2]["json"]["start_cursor"] == "cur-1"
    # GET pagination passes the cursor as a query parameter
    session = StubSession([
        StubResponse(200, {"results": [{"id": "c1"}], "has_more": True, "next_cursor": "n2"}),
        StubResponse(200, {"results": [{"id": "c2"}], "has_more": False}),
    ])
    backend, _ = _backend(session)
    assert [c["id"] for c in backend.list_comments("blk")] == ["c1", "c2"]
    assert session.requests[0][2]["params"] == {"block_id": "blk", "page_size": 100}
    assert session.requests[1][2]["params"]["start_cursor"] == "n2"


def test_rate_limit_and_server_errors_are_retried():
    session = StubSession([StubResponse(429, {"code": "rate_limited"}, {"Retry-After": "2"}), StubResponse(200, {"id": "p1"})])
    backend, sleeps = _backend(session)
    assert backend.retrieve_page("p1")["id"] == "p1"
    assert sleeps == [2.0] and len(session.requests) == 2
    session = StubSession([StubResponse(429, {}), StubResponse(502, {}), StubResponse(503, {}), StubResponse(200, {"id": "p2"})])
    backend, sleeps = _backend(session)
    assert backend.retrieve_page("p2")["id"] == "p2"
    assert sleeps == [1.0, 1.0, 2.0]
    session = StubSession([StubResponse(500, {})] * 6)
    backend, sleeps = _backend(session)
    with pytest.raises(NotionError) as exc:
        backend.retrieve_page("p3")
    assert exc.value.status == 500 and len(session.requests) == 6


def test_api_errors_raise_without_the_token():
    session = StubSession([StubResponse(400, {"code": "validation_error", "message": "body failed validation"})])
    backend, _ = _backend(session, token="secret_token_value")
    with pytest.raises(NotionError) as exc:
        backend.create_page({"page_id": "x"}, {})
    assert exc.value.status == 400 and exc.value.code == "validation_error"
    assert "body failed validation" in str(exc.value) and "secret_token_value" not in str(exc.value)


class FakeClock:
    def __init__(self):
        self.now = 0.0
        self.sleeps: list[float] = []

    def sleep(self, seconds):
        self.sleeps.append(round(seconds, 6))
        self.now += seconds


def test_throttle_spaces_requests():
    clock = FakeClock()
    session = StubSession([StubResponse(200, {}), StubResponse(200, {}), StubResponse(200, {}), StubResponse(200, {})])
    backend = RealNotionBackend(None, session=session, min_interval=0.34, sleep=clock.sleep, clock=lambda: clock.now)
    backend.retrieve_page("a")  # first call: no wait
    clock.now += 0.1
    backend.retrieve_page("b")  # 0.1 s later: waits the remaining 0.24 s
    clock.now += 0.5
    backend.retrieve_page("c")  # long after: no wait
    backend.retrieve_page("d")  # right away: full interval
    assert clock.sleeps == [0.24, 0.34]


def test_append_block_children_chunks_by_100():
    session = StubSession([StubResponse(200, {"results": [{"id": f"b{i}"}]}) for i in range(3)])
    backend, _ = _backend(session)
    blocks = [{"object": "block", "type": "paragraph", "paragraph": {"rich_text": []}} for _ in range(250)]
    created = backend.append_block_children("page1", blocks)
    assert [len(r[2]["json"]["children"]) for r in session.requests] == [100, 100, 50]
    assert all(r[0] == "PATCH" and r[1].endswith("/blocks/page1/children") for r in session.requests)
    assert [c["id"] for c in created] == ["b0", "b1", "b2"]


def test_write_calls_have_the_documented_shapes(tmp_path):
    session = StubSession([
        StubResponse(200, {"id": "db1"}),
        StubResponse(200, {"id": "pg1"}),
        StubResponse(200, {"id": "c1"}),
        StubResponse(200, {"id": "up1", "upload_url": "https://api.notion.com/v1/file_uploads/up1/send"}),
        StubResponse(200, {"id": "up1", "status": "uploaded"}),
        StubResponse(200, {"id": "pg1", "archived": True}),
        StubResponse(200, {"id": "blk"}),
    ])
    backend, _ = _backend(session)
    backend.create_database("parent", "Resources", {"resource_id": {"title": {}}, "title": {"rich_text": {}}})
    backend.create_page({"database_id": "db1"}, {"resource_id": {"title": [{"type": "text", "text": {"content": "R-1"}}]}}, icon={"type": "emoji", "emoji": "x"})
    backend.create_comment(discussion_id="d1", text="Applied.")
    pic = tmp_path / "a.png"
    pic.write_bytes(b"png")
    assert backend.upload_file(pic, "image/png") == "up1"
    backend.update_page("pg1", archived=True)
    backend.delete_block("blk")
    reqs = session.requests
    assert reqs[0][2]["json"]["parent"] == {"type": "page_id", "page_id": "parent"} and plain_text(reqs[0][2]["json"]["title"]) == "Resources"
    assert reqs[1][2]["json"]["icon"] == {"type": "emoji", "emoji": "x"} and "children" not in reqs[1][2]["json"]
    assert reqs[2][2]["json"] == {"discussion_id": "d1", "rich_text": [{"type": "text", "text": {"content": "Applied.", "link": None}}]}
    assert reqs[3][2]["json"] == {"mode": "single_part", "filename": "a.png", "content_type": "image/png"}
    assert reqs[4][1].endswith("/file_uploads/up1/send") and reqs[4][2]["json"] is None
    assert reqs[4][2]["files"]["file"] == ("a.png", b"png", "image/png")
    assert reqs[5][2]["json"] == {"archived": True}
    assert reqs[6][0] == "DELETE" and reqs[6][1].endswith("/blocks/blk")
    assert backend.page_url("0123-4567") == "https://www.notion.so/01234567"
