#!/usr/bin/env python3
"""Live end-to-end rehearsal against the real accounts (run after /setup succeeded).

Creates a temporary stage "Smoke Test" under the first domain/primer, ingests a small generated PDF twice
(second run must be skipped as a duplicate), moves it to another stage, renames the stage and syncs, then
cleans up (trashes the files, archives the stage row). Nothing here touches existing resources.

    python scripts/smoke_live.py [--youtube https://www.youtube.com/watch?v=...]
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.context import AppContext  # noqa: E402
from pipeline.ingest import Ingestor  # noqa: E402
from pipeline.resources import move  # noqa: E402
from pipeline.taxonomy import add_node, load_taxonomy, rename_node, sync  # noqa: E402


def step(name: str, data) -> None:
    print(f"\n== {name}\n{json.dumps(data, indent=1, default=str)[:1500]}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--youtube", help="a short public video URL to test the Apify path")
    parser.add_argument("--keep", action="store_true", help="do not clean up")
    args = parser.parse_args()

    ctx = AppContext()
    reg = ctx.registry
    tax = load_taxonomy(reg)
    domain = tax.domains()[0]
    primer = tax.children(domain.node_id)[0]
    stage = add_node(reg, "stage", "Smoke Test", primer.node_id, "temporary stage created by scripts/smoke_live.py")
    stage2 = add_node(reg, "stage", "Smoke Test 2", primer.node_id, "temporary")
    step("stage", {"path": stage.path, "node_id": stage.node_id})

    import pymupdf

    with tempfile.TemporaryDirectory() as tmp:
        pdf = Path(tmp) / "smoke.pdf"
        doc = pymupdf.open()
        page = doc.new_page()
        page.insert_text((72, 90), "Smoke Test Document", fontsize=22)
        page.insert_text((72, 130), "by Smoke Tester", fontsize=12)
        page.insert_text((72, 160), "Copyright 2020 Nobody", fontsize=10)
        page.insert_text((72, 220), "Keep the rear heel light and pivot on the ball of the foot.", fontsize=11)
        doc.save(str(pdf))

        ing = Ingestor(ctx)
        step("probe", [p.__dict__ for p in ing.probe([str(pdf)])])
        r1 = ing.run(str(pdf), stage.node_id, author="Smoke Tester")
        step("run 1", r1)
        assert r1["status"] == "ingested", r1
        r2 = ing.run(str(pdf), stage.node_id, author="Smoke Tester")
        step("run 2 (must skip)", r2)
        assert r2["status"] == "skipped", r2
        mv = move(ctx, r1["resource_id"], stage2.node_id)
        step("move", mv)
        rn = rename_node(reg, stage2.node_id, "Smoke Test Renamed", ctx.drive_for)
        step("rename", rn.__dict__)
        sy = sync(reg, ctx.drives())
        step("sync", sy.__dict__)
        if args.youtube:
            ry = ing.run(args.youtube, stage.node_id)
            step("youtube", ry)

        if not args.keep:
            res = reg.resource(r1["resource_id"])
            drive = ctx.drive_by_id(res.account_id)
            for fid in [res.raw_file_id, res.text_file_id, *res.data_file_ids]:
                if fid and drive:
                    drive.backend.trash_file(fid)
            res.status = "failed"
            res.notes = "smoke test; files trashed"
            reg.upsert_resource(res)
            for n in (stage, stage2):
                node = reg.node(n.node_id)
                if node:
                    node.status = "archived"
                    reg.upsert_nodes([node])
            for fm in reg.folders():
                if fm.node_id in (stage.node_id, stage2.node_id):
                    d = ctx.drive_by_id(fm.account_id)
                    if d:
                        d.backend.trash_file(fm.folder_id)
            step("cleanup", "done (resource marked failed, stage rows archived, files trashed)")
    print("\nSMOKE TEST PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
