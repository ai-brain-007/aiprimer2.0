"""Shared pytest fixtures: in-memory fakes for Google Sheets / Drive and the Apify runner.

Also makes the repository root importable when pytest is started from any directory.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from pipeline.config import load_settings  # noqa: E402
from tests.fakes import FakeApifyRunner, FakeDriveBackend, FakeSheetsBackend  # noqa: E402


_LIVE_ENV = frozenset({
    "AIPRIMER_NOTION_PAGE_ID", "NOTION_TOKEN", "AIPRIMER_CONFIG", "APIFY_TOKEN", "AIPRIMER_CONTROL_SHEET_ID",
    "AIPRIMER_RAW_DRIVE_ID", "AIPRIMER_SUMMARY_DRIVE_ID", "GOOGLE_SERVICE_ACCOUNT_JSON", "GOOGLE_SERVICE_ACCOUNT_JSON_RAW",
    "GOOGLE_SERVICE_ACCOUNT_JSON_SUMMARY", "GOOGLE_OAUTH_CLIENT_ID", "GOOGLE_OAUTH_CLIENT_SECRET",
})


@pytest.fixture(autouse=True)
def _no_live_credentials(monkeypatch):
    """The suite runs offline against fakes. In the cloud environment the real v2 variables (Notion page id,
    Backblaze keys and bucket names) are set; they must not leak into the tests, which set their own."""
    for name in list(os.environ):
        if name in _LIVE_ENV or name.startswith(("B2_", "GOOGLE_REFRESH_TOKEN_")):
            monkeypatch.delenv(name, raising=False)


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setenv("AIPRIMER_CACHE_DIR", str(tmp_path / ".cache"))
    monkeypatch.setenv("AIPRIMER_WORK_DIR", str(tmp_path / ".work"))
    return load_settings(repo_root=REPO_ROOT)


@pytest.fixture
def fake_sheets():
    return FakeSheetsBackend()


@pytest.fixture
def fake_drive(tmp_path):
    return FakeDriveBackend(storage_dir=tmp_path / "drive_blobs", email="ai.primer.rawfile.0001@gmail.com")


@pytest.fixture
def fake_apify():
    return FakeApifyRunner()
