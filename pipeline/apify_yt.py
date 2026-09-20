"""YouTube metadata and transcripts through Apify actors.

Two actors (configured in config/pipeline.yaml):
- metadata:   apidojo/youtube-scraper           (video or channel URL -> video details)
- transcript: supreme_coder/youtube-transcript-scraper (video URLs -> transcript)

The token is normally attached by the cloud environment's API-credential proxy for api.apify.com,
so the client is created without a token; APIFY_TOKEN is only a fallback.
Actor output shapes vary between versions, so parsing is tolerant (several key spellings).
"""

from __future__ import annotations

import inspect
import json
import re
from datetime import timedelta
from pathlib import Path
from typing import Any, Protocol

from .config import Settings, save_config
from .ids import youtube_video_id
from .metadata import parse_date
from .models import MetadataGuess


class ApifyRunner(Protocol):
    def run(self, actor_id: str, run_input: dict, timeout_secs: int = 1800) -> dict: ...
    def dataset_items(self, run_id: str) -> list[dict]: ...
    def actor_input_schema(self, actor_id: str) -> dict: ...


def as_dict(obj: Any) -> dict:
    """Normalize an apify-client response to a camelCase dict.

    apify-client < 3 returns plain dicts; apify-client >= 3 returns pydantic models whose fields are
    snake_case with camelCase aliases (``run.default_dataset_id`` <-> ``"defaultDatasetId"``).
    """
    if obj is None:
        return {}
    if isinstance(obj, dict):
        return obj
    dump = getattr(obj, "model_dump", None)
    if callable(dump):
        return dump(by_alias=True, mode="json")
    return dict(obj)


class RealApifyRunner:
    def __init__(self, token: str | None = None, client: Any | None = None):
        if client is None:
            from apify_client import ApifyClient

            client = ApifyClient(token=token) if token else ApifyClient()
        self.client = client

    def _call_actor(self, actor_id: str, run_input: dict, timeout_secs: int) -> dict:
        """Run an actor and wait for it, with the keyword names of the installed apify-client."""
        actor = self.client.actor(actor_id)
        params = inspect.signature(actor.call).parameters
        kwargs: dict[str, Any] = {"run_input": run_input}
        if "timeout_secs" in params:  # apify-client 1.x / 2.x
            kwargs["timeout_secs"] = timeout_secs
            kwargs["wait_secs"] = timeout_secs
        else:  # apify-client >= 3: timedeltas
            kwargs["run_timeout"] = timedelta(seconds=timeout_secs)
            kwargs["wait_duration"] = timedelta(seconds=timeout_secs)
        return as_dict(actor.call(**kwargs))

    def run(self, actor_id: str, run_input: dict, timeout_secs: int = 1800) -> dict:
        run = self._call_actor(actor_id, run_input, timeout_secs)
        if not run:
            raise RuntimeError(f"actor {actor_id} returned no run")
        items = [as_dict(i) for i in self.client.dataset(run["defaultDatasetId"]).iterate_items()]
        usage = run.get("usageTotalUsd")
        return {"run_id": run.get("id", ""), "items": items, "usage_usd": float(usage) if usage is not None else None, "status": run.get("status", "")}

    def dataset_items(self, run_id: str) -> list[dict]:
        run = as_dict(self.client.run(run_id).get())
        if not run:
            return []
        return [as_dict(i) for i in self.client.dataset(run["defaultDatasetId"]).iterate_items()]

    def actor_input_schema(self, actor_id: str) -> dict:
        actor = as_dict(self.client.actor(actor_id).get())
        build_id = ((actor.get("taggedBuilds") or {}).get("latest") or {}).get("buildId")
        if not build_id:
            return {}
        build = as_dict(self.client.build(build_id).get())
        schema = build.get("inputSchema")
        if isinstance(schema, str):
            try:
                return json.loads(schema)
            except json.JSONDecodeError:
                return {}
        return schema or {}


# ----------------------------------------------------------------------------- tolerant parsing


def _first(item: dict, *keys: str, default: Any = None) -> Any:
    for k in keys:
        if "." in k:
            cur: Any = item
            ok = True
            for part in k.split("."):
                if isinstance(cur, dict) and part in cur:
                    cur = cur[part]
                else:
                    ok = False
                    break
            if ok and cur not in (None, ""):
                return cur
        elif item.get(k) not in (None, ""):
            return item[k]
    return default


def parse_duration(value: Any) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return int(value)
    s = str(value).strip()
    if s.isdigit():
        return int(s)
    m = re.match(r"^PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?$", s)
    if m:
        h, mi, se = (int(x) if x else 0 for x in m.groups())
        return h * 3600 + mi * 60 + se
    parts = s.split(":")
    if all(p.strip().isdigit() for p in parts) and 1 < len(parts) <= 3:
        nums = [int(p) for p in parts]
        while len(nums) < 3:
            nums.insert(0, 0)
        return nums[0] * 3600 + nums[1] * 60 + nums[2]
    return None


def parse_video_item(item: dict) -> dict:
    """Normalize one metadata-actor item to a flat dict."""
    url = _first(item, "url", "videoUrl", "link", default="")
    vid = _first(item, "id", "videoId", "video_id", default="") or (youtube_video_id(url) if url else "")
    if vid and not url:
        url = f"https://www.youtube.com/watch?v={vid}"
    date_raw = _first(item, "date", "publishedAt", "uploadDate", "publishDate", "published_at", "datePublished", "publishedTimeText", "publishedTime", default="")
    iso, precision = parse_date(str(date_raw)) if date_raw else ("", "unknown")
    channel_name = _first(item, "channelName", "channel.name", "channelTitle", "author", "author.name", "channel", "uploader", default="")
    if isinstance(channel_name, dict):
        channel_name = channel_name.get("name", "")
    channel_url = _first(item, "channelUrl", "channel.url", "channelLink", "author.url", "uploader_url", default="")
    return {
        "video_id": vid,
        "url": url,
        "title": _first(item, "title", "name", default="") or "",
        "channel_name": str(channel_name or ""),
        "channel_url": str(channel_url or ""),
        "published_date": iso,
        "date_precision": precision,
        "date_raw": str(date_raw),
        "duration_sec": parse_duration(_first(item, "duration", "lengthSeconds", "duration_seconds", "durationSec", default=None)),
        "description": str(_first(item, "description", "text", default="") or "")[:5000],
        "view_count": _first(item, "viewCount", "views", default=None),
        "is_short": bool(_first(item, "isShort", default=False)) or str(_first(item, "type", default="")).lower() == "short" or "/shorts/" in str(url),
        "raw": item,
    }


def metadata_guess_from_video(v: dict) -> MetadataGuess:
    evidence = []
    if v.get("title"):
        evidence.append("title from YouTube")
    if v.get("channel_name"):
        evidence.append("author = YouTube channel name")
    if v.get("published_date"):
        evidence.append(f"date from YouTube ({v.get('date_raw')})")
    return MetadataGuess(
        title=v.get("title", ""),
        author_raw=v.get("channel_name", ""),
        published_date=v.get("published_date", ""),
        date_precision=v.get("date_precision", "unknown"),
        confidence=0.9 if v.get("title") and v.get("channel_name") else 0.5,
        evidence=evidence,
        channel_url=v.get("channel_url", ""),
        duration_sec=v.get("duration_sec"),
        description=v.get("description", ""),
    )


def parse_transcript_item(item: dict) -> dict:
    """Normalize one transcript-actor item: {video_id, url, segments (raw), text, language, kind, title, metadata}."""
    url = _first(item, "url", "videoUrl", "video_url", default="")
    vid = _first(item, "videoId", "id", "video_id", default="") or (youtube_video_id(url) if url else "")
    segments = _first(item, "transcript", "captions", "segments", "data", "subtitles", "items", default=None)
    text = ""
    if isinstance(segments, str):
        text, segments = segments, None
    if segments is None:
        text = text or str(_first(item, "text", "fullText", "plainText", "transcriptText", default="") or "")
    lang = str(_first(item, "language", "lang", "languageCode", default="") or "")
    auto = _first(item, "isAutoGenerated", "isGenerated", "auto_generated", "kind", default=None)
    kind = "auto" if (auto is True or str(auto).lower() in ("asr", "auto", "true")) else ("manual" if auto is False else "auto")
    return {"video_id": vid, "url": url, "segments": segments, "text": text, "language": lang, "kind": kind, "title": _first(item, "title", default=""), "raw": item}


# ----------------------------------------------------------------------------- client


class ApifyYouTube:
    def __init__(self, runner: ApifyRunner, settings: Settings, cache_dir: Path | None = None):
        self.runner = runner
        self.settings = settings
        self.cfg = settings.apify
        self.cache_dir = cache_dir or settings.cache_dir / "youtube"
        self.run_ids: list[str] = []
        self.cost_usd: float = 0.0

    # -- input building
    def _start_urls(self, kind: str, urls: list[str]) -> Any:
        tpl = (self.cfg.get("input_templates") or {}).get(kind) or {}
        fmt = tpl.get("start_urls_format", "strings")
        return [{"url": u} for u in urls] if fmt == "objects" else list(urls)

    def _input(self, kind: str, urls: list[str], **extra: Any) -> dict:
        tpl = (self.cfg.get("input_templates") or {}).get(kind) or {}
        data: dict[str, Any] = {tpl.get("start_urls_key", "startUrls"): self._start_urls(kind, urls)}
        data.update(tpl.get("extra") or {})
        data.update({k: v for k, v in extra.items() if v is not None})
        return data

    def _run(self, actor: str, run_input: dict) -> tuple[list[dict], float]:
        result = self.runner.run(actor, run_input, timeout_secs=int(self.cfg.get("timeout_secs", 1800)))
        if result.get("run_id"):
            self.run_ids.append(result["run_id"])
        usage = float(result.get("usage_usd") or 0.0)
        self.cost_usd += usage
        return result.get("items", []), usage

    @staticmethod
    def _share(usage: float, n: int) -> float:
        return round(usage / n, 6) if n else 0.0

    def cost_for(self, video_id: str) -> float | None:
        """Apify cost attributed to one video: its share of the metadata run(s) plus of the transcript run."""
        total, found = 0.0, False
        for kind in ("meta", "transcript"):
            cached = self._cached(kind, video_id)
            if cached and cached.get("cost_usd") is not None:
                total += float(cached["cost_usd"])
                found = True
        return round(total, 4) if found else None

    # -- cache
    def _cache_path(self, kind: str, key: str) -> Path:
        return self.cache_dir / kind / f"{key}.json"

    def _cached(self, kind: str, key: str) -> dict | None:
        p = self._cache_path(kind, key)
        if p.exists():
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                return None
        return None

    def _store(self, kind: str, key: str, data: dict) -> None:
        p = self._cache_path(kind, key)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data, ensure_ascii=False, indent=1, default=str), encoding="utf-8")

    # -- public
    def video_metadata(self, video_ids: list[str], use_cache: bool = True) -> dict[str, dict]:
        out: dict[str, dict] = {}
        missing = []
        for vid in video_ids:
            cached = self._cached("meta", vid) if use_cache else None
            if cached:
                out[vid] = cached
            else:
                missing.append(vid)
        if missing:
            tpl = (self.cfg.get("input_templates") or {}).get("metadata") or {}
            urls = [f"https://www.youtube.com/watch?v={v}" for v in missing]
            items, usage = self._run(self.cfg["metadata_actor"], self._input("metadata", urls, **{tpl.get("max_results_key", "maxResults"): len(urls)}))
            parsed = [parse_video_item(item) for item in items]
            parsed = [v for v in parsed if v["video_id"]]
            for v in parsed:
                v.pop("raw", None)
                v["cost_usd"] = self._share(usage, len(parsed))
                self._store("meta", v["video_id"], v)
                out[v["video_id"]] = v
        return out

    def channel_videos(self, channel_url: str, max_results: int | None = None, use_cache: bool = True) -> list[dict]:
        key = re.sub(r"[^a-z0-9]+", "-", channel_url.lower()).strip("-")[:120]
        cached = self._cached("channel", key) if use_cache else None
        if cached and cached.get("videos"):
            return cached["videos"]
        tpl = (self.cfg.get("input_templates") or {}).get("metadata") or {}
        mr = max_results if max_results is not None else int(self.cfg.get("channel_max_results", 0) or 0)
        run_input = self._input("metadata", [channel_url], **({tpl.get("max_results_key", "maxResults"): mr} if mr else {}))
        items, usage = self._run(self.cfg["metadata_actor"], run_input)
        videos = []
        seen = set()
        for item in items:
            v = parse_video_item(item)
            if v["video_id"] and v["video_id"] not in seen:
                seen.add(v["video_id"])
                v.pop("raw", None)
                videos.append(v)
        for v in videos:
            v["cost_usd"] = self._share(usage, len(videos))
            self._store("meta", v["video_id"], v)
        self._store("channel", key, {"channel_url": channel_url, "videos": videos})
        return videos

    def transcripts(self, video_ids: list[str], use_cache: bool = True) -> dict[str, dict]:
        out: dict[str, dict] = {}
        missing = []
        for vid in video_ids:
            cached = self._cached("transcript", vid) if use_cache else None
            if cached:
                out[vid] = cached
            else:
                missing.append(vid)
        batch = int(self.cfg.get("transcript_batch_size", 25) or 25)
        for i in range(0, len(missing), batch):
            chunk = missing[i : i + batch]
            urls = [f"https://www.youtube.com/watch?v={v}" for v in chunk]
            items, usage = self._run(self.cfg["transcript_actor"], self._input("transcript", urls))
            parsed = []
            for item in items:
                t = parse_transcript_item(item)
                if not t["video_id"] and len(chunk) == 1:
                    t["video_id"] = chunk[0]
                if t["video_id"]:
                    parsed.append(t)
            for t in parsed:
                t["cost_usd"] = self._share(usage, len(parsed))
                self._store("transcript", t["video_id"], t)
                out[t["video_id"]] = t
        return out

    def estimate_cost(self, n_videos: int, with_transcripts: bool = True) -> float:
        per_meta = float(self.cfg.get("est_usd_per_video_metadata", 0.0005))
        per_tr = float(self.cfg.get("est_usd_per_transcript", 0.0007))
        return round(n_videos * (per_meta + (per_tr if with_transcripts else 0.0)), 4)

    def fetch_and_store_schemas(self) -> dict:
        """Verify the actors' input field names against their live input schema; rewrite config if needed."""
        report: dict[str, Any] = {}
        templates = self.settings.config.setdefault("apify", {}).setdefault("input_templates", {})
        changed = False
        for kind, actor_key in (("metadata", "metadata_actor"), ("transcript", "transcript_actor")):
            actor = self.cfg[actor_key]
            schema = self.runner.actor_input_schema(actor) or {}
            props = schema.get("properties") or {}
            tpl = templates.setdefault(kind, {})
            found = {}
            for name, spec in props.items():
                low = name.lower()
                if "starturl" in low or low in ("urls", "videourls", "videos"):
                    found["start_urls_key"] = name
                    items = spec.get("items") or {}
                    found["start_urls_format"] = "objects" if items.get("type") == "object" else "strings"
                if kind == "metadata" and ("maxresult" in low or low in ("maxitems", "limit", "maxvideos")):
                    found["max_results_key"] = name
            if any(tpl.get(k) != v for k, v in found.items()):
                changed = True
            tpl.update(found)
            report[kind] = {"actor": actor, "schema_found": bool(props), "fields": found, "required": schema.get("required", [])}
        # Only rewrite config/pipeline.yaml when an actor's input format actually changed (the dump drops comments).
        if changed:
            save_config(self.settings)
        report["config_updated"] = changed
        return report
