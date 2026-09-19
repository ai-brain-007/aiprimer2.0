"""Shared pytest fixtures: in-memory fakes for Google Sheets / Drive and the Apify runner.

Also makes the repository root importable when pytest is started from any directory.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from pipeline.config import load_settings  # noqa: E402
from tests.fakes import FakeApifyRunner, FakeDriveBackend, FakeSheetsBackend  # noqa: E402


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
