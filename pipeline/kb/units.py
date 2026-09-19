"""Unit card files and the per-author UnitStore.

Layout of knowledge/<author-slug>/:
    author.yaml                      author metadata
    units/U-xxxxxx.md                one card per file
    _rejected/<timestamp>-<id>.md    audit trail of rejected extractions
    summary.md                       rendered summary (written by the CLI)

Card file format: YAML front matter with every Unit field except the four body fields, then
the markdown body with "## Description", "## Details", "## Notes" and, only when non-empty,
"## Earlier version". parse_unit_file(render_unit_file(unit)) == unit.
"""

from __future__ import annotations

import hashlib
import re
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import frontmatter
import yaml

from pipeline.ids import now_iso
from pipeline.kb.verify import normalize_text
from pipeline.models import ExtractedUnit, Unit

BODY_FIELDS = ("description", "details", "notes", "earlier_versions")
_SECTION_TITLES = {
    "description": "Description",
    "details": "Details",
    "notes": "Notes",
    "earlier_versions": "Earlier version",
}
_SECTION_RE = re.compile(
    r"^##[ \t]+(Description|Details|Notes|Earlier version)[ \t]*$", re.MULTILINE | re.IGNORECASE
)
_TITLE_TO_FIELD = {v.lower(): k for k, v in _SECTION_TITLES.items()}


# --------------------------------------------------------------------------- file format


def _yaml_dump(data: dict[str, Any]) -> str:
    return yaml.safe_dump(data, sort_keys=False, allow_unicode=True, default_flow_style=False, width=100_000)


def _stringify_dates(value: Any) -> Any:
    """YAML may load unquoted dates as date objects; the models want ISO strings."""
    if isinstance(value, dict):
        return {k: _stringify_dates(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_stringify_dates(v) for v in value]
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return value


def render_unit_file(unit: Unit) -> str:
    meta = unit.model_dump(mode="json", exclude=set(BODY_FIELDS))
    parts = [f"---\n{_yaml_dump(meta)}---\n"]
    for field in BODY_FIELDS:
        text = (getattr(unit, field) or "").strip()
        if field == "earlier_versions" and not text:
            continue
        parts.append(f"\n## {_SECTION_TITLES[field]}\n\n{text}\n")
    return "".join(parts)


def split_body_sections(body: str) -> dict[str, str]:
    """Split a card body into its known sections; text before the first heading is ignored."""
    sections = {field: "" for field in BODY_FIELDS}
    matches = list(_SECTION_RE.finditer(body))
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
        field = _TITLE_TO_FIELD[m.group(1).lower()]
        sections[field] = body[m.end() : end].strip()
    return sections


def parse_unit_file(text: str) -> Unit:
    post = frontmatter.loads(text)
    meta = _stringify_dates(dict(post.metadata))
    meta.update(split_body_sections(post.content))
    return Unit(**meta)


def unit_details_hash(unit_or_extracted: Unit | ExtractedUnit | dict) -> str:
    """sha1 of the normalized details text (used by the matcher to spot identical details)."""
    if isinstance(unit_or_extracted, dict):
        details = unit_or_extracted.get("details") or ""
    else:
        details = getattr(unit_or_extracted, "details", "") or ""
    return hashlib.sha1(normalize_text(details).encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- store


def _safe_filename(text: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", text or "").strip("-.")
    return cleaned[:80] or "item"


class UnitStore:
    """Reads and writes the cards of one author directory."""

    def __init__(self, author_dir: Path):
        self.author_dir = Path(author_dir)
        self.units_dir = self.author_dir / "units"
        self.rejected_dir = self.author_dir / "_rejected"
        self.author_meta_path = self.author_dir / "author.yaml"

    # ---- units
    def unit_path(self, unit_id: str) -> Path:
        return self.units_dir / f"{unit_id}.md"

    def load_all(self) -> list[Unit]:
        if not self.units_dir.is_dir():
            return []
        units: list[Unit] = []
        for path in sorted(self.units_dir.glob("U-*.md")):
            units.append(parse_unit_file(path.read_text(encoding="utf-8")))
        return units

    def get(self, unit_id: str) -> Unit | None:
        path = self.unit_path(unit_id)
        if not path.is_file():
            return None
        return parse_unit_file(path.read_text(encoding="utf-8"))

    def save(self, unit: Unit) -> Path:
        unit.updated_at = now_iso()
        if not unit.created_at:
            unit.created_at = unit.updated_at
        self.units_dir.mkdir(parents=True, exist_ok=True)
        path = self.unit_path(unit.id)
        path.write_text(render_unit_file(unit), encoding="utf-8")
        return path

    def delete(self, unit_id: str) -> None:
        path = self.unit_path(unit_id)
        if path.is_file():
            path.unlink()

    def reject(self, item: dict | ExtractedUnit | Unit, reason: str, temp_id: str = "") -> Path:
        """Write the rejected item to _rejected/ with the reason in the front matter."""
        data: dict[str, Any] = item.model_dump(mode="json") if hasattr(item, "model_dump") else dict(item)
        if not temp_id:
            temp_id = str(data.get("temp_id") or data.get("id") or "")
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        meta: dict[str, Any] = {
            "rejected_at": now_iso(),
            "reason": reason,
            "temp_id": temp_id,
            "name": data.get("name", ""),
            "type": data.get("type", ""),
            "aliases": data.get("aliases") or [],
            "citations": data.get("citations") or [],
        }
        if data.get("verified_citations"):
            meta["verified_citations"] = data["verified_citations"]
        if data.get("id"):
            meta["unit_id"] = data["id"]
        body = "".join(
            f"\n## {_SECTION_TITLES[field]}\n\n{(data.get(field) or '').strip()}\n"
            for field in ("description", "details", "notes")
        )
        self.rejected_dir.mkdir(parents=True, exist_ok=True)
        base = f"{stamp}-{_safe_filename(temp_id or data.get('name', ''))}"
        path = self.rejected_dir / f"{base}.md"
        n = 1
        while path.exists():
            n += 1
            path = self.rejected_dir / f"{base}-{n}.md"
        path.write_text(f"---\n{_yaml_dump(meta)}---\n{body}", encoding="utf-8")
        return path

    def index(self) -> list[dict]:
        return [
            {
                "unit_id": u.id,
                "name": u.name,
                "aliases": list(u.aliases),
                "type": u.type,
                "stages": list(u.stages),
                "description": u.description,
                "last_seen": u.last_seen,
            }
            for u in self.load_all()
        ]

    # ---- author metadata
    def load_author_meta(self) -> dict:
        if not self.author_meta_path.is_file():
            return {}
        with open(self.author_meta_path, encoding="utf-8") as fh:
            return _stringify_dates(yaml.safe_load(fh) or {})

    def save_author_meta(self, meta: dict) -> Path:
        self.author_dir.mkdir(parents=True, exist_ok=True)
        self.author_meta_path.write_text(_yaml_dump(dict(meta)), encoding="utf-8")
        return self.author_meta_path
