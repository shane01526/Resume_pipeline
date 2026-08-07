"""Run state, stored as files in the repo.

Why git and not a database: this system takes one write per week from one user, and
Render's free Postgres is deleted after 30 days while its web disks are ephemeral.
Committing `state/` gives us durable storage, an audit trail, and the guarantee that a
diff baseline and the artifacts it produced sit in the same commit.

Everything the rest of the pipeline needs goes through this module, so swapping in
Postgres later means reimplementing these functions and nothing else.
"""

from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field

from pipeline.config import Settings


class RunStatus(StrEnum):
    BUILDING = "Building"
    PENDING_APPROVAL = "Pending Approval"
    APPROVED = "Approved"
    REJECTED = "Rejected"
    FAILED = "Failed"
    EXPIRED = "Expired"
    NO_CHANGE = "No Change"

    @property
    def is_terminal(self) -> bool:
        return self is not RunStatus.BUILDING and self is not RunStatus.PENDING_APPROVAL


class Trigger(StrEnum):
    SCHEDULED = "Scheduled"
    MANUAL = "Manual"
    SOURCE_CHANGE = "Source-change"


class DiffCounts(BaseModel):
    model_config = ConfigDict(extra="forbid")

    added: int = 0
    modified: int = 0
    removed: int = 0

    @property
    def total(self) -> int:
        return self.added + self.modified + self.removed

    def summary(self) -> str:
        return f"+{self.added} added / ~{self.modified} modified / -{self.removed} removed"


class Run(BaseModel):
    """One pipeline execution. Serialized to `state/runs/<id>/run.json`."""

    model_config = ConfigDict(extra="forbid")

    id: str
    trigger: Trigger
    status: RunStatus = RunStatus.BUILDING
    created_at: datetime
    updated_at: datetime
    counts: DiffCounts = Field(default_factory=DiffCounts)
    notion_run_page_id: str | None = None
    commit_sha: str | None = None
    error: str | None = None
    artifacts: list[str] = Field(
        default_factory=list, description="Artifact paths relative to the run directory"
    )
    decided_by: str | None = Field(
        default=None, description="'slack' or 'web' — which surface approved or rejected"
    )

    def touch(self) -> Self:
        self.updated_at = datetime.now(UTC)
        return self

    def is_expired(self, timeout_hours: float, *, now: datetime | None = None) -> bool:
        if self.status is not RunStatus.PENDING_APPROVAL:
            return False
        now = now or datetime.now(UTC)
        return now - self.created_at > timedelta(hours=timeout_hours)

    def preview_url(self, base_url: str) -> str:
        return f"{base_url}/runs/{self.id}"


def new_run_id(trigger: Trigger, *, now: datetime | None = None) -> str:
    """Timestamp-based, so run directories sort chronologically in `git log` and on disk."""
    now = now or datetime.now(UTC)
    return f"{now:%Y%m%dT%H%M%SZ}-{trigger.value.lower().replace('-', '')}"


class RunStore:
    """File-backed run storage under `state/`."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    # --- paths -------------------------------------------------------------
    def run_dir(self, run_id: str) -> Path:
        return self._settings.runs_dir / run_id

    def artifacts_dir(self, run_id: str) -> Path:
        return self.run_dir(run_id) / "artifacts"

    def pages_dir(self, run_id: str) -> Path:
        return self.run_dir(run_id) / "pages"

    # --- run lifecycle -----------------------------------------------------
    def create(self, trigger: Trigger) -> Run:
        now = datetime.now(UTC)
        run = Run(id=new_run_id(trigger, now=now), trigger=trigger, created_at=now, updated_at=now)
        self.artifacts_dir(run.id).mkdir(parents=True, exist_ok=True)
        self.pages_dir(run.id).mkdir(parents=True, exist_ok=True)
        self.save(run)
        return run

    def save(self, run: Run) -> Run:
        run.touch()
        path = self.run_dir(run.id) / "run.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        _write_json_atomic(path, run.model_dump(mode="json"))
        return run

    def load(self, run_id: str) -> Run | None:
        path = self.run_dir(run_id) / "run.json"
        if not path.is_file():
            return None
        return Run.model_validate_json(path.read_text(encoding="utf-8"))

    def list_runs(self, *, limit: int = 50) -> list[Run]:
        runs_dir = self._settings.runs_dir
        if not runs_dir.is_dir():
            return []
        runs = []
        # Directory names are timestamp-prefixed, so reverse-sorted names are newest first.
        for entry in sorted(runs_dir.iterdir(), reverse=True):
            if not entry.is_dir():
                continue
            if run := self.load(entry.name):
                runs.append(run)
            if len(runs) >= limit:
                break
        return runs

    def pending(self) -> list[Run]:
        return [r for r in self.list_runs() if r.status is RunStatus.PENDING_APPROVAL]

    def expire_stale(self) -> list[Run]:
        """Mark timed-out pending runs as Expired. Returns the runs that changed."""
        expired = []
        for run in self.pending():
            if run.is_expired(self._settings.approval_timeout_hours):
                run.status = RunStatus.EXPIRED
                self.save(run)
                expired.append(run)
        return expired

    def discard(self, run_id: str) -> None:
        """Delete a run's artifacts, keeping `run.json` as the audit record.

        Rejected and expired runs shouldn't leave rendered PDFs behind — they'd bloat the
        repo and could be mistaken for published output.
        """
        for directory in (self.artifacts_dir(run_id), self.pages_dir(run_id)):
            if directory.is_dir():
                shutil.rmtree(directory)

    # --- diff baseline -----------------------------------------------------
    def load_approved_snapshot(self) -> dict[str, Any] | None:
        """Last approved resume.json. `None` on the very first run."""
        path = self._settings.approved_snapshot
        if not path.is_file():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def save_approved_snapshot(self, resume: dict[str, Any]) -> Path:
        path = self._settings.approved_snapshot
        path.parent.mkdir(parents=True, exist_ok=True)
        _write_json_atomic(path, resume)
        return path

    # --- ingest bookkeeping ------------------------------------------------
    def load_sources_index(self) -> dict[str, str]:
        """Map of source path → SHA256 of the version already extracted."""
        path = self._settings.sources_index
        if not path.is_file():
            return {}
        return json.loads(path.read_text(encoding="utf-8"))

    def save_sources_index(self, index: dict[str, str]) -> Path:
        path = self._settings.sources_index
        path.parent.mkdir(parents=True, exist_ok=True)
        _write_json_atomic(path, index)
        return path


def _write_json_atomic(path: Path, payload: Any) -> None:
    """Write via a temp file + replace, so a crash can't leave truncated JSON behind.

    `sort_keys` keeps git diffs readable: a re-serialized file with unchanged content
    produces no diff.
    """
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    tmp.replace(path)
