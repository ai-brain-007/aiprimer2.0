from __future__ import annotations

from pathlib import Path

import yaml

from pipeline.accounts import NoStorageError, has_room, pick_raw_account, refresh_quota
from pipeline.drive import DriveClient
from pipeline.models import Account, Resource
from pipeline.registry import Registry
from pipeline.sheets import SheetsRepo
from pipeline.taxonomy import Taxonomy, add_node, ensure_node_folder, import_seed, load_taxonomy, rename_node, split_path, sync

SEED = Path(__file__).resolve().parent.parent / "config" / "taxonomy.seed.yaml"


def make_registry(fake_sheets, settings):
    repo = SheetsRepo(fake_sheets)
    repo.ensure_tabs()
    return Registry(repo, settings)


def test_drive_ensure_path_and_idempotent_upload(fake_drive, tmp_path):
    client = DriveClient(fake_drive)
    root = client.ensure_folder(None, "AI Primer Raw")
    chain = client.ensure_path(root["id"], ["Body", "Immortal Yogi", "Rest Body"])
    assert fake_drive.path_of(chain[-1]["id"]) == "AI Primer Raw/Body/Immortal Yogi/Rest Body"
    again = client.ensure_path(root["id"], ["Body", "Immortal Yogi", "Rest Body"])
    assert [c["id"] for c in again] == [c["id"] for c in chain]
    f = tmp_path / "a.txt"
    f.write_text("hello")
    up1 = client.upload(f, "a.txt", chain[-1]["id"], {"resource_id": "R-1", "role": "raw"})
    up2 = client.upload(f, "a.txt", chain[-1]["id"], {"resource_id": "R-1", "role": "raw"})
    assert up1["id"] == up2["id"]
    up3 = client.upload(f, "a.extracted.md", chain[-1]["id"], {"resource_id": "R-1", "role": "text"})
    assert up3["id"] != up1["id"]
    other = client.ensure_path(root["id"], ["Body", "Immortal Yogi", "Fuel Body"])[-1]
    moved = client.move(up1["id"], other["id"])
    assert moved["parents"] == [other["id"]]
    assert client.quota()["usage"] == 10


def test_import_seed_is_idempotent_and_paths_are_set(fake_sheets, settings):
    reg = make_registry(fake_sheets, settings)
    seed = yaml.safe_load(SEED.read_text())
    report = import_seed(reg, seed)
    nodes = reg.nodes()
    assert len([n for n in nodes if n.level == "domain"]) == 4
    assert len([n for n in nodes if n.level == "primer"]) == 9
    assert len([n for n in nodes if n.level == "stage"]) == 65
    assert len(report.created) == 78
    appends = [c for c in fake_sheets.calls if c.startswith("append:") and "Taxonomy" in c]
    assert len(appends) == 1 and appends[0].endswith(":78"), appends  # one write request, not one per node
    tax = load_taxonomy(reg)
    stage = tax.resolve("Body / Immortal Yogi / Rest Body")
    assert stage and stage.level == "stage" and stage.path == "Body / Immortal Yogi / Rest Body"
    assert stage.slug == "body.immortal-yogi.rest-body"
    assert tax.resolve("Boxing").name == "Boxing"
    assert tax.resolve("Review") is None  # ambiguous: two stages called Review
    assert tax.resolve("Mind > Strategist Commander > Review").path.endswith("Strategist Commander / Review")
    report2 = import_seed(reg, seed)
    assert report2.created == [] and len(reg.nodes()) == 78


def test_split_path_and_suggest(fake_sheets, settings):
    assert split_path("Body / Immortal Yogi > Rest Body") == ["Body", "Immortal Yogi", "Rest Body"]
    reg = make_registry(fake_sheets, settings)
    import_seed(reg, yaml.safe_load(SEED.read_text()))
    tax = load_taxonomy(reg)
    hints = tax.suggest_stages("How to sleep better: light exposure, bedroom temperature and recovery", k=3)
    assert any(h["path"].endswith("Rest Body") for h in hints)


def test_folders_rename_and_sync(fake_sheets, fake_drive, settings):
    reg = make_registry(fake_sheets, settings)
    import_seed(reg, yaml.safe_load(SEED.read_text()))
    drive = DriveClient(fake_drive)
    root = drive.ensure_folder(None, "AI Primer Raw")
    acc = Account(account_id="raw01", root_folder_id=root["id"], token_env_var="GOOGLE_REFRESH_TOKEN_RAW01")
    reg.upsert_account(acc)
    tax = load_taxonomy(reg)
    stage = tax.resolve("Body / Immortal Yogi / Rest Body")
    fm = ensure_node_folder(reg, drive, acc, stage.node_id)
    assert fake_drive.path_of(fm.folder_id) == "AI Primer Raw/Body/Immortal Yogi/Rest Body"
    assert len(reg.folders()) == 3  # domain, primer, stage recorded
    fm2 = ensure_node_folder(reg, drive, acc, stage.node_id)
    assert fm2.folder_id == fm.folder_id and len(reg.folders()) == 3
    # a resource in that stage
    reg.upsert_resource(Resource(resource_id="R-1", stage_id=stage.node_id, stage_path=stage.path, account_id="raw01", folder_id=fm.folder_id))
    report = rename_node(reg, stage.node_id, "Sleep and Recovery", lambda a: drive)
    assert report.renamed == ["Rest Body -> Sleep and Recovery"]
    assert fake_drive.files[fm.folder_id]["name"] == "Sleep and Recovery"
    assert reg.resource("R-1").stage_path == "Body / Immortal Yogi / Sleep and Recovery"
    assert reg.resource("R-1").drive_path == "AI Primer Raw / Body / Immortal Yogi / Sleep and Recovery"
    node = reg.node(stage.node_id)
    assert node.previous_names == ["Rest Body"] and node.slug == "body.immortal-yogi.sleep-and-recovery"
    # rename a primer: descendants' paths refresh too
    primer = load_taxonomy(reg).resolve("Body / Immortal Yogi")
    rename_node(reg, primer.node_id, "Immortal Yogi 2", lambda a: drive)
    assert reg.resource("R-1").stage_path == "Body / Immortal Yogi 2 / Sleep and Recovery"
    # user edits the sheet directly, then sync renames the folder
    n = reg.node(stage.node_id)
    n.name = "Rest"
    reg.upsert_nodes([n])
    report = sync(reg, {"raw01": drive})
    assert any("-> Rest" in r for r in report.folders_renamed)
    assert fake_drive.files[fm.folder_id]["name"] == "Rest"
    # orphan detection
    fake_drive.create_folder("Stray", reg.folder_for(primer.node_id, "raw01").folder_id)
    report = sync(reg, {"raw01": drive})
    assert any("Stray" in o for o in report.orphans)


def test_add_node_validation(fake_sheets, settings):
    reg = make_registry(fake_sheets, settings)
    d = add_node(reg, "domain", "Spirit")
    p = add_node(reg, "primer", "Monk", d.node_id)
    s = add_node(reg, "stage", "Meditate", p.node_id, "sit")
    assert s.path == "Spirit / Monk / Meditate"
    assert add_node(reg, "stage", "meditate", p.node_id).node_id == s.node_id  # case-insensitive dedup
    try:
        add_node(reg, "stage", "X", d.node_id)
        assert False, "stage under a domain must fail"
    except ValueError:
        pass


def test_account_routing(fake_sheets, fake_drive, settings):
    reg = make_registry(fake_sheets, settings)
    reg.upsert_account(Account(account_id="raw01", quota_bytes=1000, used_bytes=990, priority=1))
    reg.upsert_account(Account(account_id="raw02", quota_bytes=1000, used_bytes=0, priority=2))
    reg.upsert_account(Account(account_id="raw03", priority=3, status="disabled"))
    assert has_room(reg.account("raw02"), 500)
    assert pick_raw_account(reg, 500).account_id == "raw02"
    assert pick_raw_account(reg, 5, preferred_id="raw01").account_id == "raw01"
    try:
        pick_raw_account(reg, 5000)
        assert False
    except NoStorageError:
        pass
    acc = refresh_quota(reg, DriveClient(fake_drive), reg.account("raw02"))
    assert acc.quota_bytes == fake_drive.limit and acc.email == "ai.primer.rawfile.0001@gmail.com"
