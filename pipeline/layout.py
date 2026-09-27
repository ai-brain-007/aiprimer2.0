"""How the "AI Primer" page is organised in Notion (decided by the owner on 2026-09-27).

Under "AI Primer", one page per layer, in this order, each holding the registry tables that belong to it:

    LAYER 1 - RAW MATERIAL                Resources
    LAYER 2 - SUMMARY BY AUTHORS          Authors, Summaries
    LAYER 3 - DOMAINS > PRIMERS > STAGES  one page per domain > primer > stage, generated from the Taxonomy table
    LAYER 0 - CONFIG                      Accounts, Taxonomy, Folders, Jobs

The two guide pages ("Command guide", "How the pipeline works") live inside LAYER 0 - CONFIG (`guide_home`);
`doc guide` publishes them there and moves a copy left at the top by an earlier version. A "WHITEBOARD" page with
an embedded drawing board (config `notion.whiteboard_url`, Excalidraw by default) completes the top level.
`apply_layout` is idempotent: it creates what is missing, moves a table that sits in the wrong place (copy rows,
verify, archive the old one: the API cannot move databases) and keeps the tree pages named after the nodes.
Everything here is deterministic; no model is called.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .context import AppContext
from .models import TaxonomyNode
from .notion import CONTAINER_BLOCK_TYPES, NotionRepo, child_pages, markdown_to_blocks, rich_text


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
WHITEBOARD_TITLE = "WHITEBOARD"
WHITEBOARD_INTRO = (
    "Your sketchpad: boxes, arrows and notes, as on a whiteboard. What you draw on the plain Excalidraw board is kept "
    "in this browser only, so it stays on this computer and the pipeline cannot read it. To keep a drawing, use "
    "Excalidraw's menu: Save to disk, or Live collaboration to get a link that can be pinned here instead."
)


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


def _node_body(node: TaxonomyNode, path: str) -> str:
    if node.level == "domain":
        return "**Domain.** Its primers are the pages inside this one. Each primer holds stages, and each stage page will hold the study material built from the authors' cards."
    if node.level == "primer":
        return f"**Primer.** {path}\n\nIts stages are the pages inside this one."
    return (
        f"**Stage.** {path}\n\nNothing is built for this stage yet. When the learning layer runs, this page will hold the brief, "
        "procedures, drills, lists, scripts, glossary and self-test built from all authors' cards. The resources filed here "
        "are listed in LAYER 1 - RAW MATERIAL, table Resources."
    )


def sync_tree_pages(ctx: AppContext) -> dict[str, Any]:
    """One page per taxonomy node under "LAYER 3 - DOMAINS > PRIMERS > STAGES", nested domain > primer > stage.
    Creates missing pages, adopts a hand-made page of the same title, renames a page whose node was renamed, and
    stores page id and link on the Taxonomy row. Nothing is deleted: an archived node keeps its page."""
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
    listings: dict[str, dict[str, str]] = {}  # parent page -> {title: page id}

    def children_of(page_id: str) -> dict[str, str]:
        if page_id not in listings:
            listings[page_id] = child_pages(backend, page_id)
        return listings[page_id]

    def visit(node: TaxonomyNode, parent_page: str) -> None:
        if node.status == "archived":
            return
        siblings = children_of(parent_page)
        by_id = {pid: title for title, pid in siblings.items()}
        page_id = node.page_id if node.page_id in by_id else ""
        if not page_id and node.name in siblings:
            page_id = siblings[node.name]
            adopted.append(node.path or node.name)
        if not page_id:
            page = backend.create_page({"page_id": parent_page}, {"title": {"title": rich_text(node.name)}}, children=markdown_to_blocks(_node_body(node, tax.path(node.node_id))))
            page_id = page["id"]
            siblings[node.name] = page_id
            created.append(tax.path(node.node_id))
        elif by_id.get(page_id) != node.name:
            backend.update_page(page_id, properties={"title": {"title": rich_text(node.name)}})
            renamed.append(f"{by_id.get(page_id)} -> {node.name}")
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
    return {"root": backend.page_url(root), "nodes": len([n for n in tax.by_id.values() if n.status != "archived"]), "created": created, "renamed": renamed, "adopted": adopted}


def ensure_whiteboard(repo: NotionRepo, url: str) -> dict[str, Any]:
    """The WHITEBOARD page under the parent page with `url` embedded. Created when missing; when the configured
    board changes, the old embed is replaced on the same page (same link)."""
    backend = repo.backend
    page_id = child_pages(backend, repo.parent_page_id).get(WHITEBOARD_TITLE)
    embed = {"object": "block", "type": "embed", "embed": {"url": url}}
    if page_id is None:
        page = backend.create_page({"page_id": repo.parent_page_id}, {"title": {"title": rich_text(WHITEBOARD_TITLE)}}, children=[*markdown_to_blocks(WHITEBOARD_INTRO), embed])
        return {"page_id": page["id"], "url": backend.page_url(page["id"]), "embed_url": url, "created": True, "updated": False}
    embeds = [b for b in backend.list_block_children(page_id) if b.get("type") == "embed"]
    current = [((b.get("embed") or {}).get("url") or "") for b in embeds]
    updated = False
    if current != [url]:
        for b in embeds:
            backend.delete_block(b["id"])
        backend.append_block_children(page_id, [embed])
        updated = True
    return {"page_id": page_id, "url": backend.page_url(page_id), "embed_url": url, "created": False, "updated": updated}


def apply_layout(ctx: AppContext) -> dict[str, Any]:
    """Create the layer pages, put every registry table in its layer (moving it if it sits elsewhere) and build
    the Domain > Primer > Stage pages. Idempotent."""
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
    board_url = str(ctx.settings.notion.get("whiteboard_url") or "").strip()
    return {
        "parent_url": repo.url(),
        "sections": {title: {"page_id": pid, "url": repo.backend.page_url(pid), "created": title in sections["created"]} for title, pid in sections["pages"].items()},
        "tables": {tab: {"home": home_titles()[tab], "created": tab in tabs_report["created"], "moved": moved.get(tab)} for tab in homes},
        "tree": sync_tree_pages(ctx),
        "whiteboard": ensure_whiteboard(repo, board_url) if board_url else {"skipped": "notion.whiteboard_url is empty"},
    }
