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

from .accounts import GMAIL_QUOTA_HINT, is_service_account, refresh_quota
from .context import AppContext, default_key_env
from .drive import folder_url
from .google_auth import AuthError, resolve_auth_kind, service_account_email
from .ids import now_iso
from .models import Account
from .taxonomy import import_seed, load_taxonomy

SPREADSHEET_MIME = "application/vnd.google-apps.spreadsheet"


def bootstrap_accounts(ctx: AppContext) -> list[Account]:
    """Seed the Accounts tab from config (accounts.bootstrap) or from the credentials present in the environment."""
    reg = ctx.registry
    existing = reg.accounts()
    if existing:
        return existing
    seeds = ctx.settings.section("accounts").get("bootstrap")
    if not seeds:
        raw_env, sum_env = default_key_env("raw"), default_key_env("summary")
        seeds = [
            {"account_id": "raw01", "role": "raw", "token_env_var": raw_env, "priority": 1, "drive_id": os.environ.get("AIPRIMER_RAW_DRIVE_ID", "")},
            {"account_id": "summary01", "role": "summary", "token_env_var": sum_env, "priority": 1, "drive_id": os.environ.get("AIPRIMER_SUMMARY_DRIVE_ID", "")},
        ]
    rows = [Account(**s) for s in seeds]
    for r in rows:
        reg.upsert_account(r)
    return rows


def _gb(n: int | None) -> float | None:
    return round(n / 1024**3, 2) if n is not None else None


def auth_check(ctx: AppContext) -> dict[str, Any]:
    report: dict[str, Any] = {"env": ctx.settings.env_report(), "control_sheet": {}, "accounts": []}
    try:
        url = ctx.registry.repo.backend.spreadsheet_url()
        report["control_sheet"] = {"ok": True, "url": url, "read_with": ctx.summary_token_env}
    except Exception as exc:
        report["control_sheet"] = {"ok": False, "error": str(exc)}
        return report
    for account in bootstrap_accounts(ctx):
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
                    sd = drive.shared_drive(account.drive_id) if account.drive_id else None
                    entry["shared_drive"] = {"id": account.drive_id, "name": sd.get("name") if sd else None, "reachable": bool(sd)}
                    if not account.drive_id:
                        entry["warning"] = "no drive_id: " + GMAIL_QUOTA_HINT
                    elif not sd:
                        entry["warning"] = f"shared drive {account.drive_id} not reachable: add {account.email} as a Content manager of it"
                else:
                    entry.update({"quota_gb": _gb(account.quota_bytes), "used_gb": _gb(account.used_bytes), "free_gb": _gb(account.free_bytes)})
            except Exception as exc:
                entry.update({"ok": False, "error": str(exc)})
        report["accounts"].append(entry)
    return report


def create_control_sheet(ctx: AppContext, title: str = "AI Primer Control Panel") -> dict[str, Any]:
    """Create the control spreadsheet (only when AIPRIMER_CONTROL_SHEET_ID is unset).

    Refresh-token mode: created in the summary account's My Drive. Service-account mode: created inside the
    summary Shared Drive (AIPRIMER_SUMMARY_DRIVE_ID); without a shared drive the service account cannot create
    files, so the sheet must be created by hand and shared with the service account's email."""
    if ctx.settings.control_sheet_id:
        return {"created": False, "sheet_id": ctx.settings.control_sheet_id}
    env = ctx.summary_token_env
    kind = resolve_auth_kind(ctx.settings, env, "auto")
    services = ctx.services(env)
    if kind == "service_account":
        drive_id = os.environ.get("AIPRIMER_SUMMARY_DRIVE_ID", "").strip()
        sa_email = service_account_email(ctx.settings, env)
        if not drive_id:
            return {
                "created": False,
                "how": [
                    "Option A (Shared Drive): create a Google Workspace Shared Drive for summaries, add "
                    f"{sa_email} as Content manager, put its id in AIPRIMER_SUMMARY_DRIVE_ID and run this command again.",
                    "Option B (Gmail): create a Google Sheet named 'AI Primer Control Panel' in the Gmail account, share it with "
                    f"{sa_email} as Editor, and put its id in AIPRIMER_CONTROL_SHEET_ID. Note: on Gmail the service account can edit "
                    "this sheet but cannot upload files or create Docs.",
                ],
            }
        body = {"name": title, "mimeType": SPREADSHEET_MIME, "parents": [drive_id]}
        resp = services.drive.files().create(body=body, fields="id,webViewLink", supportsAllDrives=True).execute()
        return {"created": True, "sheet_id": resp["id"], "url": resp.get("webViewLink"), "next": "add AIPRIMER_CONTROL_SHEET_ID=<sheet_id> to the environment variables and start a new session"}
    resp = services.sheets.spreadsheets().create(body={"properties": {"title": title}}, fields="spreadsheetId,spreadsheetUrl").execute()
    return {"created": True, "sheet_id": resp["spreadsheetId"], "url": resp.get("spreadsheetUrl"), "next": "add AIPRIMER_CONTROL_SHEET_ID=<sheet_id> to the environment variables and start a new session"}


def init_sheet(ctx: AppContext) -> dict[str, Any]:
    report = ctx.registry.repo.ensure_tabs()
    accounts = bootstrap_accounts(ctx)
    return {"tabs": report, "accounts": [a.account_id for a in accounts], "url": ctx.registry.repo.backend.spreadsheet_url()}


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
        "control_sheet": reg.repo.backend.spreadsheet_url(),
        "accounts": [{"account_id": a.account_id, "role": a.role, "status": a.status, "auth_kind": resolve_auth_kind(ctx.settings, a.token_env_var, a.auth_kind) or "missing", "drive_id": a.drive_id, "free_gb": _gb(a.free_bytes)} for a in reg.accounts()],
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
