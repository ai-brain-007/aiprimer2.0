"""pipeline.layout: layer pages under "AI Primer", tables inside their layer, Domain > Primer > Stage pages."""

from __future__ import annotations

import pytest

from pipeline.context import AppContext
from pipeline.layout import SECTIONS, TREE_SECTION, apply_layout, home_titles, sync_tree_pages, table_homes
from pipeline.models import TAB_MODELS
from pipeline.notion import NotionError, NotionRepo, child_pages, plain_text
from pipeline.registry import Registry
from pipeline.setup_cmds import init_sheet
from pipeline.taxonomy import add_node, rename_node
from tests.fake_notion import FakeNotionBackend

LAYER_TITLES = [s.title for s in SECTIONS]


@pytest.fixture
def world(settings, monkeypatch):
    monkeypatch.setenv("AIPRIMER_NOTION_PAGE_ID", "0123456789abcdef0123456789abcdef")
    notion = FakeNotionBackend()
    parent = notion.add_page("AI Primer")
    repo = NotionRepo(notion, parent)
    reg = Registry(repo, settings)
    ctx = AppContext(settings, registry=reg, notion_backend=notion)
    return ctx, reg, repo, notion, parent


def _small_tree(reg):
    body = add_node(reg, "domain", "Body")
    spartan = add_node(reg, "primer", "Olympic Spartan", body.node_id)
    boxing = add_node(reg, "stage", "Boxing", spartan.node_id)
    add_node(reg, "stage", "Muay Thai", spartan.node_id)
    mind = add_node(reg, "domain", "Mind")
    return body, spartan, boxing, mind


def _layers(notion, parent):
    return {t: pid for t, pid in child_pages(notion, parent).items() if t in LAYER_TITLES}


def test_layout_definition_covers_every_table_once():
    homes = home_titles()
    assert set(homes) == set(TAB_MODELS)
    assert homes["Resources"] == "LAYER 1 - RAW MATERIAL" and homes["Authors"] == homes["Summaries"] == "LAYER 2 - SUMMARY BY AUTHORS"
    assert {homes[t] for t in ("Accounts", "Taxonomy", "Folders", "Jobs")} == {"LAYER 0 - CONFIG"}
    assert LAYER_TITLES[-1] == "LAYER 0 - CONFIG" and LAYER_TITLES[2] == TREE_SECTION  # config last, as the owner asked


def test_fresh_setup_creates_the_tables_inside_their_layer_pages(world):
    ctx, reg, repo, notion, parent = world
    report = init_sheet(ctx)
    assert report["tabs"]["created"] == list(TAB_MODELS)
    layers = _layers(notion, parent)
    assert list(layers) == LAYER_TITLES  # created in this order, so the sidebar shows them in this order
    assert notion.child_databases(parent) == {}
    for tab, title in home_titles().items():
        assert repo.database_id(tab) == notion.child_databases(layers[title])[tab]
        assert repo.database_parent(tab) == layers[title]
    # the intro paragraph was written on each layer page
    assert "Backblaze" in notion.page_plain_text(layers["LAYER 1 - RAW MATERIAL"])
    assert table_homes(ctx) == {tab: layers[title] for tab, title in home_titles().items()}


def test_apply_layout_moves_flat_tables_with_their_rows_and_builds_the_tree(world):
    ctx, reg, repo, notion, parent = world
    repo.ensure_tabs()  # the layout of the first live run: seven tables directly under "AI Primer"
    body, spartan, boxing, mind = _small_tree(reg)
    old_tax = repo.database_id("Taxonomy")
    assert set(notion.child_databases(parent)) == set(TAB_MODELS)

    report = apply_layout(ctx)

    layers = _layers(notion, parent)
    assert [t for t, s in report["sections"].items() if s["created"]] == LAYER_TITLES
    assert notion.child_databases(parent) == {}, "the old top-level tables are archived"
    assert notion.databases[old_tax]["archived"] is True
    for tab, title in home_titles().items():
        assert repo.database_parent(tab) == layers[title]
        assert report["tables"][tab]["moved"]["old_id"] != report["tables"][tab]["moved"]["new_id"]
    assert report["tables"]["Taxonomy"]["moved"]["rows"] == 5 and report["tables"]["Resources"]["moved"]["rows"] == 0
    nodes = {n.node_id: n for n in reg.nodes()}
    assert set(nodes) == {body.node_id, spartan.node_id, boxing.node_id, mind.node_id, *(n.node_id for n in reg.nodes() if n.name == "Muay Thai")}
    assert nodes[boxing.node_id].path == "Body / Olympic Spartan / Boxing"

    # the tree: LAYER 3 > Body > Olympic Spartan > Boxing, Muay Thai; Mind
    tree = report["tree"]
    assert tree["nodes"] == 5 and len(tree["created"]) == 5 and tree["renamed"] == []
    root = layers[TREE_SECTION]
    domains = child_pages(notion, root)
    assert set(domains) == {"Body", "Mind"}
    primers = child_pages(notion, domains["Body"])
    stages = child_pages(notion, primers["Olympic Spartan"])
    assert set(stages) == {"Boxing", "Muay Thai"}
    assert nodes[boxing.node_id].page_id == stages["Boxing"] and nodes[boxing.node_id].page_url == notion.page_url(stages["Boxing"])
    assert "Body / Olympic Spartan / Boxing" in notion.page_plain_text(stages["Boxing"]) and "Nothing is built" in notion.page_plain_text(stages["Boxing"])

    # idempotent: a second run creates and moves nothing
    again = apply_layout(ctx)
    assert not any(s["created"] for s in again["sections"].values())
    assert all(t["moved"] is None and not t["created"] for t in again["tables"].values())
    assert again["tree"]["created"] == [] and again["tree"]["renamed"] == [] and again["tree"]["adopted"] == []
    assert set(child_pages(notion, root)) == {"Body", "Mind"} and len(reg.nodes()) == 5


def test_tree_pages_follow_renames_additions_and_hand_made_pages(world):
    ctx, reg, repo, notion, parent = world
    init_sheet(ctx)
    body, spartan, boxing, mind = _small_tree(reg)
    first = sync_tree_pages(ctx)
    assert len(first["created"]) == 5
    root = _layers(notion, parent)[TREE_SECTION]
    boxing_page = reg.node(boxing.node_id).page_id

    rename_node(reg, boxing.node_id, "Boxing Fundamentals", lambda a: None)
    second = sync_tree_pages(ctx)
    assert second["renamed"] == ["Boxing -> Boxing Fundamentals"] and second["created"] == []
    assert plain_text(notion.pages[boxing_page]["properties"]["title"]["title"]) == "Boxing Fundamentals"
    assert reg.node(boxing.node_id).page_id == boxing_page  # same page, same link

    clinch = add_node(reg, "stage", "Clinch", spartan.node_id)
    third = sync_tree_pages(ctx)
    assert third["created"] == ["Body / Olympic Spartan / Clinch"] and reg.node(clinch.node_id).page_id

    # a page the owner made by hand under the right parent is adopted, not duplicated
    people = add_node(reg, "domain", "People")
    hand_made = notion.create_page({"page_id": root}, {"title": {"title": [{"type": "text", "text": {"content": "People"}}]}})["id"]
    fourth = sync_tree_pages(ctx)
    assert fourth["adopted"] == ["People"] and reg.node(people.node_id).page_id == hand_made and fourth["created"] == []
    assert set(child_pages(notion, root)) == {"Body", "Mind", "People"}


def test_relocate_tab_rolls_back_when_the_copy_is_incomplete(world, monkeypatch):
    ctx, reg, repo, notion, parent = world
    repo.ensure_tabs()
    _small_tree(reg)
    old = repo.database_id("Taxonomy")
    target = notion.create_page({"page_id": parent}, {"title": {"title": [{"type": "text", "text": {"content": "LAYER 0 - CONFIG"}}]}})["id"]
    real_query = notion.query_database
    monkeypatch.setattr(notion, "query_database", lambda db, filter=None: [] if db != old else real_query(db, filter))
    with pytest.raises(NotionError, match="copied 0 of 5"):
        repo.relocate_tab("Taxonomy", target)
    assert repo.database_id("Taxonomy") == old and notion.databases[old]["archived"] is False
    assert notion.child_databases(target) == {}, "the incomplete copy was archived"
    assert len(reg.nodes()) == 5


def test_layout_is_skipped_outside_notion_mode(settings, fake_sheets):
    from pipeline.sheets import SheetsRepo

    ctx = AppContext(settings, registry=Registry(SheetsRepo(fake_sheets), settings))
    assert apply_layout(ctx) == {"skipped": "not in Notion mode"} and table_homes(ctx) is None
