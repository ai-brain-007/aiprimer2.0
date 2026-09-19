"""Tests for pipeline.extract.pdf (fixtures generated with pymupdf at test time)."""

from __future__ import annotations

import random
import shutil
import textwrap
from pathlib import Path

import pymupdf
import pytest

from pipeline.config import Settings, load_settings
from pipeline.extract import Extraction, extract_file, render_pdf_pages
from pipeline.extract.pdf import extract_pdf, is_scanned, join_pages

REPO_ROOT = Path("/home/user/aiprimer2.0")

BODY = (
    "This is a paragraph of ordinary English prose that is used to make sure the page has enough "
    "characters to be treated as a real text page and not as a scanned image. The quick brown fox "
    "jumps over the lazy dog while the committee reviews the annual report on training methods."
)
SCAN_LINES = [
    "Scanned Page Heading",
    "The quick brown fox jumps over the lazy dog.",
    "Training pipelines should be tested with care and patience.",
    "Every extractor degrades gracefully when a tool is missing.",
    "Optical character recognition works best on clean print.",
]


@pytest.fixture
def settings() -> Settings:
    return load_settings(repo_root=REPO_ROOT)


def _fill(page: pymupdf.Page, y: int, repeats: int = 4) -> None:
    for _ in range(repeats):
        for chunk in textwrap.wrap(BODY, 85):
            page.insert_text((72, y), chunk, fontsize=11)
            y += 14
        y += 6


def make_text_pdf(path: Path, metadata: dict | None = None) -> Path:
    doc = pymupdf.open()
    p1 = doc.new_page()
    p1.insert_text((72, 100), "The Art of Testing Pipelines", fontsize=24)
    p1.insert_text((72, 140), "by Jane Doe", fontsize=12)
    _fill(p1, 180)
    p2 = doc.new_page()
    p2.insert_text((72, 100), "Copyright notice", fontsize=11)
    p2.insert_text((72, 120), "© 2019 Jane Doe. All rights reserved.", fontsize=11)
    p2.insert_text((72, 140), "ISBN 978-0-306-40615-7", fontsize=11)
    _fill(p2, 180)
    p3 = doc.new_page()
    p3.insert_text((72, 100), "Chapter 2", fontsize=16)
    _fill(p3, 140)
    doc.set_toc([[1, "Chapter 1", 1], [1, "Chapter 2", 3], [2, "Section 2.1", 3]])
    doc.set_metadata(metadata or {"title": "", "author": ""})
    doc.save(str(path))
    doc.close()
    return path


def _text_pixmap(lines: list[str], dpi: int = 150) -> pymupdf.Pixmap:
    src = pymupdf.open()
    page = src.new_page()
    y = 100
    for line in lines:
        page.insert_text((72, y), line, fontsize=16)
        y += 28
    pix = page.get_pixmap(dpi=dpi)
    src.close()
    return pix


def _noise_pixmap(dpi: int = 150) -> pymupdf.Pixmap:
    src = pymupdf.open()
    page = src.new_page()
    rnd = random.Random(1)
    for _ in range(400):
        x0, y0 = rnd.uniform(30, 560), rnd.uniform(30, 780)
        page.draw_line((x0, y0), (x0 + rnd.uniform(-15, 15), y0 + rnd.uniform(-15, 15)), width=rnd.uniform(0.3, 2))
    pix = page.get_pixmap(dpi=dpi)
    src.close()
    return pix


def make_scanned_pdf(path: Path, pixmaps: list[pymupdf.Pixmap], dpi: int = 150) -> Path:
    """A PDF whose pages are only images (no text layer), like a scan."""
    doc = pymupdf.open()
    for pix in pixmaps:
        page = doc.new_page(width=pix.width * 72 / dpi, height=pix.height * 72 / dpi)
        page.insert_image(page.rect, pixmap=pix)
    doc.save(str(path))
    doc.close()
    return path


@pytest.fixture
def text_pdf(tmp_path: Path) -> Path:
    return make_text_pdf(tmp_path / "book.pdf")


@pytest.fixture
def scanned_pdf(tmp_path: Path) -> Path:
    return make_scanned_pdf(tmp_path / "scan.pdf", [_text_pixmap(SCAN_LINES), _noise_pixmap()])


# --------------------------------------------------------------------------- text PDFs


def test_text_pdf_pages_markers_and_toc(text_pdf: Path, settings: Settings, tmp_path: Path):
    ex = extract_pdf(text_pdf, settings, tmp_path / "work")
    assert isinstance(ex, Extraction)
    assert ex.source_type == "pdf"
    assert ex.transcript_kind == "text"
    assert ex.pages == 3
    assert ex.needs_vision is False and ex.vision_pages == [] and ex.vision_files == []
    for n in (1, 2, 3):
        assert f"<!-- page {n} -->" in ex.text
    assert ex.text.index("<!-- page 1 -->") < ex.text.index("<!-- page 2 -->") < ex.text.index("<!-- page 3 -->")
    assert "The Art of Testing Pipelines" in ex.text
    assert "quick brown fox" in ex.text
    assert ex.toc == [
        {"level": 1, "title": "Chapter 1", "page": 1},
        {"level": 1, "title": "Chapter 2", "page": 3},
        {"level": 2, "title": "Section 2.1", "page": 3},
    ]
    assert ex.language == "en"
    assert ex.text.endswith("\n")


def test_text_pdf_metadata_guess(text_pdf: Path, settings: Settings, tmp_path: Path):
    ex = extract_pdf(text_pdf, settings, tmp_path / "work")
    md = ex.metadata
    assert md.title == "The Art of Testing Pipelines"
    assert md.author_raw == "Jane Doe"
    assert md.published_date == "2019"
    assert md.date_precision == "year"
    assert md.pages == 3
    assert md.language == "en"
    assert 0.3 <= md.confidence <= 0.8
    assert "title from largest font line on p.1" in md.evidence
    assert "author from 'by' line p.1" in md.evidence
    assert "year from copyright line p.2" in md.evidence
    assert any(e.startswith("isbn 9780306406157") for e in md.evidence)


def test_title_and_author_from_pdf_metadata(settings: Settings, tmp_path: Path):
    pdf = make_text_pdf(tmp_path / "meta.pdf", {"title": "A Proper Title", "author": "John Smith"})
    ex = extract_pdf(pdf, settings, tmp_path / "work")
    assert ex.metadata.title == "A Proper Title"
    assert ex.metadata.author_raw == "John Smith"
    assert "title from PDF metadata" in ex.metadata.evidence
    assert "author from PDF metadata" in ex.metadata.evidence


def test_filename_like_metadata_title_is_ignored(settings: Settings, tmp_path: Path):
    pdf = make_text_pdf(tmp_path / "meta.pdf", {"title": "thesis_final.docx", "author": ""})
    ex = extract_pdf(pdf, settings, tmp_path / "work")
    assert ex.metadata.title == "The Art of Testing Pipelines"
    assert "title from largest font line on p.1" in ex.metadata.evidence


def test_confidence_scales_with_fields_found(settings: Settings, tmp_path: Path):
    doc = pymupdf.open()
    page = doc.new_page()
    _fill(page, 100, repeats=6)
    doc.save(str(tmp_path / "plain.pdf"))
    doc.close()
    poor = extract_pdf(tmp_path / "plain.pdf", settings, tmp_path / "w1")
    rich = extract_pdf(make_text_pdf(tmp_path / "rich.pdf"), settings, tmp_path / "w2")
    assert poor.metadata.author_raw == "" and poor.metadata.published_date == ""
    assert poor.metadata.date_precision == "unknown"
    assert 0.3 <= poor.metadata.confidence < rich.metadata.confidence <= 0.8


def test_extract_file_dispatches_pdf(text_pdf: Path, settings: Settings, tmp_path: Path):
    work = tmp_path / "nested" / "work"
    ex = extract_file(text_pdf, "pdf", settings, work)
    assert work.is_dir()
    assert ex.source_type == "pdf" and ex.pages == 3


# --------------------------------------------------------------------------- helpers


def test_is_scanned_uses_median_of_sample():
    assert is_scanned(["", "", "x" * 500], sample_pages=10, threshold=200) is True
    assert is_scanned(["x" * 500, "x" * 500, ""], sample_pages=10, threshold=200) is False
    assert is_scanned(["x" * 500] + [""] * 20, sample_pages=1, threshold=200) is False
    assert is_scanned([], sample_pages=10, threshold=200) is False


def test_join_pages_markers():
    text = join_pages(["first  \n\n\n\nline", ""])
    assert text == "<!-- page 1 -->\nfirst\n\nline\n\n<!-- page 2 -->\n"


def test_render_pdf_pages_names(text_pdf: Path, tmp_path: Path):
    out = render_pdf_pages(text_pdf, [1, 3, 99], dpi=40, outdir=tmp_path / "render")
    assert [p.name for p in out] == ["page-0001.png", "page-0003.png"]
    assert all(p.is_file() and p.stat().st_size > 0 for p in out)
    assert out[0].read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


# --------------------------------------------------------------------------- scanned PDFs


def test_scanned_pdf_is_detected(scanned_pdf: Path):
    with pymupdf.open(str(scanned_pdf)) as doc:
        texts = [p.get_text("text") for p in doc]
    assert is_scanned(texts, 10, 200) is True


def test_scanned_pdf_ocr(scanned_pdf: Path, settings: Settings, tmp_path: Path):
    if not (shutil.which("tesseract") and shutil.which("pdftoppm")):
        pytest.skip("tesseract / pdftoppm not installed")
    work = tmp_path / "work"
    ex = extract_pdf(scanned_pdf, settings, work)
    assert ex.transcript_kind == "ocr"
    assert ex.pages == 2
    assert "<!-- page 1 -->" in ex.text and "<!-- page 2 -->" in ex.text
    assert "brown fox" in ex.text.lower()
    # the noise page has no readable words -> low confidence -> vision
    assert ex.vision_pages == [2]
    assert ex.needs_vision is True
    assert [p.name for p in ex.vision_files] == ["page-0002.png"]
    assert ex.vision_files[0].parent == work / "vision"
    assert ex.vision_files[0].is_file()
    assert any("confidence" in w for w in ex.warnings)
    assert not list(work.glob("ocr-*"))  # OCR temp dir cleaned up


def test_scanned_pdf_vision_fallback_without_tools(scanned_pdf: Path, settings: Settings, tmp_path: Path, monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda *a, **k: None)
    work = tmp_path / "work"
    ex = extract_pdf(scanned_pdf, settings, work)
    assert ex.transcript_kind == "vision"
    assert ex.needs_vision is True
    assert ex.vision_pages == [1, 2]
    assert ex.text == ""
    assert [p.name for p in ex.vision_files] == ["page-0001.png", "page-0002.png"]
    assert all(p.is_file() for p in ex.vision_files)
    assert ex.vision_files[0].parent == work / "vision"
    assert ex.pages == 2
    assert ex.language == ""
    assert any("tesseract" in w and "pdftoppm" in w for w in ex.warnings)


def test_vision_fallback_caps_rendered_pages_at_40(settings: Settings, tmp_path: Path, monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda *a, **k: None)
    pix = _text_pixmap(SCAN_LINES, dpi=40)
    pdf = make_scanned_pdf(tmp_path / "long-scan.pdf", [pix] * 42, dpi=40)
    ex = extract_pdf(pdf, settings, tmp_path / "work")
    assert ex.transcript_kind == "vision"
    assert ex.vision_pages == list(range(1, 43))
    assert len(ex.vision_files) == 40
    assert ex.vision_files[-1].name == "page-0040.png"
    assert any("40" in w for w in ex.warnings)


def test_threshold_from_settings_controls_scanned_detection(text_pdf: Path, tmp_path: Path, monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda *a, **k: None)
    strict = Settings(repo_root=REPO_ROOT, config={"extract": {"scanned_median_chars_threshold": 100000}})
    ex = extract_pdf(text_pdf, strict, tmp_path / "work")
    assert ex.transcript_kind == "vision"
    assert ex.vision_pages == [1, 2, 3]
