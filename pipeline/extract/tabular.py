"""Spreadsheets (xlsx) and CSV files: a markdown digest per sheet plus CSV exports."""

from __future__ import annotations

import math
import re
from pathlib import Path

import pandas as pd

from pipeline.config import Settings
from pipeline.models import MetadataGuess

from .base import Extraction, detect_encoding, safe_name, truncate

CELL_CHARS = 80
_EXCEL_SUFFIXES = {".xlsx", ".xlsm", ".xltx", ".xltm", ".xls", ".ods"}


def _cell(value) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        if math.isnan(value):
            return ""
        text = f"{value:.6g}"
    else:
        try:
            if pd.isna(value):
                return ""
        except (TypeError, ValueError):
            pass
        text = str(value)
    text = re.sub(r"\s+", " ", text).replace("|", "\\|").strip()
    return truncate(text, CELL_CHARS)


def markdown_table(headers: list[str], rows: list[list]) -> str:
    headers = [_cell(h) or f"col{i + 1}" for i, h in enumerate(headers)]
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join([" --- "] * len(headers)) + "|"]
    for row in rows:
        cells = [_cell(v) for v in row]
        cells += [""] * (len(headers) - len(cells))
        lines.append("| " + " | ".join(cells[: len(headers)]) + " |")
    return "\n".join(lines)


def read_csv_frames(path: Path, warnings: list[str]) -> dict[str, pd.DataFrame]:
    with open(path, "rb") as fh:
        raw_head = fh.read(1 << 20)
    try:
        raw_head.decode("utf-8")
        encoding = "utf-8-sig" if raw_head.startswith(b"\xef\xbb\xbf") else "utf-8"
    except UnicodeDecodeError:
        encoding = detect_encoding(raw_head) or "utf-8"
        warnings.append(f"csv decoded as {encoding}")
    attempts = (
        dict(encoding=encoding, sep=None, engine="python"),
        dict(encoding=encoding, on_bad_lines="skip"),
        dict(encoding=encoding, encoding_errors="replace", sep=None, engine="python", on_bad_lines="skip"),
    )
    last_exc: Exception | None = None
    for kwargs in attempts:
        try:
            frame = pd.read_csv(path, **kwargs)
            break
        except pd.errors.EmptyDataError:
            frame = pd.DataFrame()
            warnings.append("csv is empty")
            break
        except Exception as exc:  # parser / unicode errors: try the next strategy
            last_exc = exc
            continue
    else:
        raise ValueError(f"cannot parse csv {path.name}: {last_exc}") from last_exc
    return {path.stem: frame}


def read_excel_frames(path: Path, warnings: list[str]) -> dict[str, pd.DataFrame]:
    try:
        frames = pd.read_excel(path, sheet_name=None)
    except Exception as exc:
        raise ValueError(f"cannot read workbook {path.name}: {exc}") from exc
    return {str(name): frame for name, frame in frames.items()}


def sheet_markdown(name: str, frame: pd.DataFrame, sample_rows: int) -> str:
    parts = [f"## Sheet: {name}", "", f"Shape: {frame.shape[0]} rows x {frame.shape[1]} columns"]
    if frame.shape[1] == 0:
        parts += ["", "_(empty sheet)_"]
        return "\n".join(parts)
    columns = [str(c) for c in frame.columns]
    parts += ["", "### Columns", ""]
    parts.append(
        markdown_table(
            ["column", "dtype", "non-null"],
            [[c, str(frame[col].dtype), int(frame[col].notna().sum())] for c, col in zip(columns, frame.columns)],
        )
    )
    n = min(max(int(sample_rows), 0), len(frame))
    parts += ["", f"### First {n} rows", ""]
    if n:
        parts.append(markdown_table(columns, frame.head(n).values.tolist()))
    else:
        parts.append("_(no rows)_")
    numeric = frame.select_dtypes(include="number")
    if numeric.shape[1]:
        desc = numeric.describe()
        parts += ["", "### Numeric summary", ""]
        parts.append(
            markdown_table(
                ["stat", *[str(c) for c in desc.columns]],
                [[stat, *desc.loc[stat].tolist()] for stat in desc.index],
            )
        )
    return "\n".join(parts)


def extract_tabular(path: Path, settings: Settings, workdir: Path) -> Extraction:
    path = Path(path)
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    cfg = settings.extract if settings is not None else {}
    sample_rows = int(cfg.get("tabular_sample_rows", 20))
    warnings: list[str] = []
    is_excel = path.suffix.lower() in _EXCEL_SUFFIXES
    frames = read_excel_frames(path, warnings) if is_excel else read_csv_frames(path, warnings)
    source_type = "xlsx" if is_excel else "csv"

    sections = [f"# {path.stem}", "", f"{len(frames)} sheet(s) from `{path.name}`"]
    exports: list[Path] = []
    used_names: set[str] = set()
    for name, frame in frames.items():
        sections += ["", sheet_markdown(name, frame, sample_rows)]
        base = safe_name(name, default="sheet")
        candidate, i = base, 2
        while candidate.lower() in used_names:
            candidate = f"{base}-{i}"
            i += 1
        used_names.add(candidate.lower())
        out = workdir / f"{candidate}.csv"
        try:
            frame.to_csv(out, index=False)
            exports.append(out)
        except OSError as exc:
            warnings.append(f"could not export sheet {name!r}: {exc}")
    text = "\n".join(sections).rstrip() + "\n"
    metadata = MetadataGuess(title=path.stem, confidence=0.2, evidence=["title from file name"])
    return Extraction(
        text=text,
        metadata=metadata,
        source_type=source_type,
        transcript_kind="text",
        data_exports=exports,
        warnings=warnings,
    )
