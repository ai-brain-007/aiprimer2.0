"""Ingestion orchestration: probe (no side effects), run (resumable status machine), channels, inbox."""

from __future__ import annotations

import json
import shutil
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from . import extract as ex
from .accounts import NoStorageError, mark_full, pick_raw_account
from .apify_yt import ApifyYouTube, metadata_guess_from_video
from .config import Settings
from .context import AppContext
from .dedup import NaturalKey, find_existing, natural_key_for_file, natural_key_for_url, near_duplicates
from .drive import DriveClient, file_url
from .extract.youtube import transcript_extraction
from .ids import now_iso, youtube_video_id
from .metadata import apply_overrides, ensure_author, resolve_author
from .models import Account, MetadataGuess, Resource
from .naming import data_export_filename, drive_filename, extracted_filename, write_extracted_markdown
from .taxonomy import Taxonomy, ensure_node_folder, load_taxonomy

TEXT_MIME = "text/markdown"


@dataclass
class ProbeResult:
    source: str
    kind: str = ""
    resource_id: str = ""
    natural_key: str = ""
    status: str = "new"  # new | duplicate | error | channel
    existing: dict | None = None
    metadata: dict = field(default_factory=dict)
    size_bytes: int | None = None
    needs_vision: bool = False
    near_duplicates: list[dict] = field(default_factory=list)
    author_match: dict = field(default_factory=dict)
    stage_hints: list[dict] = field(default_factory=list)
    channel: dict | None = None
    error: str = ""


class Ingestor:
    def __init__(self, ctx: AppContext):
        self.ctx = ctx
        self.settings: Settings = ctx.settings
        self.registry = ctx.registry
        self._apify: ApifyYouTube | None = None
        self._tax: Taxonomy | None = None

    # ---- helpers
    @property
    def apify(self) -> ApifyYouTube:
        if self._apify is None:
            self._apify = self.ctx.apify
        return self._apify

    @property
    def taxonomy(self) -> Taxonomy:
        if self._tax is None:
            self._tax = load_taxonomy(self.registry)
        return self._tax

    def _workdir(self, resource_id: str) -> Path:
        d = self.settings.cache_dir / "ingest" / resource_id
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _author_name(self, resource: Resource) -> str:
        if resource.author_id:
            a = self.registry.author(resource.author_id)
            if a:
                return a.canonical_name
        return resource.author_raw

    # ---- probe -----------------------------------------------------------------
    def probe(self, sources: list[str], sample_text: bool = True) -> list[ProbeResult]:
        out = []
        for s in sources:
            try:
                out.append(self._probe_one(s, sample_text))
            except Exception as exc:
                out.append(ProbeResult(source=s, status="error", error=str(exc)))
        return out

    def _probe_one(self, source: str, sample_text: bool) -> ProbeResult:
        kind = ex.detect_kind(source)
        pr = ProbeResult(source=source, kind=kind)
        if kind in ("youtube_channel", "youtube_playlist"):
            pr.status = "channel"
            pr.channel = self.channel_list(source)
            return pr
        if kind == "youtube":
            nk = natural_key_for_url(source)
            vid = nk.key
            meta = self.apify.video_metadata([vid]).get(vid)
            guess = metadata_guess_from_video(meta) if meta else MetadataGuess(evidence=["metadata actor returned nothing"])
            text_sample = (meta or {}).get("description", "")
        elif kind == "web":
            nk = natural_key_for_url(source)
            guess = MetadataGuess(title=source, evidence=["web page: paste a screenshot or a saved file to ingest its content"])
            text_sample = ""
        else:
            path = Path(source)
            if not path.exists():
                raise FileNotFoundError(source)
            nk = natural_key_for_file(path)
            pr.size_bytes = path.stat().st_size
            guess = MetadataGuess(title=path.stem)
            text_sample = ""
            if sample_text:
                try:
                    extraction = ex.extract_file(path, kind, self.settings, self._workdir(nk.resource_id) / "probe")
                    guess = extraction.metadata
                    pr.needs_vision = extraction.needs_vision
                    text_sample = extraction.text[:20000]
                except Exception as exc:
                    guess.evidence.append(f"extraction preview failed: {exc}")
        pr.resource_id, pr.natural_key = nk.resource_id, nk.natural_key
        pr.metadata = guess.model_dump()
        existing = find_existing(self.registry, nk)
        if existing and existing.status not in ("registered", "failed"):
            pr.status = "duplicate"
            pr.existing = {"resource_id": existing.resource_id, "status": existing.status, "stage_path": existing.stage_path, "raw_file_url": existing.raw_file_url, "title": existing.title}
        elif existing:
            pr.existing = {"resource_id": existing.resource_id, "status": existing.status, "note": "previous attempt incomplete; run will resume"}
        if guess.author_raw:
            res = resolve_author(self.registry, guess.author_raw)
            pr.author_match = {"author_raw": guess.author_raw, "author_id": res.author_id, "canonical_name": res.canonical_name, "is_new": res.is_new, "matched_by": res.matched_by, "near_matches": res.near_matches}
        pr.near_duplicates = near_duplicates(self.registry, guess.title, pr.author_match.get("author_id", "") if not pr.author_match.get("is_new", True) else "")
        pr.stage_hints = self.taxonomy.suggest_stages(f"{guess.title} {guess.description} {text_sample}", k=3)
        return pr

    # ---- run -------------------------------------------------------------------
    def run(
        self,
        source: str,
        stage: str,
        author: str | None = None,
        *,
        title: str | None = None,
        date_text: str | None = None,
        date_precision: str | None = None,
        extracted_file: Path | None = None,
        ocr_patch: Path | None = None,
        account_id: str | None = None,
        force: bool = False,
        drive_file_id: str | None = None,
        dataset_note: str | None = None,
    ) -> dict[str, Any]:
        kind = ex.detect_kind(source)
        if kind in ("youtube_channel", "youtube_playlist"):
            raise ValueError("channel/playlist URL: use `ingest channel` instead")
        node = self.taxonomy.resolve(stage)
        if node is None or node.level != "stage":
            raise ValueError(f"unknown stage: {stage!r} (use a node id or 'Domain / Primer / Stage')")

        # 1. identity + metadata
        local_path: Path | None = None
        video: dict | None = None
        if kind == "youtube":
            nk = natural_key_for_url(source)
            video = self.apify.video_metadata([nk.key]).get(nk.key) or {"video_id": nk.key, "url": source, "title": ""}
            guess = metadata_guess_from_video(video)
        elif kind == "web":
            raise ValueError("a bare web address cannot be ingested: attach a screenshot, a PDF export or a saved file of the page")
        else:
            local_path = Path(source)
            if not local_path.exists():
                raise FileNotFoundError(source)
            nk = natural_key_for_file(local_path)
            guess = MetadataGuess(title=local_path.stem)

        existing = self.registry.resource(nk.resource_id) or self.registry.find_resource_by_natural_key(nk.natural_key)
        if existing and existing.status in ("ingested", "summarized", "needs_transcription") and not force:
            return {"status": "skipped", "reason": "already ingested", "resource_id": existing.resource_id, "stage_path": existing.stage_path, "raw_file_url": existing.raw_file_url, "title": existing.title}
        resource = existing or Resource(resource_id=nk.resource_id, natural_key=nk.natural_key)
        resource.source_type = "youtube" if kind == "youtube" else kind  # type: ignore[assignment]
        resource.source_url = source if kind == "youtube" else resource.source_url
        workdir = self._workdir(resource.resource_id)

        # 2. extraction (before upload for files, so metadata from the content is available for naming)
        extraction: ex.Extraction | None = None
        if kind == "youtube":
            t = self.apify.transcripts([nk.key]).get(nk.key)
            if t and (t.get("segments") or t.get("text")):
                extraction = transcript_extraction(t.get("segments") or t.get("text"), int(self.settings.extract.get("transcript_marker_seconds", 60)), language=t.get("language", ""), transcript_kind=t.get("kind", "auto"), duration_sec=video.get("duration_sec") if video else None)
            else:
                extraction = ex.Extraction(text="", metadata=guess, source_type="youtube", transcript_kind="none", warnings=["no transcript returned by the transcript actor"])
            extraction.metadata = guess
            if video:
                video_json = workdir / f"{nk.key}.youtube.json"
                video_json.write_text(json.dumps({"metadata": video, "transcript": t}, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
                local_path = video_json
        else:
            assert local_path is not None
            extraction = ex.extract_file(local_path, kind, self.settings, workdir)
            guess = extraction.metadata
        if extracted_file is not None:
            extraction.text = Path(extracted_file).read_text(encoding="utf-8")
            extraction.needs_vision = False
            extraction.transcript_kind = "vision" if extraction.transcript_kind in ("vision", "none", "ocr") else extraction.transcript_kind
        if ocr_patch is not None:
            extraction.text = _apply_ocr_patch(extraction.text, json.loads(Path(ocr_patch).read_text(encoding="utf-8")))
            extraction.needs_vision = False
        guess = apply_overrides(guess, title=title, author=author if author and not author.startswith("A-") else None, date_text=date_text, date_precision=date_precision)

        # 3. resource row
        resource.title = guess.title or resource.title or resource.resource_id
        resource.author_raw = guess.author_raw or resource.author_raw
        resource.published_date, resource.date_precision = guess.published_date, guess.date_precision  # type: ignore[assignment]
        resource.language = extraction.language or resource.language
        resource.transcript_kind = extraction.transcript_kind
        resource.pages, resource.duration_sec = extraction.pages, extraction.duration_sec or guess.duration_sec
        if kind == "youtube" and video:
            resource.duration_sec = video.get("duration_sec")
        resource.stage_id, resource.stage_path = node.node_id, self.taxonomy.path(node.node_id)
        resource.author_override = bool(author)
        if dataset_note:
            resource.notes = (resource.notes + " " if resource.notes else "") + f"dataset: {dataset_note}"
        if resource.status in ("registered", "failed") or existing is None:
            resource.status = "registered"
        # author
        author_row = self._resolve_author_arg(author, guess, video)
        if author_row:
            resource.author_id = author_row.author_id
        self.registry.upsert_resource(resource)

        # 4. account + folder
        needed = (local_path.stat().st_size if local_path else 0) + len(extraction.text.encode("utf-8")) + 1_000_000
        preferred = account_id or resource.account_id or self._account_holding(node.node_id)
        account = pick_raw_account(self.registry, needed, preferred)
        drive = self.ctx.drive_for(account)
        if drive is None:
            raise NoStorageError(f"no credentials for account {account.account_id} ({account.token_env_var})")
        folder = ensure_node_folder(self.registry, drive, account, node.node_id)
        resource.account_id, resource.folder_id = account.account_id, folder.folder_id
        if resource.status == "registered":
            resource.status = "folder_ready"
        self.registry.upsert_resource(resource)

        # 5. raw upload (or move of an inbox file)
        author_name = self._author_name(resource)
        ext = Path(local_path).suffix if local_path else ".json"
        raw_name = drive_filename(resource, ext, author_name, int(self.settings.drive.get("max_title_chars", 80)))
        try:
            if drive_file_id:
                meta = drive.move(drive_file_id, folder.folder_id)
                meta = drive.backend.update_file(drive_file_id, name=raw_name, app_properties={"resource_id": resource.resource_id, "role": "raw", "natural_key": nk.natural_key, "aiprimer": "1"})
            elif local_path is not None:
                meta = drive.upload(local_path, raw_name, folder.folder_id, {"resource_id": resource.resource_id, "role": "raw", "natural_key": nk.natural_key, "source_url": resource.source_url or "", "aiprimer": "1"})
            else:
                meta = {}
        except Exception as exc:
            if _is_quota_error(exc):
                mark_full(self.registry, account)
                resource.status = "registered"
                resource.error = f"account {account.account_id} full: {exc}"
                self.registry.upsert_resource(resource)
                return {"status": "retry", "reason": "account full, marked; run again to use the next account", "resource_id": resource.resource_id}
            raise
        if meta:
            resource.raw_file_id = meta.get("id", "")
            resource.raw_file_url = meta.get("webViewLink") or file_url(resource.raw_file_id)
            resource.raw_filename = meta.get("name", raw_name)
        if resource.status in ("registered", "folder_ready"):
            resource.status = "uploaded"
        self.registry.upsert_resource(resource)

        # 6. vision / transcription gates
        if extraction.needs_vision:
            resource.error = "needs vision transcription"
            self.registry.upsert_resource(resource)
            return {
                "status": "needs_vision",
                "resource_id": resource.resource_id,
                "vision_files": [str(p) for p in extraction.vision_files],
                "vision_pages": extraction.vision_pages,
                "partial_text_file": str(_write_partial(workdir, extraction.text)),
                "how": "transcribe the listed files (prompts/transcribe_page.md), then re-run with --extracted-file <md> (whole text) or --ocr-patch <json {page: text}> (pages only)",
                "raw_file_url": resource.raw_file_url,
            }
        if extraction.transcript_kind == "none" and kind != "youtube":
            resource.status = "needs_transcription"
            resource.error = ""
            resource.ingested_at = now_iso()
            self.registry.upsert_resource(resource)
            self._bump_author(resource)
            return {"status": "needs_transcription", "resource_id": resource.resource_id, "raw_file_url": resource.raw_file_url, "stage_path": resource.stage_path, "note": "stored; transcription backend comes in a later phase"}

        # 7. extracted text + data exports
        text_name = extracted_filename(resource, author_name, int(self.settings.drive.get("max_title_chars", 80)))
        extra = {"toc": extraction.toc, "warnings": extraction.warnings, "source_kind": kind, "transcript_kind": extraction.transcript_kind, "extracted_at": now_iso()}
        text_path = write_extracted_markdown(workdir / text_name, resource, extra, extraction.text)
        tmeta = drive.upload(text_path, text_name, folder.folder_id, {"resource_id": resource.resource_id, "role": "text", "aiprimer": "1"}, mime_type=TEXT_MIME)
        resource.text_file_id = tmeta.get("id", "")
        data_ids = []
        for export in extraction.data_exports:
            dname = data_export_filename(resource, Path(export).stem, author_name)
            dmeta = drive.upload(Path(export), dname, folder.folder_id, {"resource_id": resource.resource_id, "role": f"data:{Path(export).stem}", "aiprimer": "1"}, mime_type="text/csv")
            data_ids.append(dmeta.get("id", ""))
        resource.data_file_ids = data_ids
        resource.extracted_chars = len(extraction.text)
        resource.status = "ingested"
        resource.error = ""
        resource.ingested_at = now_iso()
        self.registry.upsert_resource(resource)
        self._bump_author(resource)
        return {
            "status": "ingested",
            "resource_id": resource.resource_id,
            "title": resource.title,
            "author": author_name,
            "author_id": resource.author_id,
            "published_date": resource.published_date,
            "date_precision": resource.date_precision,
            "stage_path": resource.stage_path,
            "account_id": resource.account_id,
            "raw_file_url": resource.raw_file_url,
            "text_file_id": resource.text_file_id,
            "extracted_chars": resource.extracted_chars,
            "warnings": extraction.warnings,
            "apify_run_ids": list(self._apify.run_ids) if self._apify else [],
            "apify_cost_usd": self._apify.cost_usd if self._apify else 0.0,
        }

    def _resolve_author_arg(self, author: str | None, guess: MetadataGuess, video: dict | None):
        channel_url = (video or {}).get("channel_url", "") if video else ""
        if author and author.startswith("A-"):
            row = self.registry.author(author)
            if row is None:
                raise ValueError(f"unknown author id {author}")
            if guess.author_raw and guess.author_raw != row.canonical_name:
                ensure_author(self.registry, guess.author_raw, alias_of=row.author_id, channel_url=channel_url)
            return row
        name = author or guess.author_raw
        if not name:
            return None
        return ensure_author(self.registry, name, "channel" if (video and not author) else "person", channel_url=channel_url)

    def _account_holding(self, node_id: str) -> str | None:
        for fm in self.registry.folders():
            if fm.node_id == node_id:
                return fm.account_id
        return None

    def _bump_author(self, resource: Resource) -> None:
        if not resource.author_id:
            return
        a = self.registry.author(resource.author_id)
        if a is None:
            return
        a.resource_count = len(self.registry.resources_for_author(a.author_id))
        self.registry.upsert_author(a)

    # ---- channels ---------------------------------------------------------------
    def channel_list(self, url: str, max_results: int | None = None, refresh: bool = False) -> dict[str, Any]:
        videos = self.apify.channel_videos(url, max_results, use_cache=not refresh)
        rows = []
        already = 0
        total_sec = 0
        for v in videos:
            existing = self.registry.resource(f"R-YT-{v['video_id']}")
            done = bool(existing and existing.status in ("ingested", "summarized"))
            already += done
            total_sec += v.get("duration_sec") or 0
            rows.append({"video_id": v["video_id"], "title": v.get("title", ""), "published_date": v.get("published_date", ""), "duration_sec": v.get("duration_sec"), "is_short": v.get("is_short", False), "ingested": done})
        rows.sort(key=lambda r: r["published_date"] or "0000", reverse=True)
        pending = len(rows) - already
        return {
            "channel_url": url,
            "count": len(rows),
            "already_ingested": already,
            "pending": pending,
            "total_hours": round(total_sec / 3600, 1),
            "date_range": [min((r["published_date"] for r in rows if r["published_date"]), default=""), max((r["published_date"] for r in rows if r["published_date"]), default="")],
            "estimated_cost_usd": self.apify.estimate_cost(pending),
            "cost_confirm_usd": float(self.settings.apify.get("cost_confirm_usd", 5.0)),
            "channel_name": next((v.get("channel_name") for v in videos if v.get("channel_name")), ""),
            "videos": rows,
        }

    def channel_run(self, url: str, video_ids: list[str], stage: str, author: str | None = None, **kwargs: Any) -> dict[str, Any]:
        results: dict[str, Any] = {"ingested": [], "skipped": [], "failed": []}
        # fetch transcripts in batches first (cached per video), then ingest one by one
        self.apify.transcripts(video_ids)
        for vid in video_ids:
            src = f"https://www.youtube.com/watch?v={vid}"
            try:
                r = self.run(src, stage, author, **kwargs)
                (results["ingested"] if r["status"] == "ingested" else results["skipped"]).append({"video_id": vid, **{k: r.get(k) for k in ("status", "resource_id", "title", "reason")}})
            except Exception as exc:
                results["failed"].append({"video_id": vid, "error": str(exc)})
        results["apify_run_ids"] = list(self.apify.run_ids)
        results["apify_cost_usd"] = self.apify.cost_usd
        return results

    # ---- inbox ------------------------------------------------------------------
    def inbox_list(self) -> list[dict[str, Any]]:
        out = []
        for account in self.registry.raw_accounts(include_full=True):
            if not account.inbox_folder_id:
                continue
            drive = self.ctx.drive_for(account)
            if drive is None:
                continue
            for f in drive.list_children(account.inbox_folder_id):
                if f.get("mimeType") == "application/vnd.google-apps.folder":
                    continue
                out.append({"account_id": account.account_id, "file_id": f["id"], "name": f.get("name"), "size": int(f.get("size", 0) or 0), "mime": f.get("mimeType"), "url": f.get("webViewLink")})
        return out

    def inbox_file(self, file_id: str, stage: str, author: str | None = None, **kwargs: Any) -> dict[str, Any]:
        for account in self.registry.raw_accounts(include_full=True):
            drive = self.ctx.drive_for(account)
            if drive is None:
                continue
            try:
                meta = drive.get(file_id)
            except Exception:
                continue
            local = self.settings.cache_dir / "inbox" / account.account_id / meta.get("name", file_id)
            drive.download(file_id, local)
            return self.run(str(local), stage, author, account_id=account.account_id, drive_file_id=file_id, **kwargs)
        raise FileNotFoundError(f"inbox file {file_id} not found in any raw account")


# ----------------------------------------------------------------------------- helpers


def _apply_ocr_patch(text: str, patch: dict[Any, str]) -> str:
    """Replace the body of the given pages (keys: 1-based page numbers) in a page-marked document.
    Pages absent from the document are appended at the end in page order."""
    import re

    by_page = {int(k): str(v) for k, v in patch.items()}
    parts = re.split(r"(<!-- page \d+ -->)", text)
    out: list[str] = []
    seen: set[int] = set()
    i = 0
    while i < len(parts):
        piece = parts[i]
        m = re.match(r"<!-- page (\d+) -->", piece)
        if m:
            page = int(m.group(1))
            body = parts[i + 1] if i + 1 < len(parts) else ""
            if page in by_page:
                seen.add(page)
                body = "\n" + by_page[page].strip() + "\n\n"
            out.append(piece)
            out.append(body)
            i += 2
            continue
        out.append(piece)
        i += 1
    for page in sorted(p for p in by_page if p not in seen):
        out.append(f"\n<!-- page {page} -->\n{by_page[page].strip()}\n")
    return "".join(out)


def _write_partial(workdir: Path, text: str) -> Path:
    p = workdir / "partial.extracted.md"
    p.write_text(text, encoding="utf-8")
    return p


def _is_quota_error(exc: Exception) -> bool:
    s = str(exc).lower()
    return "storagequotaexceeded" in s or "storage quota" in s or "quota has been exceeded" in s


def cleanup_cache(settings: Settings, resource_id: str) -> None:
    shutil.rmtree(settings.cache_dir / "ingest" / resource_id, ignore_errors=True)
