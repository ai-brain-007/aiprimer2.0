"""Wiring: settings -> registry (Notion databases, or the v1 Google Sheet) -> storage clients per account
(Backblaze B2, or v1 Google Drive) -> Apify client.

Mode is decided from the environment:
- Notion mode when AIPRIMER_NOTION_PAGE_ID is set (the "AI Primer" page); the token is attached by the proxy
  (API credentials box) or, as a fallback, read from NOTION_TOKEN.
- Backblaze mode when at least one B2_APPLICATION_KEY_<n> is set; each key becomes storage account b2-<n>.
"""

from __future__ import annotations

import os
from typing import Any, Callable

from .apify_yt import ApifyRunner, ApifyYouTube, RealApifyRunner
from .config import Settings, load_settings
from .drive import DriveBackend, DriveClient, GoogleDriveBackend
from .google_auth import SERVICE_ACCOUNT_ENV_DEFAULT, AuthError, GoogleServices
from .models import Account
from .registry import Registry
from .sheets import GoogleSheetsBackend, SheetsRepo

DEFAULT_SUMMARY_TOKEN_ENV = "GOOGLE_REFRESH_TOKEN_SUMMARY01"
DEFAULT_RAW_TOKEN_ENV = "GOOGLE_REFRESH_TOKEN_RAW01"
B2_KEY_ID_PREFIX = "B2_KEY_ID_"
B2_APP_KEY_PREFIX = "B2_APPLICATION_KEY_"


def b2_account_numbers() -> list[str]:
    """Suffixes ("0001", "0002", ...) of the Backblaze application keys present in the environment."""
    return sorted(k[len(B2_APP_KEY_PREFIX):] for k in os.environ if k.startswith(B2_APP_KEY_PREFIX) and os.environ.get(k))


def storage_mode() -> str:
    return "b2" if b2_account_numbers() else "gdrive"


def notion_mode(settings: Settings) -> bool:
    return bool(settings.notion_parent_page_id)


def default_key_env(role: str) -> str:
    """Which environment variable holds the Google credential for a role when the Accounts tab is empty (v1).
    Order: a role-specific service-account key (GOOGLE_SERVICE_ACCOUNT_JSON_RAW / _SUMMARY), then the
    shared key (GOOGLE_SERVICE_ACCOUNT_JSON), then the role's refresh token."""
    role_specific = f"{SERVICE_ACCOUNT_ENV_DEFAULT}_{'SUMMARY' if role == 'summary' else 'RAW'}"
    if os.environ.get(role_specific):
        return role_specific
    if os.environ.get(SERVICE_ACCOUNT_ENV_DEFAULT):
        return SERVICE_ACCOUNT_ENV_DEFAULT
    return DEFAULT_SUMMARY_TOKEN_ENV if role == "summary" else DEFAULT_RAW_TOKEN_ENV


def b2_bucket_names(settings: Settings, n: str) -> tuple[str, str]:
    """(private raw bucket, public media bucket) for account number `n`. B2_BUCKET_<n> overrides the config
    pattern for the raw bucket (bucket names are global across Backblaze, hence the number in the default).
    The public media bucket is optional (Backblaze asks for a payment method to create one): it exists only
    when B2_MEDIA_BUCKET_<n> is set; otherwise pictures go into Notion and videos are embedded from YouTube."""
    cfg = settings.b2
    raw = os.environ.get(f"B2_BUCKET_{n}") or str(cfg.get("raw_bucket_pattern", "ai-primer-raw-{n}")).format(n=n)
    media = os.environ.get(f"B2_MEDIA_BUCKET_{n}", "").strip()
    return raw, media


def default_storage_accounts(settings: Settings) -> list[dict[str, Any]]:
    """Seed rows for the Accounts table, from the environment: one b2-<n> row per Backblaze key (v2), or the
    two Google rows (v1)."""
    nums = b2_account_numbers()
    if nums:
        free = int(settings.b2.get("free_tier_bytes", 10_000_000_000) or 0) or None
        rows = []
        for i, n in enumerate(nums, start=1):
            raw_bucket, media_bucket = b2_bucket_names(settings, n)
            rows.append(
                {
                    "account_id": f"b2-{n}",
                    "role": "raw",
                    "backend": "b2",
                    "token_env_var": f"{B2_APP_KEY_PREFIX}{n}",
                    "key_id_env_var": f"{B2_KEY_ID_PREFIX}{n}",
                    "bucket": raw_bucket,
                    "media_bucket": media_bucket,
                    "priority": i,
                    "quota_bytes": free,
                }
            )
        return rows
    raw_env, sum_env = default_key_env("raw"), default_key_env("summary")
    return [
        {"account_id": "raw01", "role": "raw", "token_env_var": raw_env, "priority": 1, "drive_id": os.environ.get("AIPRIMER_RAW_DRIVE_ID", "")},
        {"account_id": "summary01", "role": "summary", "token_env_var": sum_env, "priority": 1, "drive_id": os.environ.get("AIPRIMER_SUMMARY_DRIVE_ID", "")},
    ]


class AppContext:
    def __init__(
        self,
        settings: Settings | None = None,
        *,
        registry: Registry | None = None,
        drive_factory: Callable[[Account], DriveClient | None] | None = None,
        apify_runner: ApifyRunner | None = None,
        notion_backend: Any | None = None,
    ):
        self.settings = settings or load_settings()
        self._registry = registry
        self._drive_factory = drive_factory
        self._apify_runner = apify_runner
        self._notion = notion_backend
        self._services: dict[str, GoogleServices] = {}
        self._drives: dict[str, Any] = {}
        self._media: dict[str, Any] = {}

    # ---- credentials (v1 Google)
    @property
    def summary_token_env(self) -> str:
        return (self.settings.section("accounts").get("summary_token_env_var")) or default_key_env("summary")

    def services(self, token_env_var: str, auth_kind: str = "auto") -> GoogleServices:
        if token_env_var not in self._services:
            self._services[token_env_var] = GoogleServices(self.settings, token_env_var, auth_kind)
        return self._services[token_env_var]

    # ---- notion
    @property
    def notion(self) -> Any | None:
        """The Notion backend in Notion mode, else None."""
        if self._notion is None and notion_mode(self.settings):
            from .notion import RealNotionBackend

            self._notion = RealNotionBackend(token=self.settings.notion_token, version=str(self.settings.notion.get("version", "2022-06-28")))
        return self._notion

    # ---- registry
    @property
    def registry(self) -> Registry:
        if self._registry is None:
            if notion_mode(self.settings):
                from .notion import NotionRepo

                self._registry = Registry(NotionRepo(self.notion, self.settings.notion_parent_page_id), self.settings)
            else:
                sheet_id = self.settings.control_sheet_id
                if not sheet_id:
                    raise AuthError("neither AIPRIMER_NOTION_PAGE_ID (Notion mode) nor AIPRIMER_CONTROL_SHEET_ID (Google mode) is set: see README.md, Setup")
                backend = GoogleSheetsBackend(self.services(self.summary_token_env).sheets, sheet_id)
                self._registry = Registry(SheetsRepo(backend), self.settings)
        return self._registry

    # ---- storage per account
    def drive_for(self, account: Account) -> Any | None:
        """The storage client for an account: B2StorageClient (backend b2) or DriveClient (backend gdrive).
        Both offer the same methods (see pipeline/drive.py::DriveClient)."""
        if account.account_id in self._drives:
            return self._drives[account.account_id]
        client: Any | None
        if self._drive_factory is not None:
            client = self._drive_factory(account)
        elif account.backend == "b2":
            client = self._b2_client(account, account.bucket)
        else:
            if account.backend != "gdrive" or not account.token_env_var:
                return None
            try:
                backend: DriveBackend = GoogleDriveBackend(self.services(account.token_env_var, account.auth_kind).drive, int(self.settings.drive.get("upload_chunk_mb", 8)))
            except AuthError:
                return None
            client = DriveClient(backend)
        if client is not None:
            self._drives[account.account_id] = client
        return client

    def _b2_client(self, account: Account, bucket: str) -> Any | None:
        if not bucket:
            return None
        key_id_env = account.key_id_env_var or account.token_env_var.replace(B2_APP_KEY_PREFIX, B2_KEY_ID_PREFIX)
        if not os.environ.get(account.token_env_var) and not os.environ.get(key_id_env):
            return None
        from .storage_b2 import B2StorageClient, RealB2Backend

        threshold = int(self.settings.b2.get("large_file_threshold_mb", 100)) * 1024 * 1024
        backend = RealB2Backend.from_env(key_id_env, account.token_env_var, bucket)
        backend.large_file_threshold_bytes = threshold
        return B2StorageClient(backend)

    def media_storage(self, account: Account) -> Any | None:
        """Client for the account's public media bucket (pictures and clips embedded in pages)."""
        if account.account_id in self._media:
            return self._media[account.account_id]
        client = None
        if account.backend == "b2" and account.media_bucket:
            if self._drive_factory is not None:
                client = self._drive_factory(account.model_copy(update={"bucket": account.media_bucket, "account_id": account.account_id + ":media"}))
            else:
                client = self._b2_client(account, account.media_bucket)
        if client is not None:
            self._media[account.account_id] = client
        return client

    def drive_by_id(self, account_id: str) -> Any | None:
        account = self.registry.account(account_id)
        return self.drive_for(account) if account else None

    def drives(self) -> dict[str, Any]:
        out = {}
        for a in self.registry.accounts():
            d = self.drive_for(a)
            if d is not None:
                out[a.account_id] = d
        return out

    # ---- apify
    @property
    def apify(self) -> ApifyYouTube:
        runner = self._apify_runner or RealApifyRunner(self.settings.apify_token)
        return ApifyYouTube(runner, self.settings)
