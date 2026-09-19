"""JSON schemas of the helper-agent contracts, generated from the pydantic models."""

from __future__ import annotations

import json
from pathlib import Path

from pipeline.models import DecisionsFile, ExtractionOutput, ReviewFile, Unit

SCHEMAS = {
    "extraction_output.schema.json": ExtractionOutput,
    "decisions.schema.json": DecisionsFile,
    "review.schema.json": ReviewFile,
    "unit.schema.json": Unit,
}
DEFAULT_DIR = Path(__file__).resolve().parent.parent / "schemas"


def write_schemas(out_dir: Path | None = None) -> list[Path]:
    out_dir = Path(out_dir) if out_dir else DEFAULT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for filename, model in SCHEMAS.items():
        schema = model.model_json_schema()
        schema.setdefault("$schema", "https://json-schema.org/draft/2020-12/schema")
        path = out_dir / filename
        path.write_text(json.dumps(schema, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        written.append(path)
    return written


if __name__ == "__main__":
    for p in write_schemas():
        print(p)
