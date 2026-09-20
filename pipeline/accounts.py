"""Storage account routing: which raw-file account receives a new file, and how full each one is."""

from __future__ import annotations

from .drive import DriveClient
from .google_auth import resolve_auth_kind, service_account_email
from .ids import now_iso
from .models import Account
from .registry import Registry

SAFETY_MARGIN = 1.05  # keep 5% headroom

GMAIL_QUOTA_HINT = (
    "a service account has no Drive storage of its own, so Google refuses its uploads into a personal Gmail Drive "
    "(even into a folder shared with it); give it a Google Workspace Shared Drive, or switch the account to the "
    "Gmail sign-in (refresh token)"
)


def is_service_account(registry: Registry, account: Account) -> bool:
    return resolve_auth_kind(registry.settings, account.token_env_var, account.auth_kind) == "service_account"


def refresh_quota(registry: Registry, drive: DriveClient, account: Account) -> Account:
    """Record free space. Service accounts report their own (empty, 0-byte) quota, which says nothing about
    the Shared Drive they write to, so their quota is recorded as unknown and they are never marked full here."""
    if is_service_account(registry, account):
        account.quota_bytes = None
        account.used_bytes = None
        account.quota_checked_at = now_iso()
        if not account.email:
            account.email = service_account_email(registry.settings, account.token_env_var)
        if account.drive_id and drive.container(account.drive_id) is None:
            account.notes = (account.notes + " " if account.notes else "") + f"[container {account.drive_id} not reachable {now_iso()}]"
        registry.upsert_account(account)
        return account
    info = drive.quota()
    account.quota_bytes = info.get("limit")
    account.used_bytes = info.get("usage")
    account.quota_checked_at = now_iso()
    if info.get("email") and not account.email:
        account.email = info["email"]
    if account.quota_bytes is not None and account.used_bytes is not None and account.status == "active":
        if account.quota_bytes - account.used_bytes < 50 * 1024 * 1024:
            account.status = "full"
    registry.upsert_account(account)
    return account


def mark_full(registry: Registry, account: Account) -> None:
    account.status = "full"
    account.quota_checked_at = now_iso()
    registry.upsert_account(account)


def has_room(account: Account, needed_bytes: int) -> bool:
    if account.status != "active":
        return False
    free = account.free_bytes
    if free is None:
        return True  # unknown quota (shared drives): try it, a quota error marks it full
    return free >= int(needed_bytes * SAFETY_MARGIN)


def pick_raw_account(registry: Registry, needed_bytes: int, preferred_id: str | None = None) -> Account:
    """Prefer the account that already holds the stage; otherwise the first active account by priority with room."""
    accounts = registry.raw_accounts()
    if preferred_id:
        for a in accounts:
            if a.account_id == preferred_id and has_room(a, needed_bytes):
                return a
    for a in accounts:
        if has_room(a, needed_bytes):
            return a
    if not accounts:
        raise NoStorageError("no active raw-file account in the Accounts tab; run /setup")
    raise NoStorageError(
        f"no raw-file account has {needed_bytes} bytes free; add a new account "
        "(see README: adding a raw account) and a row in the Accounts tab"
    )


class NoStorageError(RuntimeError):
    pass
