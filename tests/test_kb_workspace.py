from pathlib import Path

from pipeline.kb.chunker import CHUNK_TEXT_SENTINEL, Chunk, chunk_markdown
from pipeline.kb.workspace import Workspace, strip_chunk_header
from pipeline.models import Decision, DecisionsFile


def test_paths(tmp_path: Path):
    ws = Workspace(tmp_path, "A-jane-doe")
    assert ws.root == tmp_path / "A-jane-doe"
    assert ws.chunks_dir("R-1") == ws.root / "chunks" / "R-1"
    assert ws.unit_output_path("R-1", 3) == ws.root / "units" / "R-1" / "03.json"
    assert ws.consolidated_path("R-1") == ws.root / "units" / "R-1" / "consolidated.json"
    assert ws.verified_path("R-1") == ws.root / "verified" / "R-1.json"
    assert ws.rejects_path("R-1") == ws.root / "verified" / "R-1.rejects.json"
    assert ws.candidates_path("R-1") == ws.root / "match" / "R-1.candidates.json"
    assert ws.decisions_path("R-1") == ws.root / "match" / "R-1.decisions.json"
    assert ws.evidence_path() == ws.root / "evidence.md"
    assert ws.review_path() == ws.root / "review.json"
    assert ws.index_path() == ws.root / "index.json"
    assert ws.manifest_path() == ws.root / "manifest.json"


def test_write_and_read_chunks(tmp_path: Path):
    ws = Workspace(tmp_path, "A-jane-doe")
    resource = {"resource_id": "R-1", "title": "T", "published_date": "2020", "date_precision": "year", "source_type": "txt"}
    text = "\n\n".join(f"Paragraph {i} " + "x " * 100 for i in range(20))
    chunks = chunk_markdown(text, resource, 200, 20)
    assert len(chunks) > 1
    paths = ws.write_chunks(chunks)
    assert paths == [ws.chunks_dir("R-1") / f"{i:02d}.md" for i in range(len(chunks))]
    content = paths[0].read_text(encoding="utf-8")
    assert content.startswith("# T")
    assert content == chunks[0].header + "\n\n" + chunks[0].text
    texts = ws.read_chunk_texts("R-1")
    assert list(texts) == [c.chunk_id for c in chunks]
    for c in chunks:
        assert texts[c.chunk_id] == c.text
        assert CHUNK_TEXT_SENTINEL not in texts[c.chunk_id]
    meta = ws.read_json(ws.chunk_meta_path("R-1"))
    assert [m["chunk_id"] for m in meta] == [c.chunk_id for c in chunks]
    assert ws.read_chunk_texts("R-none") == {}


def test_write_chunks_without_sentinel_in_header(tmp_path: Path):
    ws = Workspace(tmp_path, "A-x")
    chunk = Chunk(chunk_id="R-2#00", resource_id="R-2", index=0, text="Body text\n\nmore", header="# plain header")
    ws.write_chunks([chunk])
    assert ws.read_chunk_texts("R-2") == {"R-2#00": "Body text\n\nmore"}
    assert strip_chunk_header("no sentinel here") == "no sentinel here"


def test_manifest_and_json(tmp_path: Path):
    ws = Workspace(tmp_path, "A-x")
    assert ws.load_manifest() == {}
    ws.save_manifest({"resources_done": ["R-1"], "version": 2})
    assert ws.load_manifest() == {"resources_done": ["R-1"], "version": 2}
    df = DecisionsFile(resource_id="R-1", decisions=[Decision(temp_id="t0", decision="NEW")])
    path = ws.write_json(ws.decisions_path("R-1"), df)
    assert path.is_file()
    data = ws.read_json(path)
    assert data["resource_id"] == "R-1" and data["decisions"][0]["decision"] == "NEW"
    assert DecisionsFile(**data) == df
    ws.write_json(ws.index_path(), [{"a": 1}])
    assert ws.read_json(ws.index_path()) == [{"a": 1}]
