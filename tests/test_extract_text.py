"""Tests for pipeline.extract.text."""

from __future__ import annotations

from pathlib import Path

import pytest

from pipeline.config import Settings, load_settings
from pipeline.extract import extract_file
from pipeline.extract.base import decode_bytes, guess_language, normalize_text
from pipeline.extract.text import extract_text

REPO_ROOT = Path("/home/user/aiprimer2.0")


@pytest.fixture
def settings() -> Settings:
    return load_settings(repo_root=REPO_ROOT)


def test_utf8_markdown_title_from_heading(settings: Settings, tmp_path: Path):
    path = tmp_path / "notes.md"
    path.write_text("Intro line\n\n## My Title ##\r\n\r\nBody café text.\r\n", encoding="utf-8")
    ex = extract_text(path, settings, tmp_path / "work")
    assert ex.source_type == "txt"
    assert ex.transcript_kind == "text"
    assert ex.text == "Intro line\n\n## My Title ##\n\nBody café text.\n"
    assert ex.metadata.title == "My Title"
    assert "title from first markdown heading" in ex.metadata.evidence
    assert ex.warnings == []


def test_latin1_file_is_decoded(settings: Settings, tmp_path: Path):
    path = tmp_path / "latin.txt"
    path.write_bytes("Résumé café\r\nSecond line with naïve words\r\n".encode("latin-1"))
    ex = extract_text(path, settings, tmp_path / "work")
    assert ex.text == "Résumé café\nSecond line with naïve words\n"
    assert ex.metadata.title == "Résumé café"
    assert "title from first non-empty line" in ex.metadata.evidence
    assert any(e.startswith("decoded as") for e in ex.metadata.evidence)


def test_title_capped_at_120_chars(settings: Settings, tmp_path: Path):
    path = tmp_path / "long.txt"
    path.write_text("word " * 60 + "\nmore\n", encoding="utf-8")
    ex = extract_text(path, settings, tmp_path / "work")
    assert len(ex.metadata.title) <= 120


def test_empty_file(settings: Settings, tmp_path: Path):
    path = tmp_path / "empty.txt"
    path.write_bytes(b"")
    ex = extract_text(path, settings, tmp_path / "work")
    assert ex.text == ""
    assert ex.metadata.title == "empty"
    assert "file is empty" in ex.warnings


def test_bom_and_language(settings: Settings, tmp_path: Path):
    path = tmp_path / "bom.txt"
    body = (
        "The committee reviewed the report and agreed that the plan is sound. It is a matter of time "
        "and effort, and the team will be ready for the next stage of the work by the end of the year.\n"
    )
    path.write_bytes(b"\xef\xbb\xbf" + body.encode("utf-8"))
    ex = extract_text(path, settings, tmp_path / "work")
    assert not ex.text.startswith("﻿")
    assert ex.language == "en"
    assert ex.metadata.language == "en"


def test_extract_file_dispatches_txt(settings: Settings, tmp_path: Path):
    path = tmp_path / "a.txt"
    path.write_text("hello\n", encoding="utf-8")
    assert extract_file(path, "txt", settings, tmp_path / "work").metadata.title == "hello"


def test_helpers():
    assert normalize_text("a \r\nb\r\r\n\n\n\nc  ") == "a\nb\n\nc\n"
    assert decode_bytes("é".encode("utf-8"))[1] == "utf-8"
    assert decode_bytes(b"")[0] == ""
    assert guess_language("short") == ""
    assert guess_language("el perro y el gato de la casa que no es muy grande pero sí muy bonita y con sus cosas " * 2) == "es"
