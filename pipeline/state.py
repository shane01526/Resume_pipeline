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
from pipeline.storage import Storage, build_storage


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


def _unique_run_id(runs_dir: Path, trigger: Trigger) -> str:
    """A run id that is not already taken.

    Second resolution is enough for a weekly schedule, but the cron trigger and a Slack
    `/resume update` can land in the same second — and reusing an id would mean the second
    run overwrites the first's artifacts and its audit record. A suffix is cheaper than
    switching to a format that sorts less readably.
    """
    base = new_run_id(trigger)
    if not (runs_dir / base).exists():
        return base
    for suffix in range(2, 100):
        candidate = f"{base}-{suffix}"
        if not (runs_dir / candidate).exists():
            return candidate
    # 99 runs in one second is not a collision, it's a runaway loop.
    raise RuntimeError(f"could not allocate a run id from {base}")


class RunStore:
    """Run state, durable via `Storage`; large artifacts staged on local disk.

    The split matters on Cloud Run, where the disk is ephemeral and consecutive requests
    can land on different instances:

    - **Durable** (through `Storage`): run records, diffs, the approved snapshot, the
      sources index. Small JSON, read by whichever instance serves the next request.
    - **Scratch** (local disk): rendered PDFs and page images during a run. They only need
      to outlive the render, and `publish.py` promotes them to `output/` on approval.

    With the local backend both live under the same tree, which is why `state/` is a
    committed directory there.
    """

    def __init__(self, settings: Settings, storage: Storage | None = None) -> None:
        self._settings = settings
        self._storage = storage if storage is not None else build_storage(settings)

    # --- scratch paths (local disk, per-instance) ---------------------------
    def run_dir(self, run_id: str) -> Path:
        return self._settings.runs_dir / run_id

    def artifacts_dir(self, run_id: str) -> Path:
        return self.run_dir(run_id) / "artifacts"

    def pages_dir(self, run_id: str) -> Path:
        return self.run_dir(run_id) / "pages"

    # --- durable keys ------------------------------------------------------
    def _run_key(self, run_id: str) -> str:
        return f"state/runs/{run_id}/run.json"

    def _diff_key(self, run_id: str) -> str:
        return f"state/runs/{run_id}/diff.json"

    # --- run lifecycle -----------------------------------------------------
    def create(self, trigger: Trigger) -> Run:
        now = datetime.now(UTC)
        run_id = _unique_run_id(self._settings.runs_dir, trigger)
        run = Run(id=run_id, trigger=trigger, created_at=now, updated_at=now)
        self.artifacts_dir(run.id).mkdir(parents=True, exist_ok=True)
        self.pages_dir(run.id).mkdir(parents=True, exist_ok=True)
        self.save(run)
        return run

    def save(self, run: Run) -> Run:
        run.touch()
        self._storage.write_text(
            self._run_key(run.id),
            _to_json(run.model_dump(mode="json")),
            f"Run {run.id}: {run.status.value}",
        )
        return run

    def load(self, run_id: str) -> Run | None:
        raw = self._storage.read_text(self._run_key(run_id))
        return Run.model_validate_json(raw) if raw else None

    def save_diff(self, run_id: str, diff: dict[str, Any]) -> None:
        self._storage.write_text(self._diff_key(run_id), _to_json(diff), f"Run {run_id}: diff")

    def load_diff(self, run_id: str) -> dict[str, Any] | None:
        raw = self._storage.read_text(self._diff_key(run_id))
        return json.loads(raw) if raw else None

    def list_runs(self, *, limit: int = 50) -> list[Run]:
        # Run ids are timestamp-prefixed, so reverse-sorted names are newest first.
        entries = sorted(self._storage.list_prefix("state/runs"), reverse=True)
        runs = []
        for entry in entries:
            run_id = entry.rstrip("/").rsplit("/", 1)[-1]
            if run_id in (".gitkeep", "runs"):
                continue
            if run := self.load(run_id):
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
        """Drop a run's artifacts, keeping its record as the audit trail.

        Rejected and expired runs shouldn't leave rendered PDFs behind — they'd bloat the
        repo and could be mistaken for published output.
        """
        for directory in (self.artifacts_dir(run_id), self.pages_dir(run_id)):
            if directory.is_dir():
                shutil.rmtree(directory)
        # Durable copies too, where a previous version of this run committed them.
        self._storage.delete_prefix(
            f"state/runs/{run_id}/artifacts", f"Run {run_id}: discard artifacts"
        )
        self._storage.delete_prefix(
            f"state/runs/{run_id}/pages", f"Run {run_id}: discard page images"
        )

    # --- diff baseline -----------------------------------------------------
    def load_approved_snapshot(self) -> dict[str, Any] | None:
        """Last approved resume.json. `None` on the very first run."""
        raw = self._storage.read_text("state/approved.json")
        return json.loads(raw) if raw else None

    def save_approved_snapshot(self, resume: dict[str, Any]) -> None:
        self._storage.write_text(
            "state/approved.json", _to_json(resume), "Update the approved resume snapshot"
        )

    # --- ingest bookkeeping ------------------------------------------------
    def load_sources_index(self) -> dict[str, str]:
        """Map of source path → SHA256 of the version already extracted."""
        raw = self._storage.read_text("state/sources.json")
        return json.loads(raw) if raw else {}

    def save_sources_index(self, index: dict[str, str]) -> None:
        self._storage.write_text(
            "state/sources.json", _to_json(index), "Record processed source files"
        )


def _to_json(payload: Any) -> str:
    """Canonical JSON for storage.

    `sort_keys` keeps git diffs readable: re-serializing unchanged content produces no
    diff, which matters because every write here is a commit.
    """
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
