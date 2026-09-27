"""Video files are stored as audio track + key frames unless --keep-full (needs ffmpeg; skipped otherwise)."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from pipeline.context import AppContext
from pipeline.drive import DriveClient
from pipeline.extract.video import extract_media
from pipeline.ingest import Ingestor
from pipeline.models import Account
from pipeline.registry import Registry
from pipeline.sheets import SheetsRepo
from pipeline.taxonomy import import_seed

SEED = Path(__file__).resolve().parent.parent / "config" / "taxonomy.seed.yaml"
pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")


@pytest.fixture
def tiny_video(tmp_path) -> Path:
    out = tmp_path / "clip.mp4"
    cmd = ["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc=duration=3:size=128x96:rate=10", "-f", "lavfi", "-i", "sine=frequency=440:duration=3", "-shortest", "-pix_fmt", "yuv420p", str(out)]
    proc = subprocess.run(cmd, capture_output=True)
    if proc.returncode != 0 or not out.exists():
        pytest.skip(f"could not generate a test video: {proc.stderr.decode()[-200:]}")
    return out


def test_extract_media_derives_audio_and_frames(settings, tiny_video, tmp_path):
    ex = extract_media(tiny_video, settings, tmp_path / "work", kind="video")
    roles = [r for r, _p, _m in ex.derived_files]
    assert roles[0] == "audio" and any(r.startswith("frame:") for r in roles)
    audio = next(p for r, p, _m in ex.derived_files if r == "audio")
    assert audio.suffix == ".m4a" and audio.stat().st_size > 0
    assert ex.transcript_kind == "none" and ex.duration_sec == 3


def _ctx(settings, fake_sheets, fake_drive, fake_apify, monkeypatch):
    monkeypatch.setenv("GOOGLE_REFRESH_TOKEN_RAW01", "1//0gRefreshToken")
    repo = SheetsRepo(fake_sheets)
    repo.ensure_tabs()
    reg = Registry(repo, settings)
    drive = DriveClient(fake_drive)
    import_seed(reg, yaml.safe_load(SEED.read_text()))
    root = drive.ensure_folder(None, "AI Primer Raw")
    reg.upsert_account(Account(account_id="raw01", token_env_var="GOOGLE_REFRESH_TOKEN_RAW01", root_folder_id=root["id"]))
    return AppContext(settings, registry=reg, drive_factory=lambda a: drive, apify_runner=fake_apify), reg


def test_ingest_video_stores_audio_and_frames_unless_keep_full(settings, fake_sheets, fake_drive, fake_apify, monkeypatch, tiny_video):
    ctx, reg = _ctx(settings, fake_sheets, fake_drive, fake_apify, monkeypatch)
    out = Ingestor(ctx).run(str(tiny_video), "Body / Immortal Yogi / Rest Body", author="Coach")
    assert out["status"] == "needs_transcription"
    row = reg.resource(out["resource_id"])
    assert row.raw_filename.endswith(".m4a") and "full video not stored" in row.notes
    assert len(row.data_file_ids) >= 1 and all(fake_drive.files[i]["name"].endswith(".jpg") for i in row.data_file_ids)
    assert fake_drive.files[row.raw_file_id]["appProperties"]["derived"] == "audio"
    # the same file, kept in full, on request
    out2 = Ingestor(ctx).run(str(tiny_video), "Body / Immortal Yogi / Rest Body", author="Coach", force=True, keep_full=True)
    row2 = reg.resource(out2["resource_id"])
    assert row2.resource_id == row.resource_id
    names = [fake_drive.files[f]["name"] for f in fake_drive.files if fake_drive.files[f].get("appProperties", {}).get("resource_id") == row.resource_id]
    assert any(n.endswith(".mp4") for n in names)
