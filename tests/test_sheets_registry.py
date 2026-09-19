from __future__ import annotations

from pipeline.models import Account, Resource
from pipeline.registry import Registry
from pipeline.sheets import SheetsRepo, a1, col_letter


def test_col_letter_and_a1():
    assert col_letter(0) == "A"
    assert col_letter(25) == "Z"
    assert col_letter(26) == "AA"
    assert a1("Resources", 0, 1, 2, 1) == "'Resources'!A1:C1"


def test_ensure_tabs_creates_headers_and_readme(fake_sheets):
    repo = SheetsRepo(fake_sheets)
    report = repo.ensure_tabs()
    assert "Accounts" in report["created"] and "Readme" in report["created"]
    assert fake_sheets.tabs["Resources"][0] == Resource.headers()
    assert "Resources" in fake_sheets.frozen
    # second run is a no-op
    report2 = repo.ensure_tabs()
    assert report2["created"] == [] and report2["columns_added"] == {}


def test_ensure_tabs_appends_missing_columns(fake_sheets):
    fake_sheets.tabs["Accounts"] = [["account_id", "email"], ["raw01", "a@b.c"]]
    repo = SheetsRepo(fake_sheets)
    report = repo.ensure_tabs()
    assert report["columns_added"]["Accounts"][0] == "role"
    headers = fake_sheets.tabs["Accounts"][0]
    assert headers[:2] == ["account_id", "email"] and "priority" in headers
    rows = repo.load("Accounts")
    assert rows[0].account_id == "raw01" and rows[0].email == "a@b.c" and rows[0].status == "active"


def test_append_update_upsert_roundtrip(fake_sheets, settings):
    repo = SheetsRepo(fake_sheets)
    repo.ensure_tabs()
    reg = Registry(repo, settings)
    reg.upsert_account(Account(account_id="raw01", email="raw@x", token_env_var="GOOGLE_REFRESH_TOKEN_RAW01", quota_bytes=100, used_bytes=10))
    reg.upsert_account(Account(account_id="sum01", role="summary"))
    assert [a.account_id for a in reg.accounts()] == ["raw01", "sum01"]
    acc = reg.account("raw01")
    acc.used_bytes = 42
    reg.upsert_account(acc)
    # the fake sheet holds the updated value at the right row
    assert fake_sheets.tabs["Accounts"][1][Account.headers().index("used_bytes")] == "42"
    assert reg.account("raw01").used_bytes == 42
    # a fresh repo sees the same data
    reg2 = Registry(SheetsRepo(fake_sheets), settings)
    assert reg2.account("raw01").free_bytes == 58


def test_resources_lists_and_lookup(fake_sheets, settings):
    repo = SheetsRepo(fake_sheets)
    repo.ensure_tabs()
    reg = Registry(repo, settings)
    r = Resource(resource_id="R-YT-abc", title="Boxing footwork basics", natural_key="yt:abc", author_id="A-x", data_file_ids=["f1", "f2"])
    reg.upsert_resource(r)
    got = reg.resource("R-YT-abc")
    assert got.data_file_ids == ["f1", "f2"] and got.updated_at
    assert reg.find_resource_by_natural_key("yt:abc").resource_id == "R-YT-abc"
    assert reg.find_resources("footwork")[0].resource_id == "R-YT-abc"
    assert reg.resources_for_author("A-x")[0].title.startswith("Boxing")


def test_job_logging_redacts_secrets(fake_sheets, settings):
    repo = SheetsRepo(fake_sheets)
    repo.ensure_tabs()
    reg = Registry(repo, settings)
    import time

    job = reg.log_job("ingest run", {"source": "x", "apify_token": "apify_api_secret"}, "ok", time.time(), resources=["R-1"])
    assert "apify_api_secret" not in job.args and "<redacted>" in job.args
    assert fake_sheets.tabs["Jobs"][1][0] == job.job_id
