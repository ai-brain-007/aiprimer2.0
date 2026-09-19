"""Storage account routing: which raw-file account receives a new file."""

from __future__ import annotations

from .drive import DriveClient
from .ids import now_iso
from .models import Account
from .registry import Registry

SAFETY_MARGIN = 1.05  # keep 5% headroom


class NoStorageError(RuntimeError):
    pass


def refresh_quota(registry: Registry, drive: DriveClient, account: Account) -> Account:
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
        return True  # unknown quota: try it, a quota error marks it full
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
        f"no raw-file account has {needed_bytes} bytes free; add a new Gmail account "
        "(see README: adding a raw account) and a row in the Accounts tab"
    )
