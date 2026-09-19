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
