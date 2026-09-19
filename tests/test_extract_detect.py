"""Tests for pipeline.extract.detect_kind and the dispatch of image/media extractors."""

from __future__ import annotations

import shutil
import wave
import zipfile
from pathlib import Path

import pytest

from pipeline.config import Settings, load_settings
from pipeline.extract import Extraction, detect_kind, extract_file
from pipeline.extract.image import extract_image
from pipeline.extract.video import extract_media

REPO_ROOT = Path("/home/user/aiprimer2.0")


@pytest.fixture
def settings() -> Settings:
    return load_settings(repo_root=REPO_ROOT)


@pytest.mark.parametrize(
    "url, kind",
    [
        ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", "youtube"),
        ("https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=42s", "youtube"),
        ("https://youtu.be/dQw4w9WgXcQ", "youtube"),
        ("https://www.youtube.com/shorts/dQw4w9WgXcQ", "youtube"),
        ("https://m.youtube.com/watch?v=dQw4w9WgXcQ", "youtube"),
        ("https://www.youtube.com/@veritasium", "youtube_channel"),
        ("https://www.youtube.com/@veritasium/videos", "youtube_channel"),
        ("https://www.youtube.com/channel/UCHnyfMqiRRG1u-2MsSQLbXA", "youtube_channel"),
        ("https://www.youtube.com/c/SomeChannel", "youtube_channel"),
        ("https://www.youtube.com/user/SomeUser", "youtube_channel"),
        ("https://www.youtube.com/playlist?list=PL1234567890", "youtube_playlist"),
        ("https://example.com/article", "web"),
        ("https://example.com/paper.pdf", "web"),
        ("www.example.com/page", "web"),
        ("  https://youtu.be/dQw4w9WgXcQ  ", "youtube"),
    ],
)
def test_detect_kind_urls(url: str, kind: str):
    assert detect_kind(url) == kind


@pytest.mark.parametrize(
    "name, kind",
    [
        ("a.pdf", "pdf"),
        ("a.PDF", "pdf"),
        ("a.docx", "docx"),
        ("a.xlsx", "xlsx"),
        ("a.csv", "csv"),
        ("a.txt", "txt"),
        ("a.md", "txt"),
        ("a.png", "image"),
        ("a.jpg", "image"),
        ("a.jpeg", "image"),
        ("a.webp", "image"),
        ("a.gif", "image"),
        ("a.mp4", "video"),
        ("a.mov", "video"),
        ("a.mkv", "video"),
        ("a.webm", "video"),
        ("a.avi", "video"),
        ("a.mp3", "audio"),
        ("a.m4a", "audio"),
        ("a.wav", "audio"),
        ("a.ogg", "audio"),
        ("a.flac", "audio"),
    ],
)
def test_detect_kind_extensions(name: str, kind: str, tmp_path: Path):
    # by extension only (file need not exist)
    assert detect_kind(str(tmp_path / name)) == kind


def test_detect_kind_magic_sniffing(tmp_path: Path):
    pdf = tmp_path / "paper.bin"
    pdf.write_bytes(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    assert detect_kind(str(pdf)) == "pdf"

    docx_zip = tmp_path / "report.zip"
    with zipfile.ZipFile(docx_zip, "w") as zf:
        zf.writestr("[Content_Types].xml", "<Types/>")
        zf.writestr("word/document.xml", "<w:document/>")
    assert detect_kind(str(docx_zip)) == "docx"

    xlsx_zip = tmp_path / "book.dat"
    with zipfile.ZipFile(xlsx_zip, "w") as zf:
        zf.writestr("xl/workbook.xml", "<workbook/>")
    assert detect_kind(str(xlsx_zip)) == "xlsx"

    mislabeled = tmp_path / "actually-a-pdf.txt"
    mislabeled.write_bytes(b"%PDF-1.7 rest")
    assert detect_kind(str(mislabeled)) == "pdf"


def test_detect_kind_unknown_extension(tmp_path: Path):
    text = tmp_path / "notes.unknownext"
    text.write_text("hello there\n", encoding="utf-8")
    assert detect_kind(str(text)) == "txt"

    latin = tmp_path / "notes.log"
    latin.write_bytes("Résumé\n".encode("latin-1"))
    assert detect_kind(str(latin)) == "txt"

    blob = tmp_path / "blob.dat"
    blob.write_bytes(bytes(range(256)) * 8)
    with pytest.raises(ValueError):
        detect_kind(str(blob))

    with pytest.raises(ValueError):
        detect_kind(str(tmp_path / "missing.unknownext"))
    with pytest.raises(ValueError):
        detect_kind("")


def test_extract_file_rejects_unknown_kind_and_missing_file(settings: Settings, tmp_path: Path):
    path = tmp_path / "a.txt"
    path.write_text("x", encoding="utf-8")
    with pytest.raises(ValueError):
        extract_file(path, "bogus", settings, tmp_path / "work")
    with pytest.raises(FileNotFoundError):
        extract_file(tmp_path / "missing.txt", "txt", settings, tmp_path / "work")


def test_image_extraction(settings: Settings, tmp_path: Path):
    img = tmp_path / "diagram.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 16)
    ex = extract_file(img, "image", settings, tmp_path / "work")
    assert isinstance(ex, Extraction)
    assert ex.source_type == "image"
    assert ex.transcript_kind == "vision"
    assert ex.needs_vision is True
    assert ex.vision_files == [img]
    assert ex.text.startswith("<!-- image: needs vision transcription -->")
    assert ex.metadata.title == "diagram"
    assert extract_image(img).vision_files == [img]


def _write_wav(path: Path, seconds: int = 2, rate: int = 8000) -> Path:
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(b"\x00\x00" * rate * seconds)
    return path


def test_media_extraction_with_ffprobe(settings: Settings, tmp_path: Path):
    if not shutil.which("ffprobe"):
        pytest.skip("ffprobe not installed")
    wav = _write_wav(tmp_path / "tone.wav")
    ex = extract_file(wav, "audio", settings, tmp_path / "work")
    assert ex.source_type == "audio"
    assert ex.transcript_kind == "none"
    assert ex.duration_sec == 2
    assert ex.metadata.duration_sec == 2
    assert ex.text.startswith("<!-- media: needs transcription -->")
    assert "needs transcription" in ex.warnings
    assert ex.metadata.title == "tone"


def test_media_extraction_without_ffprobe(settings: Settings, tmp_path: Path, monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda *a, **k: None)
    wav = _write_wav(tmp_path / "clip.wav")
    ex = extract_media(wav, settings, tmp_path / "work", kind="video")
    assert ex.source_type == "video"
    assert ex.duration_sec is None
    assert ex.transcript_kind == "none"
    assert any("ffprobe" in w for w in ex.warnings)
    assert "needs transcription" in ex.warnings
