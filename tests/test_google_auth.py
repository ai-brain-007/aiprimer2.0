from __future__ import annotations

import pytest

from pipeline.google_auth import AuthError, consent_url, extract_auth_code


def test_extract_auth_code_from_redirect_url():
    url = "http://localhost:8765/?state=abc&code=4%2F0AbCdEf-ghi_jkl&scope=https://www.googleapis.com/auth/drive"
    assert extract_auth_code(url) == "4/0AbCdEf-ghi_jkl"
    assert extract_auth_code("code=4/0xyz&scope=x") == "4/0xyz"
    assert extract_auth_code("4/0AbCdEfGhIjKlMnOpQrStUvWxYz0123456789") == "4/0AbCdEfGhIjKlMnOpQrStUvWxYz0123456789"
    with pytest.raises(AuthError):
        extract_auth_code("http://localhost:8765/?error=access_denied")
    with pytest.raises(AuthError):
        extract_auth_code("hello world")


def test_consent_url_uses_client_and_loopback(settings, monkeypatch):
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "id-123.apps.googleusercontent.com")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "not-a-real-secret")
    url = consent_url(settings, port=8765)
    assert url.startswith("https://accounts.google.com/o/oauth2/auth?")
    assert "client_id=id-123.apps.googleusercontent.com" in url
    assert "redirect_uri=http%3A%2F%2Flocalhost%3A8765%2F" in url
    assert "access_type=offline" in url and "prompt=consent" in url
    assert "auth%2Fdrive" in url and "auth%2Fspreadsheets" in url and "auth%2Fdocuments" in url


def test_consent_url_requires_client(settings, monkeypatch):
    monkeypatch.delenv("GOOGLE_OAUTH_CLIENT_ID", raising=False)
    monkeypatch.delenv("GOOGLE_OAUTH_CLIENT_SECRET", raising=False)
    with pytest.raises(AuthError):
        consent_url(settings)


def test_consent_url_scopes_follow_config(settings, monkeypatch):
    from urllib.parse import parse_qs, urlparse

    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "id-123.apps.googleusercontent.com")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "secret")
    settings.config["google"] = {"oauth_scopes": ["https://www.googleapis.com/auth/drive.file"]}
    q = parse_qs(urlparse(consent_url(settings)).query)
    assert q["scope"] == ["https://www.googleapis.com/auth/drive.file"]
    assert q["access_type"] == ["offline"] and q["prompt"] == ["consent"]
    settings.config.pop("google")  # no config -> full Drive, Sheets and Docs
    q = parse_qs(urlparse(consent_url(settings)).query)
    assert set(q["scope"][0].split()) == {
        "https://www.googleapis.com/auth/drive",
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/documents",
    }


def test_refresh_token_credentials_use_configured_scopes(settings, monkeypatch):
    from pipeline.google_auth import credentials_for

    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "id-123.apps.googleusercontent.com")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "secret")
    monkeypatch.setenv("GOOGLE_REFRESH_TOKEN_RAW01", "1//0gRefreshToken")
    settings.config["google"] = {"oauth_scopes": ["https://www.googleapis.com/auth/drive.file"]}
    creds = credentials_for(settings, "GOOGLE_REFRESH_TOKEN_RAW01")
    assert list(creds.scopes) == ["https://www.googleapis.com/auth/drive.file"]
