"""`pipeline auth check` and `pipeline setup ...`: bootstrap and health checks. All idempotent.

Service-account mode (primary): one key in GOOGLE_SERVICE_ACCOUNT_JSON serves every account row; each row
names the Google Workspace Shared Drive it writes to (column drive_id). Refresh-token mode (fallback):
one GOOGLE_REFRESH_TOKEN_* per Gmail account, files in that account's My Drive.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

from .accounts import B2_CAP_HINT, GMAIL_QUOTA_HINT, is_service_account, refresh_quota
from .context import AppContext, default_storage_accounts, notion_mode, storage_mode
from .drive import folder_url
from .google_auth import AuthError, resolve_auth_kind, service_account_email
from .ids import now_iso
from .models import Account
from .taxonomy import import_seed, load_taxonomy

SPREADSHEET_MIME = "application/vnd.google-apps.spreadsheet"


def bootstrap_accounts(ctx: AppContext) -> list[Account]:
    """Seed the Accounts table from config (accounts.bootstrap) or from the credentials present in the environment.
    In Backblaze mode a key that appears in the environment after the first run is added as a new account row."""
    reg = ctx.registry
    existing = reg.accounts()
    seeds = ctx.settings.section("accounts").get("bootstrap") or default_storage_accounts(ctx.settings)
    if existing:
        known = {a.account_id for a in existing}
        added = [Account(**s) for s in seeds if s["account_id"] not in known and s.get("backend") == "b2"]
        for r in added:
            reg.upsert_account(r)
        return existing + added
    rows = [Account(**s) for s in seeds]
    for r in rows:
        reg.upsert_account(r)
    return rows


def _gb(n: int | None) -> float | None:
    return round(n / 1024**3, 2) if n is not None else None


def repo_url(ctx: AppContext) -> str:
    repo = ctx.registry.repo
    if hasattr(repo, "url"):
        return repo.url()
    return repo.backend.spreadsheet_url()


def auth_check(ctx: AppContext) -> dict[str, Any]:
    report: dict[str, Any] = {"env": ctx.settings.env_report(), "mode": {"control_panel": "notion" if notion_mode(ctx.settings) else "google_sheet", "storage": storage_mode()}, "control_panel": {}, "accounts": []}
    try:
        if notion_mode(ctx.settings):
            tabs = ctx.registry.repo.ensure_tabs()  # idempotent; the databases must exist before accounts can be read
            report["control_panel"] = {"ok": True, "kind": "notion", "url": repo_url(ctx), "databases_created": tabs.get("created", [])}
        else:
            report["control_panel"] = {"ok": True, "kind": "google_sheet", "url": repo_url(ctx), "read_with": ctx.summary_token_env}
    except Exception as exc:
        report["control_panel"] = {"ok": False, "error": str(exc), "hint": "Notion mode: is AIPRIMER_NOTION_PAGE_ID the id of the 'AI Primer' page, is the integration connected to that page, is api.notion.com allowed and the token stored as an API credential?" if notion_mode(ctx.settings) else ""}
        return report
    for account in bootstrap_accounts(ctx):
        if account.backend == "b2":
            report["accounts"].append(_check_b2_account(ctx, account))
            continue
        kind = resolve_auth_kind(ctx.settings, account.token_env_var, account.auth_kind)
        entry: dict[str, Any] = {"account_id": account.account_id, "role": account.role, "token_env_var": account.token_env_var, "auth_kind": kind or "missing", "status": account.status}
        drive = ctx.drive_for(account)
        if drive is None:
            entry.update({"ok": False, "error": f"no credentials ({account.token_env_var} missing?)"})
        else:
            try:
                account = refresh_quota(ctx.registry, drive, account)
                entry.update({"ok": True, "email": account.email, "status": account.status})
                if kind == "service_account":
                    container = drive.container(account.drive_id) if account.drive_id else None
                    entry["writes_into"] = {"id": account.drive_id, "kind": container.get("kind") if container else None, "name": container.get("name") if container else None, "reachable": bool(container)}
                    if not account.drive_id:
                        entry["warning"] = "no drive_id (Shared Drive id or shared folder id): " + GMAIL_QUOTA_HINT
                    elif not container:
                        entry["warning"] = f"{account.drive_id} not reachable: share it with {account.email} (Editor / Content manager)"
                    else:
                        test = drive.write_test(account.drive_id)
                        entry["write_test"] = test
                        if not test["can_write"]:
                            entry["warning"] = "Google refused a test upload into that location: " + GMAIL_QUOTA_HINT
                else:
                    entry.update({"quota_gb": _gb(account.quota_bytes), "used_gb": _gb(account.used_bytes), "free_gb": _gb(account.free_bytes)})
            except Exception as exc:
                entry.update({"ok": False, "error": str(exc)})
        report["accounts"].append(entry)
    return report


def _check_b2_account(ctx: AppContext, account: Account) -> dict[str, Any]:
    """Backblaze account: can we reach the bucket, can we write into it, how much is stored."""
    entry: dict[str, Any] = {"account_id": account.account_id, "role": account.role, "backend": "b2", "bucket": account.bucket, "media_bucket": account.media_bucket, "key_env_vars": [account.key_id_env_var, account.token_env_var], "status": account.status}
    drive = ctx.drive_for(account)
    if drive is None:
        entry.update({"ok": False, "error": f"no credentials ({account.key_id_env_var} / {account.token_env_var} missing?)"})
        return entry
    try:
        container = drive.container(account.bucket)
        entry["writes_into"] = {"kind": "bucket", "name": account.bucket, "reachable": bool(container)}
        if not container:
            entry.update({"ok": False, "error": f"bucket {account.bucket!r} not reachable with this key: check the bucket name (B2_BUCKET_<n>) and that the application key is allowed on it"})
            return entry
        test = drive.write_test(account.root_folder_id or "")
        entry["write_test"] = test
        account = refresh_quota(ctx.registry, drive, account)
        entry.update({"ok": bool(test.get("can_write")), "used_gb": _gb(account.used_bytes), "cap_gb": _gb(account.quota_bytes), "free_gb": _gb(account.free_bytes), "status": account.status})
        if not test.get("can_write"):
            entry["warning"] = "Backblaze refused a test upload: " + (B2_CAP_HINT if "cap" in str(test.get("error", "")).lower() else str(test.get("error", "")))
        media = ctx.media_storage(account)
        if media is not None:
            try:
                raw_backend = getattr(media, "b2", None)
                info = raw_backend.bucket_info() if raw_backend is not None else {}
                entry["media"] = {"bucket": account.media_bucket, "reachable": True, "public": info.get("bucket_type") == "allPublic"}
                if info and info.get("bucket_type") != "allPublic":
                    entry["media"]["warning"] = "the media bucket should be Public so pictures and clips display in pages"
            except Exception as exc:
                entry["media"] = {"bucket": account.media_bucket, "reachable": False, "error": str(exc)}
    except Exception as exc:
        entry.update({"ok": False, "error": str(exc)})
    return entry


def create_control_sheet(ctx: AppContext, title: str = "AI Primer Control Panel") -> dict[str, Any]:
    """Create the control spreadsheet (only when AIPRIMER_CONTROL_SHEET_ID is unset).

    Refresh-token mode: created in the summary account's My Drive. Service-account mode: created inside the
    summary Shared Drive (AIPRIMER_SUMMARY_DRIVE_ID); without a shared drive the service account cannot create
    files, so the sheet must be created by hand and shared with the service account's email."""
    if notion_mode(ctx.settings):
        report = ctx.registry.repo.ensure_tabs()
        return {"created": False, "note": "Notion mode: the control panel is the set of databases under the 'AI Primer' page; they are created by `setup init-sheet` / `setup all`", "url": repo_url(ctx), **report}
    if ctx.settings.control_sheet_id:
        return {"created": False, "sheet_id": ctx.settings.control_sheet_id}
    env = ctx.summary_token_env
    kind = resolve_auth_kind(ctx.settings, env, "auto")
    services = ctx.services(env)
    if kind == "service_account":
        drive_id = os.environ.get("AIPRIMER_SUMMARY_DRIVE_ID", "").strip()
        sa_email = service_account_email(ctx.settings, env)
        by_hand = (
            "Create a Google Sheet named 'AI Primer Control Panel' yourself, signed in as the summaries Gmail account, inside the "
            f"folder shared with {sa_email} (it inherits the share; otherwise share the sheet with that email as Editor), then put its "
            "id (from the address bar) in AIPRIMER_CONTROL_SHEET_ID and start a new session."
        )
        if not drive_id:
            return {"created": False, "how": [by_hand, "Or create a Google Workspace Shared Drive, add the service account as Content manager, and set AIPRIMER_SUMMARY_DRIVE_ID."]}
        body = {"name": title, "mimeType": SPREADSHEET_MIME, "parents": [drive_id]}
        try:
            resp = services.drive.files().create(body=body, fields="id,webViewLink", supportsAllDrives=True).execute()
        except Exception as exc:
            return {"created": False, "error": f"Google refused to create the sheet with the service account: {exc}", "how": [by_hand]}
        return {"created": True, "sheet_id": resp["id"], "url": resp.get("webViewLink"), "next": "add AIPRIMER_CONTROL_SHEET_ID=<sheet_id> to the environment variables and start a new session"}
    resp = services.sheets.spreadsheets().create(body={"properties": {"title": title}}, fields="spreadsheetId,spreadsheetUrl").execute()
    return {"created": True, "sheet_id": resp["spreadsheetId"], "url": resp.get("spreadsheetUrl"), "next": "add AIPRIMER_CONTROL_SHEET_ID=<sheet_id> to the environment variables and start a new session"}


def init_sheet(ctx: AppContext) -> dict[str, Any]:
    report = ctx.registry.repo.ensure_tabs()
    accounts = bootstrap_accounts(ctx)
    return {"tabs": report, "accounts": [a.account_id for a in accounts], "url": repo_url(ctx)}


def init_drive(ctx: AppContext) -> dict[str, Any]:
    cfg = ctx.settings.drive
    out: dict[str, Any] = {"accounts": []}
    for account in bootstrap_accounts(ctx):
        drive = ctx.drive_for(account)
        entry: dict[str, Any] = {"account_id": account.account_id, "role": account.role}
        if drive is None:
            entry["skipped"] = f"no credentials for {account.token_env_var}"
            out["accounts"].append(entry)
            continue
        if account.backend == "b2":
            try:
                b2cfg = ctx.settings.b2
                root = drive.ensure_folder("", str(b2cfg.get("raw_prefix", "raw")))
                inbox = drive.ensure_folder(root["id"], str(b2cfg.get("inbox_prefix", "_Inbox")))
                account.root_folder_id, account.inbox_folder_id = root["id"], inbox["id"]
                account.quota_checked_at = now_iso()
                ctx.registry.upsert_account(account)
                refresh_quota(ctx.registry, drive, account)
                entry.update({"ok": True, "bucket": account.bucket, "root": f"b2://{account.bucket}/{root['id']}", "inbox": f"b2://{account.bucket}/{inbox['id']}"})
            except Exception as exc:
                entry.update({"ok": False, "error": str(exc)})
            out["accounts"].append(entry)
            continue
        sa = is_service_account(ctx.registry, account)
        parent: str | None = account.drive_id or None
        if sa and not parent:
            entry.update({"ok": False, "error": "service account without drive_id: " + GMAIL_QUOTA_HINT})
            out["accounts"].append(entry)
            continue
        try:
            if account.role == "raw":
                root = drive.ensure_folder(parent, cfg.get("raw_root_name", "AI Primer Raw"))
                inbox = drive.ensure_folder(root["id"], cfg.get("inbox_name", "_Inbox"))
                account.root_folder_id, account.inbox_folder_id = root["id"], inbox["id"]
                entry.update({"root": folder_url(root["id"]), "inbox": folder_url(inbox["id"])})
            else:
                root = drive.ensure_folder(parent, cfg.get("summaries_root_name", "AI Primer Summaries"))
                account.summaries_folder_id = root["id"]
                entry.update({"summaries": folder_url(root["id"])})
            account.quota_checked_at = now_iso()
            ctx.registry.upsert_account(account)
            refresh_quota(ctx.registry, drive, account)
            entry["ok"] = True
        except Exception as exc:
            msg = str(exc)
            if sa and ("quota" in msg.lower()):
                msg += " | " + GMAIL_QUOTA_HINT
            entry.update({"ok": False, "error": msg})
        out["accounts"].append(entry)
    return out


def import_taxonomy(ctx: AppContext, seed_path: Path | None = None) -> dict[str, Any]:
    path = seed_path or ctx.settings.config_dir / "taxonomy.seed.yaml"
    seed = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    report = import_seed(ctx.registry, seed)
    tax = load_taxonomy(ctx.registry)
    return {"created": report.created, "total_nodes": len(tax.by_id), "domains": [d.name for d in tax.domains()]}


def status(ctx: AppContext) -> dict[str, Any]:
    reg = ctx.registry
    tax = load_taxonomy(reg)
    res = reg.resources()
    by_status: dict[str, int] = {}
    for r in res:
        by_status[r.status] = by_status.get(r.status, 0) + 1
    return {
        "control_panel": repo_url(ctx),
        "mode": {"control_panel": "notion" if notion_mode(ctx.settings) else "google_sheet", "storage": storage_mode()},
        "accounts": [
            {"account_id": a.account_id, "role": a.role, "backend": a.backend, "status": a.status, "bucket": a.bucket, "auth_kind": ("b2_key" if a.backend == "b2" else resolve_auth_kind(ctx.settings, a.token_env_var, a.auth_kind) or "missing"), "drive_id": a.drive_id, "used_gb": _gb(a.used_bytes), "free_gb": _gb(a.free_bytes)}
            for a in reg.accounts()
        ],
        "taxonomy": {"domains": len(tax.domains()), "nodes": len(tax.by_id)},
        "resources": {"total": len(res), "by_status": by_status},
        "authors": len(reg.authors()),
    }


def fetch_apify_schemas(ctx: AppContext) -> dict[str, Any]:
    try:
        return ctx.apify.fetch_and_store_schemas()
    except Exception as exc:
        raise RuntimeError(f"could not fetch Apify actor schemas (is api.apify.com allowed and the credential set?): {exc}") from exc


def health(ctx: AppContext) -> dict[str, Any]:
    out: dict[str, Any] = {}
    try:
        out["auth"] = auth_check(ctx)
    except AuthError as exc:
        out["auth"] = {"error": str(exc)}
    try:
        out["status"] = status(ctx)
    except Exception as exc:
        out["status"] = {"error": str(exc)}
    return out
