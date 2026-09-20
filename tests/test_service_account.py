"""Service-account mode: key parsing, credential kind detection, shared-drive setup, quota behaviour."""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest
import yaml

from pipeline.accounts import GMAIL_QUOTA_HINT, NoStorageError, has_room, is_service_account, refresh_quota
from pipeline.context import AppContext, default_key_env
from pipeline.drive import DriveClient
from pipeline.google_auth import credentials_for, detect_auth_kind, parse_service_account_value, resolve_auth_kind, service_account_email
from pipeline.ingest import Ingestor
from pipeline.models import Account
from pipeline.registry import Registry
from pipeline.setup_cmds import bootstrap_accounts, init_drive
from pipeline.sheets import SheetsRepo
from pipeline.taxonomy import import_seed

SEED = Path(__file__).resolve().parent.parent / "config" / "taxonomy.seed.yaml"


def make_sa_json() -> str:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
    return json.dumps(
        {
            "type": "service_account",
            "project_id": "aiprimer-test",
            "private_key_id": "abc",
            "private_key": pem,
            "client_email": "ai-primer-rawfile-0001@aiprimer-test.iam.gserviceaccount.com",
            "client_id": "1234567890",
            "token_uri": "https://oauth2.googleapis.com/token",
        }
    )


@pytest.fixture(scope="module")
def sa_json() -> str:
    return make_sa_json()


def test_parse_service_account_value(sa_json, tmp_path):
    assert parse_service_account_value(sa_json)["client_email"].endswith("gserviceaccount.com")
    b64 = base64.b64encode(sa_json.encode()).decode()
    assert parse_service_account_value(b64)["type"] == "service_account"
    p = tmp_path / "key.json"
    p.write_text(sa_json)
    assert parse_service_account_value(str(p))["project_id"] == "aiprimer-test"
    assert parse_service_account_value("1//0gRefreshTokenLooksLikeThis") is None
    assert parse_service_account_value('{"type": "authorized_user"}') is None
    assert parse_service_account_value("") is None


def test_detect_and_resolve_kind(settings, sa_json, monkeypatch):
    assert detect_auth_kind(sa_json) == "service_account"
    assert detect_auth_kind("1//0gRefreshToken") == "oauth"
    assert detect_auth_kind("") == ""
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON", sa_json)
    assert resolve_auth_kind(settings, "GOOGLE_SERVICE_ACCOUNT_JSON", "auto") == "service_account"
    assert resolve_auth_kind(settings, "GOOGLE_SERVICE_ACCOUNT_JSON", "oauth") == "oauth"  # explicit wins
    assert service_account_email(settings, "GOOGLE_SERVICE_ACCOUNT_JSON").startswith("ai-primer-rawfile-0001@")
    creds = credentials_for(settings, "GOOGLE_SERVICE_ACCOUNT_JSON")
    assert creds.service_account_email.startswith("ai-primer-rawfile-0001@")
    assert "https://www.googleapis.com/auth/drive" in creds.scopes


def test_default_key_env_prefers_service_account(monkeypatch, sa_json):
    monkeypatch.delenv("GOOGLE_SERVICE_ACCOUNT_JSON", raising=False)
    assert default_key_env("raw") == "GOOGLE_REFRESH_TOKEN_RAW01"
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON", sa_json)
    assert default_key_env("raw") == "GOOGLE_SERVICE_ACCOUNT_JSON" and default_key_env("summary") == "GOOGLE_SERVICE_ACCOUNT_JSON"
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON_RAW", sa_json)
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON_SUMMARY", sa_json)
    assert default_key_env("raw") == "GOOGLE_SERVICE_ACCOUNT_JSON_RAW" and default_key_env("summary") == "GOOGLE_SERVICE_ACCOUNT_JSON_SUMMARY"


def _ctx(settings, fake_sheets, fake_drive, fake_apify):
    repo = SheetsRepo(fake_sheets)
    repo.ensure_tabs()
    reg = Registry(repo, settings)
    drive = DriveClient(fake_drive)
    return AppContext(settings, registry=reg, drive_factory=lambda a: drive, apify_runner=fake_apify), reg, drive


def test_bootstrap_and_init_drive_in_shared_drives(settings, fake_sheets, fake_drive, fake_apify, monkeypatch, sa_json):
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON", sa_json)
    monkeypatch.setenv("AIPRIMER_RAW_DRIVE_ID", "0ARawDrive")
    monkeypatch.setenv("AIPRIMER_SUMMARY_DRIVE_ID", "0ASumDrive")
    fake_drive.add_shared_drive("0ARawDrive", "AI Primer Raw Files")
    fake_drive.add_shared_drive("0ASumDrive", "AI Primer Summaries")
    ctx, reg, drive = _ctx(settings, fake_sheets, fake_drive, fake_apify)
    rows = bootstrap_accounts(ctx)
    assert {r.account_id: r.token_env_var for r in rows} == {"raw01": "GOOGLE_SERVICE_ACCOUNT_JSON", "summary01": "GOOGLE_SERVICE_ACCOUNT_JSON"}
    assert reg.account("raw01").drive_id == "0ARawDrive"
    assert is_service_account(reg, reg.account("raw01"))
    report = init_drive(ctx)
    assert all(e.get("ok") for e in report["accounts"]), report
    raw = reg.account("raw01")
    assert fake_drive.path_of(raw.root_folder_id) == "AI Primer Raw Files/AI Primer Raw"
    assert fake_drive.path_of(raw.inbox_folder_id) == "AI Primer Raw Files/AI Primer Raw/_Inbox"
    assert fake_drive.path_of(reg.account("summary01").summaries_folder_id) == "AI Primer Summaries/AI Primer Summaries"
    # quota is unknown for service accounts: never marked full, always has room
    assert raw.quota_bytes is None and raw.status == "active" and has_room(raw, 10**9)
    assert raw.email.startswith("ai-primer-rawfile-0001@")
    # second run changes nothing
    report2 = init_drive(ctx)
    assert reg.account("raw01").root_folder_id == raw.root_folder_id and all(e.get("ok") for e in report2["accounts"])


def test_init_drive_without_shared_drive_explains(settings, fake_sheets, fake_drive, fake_apify, monkeypatch, sa_json):
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON", sa_json)
    monkeypatch.delenv("AIPRIMER_RAW_DRIVE_ID", raising=False)
    monkeypatch.delenv("AIPRIMER_SUMMARY_DRIVE_ID", raising=False)
    ctx, reg, drive = _ctx(settings, fake_sheets, fake_drive, fake_apify)
    report = init_drive(ctx)
    assert all(not e.get("ok") and "Shared Drive" in e["error"] for e in report["accounts"])


def test_refresh_quota_for_refresh_token_account_still_uses_about(settings, fake_sheets, fake_drive, fake_apify, monkeypatch):
    monkeypatch.setenv("GOOGLE_REFRESH_TOKEN_RAW01", "1//0gRefreshToken")
    ctx, reg, drive = _ctx(settings, fake_sheets, fake_drive, fake_apify)
    acc = Account(account_id="raw01", token_env_var="GOOGLE_REFRESH_TOKEN_RAW01")
    reg.upsert_account(acc)
    acc = refresh_quota(reg, drive, acc)
    assert acc.quota_bytes == fake_drive.limit and not is_service_account(reg, acc)


def test_ingest_quota_error_with_service_account_is_explained(settings, fake_sheets, fake_drive, fake_apify, monkeypatch, sa_json, tmp_path):
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON", sa_json)
    ctx, reg, drive = _ctx(settings, fake_sheets, fake_drive, fake_apify)
    import_seed(reg, yaml.safe_load(SEED.read_text()))
    # a Gmail-style setup: folder shared with the service account, no shared drive
    root = drive.ensure_folder(None, "AI Primer Raw")
    reg.upsert_account(Account(account_id="raw01", token_env_var="GOOGLE_SERVICE_ACCOUNT_JSON", root_folder_id=root["id"]))
    fake_drive.fail_upload_with = RuntimeError("The user's Drive storage quota has been exceeded (storageQuotaExceeded)")
    src = tmp_path / "a.txt"
    src.write_text("hello " * 50)
    with pytest.raises(NoStorageError) as exc:
        Ingestor(ctx).run(str(src), "Body / Immortal Yogi / Rest Body", author="X")
    assert GMAIL_QUOTA_HINT in str(exc.value)
    assert reg.account("raw01").status == "active"  # not marked full: the account is not the problem


def test_auth_check_write_test_reports_google_refusal(settings, fake_sheets, fake_drive, fake_apify, monkeypatch, sa_json):
    from pipeline.setup_cmds import auth_check

    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON", sa_json)
    monkeypatch.setenv("AIPRIMER_CONTROL_SHEET_ID", "sheet123")
    ctx, reg, drive = _ctx(settings, fake_sheets, fake_drive, fake_apify)
    shared = drive.ensure_folder(None, "AI PRIMER - RAWFILE")  # a folder the human shared with the robot
    reg.upsert_account(Account(account_id="raw01", token_env_var="GOOGLE_SERVICE_ACCOUNT_JSON", drive_id=shared["id"]))
    reg.upsert_account(Account(account_id="summary01", role="summary", token_env_var="GOOGLE_SERVICE_ACCOUNT_JSON", drive_id=shared["id"]))
    # first: Google accepts (e.g. a Workspace Shared Drive)
    report = auth_check(ctx)
    raw = next(e for e in report["accounts"] if e["account_id"] == "raw01")
    assert raw["writes_into"]["kind"] == "folder" and raw["write_test"]["can_write"] is True and "warning" not in raw
    assert not [f for f in fake_drive.files.values() if f["name"] == "aiprimer-write-test.txt" and not f["trashed"]]
    # then: Google refuses (a personal Gmail Drive)
    fake_drive.fail_upload_with = RuntimeError("The user's Drive storage quota has been exceeded (storageQuotaExceeded)")
    report = auth_check(ctx)
    raw = next(e for e in report["accounts"] if e["account_id"] == "raw01")
    assert raw["write_test"]["can_write"] is False and "refused" in raw["warning"] and "sign-in" in raw["warning"]
