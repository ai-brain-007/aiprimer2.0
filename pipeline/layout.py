"""How the "AI Primer" page is organised in Notion (decided by the owner on 2026-09-27).

Under "AI Primer", one page per layer, in this order, each holding the registry tables that belong to it:

    LAYER 1 - RAW MATERIAL                Resources
    LAYER 2 - SUMMARY BY AUTHORS          Authors, Summaries
    LAYER 3 - DOMAINS > PRIMERS > STAGES  one page per domain > primer > stage, generated from the Taxonomy table
    LAYER 0 - CONFIG                      Accounts, Taxonomy, Folders, Jobs, and the two guide pages

The two guide pages ("Command guide", "How the pipeline works") live inside LAYER 0 - CONFIG (`guide_home`);
`doc guide` publishes them there and moves a copy left at the top by an earlier version.

Drawings belong to the tree pages, not to a page of their own: every domain and primer page carries a diagram of
its part of the tree (a Mermaid code block, which Notion renders), redrawn by `sync_tree_pages` when the tree
changes. A hand-drawn Excalidraw board is pinned into a page by putting its link in the page's markdown.

`apply_layout` is idempotent: it creates what is missing, moves a table that sits in the wrong place (copy rows,
verify, archive the old one: the API cannot move databases) and keeps the tree pages named after the nodes.
Everything here is deterministic; no model is called.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal

from .context import AppContext
from .models import TaxonomyNode
from .notion import NotionPublisher, NotionRepo, child_pages, markdown_to_blocks, rich_text


@dataclass(frozen=True)
class Section:
    title: str
    tabs: tuple[str, ...]
    intro: str


SECTIONS: tuple[Section, ...] = (
    Section(
        "LAYER 1 - RAW MATERIAL",
        ("Resources",),
        "Everything you have added, as it came in. The Resources table lists each video, file or screenshot with its "
        "author, its place in the tree, its dates, how it was extracted and the link to the stored original. The files "
        "themselves live in Backblaze, not in Notion.",
    ),
    Section(
        "LAYER 2 - SUMMARY BY AUTHORS",
        ("Authors", "Summaries"),
        "One page per author, built from that author's verified knowledge cards. Open the Authors table and click a row "
        "to read the author's page. The Summaries table is the version history: one row each time a page was published.",
    ),
    Section(
        "LAYER 3 - DOMAINS > PRIMERS > STAGES",
        (),
        "Your tree of categories as pages: a page per domain, inside it a page per primer, inside that a page per stage. "
        "The stage pages will hold the study material built from all authors' cards: brief, procedures, drills, lists, "
        "scripts, glossary and self-test. Until the learning layer is built they only show their place in the tree.",
    ),
    Section(
        "LAYER 0 - CONFIG",
        ("Accounts", "Taxonomy", "Folders", "Jobs"),
        "The pipeline's own bookkeeping. You rarely need these tables: Accounts (storage accounts and their free space), "
        "Taxonomy (the tree, one row per node), Folders (storage folders mapped to the tree) and Jobs (a log of every "
        "command that ran).",
    ),
)
TREE_SECTION = SECTIONS[2].title
GUIDES_SECTION = SECTIONS[3].title
Refresh = Literal["changed", "all", "none"]


def home_titles() -> dict[str, str]:
    """Registry table -> title of the layer page it belongs in."""
    return {tab: s.title for s in SECTIONS for tab in s.tabs}


def _notion_repo(ctx: AppContext) -> NotionRepo | None:
    repo = ctx.registry.repo
    return repo if isinstance(repo, NotionRepo) else None


def ensure_sections(repo: NotionRepo) -> dict[str, Any]:
    """The layer pages under the parent page, created when missing (intro text written once, at creation)."""
    existing = child_pages(repo.backend, repo.parent_page_id)
    created: list[str] = []
    pages: dict[str, str] = {}
    for section in SECTIONS:
        page_id = existing.get(section.title)
        if page_id is None:
            page = repo.backend.create_page({"page_id": repo.parent_page_id}, {"title": {"title": rich_text(section.title)}}, children=markdown_to_blocks(section.intro))
            page_id = page["id"]
            created.append(section.title)
        pages[section.title] = page_id
    return {"pages": pages, "created": created}


def guide_home(ctx: AppContext) -> str:
    """Page id of the layer page that holds the owner's guides (LAYER 0 - CONFIG), created if missing."""
    repo = _notion_repo(ctx)
    if repo is None:
        raise RuntimeError("Notion mode is off: AIPRIMER_NOTION_PAGE_ID is not set (see README.md, Setup)")
    return ensure_sections(repo)["pages"][GUIDES_SECTION]


def table_homes(ctx: AppContext) -> dict[str, str] | None:
    """{table: layer page id} in Notion mode (creating the layer pages if needed); None otherwise."""
    repo = _notion_repo(ctx)
    if repo is None:
        return None
    pages = ensure_sections(repo)["pages"]
    return {tab: pages[title] for tab, title in home_titles().items()}


# ---------------------------------------------------------------------------- tree pages and their diagrams


def _mid(node: TaxonomyNode) -> str:
    return "n_" + re.sub(r"[^A-Za-z0-9_]", "_", node.node_id)


def _label(name: str) -> str:
    return name.replace('"', "'")


def tree_diagram(tax: Any, root: TaxonomyNode) -> str:
    """A Mermaid flowchart of `root` and everything below it (Notion renders it as a diagram)."""
    lines = ["graph TD", f'    {_mid(root)}["{_label(root.name)}"]']

    def walk(n: TaxonomyNode) -> None:
        for c in tax.children(n.node_id):
            if c.status == "archived":
                continue
            lines.append(f'    {_mid(n)} --> {_mid(c)}["{_label(c.name)}"]')
            walk(c)

    walk(root)
    return "```mermaid\n" + "\n".join(lines) + "\n```"


def node_body(tax: Any, node: TaxonomyNode) -> str:
    """The markdown of a node's page: what it is, its place, and (domain, primer) the diagram of its subtree.
    Written at creation and rewritten by `sync_tree_pages` when the tree changes."""
    path = tax.path(node.node_id)
    if node.level == "domain":
        return (
            "**Domain.** Its primers are the pages inside this one. Each primer holds stages, and each stage page will "
            "hold the study material built from the authors' cards.\n\n" + tree_diagram(tax, node)
        )
    if node.level == "primer":
        return f"**Primer.** {path}\n\nIts stages are the pages inside this one.\n\n" + tree_diagram(tax, node)
    chain = [*tax.ancestors(node.node_id), node]
    where = "```mermaid\ngraph LR\n" + "\n".join(f'    {_mid(a)}["{_label(a.name)}"]' + (" --> " + _mid(chain[i + 1]) if i + 1 < len(chain) else "") for i, a in enumerate(chain)) + "\n```"
    return (
        f"**Stage.** {path}\n\nNothing is built for this stage yet. When the learning layer runs, this page will hold the brief, "
        "procedures, drills, lists, scripts, glossary and self-test built from all authors' cards. The resources filed here "
        "are listed in LAYER 1 - RAW MATERIAL, table Resources.\n\n" + where
    )


def sync_tree_pages(ctx: AppContext, refresh: Refresh = "changed") -> dict[str, Any]:
    """One page per taxonomy node under "LAYER 3 - DOMAINS > PRIMERS > STAGES", nested domain > primer > stage.
    Creates missing pages, adopts a hand-made page of the same title, renames a page whose node was renamed, stores
    page id and link on the Taxonomy row, and rewrites the bodies (text and diagram) of the pages the change affects:
    `refresh="changed"` the changed nodes with their ancestors and descendants, `"all"` every page, `"none"` no body.
    Nothing is deleted: an archived node keeps its page."""
    repo = _notion_repo(ctx)
    if repo is None:
        return {"skipped": "not in Notion mode"}
    from .taxonomy import load_taxonomy

    backend = repo.backend
    root = ensure_sections(repo)["pages"][TREE_SECTION]
    tax = load_taxonomy(ctx.registry)
    created: list[str] = []
    renamed: list[str] = []
    adopted: list[str] = []
    changed: list[TaxonomyNode] = []
    touched: set[str] = set()  # node ids whose page body must be rewritten
    fresh: set[str] = set()  # node ids created now (body already written)
    parent_page_of: dict[str, str] = {}
    listings: dict[str, dict[str, str]] = {}  # parent page -> {title: page id}

    def children_of(page_id: str) -> dict[str, str]:
        if page_id not in listings:
            listings[page_id] = child_pages(backend, page_id)
        return listings[page_id]

    def visit(node: TaxonomyNode, parent_page: str) -> None:
        if node.status == "archived":
            return
        parent_page_of[node.node_id] = parent_page
        siblings = children_of(parent_page)
        by_id = {pid: title for title, pid in siblings.items()}
        page_id = node.page_id if node.page_id in by_id else ""
        if not page_id and node.name in siblings:
            page_id = siblings[node.name]
            adopted.append(node.path or node.name)
            touched.add(node.node_id)
        if not page_id:
            page = backend.create_page({"page_id": parent_page}, {"title": {"title": rich_text(node.name)}}, children=markdown_to_blocks(node_body(tax, node)))
            page_id = page["id"]
            siblings[node.name] = page_id
            created.append(tax.path(node.node_id))
            fresh.add(node.node_id)
            touched.add(node.node_id)
        elif by_id.get(page_id) != node.name:
            backend.update_page(page_id, properties={"title": {"title": rich_text(node.name)}})
            renamed.append(f"{by_id.get(page_id)} -> {node.name}")
            touched.add(node.node_id)
        url = backend.page_url(page_id)
        if node.page_id != page_id or node.page_url != url:
            node.page_id, node.page_url = page_id, url
            changed.append(node)
        for child in tax.children(node.node_id):
            visit(child, page_id)

    for domain in tax.domains():
        visit(domain, root)
    if changed:
        ctx.registry.upsert_nodes(changed)

    live = {n.node_id for n in tax.by_id.values() if n.status != "archived" and n.page_id}
    if refresh == "all":
        to_refresh = set(live)
    elif refresh == "changed":
        to_refresh = set()
        for nid in touched:
            to_refresh.add(nid)
            to_refresh.update(a.node_id for a in tax.ancestors(nid))
            to_refresh.update(d.node_id for d in tax.descendants(nid))
        to_refresh &= live
    else:
        to_refresh = set()
    to_refresh -= fresh
    publisher = NotionPublisher(backend, scan_blocks_for_comments=0)
    refreshed: list[str] = []
    for node in sorted((tax.by_id[nid] for nid in to_refresh), key=lambda n: tax.path(n.node_id)):
        publisher.publish_markdown(node_body(tax, node), node.name, parent_page_of.get(node.node_id, root), node.page_id)
        refreshed.append(tax.path(node.node_id))
    return {"root": backend.page_url(root), "nodes": len(live), "created": created, "renamed": renamed, "adopted": adopted, "refreshed": refreshed}


def apply_layout(ctx: AppContext, refresh_pages: bool = False) -> dict[str, Any]:
    """Create the layer pages, put every registry table in its layer (moving it if it sits elsewhere) and build
    the Domain > Primer > Stage pages (`refresh_pages` rewrites every tree page's body). Idempotent."""
    repo = _notion_repo(ctx)
    if repo is None:
        return {"skipped": "not in Notion mode"}
    sections = ensure_sections(repo)
    homes = {tab: sections["pages"][title] for tab, title in home_titles().items()}
    tabs_report = repo.ensure_tabs(homes)
    moved: dict[str, Any] = {}
    for tab, home in homes.items():
        if repo.database_parent(tab) != home:
            moved[tab] = repo.relocate_tab(tab, home)
    ctx.registry.repo.invalidate()
    return {
        "parent_url": repo.url(),
        "sections": {title: {"page_id": pid, "url": repo.backend.page_url(pid), "created": title in sections["created"]} for title, pid in sections["pages"].items()},
        "tables": {tab: {"home": home_titles()[tab], "created": tab in tabs_report["created"], "moved": moved.get(tab)} for tab in homes},
        "tree": sync_tree_pages(ctx, "all" if refresh_pages else "changed"),
    }
