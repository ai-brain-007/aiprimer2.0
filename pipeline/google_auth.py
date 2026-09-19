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


DEFAULT_LOOPBACK_PORT = 8765


def _flow(settings: Settings, port: int, client_id: str | None = None, client_secret: str | None = None):
    """OAuth flow for a 'Desktop app' client using the loopback redirect (no server is actually started:
    the user pastes back the address of the page that fails to load)."""
    from google_auth_oauthlib.flow import Flow

    cid = client_id or settings.google_client_id
    secret = client_secret or settings.google_client_secret
    if not cid or not secret:
        raise AuthError("GOOGLE_OAUTH_CLIENT_ID / GOOGLE_OAUTH_CLIENT_SECRET are not set (add them to the environment variables)")
    client_config = {
        "installed": {
            "client_id": cid,
            "client_secret": secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [f"http://localhost:{port}/"],
        }
    }
    flow = Flow.from_client_config(client_config, scopes=GOOGLE_SCOPES)
    flow.redirect_uri = f"http://localhost:{port}/"
    return flow


def consent_url(settings: Settings, port: int = DEFAULT_LOOPBACK_PORT, client_id: str | None = None, client_secret: str | None = None) -> str:
    """The link the user opens (signed in as the Gmail account to authorise) and clicks Allow on."""
    flow = _flow(settings, port, client_id, client_secret)
    url, _state = flow.authorization_url(access_type="offline", prompt="consent", include_granted_scopes="true")
    return url


def extract_auth_code(pasted: str) -> str:
    """Accept the full http://localhost:PORT/?code=...&scope=... address, or a bare code."""
    from urllib.parse import parse_qs, urlparse

    import re

    text = pasted.strip()
    looks_like_url = "://" in text or "?" in text or "=" in text
    if looks_like_url:
        parsed = urlparse(text if "://" in text else f"http://localhost/?{text.lstrip('?')}")
        code = parse_qs(parsed.query).get("code", [""])[0]
        if code:
            return code
    elif re.fullmatch(r"4/[A-Za-z0-9_\-/]{10,}", text) or re.fullmatch(r"[A-Za-z0-9_\-/]{30,}", text):
        return text
    raise AuthError("could not find the code: paste the full address of the page Google redirected to (it starts with http://localhost)")


def exchange_code(settings: Settings, pasted: str, port: int = DEFAULT_LOOPBACK_PORT, client_id: str | None = None, client_secret: str | None = None) -> dict:
    """Turn the pasted redirect address into a refresh token. Returns {refresh_token, email, scopes}."""
    import os as _os

    _os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")  # the loopback redirect is plain http by design
    flow = _flow(settings, port, client_id, client_secret)
    flow.fetch_token(code=extract_auth_code(pasted))
    creds = flow.credentials
    if not creds.refresh_token:
        raise AuthError("Google returned no refresh token: remove the app at myaccount.google.com/permissions and run `auth url` again")
    email = ""
    try:
        service = build_service("drive", "v3", creds)
        email = service.about().get(fields="user").execute().get("user", {}).get("emailAddress", "")
    except Exception:
        pass
    return {"refresh_token": creds.refresh_token, "email": email, "scopes": list(creds.scopes or [])}


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
