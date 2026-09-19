"""Wiring: settings -> Google services per account -> registry, drive clients, Apify client."""

from __future__ import annotations

from typing import Callable

from .apify_yt import ApifyRunner, ApifyYouTube, RealApifyRunner
from .config import Settings, load_settings
from .drive import DriveBackend, DriveClient, GoogleDriveBackend
from .google_auth import AuthError, GoogleServices
from .models import Account
from .registry import Registry
from .sheets import GoogleSheetsBackend, SheetsRepo

DEFAULT_SUMMARY_TOKEN_ENV = "GOOGLE_REFRESH_TOKEN_SUMMARY01"
DEFAULT_RAW_TOKEN_ENV = "GOOGLE_REFRESH_TOKEN_RAW01"


class AppContext:
    def __init__(
        self,
        settings: Settings | None = None,
        *,
        registry: Registry | None = None,
        drive_factory: Callable[[Account], DriveClient | None] | None = None,
        apify_runner: ApifyRunner | None = None,
    ):
        self.settings = settings or load_settings()
        self._registry = registry
        self._drive_factory = drive_factory
        self._apify_runner = apify_runner
        self._services: dict[str, GoogleServices] = {}
        self._drives: dict[str, DriveClient] = {}

    # ---- credentials
    @property
    def summary_token_env(self) -> str:
        return (self.settings.section("accounts").get("summary_token_env_var")) or DEFAULT_SUMMARY_TOKEN_ENV

    def services(self, token_env_var: str) -> GoogleServices:
        if token_env_var not in self._services:
            self._services[token_env_var] = GoogleServices(self.settings, token_env_var)
        return self._services[token_env_var]

    # ---- registry (control sheet, read with the summary account's credentials)
    @property
    def registry(self) -> Registry:
        if self._registry is None:
            sheet_id = self.settings.control_sheet_id
            if not sheet_id:
                raise AuthError("AIPRIMER_CONTROL_SHEET_ID is not set: run `pipeline setup init-sheet` first")
            backend = GoogleSheetsBackend(self.services(self.summary_token_env).sheets, sheet_id)
            self._registry = Registry(SheetsRepo(backend), self.settings)
        return self._registry

    # ---- drive per account
    def drive_for(self, account: Account) -> DriveClient | None:
        if account.account_id in self._drives:
            return self._drives[account.account_id]
        client: DriveClient | None
        if self._drive_factory is not None:
            client = self._drive_factory(account)
        else:
            if account.backend != "gdrive" or not account.token_env_var:
                return None
            try:
                backend: DriveBackend = GoogleDriveBackend(self.services(account.token_env_var).drive, int(self.settings.drive.get("upload_chunk_mb", 8)))
            except AuthError:
                return None
            client = DriveClient(backend)
        if client is not None:
            self._drives[account.account_id] = client
        return client

    def drive_by_id(self, account_id: str) -> DriveClient | None:
        account = self.registry.account(account_id)
        return self.drive_for(account) if account else None

    def drives(self) -> dict[str, DriveClient]:
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
