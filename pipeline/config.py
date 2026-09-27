"""Settings: config/pipeline.yaml merged with environment variables. Secrets are never logged."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

SECRET_ENV_PREFIXES = ("GOOGLE_REFRESH_TOKEN_", "GOOGLE_OAUTH_CLIENT_SECRET", "APIFY_TOKEN", "B2_APPLICATION_KEY", "NOTION_TOKEN")
GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/documents",
]


def find_repo_root(start: Path | None = None) -> Path:
    """Walk up from `start` (default: cwd) until a directory containing pyproject.toml is found."""
    here = (start or Path.cwd()).resolve()
    for candidate in (here, *here.parents):
        if (candidate / "pyproject.toml").exists() and (candidate / "pipeline").is_dir():
            return candidate
    return Path(__file__).resolve().parent.parent


@dataclass
class Settings:
    repo_root: Path
    config: dict[str, Any] = field(default_factory=dict)

    # ---- paths
    @property
    def cache_dir(self) -> Path:
        return Path(os.environ.get("AIPRIMER_CACHE_DIR") or self.repo_root / ".cache")

    @property
    def work_dir(self) -> Path:
        return Path(os.environ.get("AIPRIMER_WORK_DIR") or self.repo_root / ".work")

    @property
    def knowledge_dir(self) -> Path:
        return self.repo_root / "knowledge"

    @property
    def config_dir(self) -> Path:
        return self.repo_root / "config"

    # ---- environment
    @property
    def control_sheet_id(self) -> str:
        return os.environ.get("AIPRIMER_CONTROL_SHEET_ID", "").strip()

    @property
    def google_client_id(self) -> str:
        return os.environ.get("GOOGLE_OAUTH_CLIENT_ID", "").strip()

    @property
    def google_client_secret(self) -> str:
        return os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET", "").strip()

    @property
    def apify_token(self) -> str | None:
        return os.environ.get("APIFY_TOKEN") or None

    # ---- v2: Notion control panel + pages, Backblaze B2 storage
    @property
    def notion_parent_page_id(self) -> str:
        """Id of the "AI Primer" Notion page (32 hex chars at the end of its address). Set => Notion mode."""
        return os.environ.get("AIPRIMER_NOTION_PAGE_ID", "").strip().replace("-", "")

    @property
    def notion_token(self) -> str | None:
        """Only set when the token is given as a variable; normally the proxy attaches it (API credentials box)."""
        return os.environ.get("NOTION_TOKEN") or None

    @property
    def b2(self) -> dict[str, Any]:
        return self.section("b2")

    @property
    def notion(self) -> dict[str, Any]:
        return self.section("notion")

    @property
    def session_ref(self) -> str:
        sid = os.environ.get("CLAUDE_CODE_REMOTE_SESSION_ID", "")
        return sid.replace("cse_", "session_") if sid else ""

    def refresh_token(self, env_var: str) -> str:
        return os.environ.get(env_var, "").strip()

    # ---- config sections
    def section(self, name: str) -> dict[str, Any]:
        return dict(self.config.get(name) or {})

    @property
    def apify(self) -> dict[str, Any]:
        return self.section("apify")

    @property
    def drive(self) -> dict[str, Any]:
        return self.section("drive")

    @property
    def extract(self) -> dict[str, Any]:
        return self.section("extract")

    @property
    def kb(self) -> dict[str, Any]:
        return self.section("kb")

    @property
    def docs(self) -> dict[str, Any]:
        return self.section("docs")

    def env_report(self) -> dict[str, bool]:
        """Which settings are present (names only; values never returned)."""
        names = [
            "AIPRIMER_NOTION_PAGE_ID",
            "NOTION_TOKEN",
            *sorted(k for k in os.environ if k.startswith(("B2_KEY_ID_", "B2_APPLICATION_KEY_", "B2_BUCKET_", "B2_MEDIA_BUCKET_"))),
            "GOOGLE_SERVICE_ACCOUNT_JSON",
            "GOOGLE_SERVICE_ACCOUNT_JSON_RAW",
            "GOOGLE_SERVICE_ACCOUNT_JSON_SUMMARY",
            "AIPRIMER_RAW_DRIVE_ID",
            "AIPRIMER_SUMMARY_DRIVE_ID",
            "GOOGLE_OAUTH_CLIENT_ID",
            "GOOGLE_OAUTH_CLIENT_SECRET",
            "AIPRIMER_CONTROL_SHEET_ID",
            "APIFY_TOKEN",
        ]
        names += sorted(k for k in os.environ if k.startswith("GOOGLE_REFRESH_TOKEN_"))
        return {n: bool(os.environ.get(n)) for n in names}


def load_settings(repo_root: Path | None = None, config_path: Path | None = None) -> Settings:
    root = repo_root or find_repo_root()
    path = config_path or Path(os.environ.get("AIPRIMER_CONFIG") or root / "config" / "pipeline.yaml")
    config: dict[str, Any] = {}
    if path.exists():
        with open(path, encoding="utf-8") as fh:
            config = yaml.safe_load(fh) or {}
    return Settings(repo_root=root, config=config)


def _merge_into(target: Any, source: dict[str, Any]) -> None:
    """Update the round-trip mapping `target` in place from the plain dict `source`: changed values are replaced,
    new keys appended, keys missing from `source` removed; untouched lines keep their comments and quoting."""
    for key, value in source.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            _merge_into(target[key], value)
        else:
            target[key] = value
    for key in [k for k in target if k not in source]:
        del target[key]


def save_config(settings: Settings, config_path: Path | None = None) -> Path:
    """Write `settings.config` back to the YAML file, keeping the file's comments (ruamel.yaml round trip).
    Without ruamel.yaml, or for a new file, a plain dump is written."""
    path = config_path or settings.repo_root / "config" / "pipeline.yaml"
    try:
        from ruamel.yaml import YAML
    except ImportError:  # pragma: no cover - the requirements install it
        YAML = None  # type: ignore[assignment]
    if YAML is not None and path.exists():
        rt = YAML()
        rt.preserve_quotes = True
        rt.width = 4096
        with open(path, encoding="utf-8") as fh:
            doc = rt.load(fh)
        if isinstance(doc, dict):
            _merge_into(doc, settings.config)
            with open(path, "w", encoding="utf-8") as fh:
                rt.dump(doc, fh)
            return path
    with open(path, "w", encoding="utf-8") as fh:
        yaml.safe_dump(settings.config, fh, sort_keys=False, allow_unicode=True)
    return path


def redact_args(args: dict[str, Any] | list[str]) -> str:
    """Render CLI arguments for the Jobs log with any secret-looking value removed."""
    if isinstance(args, dict):
        parts = []
        for k, v in args.items():
            if any(s in k.upper() for s in ("TOKEN", "SECRET", "PASSWORD", "KEY")):
                v = "<redacted>"
            parts.append(f"{k}={v}")
        return " ".join(parts)
    out = []
    for a in args:
        if a.startswith("apify_api_") or len(a) > 120:
            out.append("<redacted>")
        else:
            out.append(a)
    return " ".join(out)
