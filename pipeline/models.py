"""Typed rows of the control-panel tabs and the knowledge-unit ("card") model.

Every tab row model derives from TabRow. Column order in the sheet is the field order here;
new columns must be appended at the END of a model so existing sheets keep working.
All values are stored as text in the sheet; `to_row` / `from_row` convert.
"""

from __future__ import annotations

from typing import Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

LIST_SEP = "|"


def _to_cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "Y" if value else "N"
    if isinstance(value, list):
        return LIST_SEP.join(str(v) for v in value)
    return str(value)


class TabRow(BaseModel):
    model_config = ConfigDict(extra="ignore", validate_assignment=True)
    key_field: ClassVar[str] = ""

    @classmethod
    def headers(cls) -> list[str]:
        return list(cls.model_fields)

    def key(self) -> str:
        return getattr(self, self.key_field)

    def to_row(self) -> dict[str, str]:
        return {name: _to_cell(getattr(self, name)) for name in type(self).model_fields}

    @classmethod
    def from_row(cls, row: dict[str, str]):
        data: dict[str, Any] = {}
        for name, info in cls.model_fields.items():
            raw = row.get(name, "")
            if raw is None:
                raw = ""
            annotation = str(info.annotation)
            if "list[" in annotation:
                data[name] = [p for p in str(raw).split(LIST_SEP) if p != ""] if raw != "" else []
            elif "bool" in annotation:
                data[name] = str(raw).strip().upper() in {"Y", "YES", "TRUE", "1"}
            elif "int" in annotation:
                data[name] = int(float(raw)) if str(raw).strip() not in {"", "None"} else None
            elif "float" in annotation:
                data[name] = float(raw) if str(raw).strip() not in {"", "None"} else None
            else:
                data[name] = raw if raw != "" else info.default if info.default is not None else ""
        return cls(**data)


# --------------------------------------------------------------------------- tabs


class Account(TabRow):
    key_field: ClassVar[str] = "account_id"
    account_id: str
    email: str = ""
    role: Literal["raw", "summary"] = "raw"
    backend: Literal["gdrive", "b2"] = "gdrive"
    token_env_var: str = ""
    root_folder_id: str = ""
    inbox_folder_id: str = ""
    summaries_folder_id: str = ""
    quota_bytes: int | None = None
    used_bytes: int | None = None
    quota_checked_at: str = ""
    status: Literal["active", "full", "disabled"] = "active"
    priority: int | None = 1
    notes: str = ""
    # auto: decided from the shape of the setting's value (JSON key -> service account, else refresh token)
    auth_kind: Literal["auto", "service_account", "oauth"] = "auto"
    # Google Workspace Shared Drive id when files live in a shared drive (service-account mode)
    drive_id: str = ""

    @property
    def free_bytes(self) -> int | None:
        if self.quota_bytes is None or self.used_bytes is None:
            return None
        return max(self.quota_bytes - self.used_bytes, 0)


class TaxonomyNode(TabRow):
    key_field: ClassVar[str] = "node_id"
    node_id: str
    level: Literal["domain", "primer", "stage"]
    name: str
    parent_id: str = ""
    path: str = ""
    slug: str = ""
    order: int | None = None
    status: Literal["active", "archived"] = "active"
    description: str = ""
    previous_names: list[str] = Field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""


class FolderMap(TabRow):
    key_field: ClassVar[str] = "folder_id"
    node_id: str
    account_id: str
    folder_id: str
    folder_url: str = ""
    created_at: str = ""


ResourceStatus = Literal[
    "registered", "folder_ready", "uploaded", "ingested", "needs_transcription", "summarized", "failed"
]
SourceType = Literal["youtube", "pdf", "docx", "xlsx", "csv", "txt", "image", "video", "audio", "web"]
DatePrecision = Literal["day", "month", "year", "unknown"]


class Resource(TabRow):
    key_field: ClassVar[str] = "resource_id"
    resource_id: str
    status: ResourceStatus = "registered"
    title: str = ""
    author_id: str = ""
    author_raw: str = ""
    author_override: bool = False
    published_date: str = ""
    date_precision: DatePrecision = "unknown"
    source_type: SourceType = "txt"
    source_url: str = ""
    natural_key: str = ""
    stage_id: str = ""
    stage_path: str = ""
    account_id: str = ""
    folder_id: str = ""
    raw_file_id: str = ""
    raw_file_url: str = ""
    raw_filename: str = ""
    text_file_id: str = ""
    data_file_ids: list[str] = Field(default_factory=list)
    extracted_chars: int | None = None
    language: str = ""
    transcript_kind: str = ""
    pages: int | None = None
    duration_sec: int | None = None
    ingested_at: str = ""
    updated_at: str = ""
    summarized_at: str = ""
    error: str = ""
    notes: str = ""
    # --- storage and provenance details (appended columns)
    drive_path: str = ""          # "AI Primer Raw / Body / Olympic Spartan / Boxing"
    folder_url: str = ""          # link to the stage folder holding the files
    text_file_url: str = ""       # link to the text version
    file_size_bytes: int | None = None
    stored_at: str = ""           # when the original landed in Drive
    resource_kind: str = ""       # plain-language kind: "YouTube video", "book (PDF)", ...
    extraction_method: str = ""   # apify transcript | pdf text | ocr | vision | word | spreadsheet | text
    apify_cost_usd: float | None = None
    warnings: list[str] = Field(default_factory=list)


class Author(TabRow):
    key_field: ClassVar[str] = "author_id"
    author_id: str
    canonical_name: str
    aliases: list[str] = Field(default_factory=list)
    type: Literal["person", "channel", "org"] = "person"
    channel_url: str = ""
    summary_doc_id: str = ""
    summary_doc_url: str = ""
    resource_count: int | None = 0
    unit_count: int | None = 0
    last_summarized_at: str = ""
    kb_path: str = ""
    notes: str = ""
    # --- summary location details (appended columns)
    summary_doc_created_at: str = ""
    summary_folder_url: str = ""
    summary_version: int | None = None
    kb_url: str = ""                    # link to the cards on GitHub
    part_doc_urls: list[str] = Field(default_factory=list)


class SummaryRun(TabRow):
    key_field: ClassVar[str] = "summary_id"
    summary_id: str
    author_id: str
    version: int | None = None
    generated_at: str = ""
    resources_included: list[str] = Field(default_factory=list)
    resources_new: list[str] = Field(default_factory=list)
    units_total: int | None = None
    units_new: int | None = None
    units_updated: int | None = None
    units_evolved: int | None = None
    units_contradicted: int | None = None
    units_rejected: int | None = None
    review_verdict: str = ""
    doc_url: str = ""
    kb_commit: str = ""
    notes: str = ""
    resources_count: int | None = None
    part_doc_urls: list[str] = Field(default_factory=list)


class Job(TabRow):
    key_field: ClassVar[str] = "job_id"
    job_id: str
    timestamp: str = ""
    command: str = ""
    args: str = ""
    status: Literal["ok", "failed", "skipped"] = "ok"
    duration_sec: float | None = None
    resources_affected: list[str] = Field(default_factory=list)
    apify_run_ids: list[str] = Field(default_factory=list)
    apify_cost_usd: float | None = None
    error: str = ""
    session_ref: str = ""


TAB_MODELS: dict[str, type[TabRow]] = {
    "Accounts": Account,
    "Taxonomy": TaxonomyNode,
    "Folders": FolderMap,
    "Resources": Resource,
    "Authors": Author,
    "Summaries": SummaryRun,
    "Jobs": Job,
}

# --------------------------------------------------------------------------- knowledge units

UnitType = Literal[
    "principle", "concept", "technique", "drill", "exercise", "list", "script", "example",
    "glossary", "procedure", "claim", "mistake", "dataset",
]
UNIT_TYPES: tuple[str, ...] = (
    "principle", "concept", "technique", "drill", "exercise", "list", "script", "example",
    "glossary", "procedure", "claim", "mistake", "dataset",
)


class Citation(BaseModel):
    model_config = ConfigDict(extra="ignore")
    resource_id: str
    location: str = ""
    quote: str = ""
    verified: bool = False
    score: int | None = None


class UnitVersion(BaseModel):
    model_config = ConfigDict(extra="ignore")
    date: str
    resource_id: str
    summary: str = ""
    what_changed: str = ""


class Contradiction(BaseModel):
    model_config = ConfigDict(extra="ignore")
    resource_id: str
    claim: str
    conflicts_with: str = ""


class Unit(BaseModel):
    """A knowledge card. Front matter fields plus three markdown body sections."""

    model_config = ConfigDict(extra="ignore")
    id: str
    author_id: str
    type: UnitType
    name: str
    aliases: list[str] = Field(default_factory=list)
    stages: list[str] = Field(default_factory=list)
    status: Literal["active", "contradicted", "superseded", "needs_review"] = "active"
    first_seen: str = ""
    last_seen: str = ""
    citations: list[Citation] = Field(default_factory=list)
    versions: list[UnitVersion] = Field(default_factory=list)
    contradictions: list[Contradiction] = Field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""
    description: str = ""
    details: str = ""
    notes: str = ""
    earlier_versions: str = ""

    @field_validator("aliases", "stages", mode="before")
    @classmethod
    def _none_to_list(cls, v):
        return v or []


# --------------------------------------------------------------------------- helper-agent contracts


class ExtractedCitation(BaseModel):
    model_config = ConfigDict(extra="ignore")
    location: str = ""
    quote: str


class ExtractedUnit(BaseModel):
    """What an extraction helper writes for one unit found in one chunk."""

    model_config = ConfigDict(extra="ignore")
    type: UnitType
    name: str
    aliases: list[str] = Field(default_factory=list)
    description: str
    details: str = ""
    notes: str = ""
    citations: list[ExtractedCitation] = Field(min_length=1)
    confidence: float | None = None
    stage_hint: str = ""

    @field_validator("aliases", mode="before")
    @classmethod
    def _none_to_list(cls, v):
        return v or []


class ExtractionOutput(BaseModel):
    model_config = ConfigDict(extra="ignore")
    resource_id: str
    chunk_id: str = ""
    units: list[ExtractedUnit] = Field(default_factory=list)


class VerifiedUnit(ExtractedUnit):
    temp_id: str
    verified_citations: list[Citation] = Field(default_factory=list)


class Candidate(BaseModel):
    model_config = ConfigDict(extra="ignore")
    unit_id: str
    name: str
    score: int
    type: str = ""
    description: str = ""
    details: str = ""
    last_seen: str = ""


class MatchItem(BaseModel):
    model_config = ConfigDict(extra="ignore")
    temp_id: str
    unit: VerifiedUnit
    candidates: list[Candidate] = Field(default_factory=list)
    auto_decision: Literal["NEW", "SAME", ""] = ""
    hint: str = ""


DecisionKind = Literal["NEW", "SAME", "EVOLVED", "CONTRADICTS"]


class Decision(BaseModel):
    model_config = ConfigDict(extra="ignore")
    temp_id: str
    decision: DecisionKind
    target_unit_id: str = ""
    rationale: str = ""
    what_changed: str = ""
    merged_details: str = ""
    merged_description: str = ""
    conflicting_claim: str = ""


class DecisionsFile(BaseModel):
    model_config = ConfigDict(extra="ignore")
    resource_id: str
    decisions: list[Decision] = Field(default_factory=list)


class ReviewItem(BaseModel):
    model_config = ConfigDict(extra="ignore")
    unit_id: str
    verdict: Literal["accept", "reject", "needs_review"]
    reason: str = ""


class ReviewFile(BaseModel):
    model_config = ConfigDict(extra="ignore")
    author_id: str
    overall: Literal["pass", "pass_with_flags", "fail"] = "pass"
    items: list[ReviewItem] = Field(default_factory=list)


class MetadataGuess(BaseModel):
    model_config = ConfigDict(extra="ignore")
    title: str = ""
    author_raw: str = ""
    published_date: str = ""
    date_precision: DatePrecision = "unknown"
    confidence: float = 0.0
    evidence: list[str] = Field(default_factory=list)
    language: str = ""
    channel_url: str = ""
    duration_sec: int | None = None
    pages: int | None = None
    description: str = ""
