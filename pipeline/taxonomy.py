"""Domain > Primer > Stage tree: sheet rows <-> Drive folders.

The Taxonomy tab is the source of truth. Nodes are referenced by permanent `node_id`; the readable
`path` ("Body / Immortal Yogi / Rest Body") is denormalized and refreshed on rename. Drive folders
are recorded per (node, account) in the Folders tab so a stage may live in several accounts.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Callable

from rapidfuzz import fuzz

from .drive import DriveClient, folder_url
from .ids import new_node_id, now_iso, slugify
from .models import Account, FolderMap, Resource, TaxonomyNode
from .registry import Registry

LEVELS = ("domain", "primer", "stage")
PATH_SEP = " / "
_SPLIT_RE = re.compile(r"\s*(?:/|>|»|→)\s*")


def split_path(text: str) -> list[str]:
    return [p.strip() for p in _SPLIT_RE.split(text.strip()) if p.strip()]


class Taxonomy:
    def __init__(self, nodes: list[TaxonomyNode]):
        self.by_id: dict[str, TaxonomyNode] = {n.node_id: n for n in nodes}
        self._children: dict[str, list[TaxonomyNode]] = defaultdict(list)
        for n in nodes:
            self._children[n.parent_id].append(n)
        for kids in self._children.values():
            kids.sort(key=lambda n: ((n.order if n.order is not None else 999), n.name.lower()))

    # ---- navigation
    def children(self, parent_id: str) -> list[TaxonomyNode]:
        return [n for n in self._children.get(parent_id, []) if n.status == "active"]

    def domains(self) -> list[TaxonomyNode]:
        return self.children("")

    def ancestors(self, node_id: str) -> list[TaxonomyNode]:
        out: list[TaxonomyNode] = []
        cur = self.by_id.get(node_id)
        while cur is not None:
            out.append(cur)
            cur = self.by_id.get(cur.parent_id) if cur.parent_id else None
        return list(reversed(out))

    def path(self, node_id: str) -> str:
        return PATH_SEP.join(n.name for n in self.ancestors(node_id))

    def path_names(self, node_id: str) -> list[str]:
        return [n.name for n in self.ancestors(node_id)]

    def domain_of(self, node_id: str) -> TaxonomyNode | None:
        anc = self.ancestors(node_id)
        return anc[0] if anc else None

    def descendants(self, node_id: str) -> list[TaxonomyNode]:
        out: list[TaxonomyNode] = []
        stack = list(self._children.get(node_id, []))
        while stack:
            n = stack.pop()
            out.append(n)
            stack.extend(self._children.get(n.node_id, []))
        return out

    def find(self, level: str, name: str, parent_id: str | None = None) -> TaxonomyNode | None:
        needle = name.strip().lower()
        for n in self.by_id.values():
            if n.level == level and n.name.strip().lower() == needle and (parent_id is None or n.parent_id == parent_id):
                return n
        return None

    def resolve(self, text: str) -> TaxonomyNode | None:
        """Resolve a node id, a full path ('Body / Immortal Yogi / Rest Body') or a unique stage name."""
        text = text.strip()
        if text in self.by_id:
            return self.by_id[text]
        parts = split_path(text)
        if not parts:
            return None
        if len(parts) > 1:
            parent = ""
            node = None
            for depth, part in enumerate(parts):
                level = LEVELS[min(depth, 2)]
                node = self.find(level, part, parent)
                if node is None:
                    return None
                parent = node.node_id
            return node
        matches = [n for n in self.by_id.values() if n.name.strip().lower() == parts[0].lower() and n.status == "active"]
        if len(matches) == 1:
            return matches[0]
        stages = [m for m in matches if m.level == "stage"]
        return stages[0] if len(stages) == 1 else None

    def to_render_map(self) -> dict[str, dict[str, Any]]:
        out = {}
        for n in self.by_id.values():
            dom = self.domain_of(n.node_id)
            out[n.node_id] = {"name": n.name, "path": self.path(n.node_id), "level": n.level, "domain": dom.name if dom else ""}
        return out

    def tree(self) -> list[dict[str, Any]]:
        def node_dict(n: TaxonomyNode) -> dict[str, Any]:
            d = {"node_id": n.node_id, "level": n.level, "name": n.name, "path": self.path(n.node_id), "description": n.description}
            kids = self.children(n.node_id)
            if kids:
                d["children"] = [node_dict(k) for k in kids]
            return d

        return [node_dict(d) for d in self.domains()]

    def suggest_stages(self, text: str, k: int = 3) -> list[dict[str, Any]]:
        """Cheap keyword hint for the agent (which makes the real judgement): stages whose name and
        description share the most distinctive words with the text."""
        words = _words(text[:50000])
        if not words:
            return []
        scored = []
        for n in self.by_id.values():
            if n.level != "stage" or n.status != "active":
                continue
            name_words = _words(n.name)
            desc_words = _words(n.description) | _words(self.path(n.node_id))
            score = 3.0 * len(name_words & words) + 1.0 * len(desc_words & words)
            # small fuzzy bonus so "recover" ~ "recovery" still counts a little
            score += 0.02 * fuzz.partial_token_set_ratio(n.description, text[:5000])
            scored.append((score, n))
        scored.sort(key=lambda s: (-s[0], s[1].name))
        return [{"node_id": n.node_id, "path": self.path(n.node_id), "score": round(s, 2)} for s, n in scored[:k]]


_STOPWORDS = {
    "the", "and", "that", "with", "from", "this", "your", "you", "for", "are", "not", "but", "into", "than",
    "them", "they", "their", "what", "when", "where", "how", "who", "will", "have", "has", "had", "its", "it's",
    "one", "all", "any", "can", "our", "out", "over", "under", "before", "after", "about", "against", "never",
    "body", "know", "keep", "make", "own", "read", "real", "right", "turn", "hold", "see", "well", "live",
}


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z][a-z'-]{3,}", (text or "").lower()) if w not in _STOPWORDS}


def _slug_for(level: str, name: str, parent: TaxonomyNode | None) -> str:
    base = slugify(name, max_length=40)
    if level == "domain" or parent is None:
        return base
    return f"{parent.slug}.{base}"


@dataclass
class TaxonomyReport:
    created: list[str] = field(default_factory=list)
    renamed: list[str] = field(default_factory=list)
    folders_created: list[str] = field(default_factory=list)
    folders_renamed: list[str] = field(default_factory=list)
    resources_updated: int = 0
    orphans: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def load_taxonomy(registry: Registry) -> Taxonomy:
    return Taxonomy(registry.nodes())


def add_node(registry: Registry, level: str, name: str, parent_id: str = "", description: str = "", order: int | None = None) -> TaxonomyNode:
    if level not in LEVELS:
        raise ValueError(f"level must be one of {LEVELS}")
    tax = load_taxonomy(registry)
    parent = tax.by_id.get(parent_id) if parent_id else None
    if level != "domain" and parent is None:
        raise ValueError("primer and stage nodes need an existing parent")
    if level == "primer" and parent and parent.level != "domain":
        raise ValueError("a primer's parent must be a domain")
    if level == "stage" and parent and parent.level != "primer":
        raise ValueError("a stage's parent must be a primer")
    existing = tax.find(level, name, parent_id)
    if existing:
        return existing
    siblings = tax.children(parent_id)
    node = TaxonomyNode(
        node_id=new_node_id(),
        level=level,  # type: ignore[arg-type]
        name=name.strip(),
        parent_id=parent_id,
        slug=_slug_for(level, name, parent),
        order=order if order is not None else (len(siblings) + 1),
        description=description,
        created_at=now_iso(),
        updated_at=now_iso(),
    )
    node.path = PATH_SEP.join([*tax.path_names(parent_id), node.name]) if parent_id else node.name
    registry.upsert_nodes([node])
    return node


def import_seed(registry: Registry, seed: dict[str, Any]) -> TaxonomyReport:
    """Create nodes from config/taxonomy.seed.yaml that do not exist yet (matched by name under parent)."""
    report = TaxonomyReport()
    for d_order, dom in enumerate(seed.get("domains", []), start=1):
        before = {n.node_id for n in registry.nodes()}
        d = add_node(registry, "domain", dom["name"], "", dom.get("description", ""), d_order)
        if d.node_id not in before:
            report.created.append(d.path)
        for p_order, primer in enumerate(dom.get("primers", []), start=1):
            before = {n.node_id for n in registry.nodes()}
            p = add_node(registry, "primer", primer["name"], d.node_id, primer.get("description", ""), p_order)
            if p.node_id not in before:
                report.created.append(p.path)
            for s_order, stage in enumerate(primer.get("stages", []), start=1):
                before = {n.node_id for n in registry.nodes()}
                s = add_node(registry, "stage", stage["name"], p.node_id, stage.get("description", ""), s_order)
                if s.node_id not in before:
                    report.created.append(s.path)
    return report


def ensure_node_folder(registry: Registry, drive: DriveClient, account: Account, node_id: str) -> FolderMap:
    """Create (if needed) the folder chain for `node_id` under the account's root and record it."""
    if not account.root_folder_id:
        raise ValueError(f"account {account.account_id} has no root_folder_id (run setup init-drive)")
    tax = load_taxonomy(registry)
    chain = tax.ancestors(node_id)
    if not chain:
        raise KeyError(node_id)
    parent_folder_id = account.root_folder_id
    result: FolderMap | None = None
    for node in chain:
        fm = registry.folder_for(node.node_id, account.account_id)
        if fm is None:
            folder = drive.ensure_folder(parent_folder_id, node.name)
            fm = FolderMap(node_id=node.node_id, account_id=account.account_id, folder_id=folder["id"], folder_url=folder_url(folder["id"]), created_at=now_iso())
            registry.upsert_folder(fm)
        parent_folder_id = fm.folder_id
        result = fm
    assert result is not None
    return result


def rename_node(registry: Registry, node_id: str, new_name: str, drives: Callable[[Account], DriveClient | None]) -> TaxonomyReport:
    """Rename in the sheet, then every account's folder, then the readable path on affected resources."""
    report = TaxonomyReport()
    tax = load_taxonomy(registry)
    node = tax.by_id.get(node_id)
    if node is None:
        raise KeyError(node_id)
    old_name = node.name
    new_name = new_name.strip()
    if not new_name or new_name == old_name:
        return report
    parent = tax.by_id.get(node.parent_id) if node.parent_id else None
    node.previous_names = [*node.previous_names, old_name]
    node.name = new_name
    node.slug = _slug_for(node.level, new_name, parent)
    node.updated_at = now_iso()
    # refresh paths for the node and its descendants
    tax = Taxonomy([*(n for n in tax.by_id.values() if n.node_id != node_id), node])
    to_update = [node, *tax.descendants(node_id)]
    for n in to_update:
        n.path = tax.path(n.node_id)
        if n.node_id != node_id:
            n.slug = _slug_for(n.level, n.name, tax.by_id.get(n.parent_id))
    registry.upsert_nodes(to_update)
    report.renamed.append(f"{old_name} -> {new_name}")
    # folders in every account
    accounts = {a.account_id: a for a in registry.accounts()}
    for fm in registry.folders():
        if fm.node_id != node_id:
            continue
        account = accounts.get(fm.account_id)
        drive = drives(account) if account else None
        if drive is None:
            report.errors.append(f"no drive client for account {fm.account_id}; run /taxonomy sync later")
            continue
        try:
            drive.rename(fm.folder_id, new_name)
            report.folders_renamed.append(f"{fm.account_id}:{fm.folder_id}")
        except Exception as exc:
            report.errors.append(f"rename folder {fm.folder_id} in {fm.account_id} failed: {exc}")
    # resources
    affected_ids = {n.node_id for n in to_update}
    changed: list[Resource] = []
    for r in registry.resources():
        if r.stage_id in affected_ids:
            new_path = tax.path(r.stage_id)
            if r.stage_path != new_path:
                r.stage_path = new_path
                changed.append(r)
    if changed:
        registry.repo.update("Resources", changed)
        report.resources_updated = len(changed)
    return report


def sync(registry: Registry, drives: dict[str, DriveClient], create_missing_for: list[str] | None = None) -> TaxonomyReport:
    """Make Drive match the sheet: rename mismatched folders, create missing ones, report orphans."""
    report = TaxonomyReport()
    tax = load_taxonomy(registry)
    accounts = {a.account_id: a for a in registry.accounts()}
    folders = registry.folders()
    for fm in folders:
        node = tax.by_id.get(fm.node_id)
        drive = drives.get(fm.account_id)
        if node is None or drive is None:
            continue
        try:
            meta = drive.get(fm.folder_id)
        except Exception as exc:
            report.errors.append(f"folder {fm.folder_id} ({fm.account_id}) unreachable: {exc}")
            continue
        if meta.get("name") != node.name:
            drive.rename(fm.folder_id, node.name)
            report.folders_renamed.append(f"{fm.account_id}:{meta.get('name')} -> {node.name}")
    for account_id in create_missing_for or []:
        account = accounts.get(account_id)
        drive = drives.get(account_id)
        if account is None or drive is None:
            continue
        for node in tax.by_id.values():
            if node.status != "active" or registry.folder_for(node.node_id, account_id):
                continue
            ensure_node_folder(registry, drive, account, node.node_id)
            report.folders_created.append(f"{account_id}:{tax.path(node.node_id)}")
    # orphans: folders under a known parent folder that no node references
    known = {fm.folder_id for fm in registry.folders()}
    for fm in registry.folders():
        drive = drives.get(fm.account_id)
        node = tax.by_id.get(fm.node_id)
        if drive is None or node is None or node.level == "stage":
            continue
        for child in drive.list_children(fm.folder_id, folders_only=True):
            if child["id"] not in known:
                report.orphans.append(f"{fm.account_id}:{tax.path(fm.node_id)} / {child['name']}")
    return report
