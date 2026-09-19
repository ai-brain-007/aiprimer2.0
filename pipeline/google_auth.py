"""Google credentials from a refresh token per account, and API service builders.

Why refresh tokens: service accounts have no Drive storage quota (since 2023) and cannot own files
in a consumer Gmail Drive. A refresh token obtained once with scripts/auth_local.py lets the pipeline
act AS the Gmail account, so files are owned by that account and use its quota.

Behind the cloud-session proxy, httplib2 (used by google-api-python-client) needs the CA bundle and
SOCKS/CONNECT support (pysocks); `_http()` wires both.
"""

from __future__ import annotations

import os
from typing import Any

from .config import GOOGLE_SCOPES, Settings


class AuthError(RuntimeError):
    pass


def _ca_certs() -> str | None:
    for var in ("HTTPLIB2_CA_CERTS", "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE"):
        value = os.environ.get(var)
        if value and os.path.exists(value):
            return value
    default = "/root/.ccr/ca-bundle.crt"
    return default if os.path.exists(default) else None


def _http():
    import httplib2

    return httplib2.Http(ca_certs=_ca_certs(), timeout=120)


def credentials_for(settings: Settings, token_env_var: str):
    """Build google.oauth2 Credentials for the account whose refresh token lives in `token_env_var`."""
    from google.oauth2.credentials import Credentials

    if not token_env_var:
        raise AuthError("account has no token_env_var set in the Accounts tab")
    refresh_token = settings.refresh_token(token_env_var)
    if not refresh_token:
        raise AuthError(f"environment variable {token_env_var} is not set")
    if not settings.google_client_id or not settings.google_client_secret:
        raise AuthError("GOOGLE_OAUTH_CLIENT_ID / GOOGLE_OAUTH_CLIENT_SECRET are not set")
    return Credentials(
        token=None,
        refresh_token=refresh_token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=settings.google_client_id,
        client_secret=settings.google_client_secret,
        scopes=GOOGLE_SCOPES,
    )


def build_service(api: str, version: str, creds) -> Any:
    from google_auth_httplib2 import AuthorizedHttp
    from googleapiclient.discovery import build

    authed = AuthorizedHttp(creds, http=_http())
    return build(api, version, http=authed, cache_discovery=False)


class GoogleServices:
    """Lazily built Drive / Sheets / Docs services for one account (identified by its token env var)."""

    def __init__(self, settings: Settings, token_env_var: str):
        self.settings = settings
        self.token_env_var = token_env_var
        self._creds = None
        self._services: dict[str, Any] = {}

    @property
    def creds(self):
        if self._creds is None:
            self._creds = credentials_for(self.settings, self.token_env_var)
        return self._creds

    def service(self, api: str, version: str) -> Any:
        key = f"{api}:{version}"
        if key not in self._services:
            self._services[key] = build_service(api, version, self.creds)
        return self._services[key]

    @property
    def drive(self):
        return self.service("drive", "v3")

    @property
    def sheets(self):
        return self.service("sheets", "v4")

    @property
    def docs(self):
        return self.service("docs", "v1")
