"""Control-panel spreadsheet access.

`SheetsBackend` is the tiny set of raw operations we need from the Sheets API; `GoogleSheetsBackend`
implements it for real and tests use an in-memory fake. `SheetsRepo` adds the typed, header-driven
layer: every tab is loaded once per command (one batchGet), rows are appended/updated in batches.
"""

from __future__ import annotations

import time
from typing import Any, Protocol

from .models import TAB_MODELS, TabRow

README_TEXT = [
    ["AI Primer Control Panel"],
    [""],
    ["This spreadsheet is the registry of the AI Primer pipeline. The agent reads and writes it; you may edit it too."],
    ["Never put passwords, keys or tokens in this file. The Accounts tab stores the NAME of the environment setting that holds each key."],
    [""],
    ["Accounts", "One row per Google account (or Backblaze bucket later): role raw|summary, token_env_var, root folder ids, quota, status active|full|disabled, priority."],
    ["Taxonomy", "Domain > Primer > Stage tree. Edit `name` here and run /taxonomy sync to rename the Drive folder. node_id never changes."],
    ["Folders", "Which Drive folder holds each taxonomy node in each account."],
    ["Resources", "One row per ingested resource: kind, where it is stored (Drive path, folder and file links), when, how the text was obtained, Apify cost, warnings, status."],
    ["Authors", "Canonical author names, aliases, the summary Doc (link, created, last updated, version) and the link to the cards on GitHub."],
    ["Summaries", "One row per generated version of an author summary."],
    ["Jobs", "Activity log: every pipeline command, result and Apify cost."],
]


def col_letter(index: int) -> str:
    """0-based column index -> A1 letter(s)."""
    index += 1
    letters = ""
    while index > 0:
        index, rem = divmod(index - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


def a1(tab: str, col_start: int, row_start: int, col_end: int | None = None, row_end: int | None = None) -> str:
    start = f"{col_letter(col_start)}{row_start}"
    if col_end is None:
        return f"'{tab}'!{start}"
    end = f"{col_letter(col_end)}{row_end or row_start}"
    return f"'{tab}'!{start}:{end}"


class SheetsBackend(Protocol):
    def sheet_titles(self) -> list[str]: ...
    def add_sheet(self, title: str) -> None: ...
    def get_values(self, range_a1: str) -> list[list[str]]: ...
    def batch_get(self, ranges: list[str]) -> dict[str, list[list[str]]]: ...
    def append(self, range_a1: str, values: list[list[str]]) -> None: ...
    def batch_update_values(self, data: list[tuple[str, list[list[str]]]]) -> None: ...
    def freeze_header(self, title: str) -> None: ...
    def spreadsheet_url(self) -> str: ...


class GoogleSheetsBackend:
    def __init__(self, service: Any, spreadsheet_id: str):
        self.service = service
        self.spreadsheet_id = spreadsheet_id
        self._meta: dict | None = None

    # -- helpers
    def _retry(self, fn, attempts: int = 5):
        delay = 1.0
        for i in range(attempts):
            try:
                return fn()
            except Exception as exc:  # googleapiclient.errors.HttpError or transport errors
                status = getattr(getattr(exc, "resp", None), "status", None)
                if i == attempts - 1 or status not in (None, 429, 500, 502, 503, 504):
                    raise
                time.sleep(delay)
                delay *= 2

    def _metadata(self, refresh: bool = False) -> dict:
        if self._meta is None or refresh:
            self._meta = self._retry(
                lambda: self.service.spreadsheets()
                .get(spreadsheetId=self.spreadsheet_id, fields="sheets.properties,spreadsheetUrl")
                .execute()
            )
        return self._meta

    def sheet_titles(self) -> list[str]:
        return [s["properties"]["title"] for s in self._metadata(refresh=True).get("sheets", [])]

    def _sheet_id(self, title: str) -> int:
        for s in self._metadata().get("sheets", []):
            if s["properties"]["title"] == title:
                return s["properties"]["sheetId"]
        raise KeyError(title)

    def add_sheet(self, title: str) -> None:
        body = {"requests": [{"addSheet": {"properties": {"title": title}}}]}
        self._retry(lambda: self.service.spreadsheets().batchUpdate(spreadsheetId=self.spreadsheet_id, body=body).execute())
        self._metadata(refresh=True)

    def get_values(self, range_a1: str) -> list[list[str]]:
        resp = self._retry(
            lambda: self.service.spreadsheets().values().get(spreadsheetId=self.spreadsheet_id, range=range_a1).execute()
        )
        return resp.get("values", [])

    def batch_get(self, ranges: list[str]) -> dict[str, list[list[str]]]:
        if not ranges:
            return {}
        resp = self._retry(
            lambda: self.service.spreadsheets().values().batchGet(spreadsheetId=self.spreadsheet_id, ranges=ranges).execute()
        )
        out: dict[str, list[list[str]]] = {}
        for requested, vr in zip(ranges, resp.get("valueRanges", [])):
            out[requested] = vr.get("values", [])
        return out

    def append(self, range_a1: str, values: list[list[str]]) -> None:
        if not values:
            return
        self._retry(
            lambda: self.service.spreadsheets()
            .values()
            .append(
                spreadsheetId=self.spreadsheet_id,
                range=range_a1,
                valueInputOption="RAW",
                insertDataOption="INSERT_ROWS",
                body={"values": values},
            )
            .execute()
        )

    def batch_update_values(self, data: list[tuple[str, list[list[str]]]]) -> None:
        if not data:
            return
        body = {"valueInputOption": "RAW", "data": [{"range": r, "values": v} for r, v in data]}
        self._retry(
            lambda: self.service.spreadsheets().values().batchUpdate(spreadsheetId=self.spreadsheet_id, body=body).execute()
        )

    def freeze_header(self, title: str) -> None:
        try:
            sheet_id = self._sheet_id(title)
        except KeyError:
            return
        body = {
            "requests": [
                {
                    "updateSheetProperties": {
                        "properties": {"sheetId": sheet_id, "gridProperties": {"frozenRowCount": 1}},
                        "fields": "gridProperties.frozenRowCount",
                    }
                }
            ]
        }
        self._retry(lambda: self.service.spreadsheets().batchUpdate(spreadsheetId=self.spreadsheet_id, body=body).execute())

    def spreadsheet_url(self) -> str:
        return self._metadata().get("spreadsheetUrl") or f"https://docs.google.com/spreadsheets/d/{self.spreadsheet_id}/edit"


class SheetsRepo:
    """Header-driven typed access to the control-panel tabs with per-command caching."""

    def __init__(self, backend: SheetsBackend, tab_models: dict[str, type[TabRow]] | None = None):
        self.backend = backend
        self.tab_models = tab_models or TAB_MODELS
        self._cache: dict[str, tuple[list[str], list[dict[str, str]], dict[str, int]]] = {}

    # ---- structure
    def ensure_tabs(self) -> dict[str, Any]:
        """Create missing tabs and append missing header columns. Returns what changed."""
        report: dict[str, Any] = {"created": [], "columns_added": {}}
        existing = set(self.backend.sheet_titles())
        for tab, model in self.tab_models.items():
            headers = model.headers()
            if tab not in existing:
                self.backend.add_sheet(tab)
                self.backend.batch_update_values([(a1(tab, 0, 1, len(headers) - 1, 1), [headers])])
                self.backend.freeze_header(tab)
                report["created"].append(tab)
                continue
            current = self.backend.get_values(a1(tab, 0, 1, 200, 1))
            current_headers = current[0] if current else []
            missing = [h for h in headers if h not in current_headers]
            if not current_headers:
                self.backend.batch_update_values([(a1(tab, 0, 1, len(headers) - 1, 1), [headers])])
                self.backend.freeze_header(tab)
                report["created"].append(tab)
            elif missing:
                new_headers = current_headers + missing
                self.backend.batch_update_values([(a1(tab, 0, 1, len(new_headers) - 1, 1), [new_headers])])
                report["columns_added"][tab] = missing
        if "Readme" not in existing:
            self.backend.add_sheet("Readme")
            self.backend.batch_update_values([(a1("Readme", 0, 1, 1, len(README_TEXT)), README_TEXT)])
            report["created"].append("Readme")
        self._cache.clear()
        return report

    # ---- reading
    def _load_raw(self, tab: str, refresh: bool = False) -> tuple[list[str], list[dict[str, str]], dict[str, int]]:
        if tab in self._cache and not refresh:
            return self._cache[tab]
        values = self.backend.get_values(a1(tab, 0, 1, 200, 100000))
        headers = values[0] if values else self.tab_models[tab].headers()
        key_field = self.tab_models[tab].key_field
        rows: list[dict[str, str]] = []
        row_numbers: dict[str, int] = {}
        for offset, raw in enumerate(values[1:], start=2):
            if not any(cell.strip() for cell in raw if isinstance(cell, str)):
                continue
            row = {h: (raw[i] if i < len(raw) else "") for i, h in enumerate(headers)}
            rows.append(row)
            key = row.get(key_field, "")
            if key:
                row_numbers[key] = offset
        self._cache[tab] = (headers, rows, row_numbers)
        return self._cache[tab]

    def preload(self, tabs: list[str]) -> None:
        ranges = [a1(t, 0, 1, 200, 100000) for t in tabs if t not in self._cache]
        if not ranges:
            return
        data = self.backend.batch_get(ranges)
        for tab, rng in zip([t for t in tabs if t not in self._cache], ranges):
            values = data.get(rng, [])
            headers = values[0] if values else self.tab_models[tab].headers()
            key_field = self.tab_models[tab].key_field
            rows: list[dict[str, str]] = []
            row_numbers: dict[str, int] = {}
            for offset, raw in enumerate(values[1:], start=2):
                if not any(cell.strip() for cell in raw if isinstance(cell, str)):
                    continue
                row = {h: (raw[i] if i < len(raw) else "") for i, h in enumerate(headers)}
                rows.append(row)
                key = row.get(key_field, "")
                if key:
                    row_numbers[key] = offset
            self._cache[tab] = (headers, rows, row_numbers)

    def load(self, tab: str, refresh: bool = False) -> list[TabRow]:
        model = self.tab_models[tab]
        _headers, rows, _ = self._load_raw(tab, refresh)
        out = []
        for row in rows:
            try:
                out.append(model.from_row(row))
            except Exception as exc:  # a hand-edited bad row must not break the whole tab
                out.append(model.from_row({**row, "notes": f"{row.get('notes', '')} [parse error: {exc}]"}) if "notes" in model.model_fields else model.from_row(row))
        return out

    def get(self, tab: str, key: str) -> TabRow | None:
        for row in self.load(tab):
            if row.key() == key:
                return row
        return None

    def invalidate(self, tab: str | None = None) -> None:
        if tab is None:
            self._cache.clear()
        else:
            self._cache.pop(tab, None)

    # ---- writing
    def _row_values(self, headers: list[str], row: TabRow) -> list[str]:
        cells = row.to_row()
        return [cells.get(h, "") for h in headers]

    def append(self, tab: str, rows: list[TabRow]) -> None:
        if not rows:
            return
        headers, cached_rows, row_numbers = self._load_raw(tab)
        values = [self._row_values(headers, r) for r in rows]
        self.backend.append(a1(tab, 0, 1, len(headers) - 1, 1), values)
        next_row = (max(row_numbers.values()) + 1) if row_numbers else (len(cached_rows) + 2)
        for r in rows:
            cached_rows.append(r.to_row())
            row_numbers[r.key()] = next_row
            next_row += 1

    def update(self, tab: str, rows: list[TabRow]) -> None:
        """Rewrite existing rows (found by key); unknown keys are appended."""
        if not rows:
            return
        headers, cached_rows, row_numbers = self._load_raw(tab)
        data = []
        to_append = []
        for r in rows:
            n = row_numbers.get(r.key())
            if n is None:
                to_append.append(r)
                continue
            data.append((a1(tab, 0, n, len(headers) - 1, n), [self._row_values(headers, r)]))
            for cached in cached_rows:
                if cached.get(self.tab_models[tab].key_field) == r.key():
                    cached.update(r.to_row())
        self.backend.batch_update_values(data)
        self.append(tab, to_append)

    def upsert(self, tab: str, row: TabRow) -> None:
        self.update(tab, [row])
