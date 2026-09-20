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


@pytest.fixture(autouse=True)
def _clean_credential_env(monkeypatch):
    """Tests use in-memory fakes; the cloud environment's real credentials and ids must never leak in."""
    for var in (
        "GOOGLE_SERVICE_ACCOUNT_JSON", "GOOGLE_SERVICE_ACCOUNT_JSON_RAW", "GOOGLE_SERVICE_ACCOUNT_JSON_SUMMARY",
        "AIPRIMER_RAW_DRIVE_ID", "AIPRIMER_SUMMARY_DRIVE_ID", "AIPRIMER_CONTROL_SHEET_ID",
        "GOOGLE_OAUTH_CLIENT_ID", "GOOGLE_OAUTH_CLIENT_SECRET", "APIFY_TOKEN",
    ):
        monkeypatch.delenv(var, raising=False)
    for var in [v for v in os.environ if v.startswith("GOOGLE_REFRESH_TOKEN_")]:
        monkeypatch.delenv(var, raising=False)


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
