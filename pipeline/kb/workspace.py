"""File layout of the per-author working directory used during a summary run.

    <work_dir>/<author_id>/
        manifest.json                       run state (resources done, versions, ...)
        index.json                          matcher index of the store
        chunks/<resource_id>/NN.md          chunk files (header + text) for extraction helpers
        chunks/<resource_id>/_chunks.json   chunk metadata (ids, locations, titles)
        units/<resource_id>/NN.json         per-chunk ExtractionOutput
        units/<resource_id>/consolidated.json
        verified/<resource_id>.json         VerifiedUnit list
        verified/<resource_id>.rejects.json
        match/<resource_id>.candidates.json MatchItem list
        match/<resource_id>.decisions.json  DecisionsFile
        evidence.md                         evidence pack for the reviewer
        review.json                         ReviewFile
"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from pipeline.kb.chunker import CHUNK_TEXT_SENTINEL, Chunk, chunk_id_for


def _jsonable(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, list):
        return [_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if hasattr(value, "__dataclass_fields__"):
        return asdict(value)
    return value


def strip_chunk_header(content: str) -> str:
    """Return the chunk text of a chunk file (everything after the header sentinel)."""
    idx = content.find(CHUNK_TEXT_SENTINEL)
    if idx < 0:
        return content
    body = content[idx + len(CHUNK_TEXT_SENTINEL) :]
    if body.startswith("\n\n"):
        body = body[2:]
    elif body.startswith("\n"):
        body = body[1:]
    return body


class Workspace:
    def __init__(self, work_dir: Path, author_id: str):
        self.work_dir = Path(work_dir)
        self.author_id = author_id
        self.root = self.work_dir / author_id

    # ---- paths
    def chunks_dir(self, resource_id: str) -> Path:
        return self.root / "chunks" / resource_id

    def chunk_path(self, resource_id: str, chunk_index: int) -> Path:
        return self.chunks_dir(resource_id) / f"{chunk_index:02d}.md"

    def chunk_meta_path(self, resource_id: str) -> Path:
        return self.chunks_dir(resource_id) / "_chunks.json"

    def unit_output_path(self, resource_id: str, chunk_index: int) -> Path:
        return self.root / "units" / resource_id / f"{chunk_index:02d}.json"

    def consolidated_path(self, resource_id: str) -> Path:
        return self.root / "units" / resource_id / "consolidated.json"

    def verified_path(self, resource_id: str) -> Path:
        return self.root / "verified" / f"{resource_id}.json"

    def rejects_path(self, resource_id: str) -> Path:
        return self.root / "verified" / f"{resource_id}.rejects.json"

    def candidates_path(self, resource_id: str) -> Path:
        return self.root / "match" / f"{resource_id}.candidates.json"

    def decisions_path(self, resource_id: str) -> Path:
        return self.root / "match" / f"{resource_id}.decisions.json"

    def evidence_path(self) -> Path:
        return self.root / "evidence.md"

    def review_path(self) -> Path:
        return self.root / "review.json"

    def index_path(self) -> Path:
        return self.root / "index.json"

    def manifest_path(self) -> Path:
        return self.root / "manifest.json"

    # ---- chunks
    def write_chunks(self, chunks: list[Chunk]) -> list[Path]:
        paths: list[Path] = []
        by_resource: dict[str, list[dict]] = {}
        for chunk in chunks:
            header = chunk.header or ""
            if CHUNK_TEXT_SENTINEL not in header:
                header = f"{header}\n\n{CHUNK_TEXT_SENTINEL}" if header else CHUNK_TEXT_SENTINEL
            path = self.chunk_path(chunk.resource_id, chunk.index)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"{header}\n\n{chunk.text}", encoding="utf-8")
            paths.append(path)
            by_resource.setdefault(chunk.resource_id, []).append(
                {
                    "chunk_id": chunk.chunk_id,
                    "index": chunk.index,
                    "path": str(path),
                    "location_start": chunk.location_start,
                    "location_end": chunk.location_end,
                    "title": chunk.title,
                    "chars": len(chunk.text),
                }
            )
        for resource_id, meta in by_resource.items():
            self.write_json(self.chunk_meta_path(resource_id), meta)
        return paths

    def read_chunk_texts(self, resource_id: str) -> dict[str, str]:
        """chunk_id -> chunk text (header removed)."""
        out: dict[str, str] = {}
        folder = self.chunks_dir(resource_id)
        if not folder.is_dir():
            return out
        for path in sorted(folder.glob("[0-9]*.md")):
            try:
                index = int(path.stem)
            except ValueError:
                continue
            out[chunk_id_for(resource_id, index)] = strip_chunk_header(path.read_text(encoding="utf-8"))
        return out

    # ---- json
    def write_json(self, path: Path, model_or_dict: Any) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(_jsonable(model_or_dict), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        return path

    def read_json(self, path: Path) -> Any:
        return json.loads(Path(path).read_text(encoding="utf-8"))

    def load_manifest(self) -> dict:
        path = self.manifest_path()
        if not path.is_file():
            return {}
        data = self.read_json(path)
        return data if isinstance(data, dict) else {}

    def save_manifest(self, manifest: dict) -> Path:
        return self.write_json(self.manifest_path(), dict(manifest))
