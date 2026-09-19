"""Typed access to the control-panel tabs (Accounts, Taxonomy, Folders, Resources, Authors, Summaries, Jobs)."""

from __future__ import annotations

import time
from typing import Any

from .config import Settings, redact_args
from .ids import new_job_id, now_iso
from .models import Account, Author, FolderMap, Job, Resource, SummaryRun, TaxonomyNode
from .sheets import SheetsRepo


class Registry:
    def __init__(self, repo: SheetsRepo, settings: Settings):
        self.repo = repo
        self.settings = settings

    def preload(self) -> None:
        self.repo.preload(list(self.repo.tab_models))

    # ---- accounts
    def accounts(self) -> list[Account]:
        return [a for a in self.repo.load("Accounts")]  # type: ignore[misc]

    def account(self, account_id: str) -> Account | None:
        return self.repo.get("Accounts", account_id)  # type: ignore[return-value]

    def raw_accounts(self, include_full: bool = False) -> list[Account]:
        rows = [a for a in self.accounts() if a.role == "raw" and a.status != "disabled"]
        if not include_full:
            rows = [a for a in rows if a.status == "active"]
        return sorted(rows, key=lambda a: (a.priority if a.priority is not None else 999, a.account_id))

    def summary_account(self) -> Account | None:
        rows = [a for a in self.accounts() if a.role == "summary" and a.status != "disabled"]
        return rows[0] if rows else None

    def upsert_account(self, account: Account) -> None:
        self.repo.upsert("Accounts", account)

    # ---- taxonomy
    def nodes(self) -> list[TaxonomyNode]:
        return self.repo.load("Taxonomy")  # type: ignore[return-value]

    def node(self, node_id: str) -> TaxonomyNode | None:
        return self.repo.get("Taxonomy", node_id)  # type: ignore[return-value]

    def upsert_nodes(self, nodes: list[TaxonomyNode]) -> None:
        self.repo.update("Taxonomy", nodes)

    def folders(self) -> list[FolderMap]:
        return self.repo.load("Folders")  # type: ignore[return-value]

    def folder_for(self, node_id: str, account_id: str) -> FolderMap | None:
        for f in self.folders():
            if f.node_id == node_id and f.account_id == account_id:
                return f
        return None

    def upsert_folder(self, folder: FolderMap) -> None:
        self.repo.upsert("Folders", folder)

    # ---- resources
    def resources(self) -> list[Resource]:
        return self.repo.load("Resources")  # type: ignore[return-value]

    def resource(self, resource_id: str) -> Resource | None:
        return self.repo.get("Resources", resource_id)  # type: ignore[return-value]

    def find_resource_by_natural_key(self, natural_key: str) -> Resource | None:
        for r in self.resources():
            if r.natural_key == natural_key:
                return r
        return None

    def find_resources(self, text: str) -> list[Resource]:
        needle = text.lower().strip()
        return [r for r in self.resources() if needle and (needle == r.resource_id.lower() or needle in r.title.lower())]

    def upsert_resource(self, resource: Resource) -> Resource:
        resource.updated_at = now_iso()
        self.repo.upsert("Resources", resource)
        return resource

    def resources_for_author(self, author_id: str) -> list[Resource]:
        return [r for r in self.resources() if r.author_id == author_id]

    # ---- authors
    def authors(self) -> list[Author]:
        return self.repo.load("Authors")  # type: ignore[return-value]

    def author(self, author_id: str) -> Author | None:
        return self.repo.get("Authors", author_id)  # type: ignore[return-value]

    def upsert_author(self, author: Author) -> None:
        self.repo.upsert("Authors", author)

    # ---- summaries / jobs
    def summaries(self) -> list[SummaryRun]:
        return self.repo.load("Summaries")  # type: ignore[return-value]

    def append_summary(self, run: SummaryRun) -> None:
        self.repo.append("Summaries", [run])

    def log_job(
        self,
        command: str,
        args: dict[str, Any] | list[str],
        status: str,
        started: float,
        resources: list[str] | None = None,
        apify_run_ids: list[str] | None = None,
        apify_cost_usd: float | None = None,
        error: str = "",
    ) -> Job:
        job = Job(
            job_id=new_job_id(),
            timestamp=now_iso(),
            command=command,
            args=redact_args(args)[:2000],
            status=status,  # type: ignore[arg-type]
            duration_sec=round(time.time() - started, 1),
            resources_affected=resources or [],
            apify_run_ids=apify_run_ids or [],
            apify_cost_usd=apify_cost_usd,
            error=error[:1000],
            session_ref=self.settings.session_ref,
        )
        try:
            self.repo.append("Jobs", [job])
        except Exception:
            pass  # logging must never break a command
        return job
