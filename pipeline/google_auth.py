"""Google credentials per account, and API service builders.

Two kinds of credential, chosen per account (Accounts tab, column `auth_kind`, default `auto`):

- **service_account** (primary): the JSON key of a Google Cloud service account, stored in one environment
  variable (the whole JSON on one line, base64 of it, or a path to the file). The service account acts as
  itself. Google gives service accounts NO Drive storage, so files must live in a Google Workspace
  **Shared Drive** the service account is a member of; on a personal Gmail Drive it can read and edit files
  shared with it but cannot upload or create anything.
- **oauth** (fallback): a refresh token obtained once by a human clicking Allow (scripts/auth_local.py or
  `pipeline auth url` / `auth exchange`). The pipeline then acts AS that Gmail account and uses its quota.

`auto` looks at the value: it starts with `{` (or decodes to JSON) -> service account, otherwise refresh token.

Behind the cloud-session proxy, httplib2 (used by google-api-python-client) needs the CA bundle and
SOCKS/CONNECT support (pysocks); `_http()` wires both.
"""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path
from typing import Any

from .config import GOOGLE_SCOPES, Settings

SERVICE_ACCOUNT_ENV_DEFAULT = "GOOGLE_SERVICE_ACCOUNT_JSON"


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


# ----------------------------------------------------------------------------- credential parsing


def parse_service_account_value(value: str) -> dict[str, Any] | None:
    """Accept the key JSON itself, its base64, or a path to the .json file. Returns the dict or None."""
    text = (value or "").strip()
    if not text:
        return None
    if text.startswith("{"):
        try:
            info = json.loads(text)
        except json.JSONDecodeError:
            return None
        return info if info.get("type") == "service_account" else None
    if len(text) < 4096 and os.path.exists(text) and Path(text).is_file():
        try:
            info = json.loads(Path(text).read_text(encoding="utf-8"))
            return info if info.get("type") == "service_account" else None
        except (OSError, json.JSONDecodeError):
            return None
    try:
        decoded = base64.b64decode(text, validate=False).decode("utf-8")
        info = json.loads(decoded)
        return info if isinstance(info, dict) and info.get("type") == "service_account" else None
    except Exception:
        return None


def detect_auth_kind(value: str) -> str:
    return "service_account" if parse_service_account_value(value) else ("oauth" if value.strip() else "")


def resolve_auth_kind(settings: Settings, env_var: str, declared: str = "auto") -> str:
    if declared in ("service_account", "oauth"):
        return declared
    return detect_auth_kind(settings.refresh_token(env_var))


def service_account_email(settings: Settings, env_var: str) -> str:
    info = parse_service_account_value(settings.refresh_token(env_var))
    return str(info.get("client_email", "")) if info else ""


def credentials_for(settings: Settings, token_env_var: str, auth_kind: str = "auto"):
    """Build Google credentials for the account whose key/token lives in `token_env_var`."""
    if not token_env_var:
        raise AuthError("account has no token_env_var set in the Accounts tab")
    value = settings.refresh_token(token_env_var)
    if not value:
        raise AuthError(f"environment variable {token_env_var} is not set")
    kind = resolve_auth_kind(settings, token_env_var, auth_kind)
    if kind == "service_account":
        from google.oauth2 import service_account

        info = parse_service_account_value(value)
        if info is None:
            raise AuthError(f"{token_env_var} does not contain a service-account key (JSON with type=service_account)")
        creds = service_account.Credentials.from_service_account_info(info, scopes=GOOGLE_SCOPES)
        subject = os.environ.get("GOOGLE_SA_IMPERSONATE", "").strip()
        if subject:  # domain-wide delegation (Workspace only)
            creds = creds.with_subject(subject)
        return creds
    from google.oauth2.credentials import Credentials

    if not settings.google_client_id or not settings.google_client_secret:
        raise AuthError("GOOGLE_OAUTH_CLIENT_ID / GOOGLE_OAUTH_CLIENT_SECRET are not set (needed for refresh-token accounts)")
    return Credentials(
        token=None,
        refresh_token=value,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=settings.google_client_id,
        client_secret=settings.google_client_secret,
        scopes=settings.google_oauth_scopes,
    )


def build_service(api: str, version: str, creds) -> Any:
    from google_auth_httplib2 import AuthorizedHttp
    from googleapiclient.discovery import build

    authed = AuthorizedHttp(creds, http=_http())
    return build(api, version, http=authed, cache_discovery=False)


# ----------------------------------------------------------------------------- OAuth fallback helpers

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
    # No PKCE: `auth url` and `auth exchange` are two separate processes (often two sessions), so a code
    # verifier generated for the link would be gone by the time the code is exchanged and Google would
    # answer `invalid_grant`. A Desktop-app client exchanges the code with its client secret instead.
    flow = Flow.from_client_config(client_config, scopes=settings.google_oauth_scopes, autogenerate_code_verifier=False)
    flow.redirect_uri = f"http://localhost:{port}/"
    return flow


def consent_url(settings: Settings, port: int = DEFAULT_LOOPBACK_PORT, client_id: str | None = None, client_secret: str | None = None) -> str:
    """The link the user opens (signed in as the Gmail account to authorise) and clicks Allow on."""
    flow = _flow(settings, port, client_id, client_secret)
    url, _state = flow.authorization_url(access_type="offline", prompt="consent", include_granted_scopes="true")
    return url


def extract_auth_code(pasted: str) -> str:
    """Accept the full http://localhost:PORT/?code=...&scope=... address, or a bare code."""
    import re
    from urllib.parse import parse_qs, urlparse

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
    os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")  # the loopback redirect is plain http by design
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
    """Lazily built Drive / Sheets / Docs services for one account (identified by its key/token env var)."""

    def __init__(self, settings: Settings, token_env_var: str, auth_kind: str = "auto"):
        self.settings = settings
        self.token_env_var = token_env_var
        self.auth_kind = auth_kind
        self._creds = None
        self._services: dict[str, Any] = {}

    @property
    def kind(self) -> str:
        return resolve_auth_kind(self.settings, self.token_env_var, self.auth_kind)

    @property
    def creds(self):
        if self._creds is None:
            self._creds = credentials_for(self.settings, self.token_env_var, self.auth_kind)
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
