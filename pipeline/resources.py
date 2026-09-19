"""`pipeline resource ...`: move a resource to another stage, correct its metadata, show it."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .context import AppContext
from .metadata import apply_overrides, ensure_author
from .models import MetadataGuess, Resource
from .naming import drive_filename, extracted_filename
from .taxonomy import ensure_node_folder, load_taxonomy


def _resolve(ctx: AppContext, ref: str) -> Resource:
    reg = ctx.registry
    r = reg.resource(ref)
    if r:
        return r
    matches = reg.find_resources(ref)
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise KeyError(f"no resource matches {ref!r}")
    raise ValueError(f"{len(matches)} resources match {ref!r}: " + ", ".join(f"{m.resource_id} ({m.title[:40]})" for m in matches[:5]))


def show(ctx: AppContext, ref: str) -> dict[str, Any]:
    return _resolve(ctx, ref).model_dump()


def move(ctx: AppContext, ref: str, stage: str) -> dict[str, Any]:
    reg = ctx.registry
    resource = _resolve(ctx, ref)
    tax = load_taxonomy(reg)
    node = tax.resolve(stage)
    if node is None or node.level != "stage":
        raise ValueError(f"unknown stage {stage!r}")
    account = reg.account(resource.account_id)
    if account is None:
        raise KeyError(f"account {resource.account_id} not found")
    drive = ctx.drive_for(account)
    if drive is None:
        raise RuntimeError(f"no credentials for account {account.account_id}")
    folder = ensure_node_folder(reg, drive, account, node.node_id)
    moved = []
    for fid in [resource.raw_file_id, resource.text_file_id, *resource.data_file_ids]:
        if fid:
            drive.move(fid, folder.folder_id)
            moved.append(fid)
    old = resource.stage_path
    resource.stage_id, resource.stage_path, resource.folder_id = node.node_id, tax.path(node.node_id), folder.folder_id
    reg.upsert_resource(resource)
    return {"resource_id": resource.resource_id, "from": old, "to": resource.stage_path, "files_moved": moved, "folder_url": folder.folder_url}


def set_metadata(ctx: AppContext, ref: str, *, title: str | None = None, author: str | None = None, date_text: str | None = None, date_precision: str | None = None, alias_of: str | None = None) -> dict[str, Any]:
    reg = ctx.registry
    resource = _resolve(ctx, ref)
    guess = MetadataGuess(title=resource.title, author_raw=resource.author_raw, published_date=resource.published_date, date_precision=resource.date_precision)
    guess = apply_overrides(guess, title=title, author=None if (author or "").startswith("A-") else author, date_text=date_text, date_precision=date_precision)
    resource.title, resource.published_date, resource.date_precision = guess.title, guess.published_date, guess.date_precision  # type: ignore[assignment]
    changed_author = False
    if author:
        if author.startswith("A-"):
            row = reg.author(author)
            if row is None:
                raise KeyError(author)
        else:
            row = ensure_author(reg, author, alias_of=alias_of)
        resource.author_id, resource.author_override = row.author_id, True
        if not author.startswith("A-"):
            resource.author_raw = author
        changed_author = True
    reg.upsert_resource(resource)
    # rename the Drive files so names stay truthful
    renamed = []
    account = reg.account(resource.account_id)
    drive = ctx.drive_for(account) if account else None
    if drive is not None:
        author_name = (reg.author(resource.author_id).canonical_name if resource.author_id and reg.author(resource.author_id) else resource.author_raw)
        max_chars = int(ctx.settings.drive.get("max_title_chars", 80))
        if resource.raw_file_id:
            ext = Path(resource.raw_filename).suffix if resource.raw_filename else ""
            new_name = drive_filename(resource, ext, author_name, max_chars)
            drive.rename(resource.raw_file_id, new_name)
            resource.raw_filename = new_name
            renamed.append(new_name)
        if resource.text_file_id:
            new_name = extracted_filename(resource, author_name, max_chars)
            drive.rename(resource.text_file_id, new_name)
            renamed.append(new_name)
        reg.upsert_resource(resource)
    for a in reg.authors():
        a.resource_count = len(reg.resources_for_author(a.author_id))
        if changed_author:
            reg.upsert_author(a)
    return {"resource_id": resource.resource_id, "title": resource.title, "author_id": resource.author_id, "published_date": resource.published_date, "date_precision": resource.date_precision, "renamed": renamed}
