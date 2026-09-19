"""Command-line entry point: `python -m pipeline <group> <command> [options]`.

Contract for every command (the skills rely on it):
- prints exactly ONE JSON object on stdout at the end ({"ok": true, ...} or {"ok": false, "error": ...});
- progress and warnings go to stderr;
- exit code 0 on success, 1 on failure;
- a Jobs row is appended to the control panel when the registry is reachable;
- the CLI never asks questions and never calls a language model.
"""

from __future__ import annotations

import json
import sys
import time
import traceback
from pathlib import Path
from typing import Any, Callable, Optional

import typer

from .context import AppContext

app = typer.Typer(add_completion=False, no_args_is_help=True, help="AI Primer 2.0 pipeline")
auth_app = typer.Typer(help="Credentials and access checks")
setup_app = typer.Typer(help="One-time bootstrap (idempotent)")
taxonomy_app = typer.Typer(help="Domain > Primer > Stage tree")
resource_app = typer.Typer(help="Move or correct an ingested resource")
ingest_app = typer.Typer(help="Ingest resources")
summarize_app = typer.Typer(help="Per-author knowledge base steps")
doc_app = typer.Typer(help="Summary Google Docs")
for name, sub in (("auth", auth_app), ("setup", setup_app), ("taxonomy", taxonomy_app), ("resource", resource_app), ("ingest", ingest_app), ("summarize", summarize_app), ("doc", doc_app)):
    app.add_typer(sub, name=name)

_CTX: AppContext | None = None


def get_ctx() -> AppContext:
    global _CTX
    if _CTX is None:
        _CTX = AppContext()
    return _CTX


def set_ctx(ctx: AppContext) -> None:
    """Used by tests to inject fakes."""
    global _CTX
    _CTX = ctx


def emit(payload: dict[str, Any], pretty: bool = False) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2 if pretty else None, default=str) + "\n")
    sys.stdout.flush()


def run_command(name: str, fn: Callable[[], dict[str, Any]], args: dict[str, Any] | None = None, pretty: bool = False, log: bool = True) -> None:
    started = time.time()
    ctx = get_ctx()
    try:
        result = fn() or {}
        payload = {"ok": True, "command": name, **result}
        if log:
            _log(ctx, name, args or {}, "ok", started, result)
        emit(payload, pretty)
    except Exception as exc:
        err = f"{type(exc).__name__}: {exc}"
        sys.stderr.write(traceback.format_exc())
        if log:
            _log(ctx, name, args or {}, "failed", started, {}, err)
        emit({"ok": False, "command": name, "error": err}, pretty)
        raise typer.Exit(code=1)


def _log(ctx: AppContext, name: str, args: dict[str, Any], status: str, started: float, result: dict[str, Any], error: str = "") -> None:
    try:
        resources = []
        for key in ("resource_id",):
            if result.get(key):
                resources.append(result[key])
        for key in ("ingested", "skipped"):
            for item in result.get(key, []) or []:
                if isinstance(item, dict) and item.get("resource_id"):
                    resources.append(item["resource_id"])
        ctx.registry.log_job(name, args, status, started, resources=resources, apify_run_ids=result.get("apify_run_ids"), apify_cost_usd=result.get("apify_cost_usd"), error=error)
    except Exception:
        pass


Pretty = typer.Option(False, "--pretty", help="Indent the JSON output")
JsonFlag = typer.Option(True, "--json/--no-json", help="Always JSON (kept for compatibility)")


# ----------------------------------------------------------------------------- auth / setup


@auth_app.command("check")
def auth_check(pretty: bool = Pretty, json_: bool = JsonFlag):
    from .setup_cmds import auth_check as _fn

    run_command("auth check", lambda: _fn(get_ctx()), pretty=pretty, log=False)


@setup_app.command("create-sheet")
def setup_create_sheet(title: str = "AI Primer Control Panel", pretty: bool = Pretty):
    from .setup_cmds import create_control_sheet

    run_command("setup create-sheet", lambda: create_control_sheet(get_ctx(), title), pretty=pretty, log=False)


@setup_app.command("init-sheet")
def setup_init_sheet(pretty: bool = Pretty):
    from .setup_cmds import init_sheet

    run_command("setup init-sheet", lambda: init_sheet(get_ctx()), pretty=pretty)


@setup_app.command("init-drive")
def setup_init_drive(pretty: bool = Pretty):
    from .setup_cmds import init_drive

    run_command("setup init-drive", lambda: init_drive(get_ctx()), pretty=pretty)


@setup_app.command("fetch-apify-schemas")
def setup_fetch_apify(pretty: bool = Pretty):
    from .setup_cmds import fetch_apify_schemas

    run_command("setup fetch-apify-schemas", lambda: fetch_apify_schemas(get_ctx()), pretty=pretty)


@setup_app.command("status")
def setup_status(pretty: bool = Pretty):
    from .setup_cmds import health

    run_command("setup status", lambda: health(get_ctx()), pretty=pretty, log=False)


@setup_app.command("all")
def setup_all(pretty: bool = Pretty):
    """init-sheet + init-drive + taxonomy import + status, in one go."""
    from .setup_cmds import health, import_taxonomy, init_drive, init_sheet

    def _all():
        ctx = get_ctx()
        return {"sheet": init_sheet(ctx), "drive": init_drive(ctx), "taxonomy": import_taxonomy(ctx), "health": health(ctx)}

    run_command("setup all", _all, pretty=pretty)


# ----------------------------------------------------------------------------- taxonomy


@taxonomy_app.command("list")
def taxonomy_list(pretty: bool = Pretty):
    from .taxonomy import load_taxonomy

    def _fn():
        tax = load_taxonomy(get_ctx().registry)
        return {"tree": tax.tree(), "count": len(tax.by_id)}

    run_command("taxonomy list", _fn, pretty=pretty, log=False)


@taxonomy_app.command("import")
def taxonomy_import(seed: Optional[Path] = typer.Option(None, help="YAML seed (default config/taxonomy.seed.yaml)"), pretty: bool = Pretty):
    from .setup_cmds import import_taxonomy

    run_command("taxonomy import", lambda: import_taxonomy(get_ctx(), seed), {"seed": str(seed)}, pretty)


@taxonomy_app.command("add")
def taxonomy_add(level: str, name: str, parent: str = typer.Option("", help="parent node id or path"), description: str = "", create_folders: bool = False, pretty: bool = Pretty):
    from .taxonomy import add_node, ensure_node_folder, load_taxonomy

    def _fn():
        ctx = get_ctx()
        tax = load_taxonomy(ctx.registry)
        parent_id = ""
        if parent:
            p = tax.resolve(parent)
            if p is None:
                raise ValueError(f"unknown parent {parent!r}")
            parent_id = p.node_id
        node = add_node(ctx.registry, level, name, parent_id, description)
        out: dict[str, Any] = {"node_id": node.node_id, "path": node.path, "level": node.level}
        if create_folders:
            for acc in ctx.registry.raw_accounts():
                drive = ctx.drive_for(acc)
                if drive:
                    fm = ensure_node_folder(ctx.registry, drive, acc, node.node_id)
                    out.setdefault("folders", []).append({"account_id": acc.account_id, "folder_url": fm.folder_url})
                    break
        return out

    run_command("taxonomy add", _fn, {"level": level, "name": name, "parent": parent}, pretty)


@taxonomy_app.command("rename")
def taxonomy_rename(node: str, name: str = typer.Option(..., "--name"), pretty: bool = Pretty):
    from .taxonomy import load_taxonomy, rename_node

    def _fn():
        ctx = get_ctx()
        tax = load_taxonomy(ctx.registry)
        n = tax.resolve(node)
        if n is None:
            raise ValueError(f"unknown node {node!r}")
        report = rename_node(ctx.registry, n.node_id, name, ctx.drive_for)
        return {"node_id": n.node_id, **report.__dict__}

    run_command("taxonomy rename", _fn, {"node": node, "name": name}, pretty)


@taxonomy_app.command("sync")
def taxonomy_sync(create_missing: bool = typer.Option(False, help="also create every missing folder in the first raw account"), pretty: bool = Pretty):
    from .taxonomy import sync

    def _fn():
        ctx = get_ctx()
        drives = ctx.drives()
        accounts = [a.account_id for a in ctx.registry.raw_accounts()[:1]] if create_missing else []
        report = sync(ctx.registry, drives, accounts)
        return report.__dict__

    run_command("taxonomy sync", _fn, {"create_missing": create_missing}, pretty)


@taxonomy_app.command("affected")
def taxonomy_affected(node: str, pretty: bool = Pretty):
    """How many resources / folders a rename of this node would touch."""
    from .taxonomy import load_taxonomy

    def _fn():
        ctx = get_ctx()
        tax = load_taxonomy(ctx.registry)
        n = tax.resolve(node)
        if n is None:
            raise ValueError(f"unknown node {node!r}")
        ids = {n.node_id, *(d.node_id for d in tax.descendants(n.node_id))}
        res = [r.resource_id for r in ctx.registry.resources() if r.stage_id in ids]
        folders = [f"{f.account_id}:{f.folder_id}" for f in ctx.registry.folders() if f.node_id == n.node_id]
        return {"node_id": n.node_id, "path": n.path, "resources": len(res), "folders": folders}

    run_command("taxonomy affected", _fn, pretty=pretty, log=False)


# ----------------------------------------------------------------------------- resources


@resource_app.command("show")
def resource_show(ref: str, pretty: bool = Pretty):
    from .resources import show

    run_command("resource show", lambda: show(get_ctx(), ref), pretty=pretty, log=False)


@resource_app.command("move")
def resource_move(ref: str, stage: str = typer.Option(..., "--stage"), pretty: bool = Pretty):
    from .resources import move

    run_command("resource move", lambda: move(get_ctx(), ref, stage), {"ref": ref, "stage": stage}, pretty)


@resource_app.command("set")
def resource_set(
    ref: str,
    title: Optional[str] = None,
    author: Optional[str] = typer.Option(None, help="author name or existing A-id"),
    date: Optional[str] = typer.Option(None, "--date"),
    date_precision: Optional[str] = None,
    alias_of: Optional[str] = typer.Option(None, help="record the given author name as an alias of this A-id"),
    pretty: bool = Pretty,
):
    from .resources import set_metadata

    run_command("resource set", lambda: set_metadata(get_ctx(), ref, title=title, author=author, date_text=date, date_precision=date_precision, alias_of=alias_of), {"ref": ref, "title": title, "author": author, "date": date}, pretty)


@resource_app.command("list")
def resource_list(author: Optional[str] = None, status: Optional[str] = None, pretty: bool = Pretty):
    def _fn():
        rows = get_ctx().registry.resources()
        if author:
            rows = [r for r in rows if r.author_id == author]
        if status:
            rows = [r for r in rows if r.status == status]
        return {"count": len(rows), "resources": [{"resource_id": r.resource_id, "title": r.title, "author_id": r.author_id, "published_date": r.published_date, "status": r.status, "stage_path": r.stage_path} for r in rows]}

    run_command("resource list", _fn, pretty=pretty, log=False)


# ----------------------------------------------------------------------------- ingest


@ingest_app.command("probe")
def ingest_probe(sources: list[str], no_sample: bool = typer.Option(False, help="skip the content preview"), pretty: bool = Pretty):
    from .ingest import Ingestor

    def _fn():
        ctx = get_ctx()
        ing = Ingestor(ctx)
        results = [r.__dict__ for r in ing.probe(sources, sample_text=not no_sample)]
        from .taxonomy import load_taxonomy

        return {"results": results, "taxonomy": load_taxonomy(ctx.registry).tree(), "authors": [{"author_id": a.author_id, "canonical_name": a.canonical_name, "aliases": a.aliases} for a in ctx.registry.authors()]}

    run_command("ingest probe", _fn, {"sources": sources}, pretty, log=False)


@ingest_app.command("run")
def ingest_run(
    source: str,
    stage: str = typer.Option(..., "--stage", help="stage node id or 'Domain / Primer / Stage'"),
    author: Optional[str] = typer.Option(None, help="author name or existing A-id (overrides detection)"),
    title: Optional[str] = None,
    date: Optional[str] = typer.Option(None, "--date"),
    date_precision: Optional[str] = None,
    extracted_file: Optional[Path] = typer.Option(None, help="markdown written by the agent (vision/manual transcription)"),
    ocr_patch: Optional[Path] = typer.Option(None, help="JSON {page: text} replacing given pages"),
    account: Optional[str] = typer.Option(None, help="force a raw account id"),
    dataset_note: Optional[str] = typer.Option(None, help="for spreadsheets: what the dataset represents"),
    force: bool = False,
    pretty: bool = Pretty,
):
    from .ingest import Ingestor

    run_command(
        "ingest run",
        lambda: Ingestor(get_ctx()).run(source, stage, author, title=title, date_text=date, date_precision=date_precision, extracted_file=extracted_file, ocr_patch=ocr_patch, account_id=account, force=force, dataset_note=dataset_note),
        {"source": source, "stage": stage, "author": author, "title": title, "date": date, "force": force},
        pretty,
    )


@ingest_app.command("channel")
def ingest_channel(
    url: str,
    list_only: bool = typer.Option(False, "--list", help="list videos with cost estimate, no ingestion"),
    select: Optional[Path] = typer.Option(None, help="JSON list of video ids to ingest"),
    stage: Optional[str] = typer.Option(None, "--stage"),
    author: Optional[str] = None,
    max_results: Optional[int] = None,
    refresh: bool = typer.Option(False, help="ignore the cached listing"),
    pretty: bool = Pretty,
):
    from .ingest import Ingestor

    def _fn():
        ing = Ingestor(get_ctx())
        if list_only or select is None:
            return ing.channel_list(url, max_results, refresh)
        if not stage:
            raise ValueError("--stage is required to ingest")
        ids = json.loads(Path(select).read_text(encoding="utf-8"))
        return ing.channel_run(url, ids, stage, author)

    run_command("ingest channel", _fn, {"url": url, "list": list_only, "select": str(select), "stage": stage, "author": author}, pretty, log=not list_only)


@ingest_app.command("inbox")
def ingest_inbox(
    file_id: Optional[str] = typer.Option(None, "--file", help="Drive file id from the inbox listing"),
    stage: Optional[str] = typer.Option(None, "--stage"),
    author: Optional[str] = None,
    title: Optional[str] = None,
    date: Optional[str] = typer.Option(None, "--date"),
    pretty: bool = Pretty,
):
    from .ingest import Ingestor

    def _fn():
        ing = Ingestor(get_ctx())
        if not file_id:
            return {"files": ing.inbox_list()}
        if not stage:
            raise ValueError("--stage is required")
        return ing.inbox_file(file_id, stage, author, title=title, date_text=date)

    run_command("ingest inbox", _fn, {"file": file_id, "stage": stage, "author": author}, pretty, log=bool(file_id))


@ingest_app.command("render-pages")
def ingest_render_pages(pdf: Path, pages: str = typer.Option(..., help="comma-separated 1-based pages, ranges allowed (3,5-7)"), dpi: int = 150, out: Optional[Path] = None, pretty: bool = Pretty):
    """Render PDF pages to PNG so the agent can transcribe them by eye."""
    from .extract import render_pdf_pages

    def _fn():
        nums: list[int] = []
        for part in pages.split(","):
            part = part.strip()
            if "-" in part:
                a, b = part.split("-")
                nums.extend(range(int(a), int(b) + 1))
            elif part:
                nums.append(int(part))
        outdir = out or get_ctx().settings.cache_dir / "render" / pdf.stem
        files = render_pdf_pages(pdf, nums, dpi, outdir)
        return {"files": [str(f) for f in files]}

    run_command("ingest render-pages", _fn, pretty=pretty, log=False)


# ----------------------------------------------------------------------------- summarize / doc


def _summ():
    from . import summarize_cmds

    return summarize_cmds


@summarize_app.command("plan")
def summarize_plan(author: str = typer.Option(..., "--author"), pretty: bool = Pretty):
    run_command("summarize plan", lambda: _summ().plan(get_ctx(), author), {"author": author}, pretty, log=False)


@summarize_app.command("prepare")
def summarize_prepare(author: str = typer.Option(..., "--author"), resources: str = typer.Option("", help="comma-separated resource ids (default: all pending)"), pretty: bool = Pretty):
    run_command("summarize prepare", lambda: _summ().prepare(get_ctx(), author, [r for r in resources.split(",") if r]), {"author": author, "resources": resources}, pretty)


@summarize_app.command("verify")
def summarize_verify(author: str = typer.Option(..., "--author"), resource: str = typer.Option(..., "--resource"), pretty: bool = Pretty):
    run_command("summarize verify", lambda: _summ().verify(get_ctx(), author, resource), {"author": author, "resource": resource}, pretty)


@summarize_app.command("match")
def summarize_match(author: str = typer.Option(..., "--author"), resource: str = typer.Option(..., "--resource"), pretty: bool = Pretty):
    run_command("summarize match", lambda: _summ().match(get_ctx(), author, resource), {"author": author, "resource": resource}, pretty)


@summarize_app.command("apply")
def summarize_apply(author: str = typer.Option(..., "--author"), resource: str = typer.Option(..., "--resource"), pretty: bool = Pretty):
    run_command("summarize apply", lambda: _summ().apply(get_ctx(), author, resource), {"author": author, "resource": resource}, pretty)


@summarize_app.command("review-prep")
def summarize_review_prep(author: str = typer.Option(..., "--author"), pretty: bool = Pretty):
    run_command("summarize review-prep", lambda: _summ().review_prep(get_ctx(), author), {"author": author}, pretty)


@summarize_app.command("finalize")
def summarize_finalize(author: str = typer.Option(..., "--author"), note: str = typer.Option("", help="changelog note for this version"), no_doc: bool = typer.Option(False, help="skip the Google Doc refresh"), no_commit: bool = False, pretty: bool = Pretty):
    run_command("summarize finalize", lambda: _summ().finalize(get_ctx(), author, note, publish_doc=not no_doc, commit=not no_commit), {"author": author, "note": note}, pretty)


@summarize_app.command("status")
def summarize_status(author: str = typer.Option(..., "--author"), pretty: bool = Pretty):
    run_command("summarize status", lambda: _summ().status(get_ctx(), author), {"author": author}, pretty, log=False)


@doc_app.command("comments")
def doc_comments(author: str = typer.Option(..., "--author"), resolve: Optional[str] = typer.Option(None, help="comment id to mark resolved"), pretty: bool = Pretty):
    run_command("doc comments", lambda: _summ().doc_comments(get_ctx(), author, resolve), {"author": author, "resolve": resolve}, pretty, log=bool(resolve))


def main() -> None:
    app()


if __name__ == "__main__":
    main()
