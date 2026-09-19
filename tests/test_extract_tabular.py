"""Tests for pipeline.extract.tabular (xlsx via openpyxl, csv written directly)."""

from __future__ import annotations

import re
from pathlib import Path

import openpyxl
import pytest

from pipeline.config import Settings, load_settings
from pipeline.extract import extract_file
from pipeline.extract.tabular import extract_tabular, markdown_table

REPO_ROOT = Path("/home/user/aiprimer2.0")


@pytest.fixture
def settings() -> Settings:
    return load_settings(repo_root=REPO_ROOT)


@pytest.fixture
def xlsx_file(tmp_path: Path) -> Path:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Sales"
    ws.append(["region", "amount", "note"])
    for i in range(30):
        ws.append([f"r{i % 3}", i * 1.5, "x" * 100 if i == 0 else "ok|pipe"])
    ws2 = wb.create_sheet("People & Co")
    ws2.append(["name", "age"])
    ws2.append(["Ann", 30])
    ws2.append(["Bob", None])
    path = tmp_path / "data.xlsx"
    wb.save(str(path))
    return path


@pytest.fixture
def csv_file(tmp_path: Path) -> Path:
    path = tmp_path / "scores.csv"
    path.write_text("name,score,city\nAnn,1,Paris\nBob,2,Rome\nCyd,3,Oslo\n", encoding="utf-8")
    return path


def test_xlsx_sheets_sections_and_exports(xlsx_file: Path, settings: Settings, tmp_path: Path):
    work = tmp_path / "work"
    ex = extract_tabular(xlsx_file, settings, work)
    assert ex.source_type == "xlsx"
    assert ex.transcript_kind == "text"
    assert ex.metadata.title == "data"
    assert "## Sheet: Sales" in ex.text and "## Sheet: People & Co" in ex.text
    assert "Shape: 30 rows x 3 columns" in ex.text
    assert "Shape: 2 rows x 2 columns" in ex.text
    # column table with dtype and non-null counts
    assert re.search(r"\| region \| \S+ \| 30 \|", ex.text)
    assert re.search(r"\| age \| float64 \| 1 \|", ex.text)
    # describe() output for the numeric column
    assert "### Numeric summary" in ex.text
    assert re.search(r"\| mean \| 21\.75 \|", ex.text)
    assert re.search(r"\| max \| 43\.5 \|", ex.text)
    # exports: one CSV per sheet with a file-system safe name
    assert [p.name for p in ex.data_exports] == ["Sales.csv", "People-Co.csv"]
    assert all(p.parent == work and p.is_file() for p in ex.data_exports)
    assert ex.data_exports[0].read_text(encoding="utf-8").splitlines()[0] == "region,amount,note"
    assert len(ex.data_exports[0].read_text(encoding="utf-8").splitlines()) == 31


def test_sample_rows_and_cell_truncation(xlsx_file: Path, tmp_path: Path):
    settings = Settings(repo_root=REPO_ROOT, config={"extract": {"tabular_sample_rows": 5}})
    ex = extract_tabular(xlsx_file, settings, tmp_path / "work")
    assert "### First 5 rows" in ex.text
    sales = ex.text.split("## Sheet: Sales")[1].split("## Sheet:")[0]
    sample = sales.split("### First 5 rows")[1].split("###")[0]
    data_rows = [ln for ln in sample.splitlines() if re.match(r"^\| r\d ", ln)]
    assert len(data_rows) == 5
    long_cell = [c.strip() for c in data_rows[0].split("|")][3]
    assert long_cell.endswith("…") and len(long_cell) <= 80
    assert "ok\\|pipe" in sample  # pipes escaped inside cells


def test_csv_extraction(csv_file: Path, settings: Settings, tmp_path: Path):
    work = tmp_path / "work"
    ex = extract_tabular(csv_file, settings, work)
    assert ex.source_type == "csv"
    assert ex.metadata.title == "scores"
    assert "## Sheet: scores" in ex.text
    assert "Shape: 3 rows x 3 columns" in ex.text
    assert "| Ann | 1 | Paris |" in ex.text
    assert re.search(r"\| mean \| 2 \|", ex.text)
    assert [p.name for p in ex.data_exports] == ["scores.csv"]
    assert ex.data_exports[0].read_text(encoding="utf-8").startswith("name,score,city\n")


def test_csv_with_semicolons_and_latin1(settings: Settings, tmp_path: Path):
    path = tmp_path / "latin.csv"
    path.write_bytes("nom;score\nRené;1\nZoë;2\n".encode("latin-1"))
    ex = extract_tabular(path, settings, tmp_path / "work")
    assert "Shape: 2 rows x 2 columns" in ex.text
    assert "René" in ex.text
    assert any("decoded as" in w for w in ex.warnings)


def test_extract_file_dispatches_tabular(csv_file: Path, xlsx_file: Path, settings: Settings, tmp_path: Path):
    assert extract_file(csv_file, "csv", settings, tmp_path / "w1").source_type == "csv"
    assert extract_file(xlsx_file, "xlsx", settings, tmp_path / "w2").source_type == "xlsx"


def test_markdown_table_helper():
    table = markdown_table(["a", "b"], [[1, None], ["x|y", float("nan")]])
    assert table.splitlines() == ["| a | b |", "| --- | --- |", "| 1 |  |", "| x\\|y |  |"]
