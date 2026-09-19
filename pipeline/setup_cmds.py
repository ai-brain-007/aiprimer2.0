"""`pipeline auth check` and `pipeline setup ...`: bootstrap and health checks. All idempotent."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from .accounts import refresh_quota
from .context import DEFAULT_RAW_TOKEN_ENV, DEFAULT_SUMMARY_TOKEN_ENV, AppContext
from .drive import folder_url
from .google_auth import AuthError
from .ids import now_iso
from .models import Account
from .taxonomy import import_seed, load_taxonomy


def bootstrap_accounts(ctx: AppContext) -> list[Account]:
    """Seed the Accounts tab from config (accounts.bootstrap) or from the default two env var names."""
    reg = ctx.registry
    existing = reg.accounts()
    if existing:
        return existing
    seeds = ctx.settings.section("accounts").get("bootstrap") or [
        {"account_id": "raw01", "role": "raw", "token_env_var": DEFAULT_RAW_TOKEN_ENV, "priority": 1},
        {"account_id": "summary01", "role": "summary", "token_env_var": DEFAULT_SUMMARY_TOKEN_ENV, "priority": 1},
    ]
    rows = [Account(**s) for s in seeds]
    for r in rows:
        reg.upsert_account(r)
    return rows


def auth_check(ctx: AppContext) -> dict[str, Any]:
    report: dict[str, Any] = {"env": ctx.settings.env_report(), "control_sheet": {}, "accounts": []}
    sheet_ok = False
    try:
        url = ctx.registry.repo.backend.spreadsheet_url()
        report["control_sheet"] = {"ok": True, "url": url}
        sheet_ok = True
    except Exception as exc:
        report["control_sheet"] = {"ok": False, "error": str(exc)}
    if not sheet_ok:
        return report
    for account in bootstrap_accounts(ctx):
        entry: dict[str, Any] = {"account_id": account.account_id, "role": account.role, "token_env_var": account.token_env_var, "status": account.status}
        drive = ctx.drive_for(account)
        if drive is None:
            entry.update({"ok": False, "error": f"no credentials ({account.token_env_var} missing?)"})
        else:
            try:
                account = refresh_quota(ctx.registry, drive, account)
                entry.update({"ok": True, "email": account.email, "quota_gb": _gb(account.quota_bytes), "used_gb": _gb(account.used_bytes), "free_gb": _gb(account.free_bytes), "status": account.status})
            except Exception as exc:
                entry.update({"ok": False, "error": str(exc)})
        report["accounts"].append(entry)
    return report


def _gb(n: int | None) -> float | None:
    return round(n / 1024**3, 2) if n is not None else None


def create_control_sheet(ctx: AppContext, title: str = "AI Primer Control Panel") -> dict[str, Any]:
    """Create the control spreadsheet with the summary account (only when AIPRIMER_CONTROL_SHEET_ID is unset)."""
    if ctx.settings.control_sheet_id:
        return {"created": False, "sheet_id": ctx.settings.control_sheet_id}
    service = ctx.services(ctx.summary_token_env).sheets
    resp = service.spreadsheets().create(body={"properties": {"title": title}}, fields="spreadsheetId,spreadsheetUrl").execute()
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
        try:
            if account.role == "raw":
                root = drive.ensure_folder(None, cfg.get("raw_root_name", "AI Primer Raw"))
                inbox = drive.ensure_folder(root["id"], cfg.get("inbox_name", "_Inbox"))
                account.root_folder_id, account.inbox_folder_id = root["id"], inbox["id"]
                entry.update({"root": folder_url(root["id"]), "inbox": folder_url(inbox["id"])})
            else:
                root = drive.ensure_folder(None, cfg.get("summaries_root_name", "AI Primer Summaries"))
                account.summaries_folder_id = root["id"]
                entry.update({"summaries": folder_url(root["id"])})
            account.quota_checked_at = now_iso()
            ctx.registry.upsert_account(account)
            refresh_quota(ctx.registry, drive, account)
            entry["ok"] = True
        except Exception as exc:
            entry.update({"ok": False, "error": str(exc)})
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
        "accounts": [{"account_id": a.account_id, "role": a.role, "status": a.status, "free_gb": _gb(a.free_bytes)} for a in reg.accounts()],
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
