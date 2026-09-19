"""`pipeline summarize ...` and `pipeline doc ...`: the deterministic steps of the summary flow.

The agent orchestrates: prepare -> (helpers extract) -> verify -> match -> (helper decides) -> apply ->
review-prep -> (helper reviews) -> finalize. Every step reads/writes files under .work/<author>/ so a
session that dies can resume, and nothing here calls a language model.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from .context import AppContext
from .docs import DocsPublisher
from .ids import now_iso, today_iso
from .kb.chunker import chunk_markdown
from .kb.evidence import build_evidence
from .kb.matcher import build_index, find_candidates
from .kb.merge import apply_decisions
from .kb.render import render_summary
from .kb.units import UnitStore
from .kb.verify import dedupe_within_resource, verify_outputs
from .kb.workspace import Workspace
from .metadata import resolve_author
from .models import Author, Decision, DecisionsFile, ExtractionOutput, Resource, ReviewFile, SummaryRun, Unit, VerifiedUnit
from .naming import read_extracted_markdown
from .taxonomy import load_taxonomy

PROMPTS = Path(__file__).resolve().parent / "prompts"


# ----------------------------------------------------------------------------- helpers


def _author(ctx: AppContext, ref: str) -> Author:
    reg = ctx.registry
    a = reg.author(ref)
    if a:
        return a
    res = resolve_author(reg, ref)
    if not res.is_new:
        a = reg.author(res.author_id)
        if a:
            return a
    raise KeyError(f"unknown author {ref!r}; ingest a resource for them first or pass an A- id")


def _slug(author: Author) -> str:
    return author.author_id[2:] if author.author_id.startswith("A-") else author.author_id


def _store(ctx: AppContext, author: Author) -> UnitStore:
    return UnitStore(ctx.settings.knowledge_dir / _slug(author))


def _ws(ctx: AppContext, author: Author) -> Workspace:
    return Workspace(ctx.settings.work_dir, author.author_id)


def _resource_dict(r: Resource) -> dict[str, Any]:
    return {
        "resource_id": r.resource_id,
        "title": r.title,
        "published_date": r.published_date,
        "date_precision": r.date_precision,
        "stage_id": r.stage_id,
        "stage_path": r.stage_path,
        "source_type": r.source_type,
        "source_url": r.source_url,
        "raw_file_url": r.raw_file_url,
        "author_id": r.author_id,
    }


def _text_path(ctx: AppContext, r: Resource) -> Path:
    return ctx.settings.cache_dir / "text" / f"{r.resource_id}.extracted.md"


def _fetch_text(ctx: AppContext, r: Resource) -> tuple[dict[str, Any], str]:
    path = _text_path(ctx, r)
    if not path.exists():
        if not r.text_file_id:
            raise FileNotFoundError(f"{r.resource_id} has no text version (status {r.status})")
        drive = ctx.drive_by_id(r.account_id)
        if drive is None:
            raise RuntimeError(f"no credentials for account {r.account_id}")
        drive.download(r.text_file_id, path)
    return read_extracted_markdown(path)


def _rel(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def _cited_resources(store: UnitStore) -> set[str]:
    cited: set[str] = set()
    for u in store.load_all():
        cited.update(c.resource_id for c in u.citations)
    return cited


def _author_meta(store: UnitStore, author: Author) -> dict[str, Any]:
    meta = store.load_author_meta() or {}
    meta.setdefault("author_id", author.author_id)
    meta.setdefault("canonical_name", author.canonical_name)
    meta.setdefault("aliases", list(author.aliases))
    meta.setdefault("type", author.type)
    meta.setdefault("version", 0)
    meta.setdefault("changelog", [])
    meta["summary_doc_id"] = author.summary_doc_id or meta.get("summary_doc_id", "")
    meta["summary_doc_url"] = author.summary_doc_url or meta.get("summary_doc_url", "")
    return meta


# ----------------------------------------------------------------------------- steps


def plan(ctx: AppContext, author_ref: str) -> dict[str, Any]:
    author = _author(ctx, author_ref)
    store = _store(ctx, author)
    cited = _cited_resources(store)
    kb = ctx.settings.kb
    chunk_tokens = int(kb.get("chunk_tokens", 12000))
    max_run = int(kb.get("max_tokens_per_run", 300000))
    pending = []
    for r in ctx.registry.resources_for_author(author.author_id):
        if r.status not in ("ingested", "summarized") or r.resource_id in cited or not r.text_file_id:
            continue
        if r.status == "summarized":
            continue
        est = int((r.extracted_chars or 0) / 4)
        pending.append({"resource_id": r.resource_id, "title": r.title, "published_date": r.published_date, "source_type": r.source_type, "est_tokens": est, "est_chunks": max(1, -(-est // chunk_tokens))})
    pending.sort(key=lambda p: p["published_date"] or "9999")
    proposed, total = [], 0
    for p in pending:
        if proposed and total + p["est_tokens"] > max_run:
            break
        proposed.append(p["resource_id"])
        total += p["est_tokens"]
    return {
        "author_id": author.author_id,
        "canonical_name": author.canonical_name,
        "kb_path": _rel(store.author_dir, ctx.settings.repo_root),
        "existing_units": len(store.load_all()),
        "pending": pending,
        "proposed": proposed,
        "proposed_tokens": total,
        "next": "summarize prepare --author <id> --resources <comma-separated ids>",
    }


def prepare(ctx: AppContext, author_ref: str, resource_ids: list[str]) -> dict[str, Any]:
    author = _author(ctx, author_ref)
    store = _store(ctx, author)
    ws = _ws(ctx, author)
    kb = ctx.settings.kb
    if not resource_ids:
        resource_ids = plan(ctx, author_ref)["proposed"]
    manifest = ws.load_manifest()
    manifest.setdefault("author_id", author.author_id)
    manifest.setdefault("resources", {})
    out_resources = []
    for rid in resource_ids:
        r = ctx.registry.resource(rid)
        if r is None:
            raise KeyError(f"unknown resource {rid}")
        meta, text = _fetch_text(ctx, r)
        chunks = chunk_markdown(
            text,
            _resource_dict(r),
            int(kb.get("chunk_tokens", 12000)),
            int(kb.get("chunk_overlap_tokens", 300)),
            toc=meta.get("toc") or None,
            pages_per_window=int(kb.get("pages_per_window_without_toc", 30)),
        )
        paths = ws.write_chunks(chunks)
        entry = {
            "status": "prepared",
            "chunks": [
                {"chunk_id": c.chunk_id, "path": str(p), "output_path": str(ws.unit_output_path(rid, c.index)), "location": f"{c.location_start} - {c.location_end}".strip(" -")}
                for c, p in zip(chunks, paths)
            ],
            "consolidated_path": str(ws.consolidated_path(rid)),
            "text_chars": len(text),
        }
        manifest["resources"][rid] = entry
        out_resources.append({"resource_id": rid, "title": r.title, "published_date": r.published_date, **entry})
    ws.write_json(ws.index_path(), store.index())
    ws.save_manifest(manifest)
    return {
        "author_id": author.author_id,
        "work_dir": str(ws.root),
        "index_path": str(ws.index_path()),
        "resources": out_resources,
        "prompts": {"extract": str(PROMPTS / "extract_units.md"), "consolidate": str(PROMPTS / "consolidate_resource.md")},
        "parallel_helpers": int(kb.get("parallel_helpers", 4)),
        "next": "spawn one extraction helper per chunk (writes output_path), one consolidation helper per multi-chunk resource, then `summarize verify`",
    }


def _load_outputs(ws: Workspace, rid: str, n_chunks: int) -> list[ExtractionOutput]:
    cons = ws.consolidated_path(rid)
    if cons.exists():
        return [ExtractionOutput.model_validate(ws.read_json(cons))]
    outputs = []
    missing = []
    for i in range(n_chunks):
        p = ws.unit_output_path(rid, i)
        if p.exists():
            outputs.append(ExtractionOutput.model_validate(ws.read_json(p)))
        else:
            missing.append(str(p))
    if missing:
        raise FileNotFoundError(f"missing helper outputs: {missing[:3]}{' ...' if len(missing) > 3 else ''}")
    return outputs


def verify(ctx: AppContext, author_ref: str, rid: str) -> dict[str, Any]:
    author = _author(ctx, author_ref)
    store = _store(ctx, author)
    ws = _ws(ctx, author)
    manifest = ws.load_manifest()
    entry = manifest.get("resources", {}).get(rid)
    if not entry:
        raise KeyError(f"{rid} not prepared; run `summarize prepare` first")
    r = ctx.registry.resource(rid)
    _meta, full_text = _fetch_text(ctx, r) if r else ({}, "")
    outputs = _load_outputs(ws, rid, len(entry["chunks"]))
    for o in outputs:
        o.resource_id = o.resource_id or rid
    chunk_texts = ws.read_chunk_texts(rid)
    threshold = int(ctx.settings.kb.get("quote_match_threshold", 90))
    verified, rejects = verify_outputs(outputs, chunk_texts, full_text, threshold)
    before = len(verified)
    verified = dedupe_within_resource(verified)
    for rej in rejects:
        try:
            store.reject(rej, rej.get("reason", "verification failed"), temp_id=rej.get("temp_id", ""))
        except Exception:
            pass
    ws.write_json(ws.verified_path(rid), [v.model_dump() for v in verified])
    ws.write_json(ws.rejects_path(rid), rejects)
    entry["status"] = "verified"
    entry["verified"] = len(verified)
    entry["rejected"] = len(rejects)
    ws.save_manifest(manifest)
    extracted = sum(len(o.units) for o in outputs)
    return {
        "resource_id": rid,
        "extracted": extracted,
        "verified": len(verified),
        "merged_within_resource": before - len(verified),
        "rejected": len(rejects),
        "reject_rate": round(len(rejects) / extracted, 2) if extracted else 0.0,
        "rejects": rejects[:20],
        "verified_path": str(ws.verified_path(rid)),
        "next": "summarize match",
    }


def match(ctx: AppContext, author_ref: str, rid: str) -> dict[str, Any]:
    author = _author(ctx, author_ref)
    store = _store(ctx, author)
    ws = _ws(ctx, author)
    verified = [VerifiedUnit.model_validate(v) for v in ws.read_json(ws.verified_path(rid))]
    units = store.load_all()
    index = build_index(units)
    by_id = {u.id: u for u in units}
    items = [find_candidates(v, index, by_id, ctx.settings.kb) for v in verified]
    auto = {it.temp_id: it.auto_decision for it in items if it.auto_decision}
    payload = {"resource_id": rid, "items": [it.model_dump() for it in items], "auto": auto}
    ws.write_json(ws.candidates_path(rid), payload)
    manifest = ws.load_manifest()
    manifest.setdefault("resources", {}).setdefault(rid, {})["status"] = "matched"
    ws.save_manifest(manifest)
    needs_helper = [it.temp_id for it in items if not it.auto_decision]
    return {
        "resource_id": rid,
        "units": len(items),
        "auto_new": sum(1 for d in auto.values() if d == "NEW"),
        "auto_same": sum(1 for d in auto.values() if d == "SAME"),
        "needs_helper": len(needs_helper),
        "candidates_path": str(ws.candidates_path(rid)),
        "decisions_path": str(ws.decisions_path(rid)),
        "prompt": str(PROMPTS / "match_units.md"),
        "next": "spawn the matching helper (writes decisions_path) if needs_helper > 0, then `summarize apply`",
    }


def apply(ctx: AppContext, author_ref: str, rid: str) -> dict[str, Any]:
    author = _author(ctx, author_ref)
    store = _store(ctx, author)
    ws = _ws(ctx, author)
    r = ctx.registry.resource(rid)
    if r is None:
        raise KeyError(rid)
    cand = ws.read_json(ws.candidates_path(rid))
    verified = {v["temp_id"]: VerifiedUnit.model_validate(v["unit"]) for v in cand["items"]}
    decisions: dict[str, Decision] = {}
    if ws.decisions_path(rid).exists():
        df = DecisionsFile.model_validate(ws.read_json(ws.decisions_path(rid)))
        decisions = {d.temp_id: d for d in df.decisions}
    for temp_id, auto_decision in cand.get("auto", {}).items():
        if temp_id not in decisions:
            target = ""
            if auto_decision == "SAME":
                item = next(i for i in cand["items"] if i["temp_id"] == temp_id)
                target = item["candidates"][0]["unit_id"] if item["candidates"] else ""
            decisions[temp_id] = Decision(temp_id=temp_id, decision=auto_decision, target_unit_id=target, rationale="auto")
    missing = [t for t in verified if t not in decisions]
    if missing:
        raise RuntimeError(f"no decision for {len(missing)} units (helper output incomplete): {missing[:3]}")
    # snapshot targets for the reviewer
    manifest = ws.load_manifest()
    snapshots = manifest.setdefault("before", {})
    for d in decisions.values():
        if d.target_unit_id and d.target_unit_id not in snapshots:
            u = store.get(d.target_unit_id)
            snapshots[d.target_unit_id] = u.model_dump() if u else None
    report = apply_decisions(store, DecisionsFile(resource_id=rid, decisions=list(decisions.values())), verified, _resource_dict(r), author.author_id, default_stages=[r.stage_id] if r.stage_id else None)
    changed = manifest.setdefault("changed_units", [])
    for uid in [*report.new, *report.same, *report.evolved, *report.contradicted]:
        if uid not in changed:
            changed.append(uid)
    for uid in report.new:
        snapshots.setdefault(uid, None)
    manifest.setdefault("resources", {}).setdefault(rid, {})["status"] = "applied"
    manifest["resources"][rid]["merge"] = {"new": report.new, "same": report.same, "evolved": report.evolved, "contradicted": report.contradicted, "errors": report.errors}
    ws.save_manifest(manifest)
    return {
        "resource_id": rid,
        "new": len(report.new),
        "same": len(report.same),
        "evolved": len(report.evolved),
        "contradicted": len(report.contradicted),
        "skipped": len(report.skipped),
        "errors": report.errors,
        "next": "repeat for the other resources, then `summarize review-prep`",
    }


def review_prep(ctx: AppContext, author_ref: str) -> dict[str, Any]:
    author = _author(ctx, author_ref)
    store = _store(ctx, author)
    ws = _ws(ctx, author)
    manifest = ws.load_manifest()
    changed = manifest.get("changed_units", [])
    before_raw = manifest.get("before", {})
    before = {uid: (Unit.model_validate(v) if v else None) for uid, v in before_raw.items()}
    texts: dict[str, str] = {}
    for rid in manifest.get("resources", {}):
        r = ctx.registry.resource(rid)
        if r:
            try:
                texts[rid] = _fetch_text(ctx, r)[1]
            except Exception:
                texts[rid] = ""
    evidence = build_evidence(store, changed, before, texts)
    ws.evidence_path().write_text(evidence, encoding="utf-8")
    return {"changed_units": len(changed), "evidence_path": str(ws.evidence_path()), "review_path": str(ws.review_path()), "prompt": str(PROMPTS / "review_units.md"), "next": "spawn ONE reviewer helper with only evidence_path, then `summarize finalize`"}


def finalize(ctx: AppContext, author_ref: str, note: str = "", publish_doc: bool = True, commit: bool = True) -> dict[str, Any]:
    author = _author(ctx, author_ref)
    store = _store(ctx, author)
    ws = _ws(ctx, author)
    reg = ctx.registry
    manifest = ws.load_manifest()
    # 1. review verdicts
    review = ReviewFile(author_id=author.author_id)
    if ws.review_path().exists():
        review = ReviewFile.model_validate(ws.read_json(ws.review_path()))
    rejected, flagged = [], []
    for item in review.items:
        u = store.get(item.unit_id)
        if u is None:
            continue
        if item.verdict == "reject":
            store.reject(u, item.reason or "rejected by reviewer")
            store.delete(u.id)
            rejected.append(u.id)
        elif item.verdict == "needs_review":
            u.status = "needs_review"
            store.save(u)
            flagged.append(u.id)
    # 2. render
    units = store.load_all()
    meta = _author_meta(store, author)
    version = int(meta.get("version", 0)) + 1
    resources = [_resource_dict(r) for r in reg.resources_for_author(author.author_id)]
    changelog = list(meta.get("changelog", []))
    merges = [m for rid, e in manifest.get("resources", {}).items() for m in [e.get("merge", {})]]
    counts = {k: sum(len(m.get(k, [])) for m in merges) for k in ("new", "same", "evolved", "contradicted")}
    included = sorted(manifest.get("resources", {}).keys())
    changelog.append({"version": version, "date": today_iso(), "note": note or f"+{counts['new']} new, ~{counts['evolved']} evolved, ={counts['same']} same from {', '.join(included) or 'no new resources'}", "resources": included})
    files = render_summary(meta, units, resources, load_taxonomy(reg).to_render_map(), ctx.settings.kb, version, changelog, now_iso())
    written = []
    for name, text in files.items():
        p = store.author_dir / name
        p.write_text(text, encoding="utf-8")
        written.append(str(p))
    # 3. publish the doc(s)
    doc_info: dict[str, Any] = {}
    if publish_doc:
        summary_account = reg.summary_account()
        drive = ctx.drive_for(summary_account) if summary_account else None
        if drive is None or not summary_account or not summary_account.summaries_folder_id:
            doc_info = {"skipped": "summary account not configured (run /setup)"}
        else:
            publisher = DocsPublisher(drive, str(ctx.settings.docs.get("sharing", "anyone_with_link")))
            title_prefix = str(ctx.settings.docs.get("title_prefix", "Summary — "))
            main = publisher.publish_markdown(files["summary.md"], f"{title_prefix}{author.canonical_name}", summary_account.summaries_folder_id, meta.get("summary_doc_id") or None)
            doc_info = {"doc_id": main["doc_id"], "url": main["url"], "created": main["created"]}
            meta["summary_doc_id"], meta["summary_doc_url"] = main["doc_id"], main["url"]
            parts = {}
            part_ids = dict(meta.get("part_doc_ids", {}))
            for name, text in files.items():
                if name == "summary.md":
                    continue
                pub = publisher.publish_markdown(text, f"{title_prefix}{author.canonical_name} — {Path(name).stem.replace('summary - ', '')}", summary_account.summaries_folder_id, part_ids.get(name))
                part_ids[name] = pub["doc_id"]
                parts[name] = pub["url"]
            if parts:
                meta["part_doc_ids"] = part_ids
                doc_info["parts"] = parts
    # 4. author meta + commit
    meta["version"] = version
    meta["changelog"] = changelog
    meta["last_generated_at"] = now_iso()
    meta["unit_count"] = len(units)
    store.save_author_meta(meta)
    commit_hash = ""
    if commit:
        commit_hash = _git_commit(ctx.settings.repo_root, store.author_dir, f"kb({_slug(author)}): v{version} +{counts['new']} new ~{counts['evolved']} evolved ={counts['same']} same" + (f" from {', '.join(included)}" if included else ""))
    # 5. registry
    author.summary_doc_id = meta.get("summary_doc_id", author.summary_doc_id)
    author.summary_doc_url = meta.get("summary_doc_url", author.summary_doc_url)
    author.unit_count = len(units)
    author.last_summarized_at = now_iso()
    author.kb_path = f"knowledge/{_slug(author)}"
    reg.upsert_author(author)
    reg.append_summary(
        SummaryRun(
            summary_id=f"{author.author_id}@v{version}",
            author_id=author.author_id,
            version=version,
            generated_at=now_iso(),
            resources_included=[r["resource_id"] for r in resources],
            resources_new=included,
            units_total=len(units),
            units_new=counts["new"],
            units_updated=counts["same"],
            units_evolved=counts["evolved"],
            units_contradicted=counts["contradicted"],
            units_rejected=len(rejected),
            review_verdict=review.overall if review.items else "no review",
            doc_url=author.summary_doc_url,
            kb_commit=commit_hash,
            notes=note,
        )
    )
    for rid in included:
        r = reg.resource(rid)
        if r and r.status == "ingested":
            r.status = "summarized"
            r.summarized_at = now_iso()
            reg.upsert_resource(r)
    # 6. reset the workspace bookkeeping (chunks stay for inspection)
    manifest["changed_units"] = []
    manifest["before"] = {}
    manifest["last_finalized"] = {"version": version, "at": now_iso(), "resources": included}
    manifest["resources"] = {}
    ws.save_manifest(manifest)
    if ws.review_path().exists():
        ws.review_path().rename(ws.root / f"review-v{version}.json")
    return {"author_id": author.author_id, "version": version, "units_total": len(units), **counts, "rejected_by_review": rejected, "flagged": flagged, "files": written, "doc": doc_info, "commit": commit_hash, "resources_summarized": included}


def status(ctx: AppContext, author_ref: str) -> dict[str, Any]:
    author = _author(ctx, author_ref)
    store = _store(ctx, author)
    ws = _ws(ctx, author)
    manifest = ws.load_manifest()
    units = store.load_all()
    by_type: dict[str, int] = {}
    for u in units:
        by_type[u.type] = by_type.get(u.type, 0) + 1
    return {"author_id": author.author_id, "units": len(units), "by_type": by_type, "version": _author_meta(store, author).get("version", 0), "doc_url": author.summary_doc_url, "in_progress": {rid: e.get("status") for rid, e in manifest.get("resources", {}).items()}, "pending": plan(ctx, author_ref)["pending"]}


def doc_comments(ctx: AppContext, author_ref: str, resolve: str | None = None) -> dict[str, Any]:
    author = _author(ctx, author_ref)
    if not author.summary_doc_id:
        return {"comments": [], "note": "no summary Doc yet"}
    summary_account = ctx.registry.summary_account()
    drive = ctx.drive_for(summary_account) if summary_account else None
    if drive is None:
        raise RuntimeError("summary account credentials missing")
    publisher = DocsPublisher(drive)
    if resolve:
        publisher.resolve(author.summary_doc_id, resolve)
        return {"resolved": resolve}
    return {"doc_url": author.summary_doc_url, "comments": publisher.unresolved_comments(author.summary_doc_id)}


def _git_commit(repo_root: Path, path: Path, message: str) -> str:
    try:
        rel = str(path.relative_to(repo_root))
        subprocess.run(["git", "add", "-A", rel], cwd=repo_root, check=True, capture_output=True)
        staged = subprocess.run(["git", "diff", "--cached", "--quiet", "--", rel], cwd=repo_root)
        if staged.returncode == 0:
            return ""
        subprocess.run(["git", "commit", "-q", "-m", message], cwd=repo_root, check=True, capture_output=True)
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=repo_root, check=True, capture_output=True, text=True).stdout.strip()
    except Exception as exc:  # a failed commit must not lose the rendered files
        return f"commit failed: {exc}"
