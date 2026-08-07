"""Run lifecycle: state transitions, expiry, approval signing, and idempotency.

These are the behaviours that decide whether a stale run can still publish, whether a
double-tap publishes twice, and whether a forgotten run disappears silently — all of which
matter more than any single stage working.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from pipeline.config import Settings
from pipeline.state import DiffCounts, RunStatus, RunStore, Trigger, new_run_id


@pytest.fixture
def store(tmp_path: Path) -> RunStore:
    """A store rooted in a temp directory — never the real state/ (see conftest)."""
    (tmp_path / "state").mkdir()
    return RunStore(Settings(repo_root=tmp_path, approval_timeout_hours=72.0))


# --- ids ---------------------------------------------------------------------


def test_run_ids_sort_chronologically() -> None:
    """Timestamp-prefixed, so `git log` and a directory listing agree on order."""
    early = new_run_id(Trigger.SCHEDULED, now=datetime(2026, 8, 1, 3, 0, tzinfo=UTC))
    late = new_run_id(Trigger.MANUAL, now=datetime(2026, 8, 8, 3, 0, tzinfo=UTC))
    assert early < late


def test_run_id_records_the_trigger() -> None:
    assert new_run_id(Trigger.SOURCE_CHANGE).endswith("-sourcechange")


# --- persistence -------------------------------------------------------------


def test_run_roundtrips(store: RunStore) -> None:
    run = store.create(Trigger.MANUAL)
    run.counts = DiffCounts(added=2, modified=3, removed=1)
    run.status = RunStatus.PENDING_APPROVAL
    store.save(run)

    loaded = store.load(run.id)
    assert loaded is not None
    assert loaded.counts.total == 6
    assert loaded.status is RunStatus.PENDING_APPROVAL


def test_unknown_run_loads_as_none(store: RunStore) -> None:
    assert store.load("20260101T000000Z-manual") is None


def test_runs_listed_newest_first(store: RunStore) -> None:
    ids = [store.create(Trigger.MANUAL).id for _ in range(3)]
    assert [r.id for r in store.list_runs()] == sorted(ids, reverse=True)


def test_same_second_runs_get_distinct_ids(store: RunStore) -> None:
    """The cron trigger and a Slack /resume update can land in the same second.

    Reusing an id would make the second run overwrite the first's artifacts and its audit
    record — so create() suffixes rather than trusting second resolution.
    """
    ids = [store.create(Trigger.MANUAL).id for _ in range(3)]
    assert len(set(ids)) == 3
    for run_id in ids:
        assert store.load(run_id) is not None


# --- expiry ------------------------------------------------------------------


def test_fresh_pending_run_is_not_expired(store: RunStore) -> None:
    run = store.create(Trigger.SCHEDULED)
    run.status = RunStatus.PENDING_APPROVAL
    assert not run.is_expired(72.0)


def test_old_pending_run_expires(store: RunStore) -> None:
    """A run you never looked at must not stay approvable forever.

    Its rendered content reflects a Notion state from days ago; publishing it later would
    silently revert edits you made since.
    """
    run = store.create(Trigger.SCHEDULED)
    run.status = RunStatus.PENDING_APPROVAL
    run.created_at = datetime.now(UTC) - timedelta(hours=73)
    assert run.is_expired(72.0)


def test_only_pending_runs_expire(store: RunStore) -> None:
    """An approved run is history, not a pending decision."""
    run = store.create(Trigger.SCHEDULED)
    run.status = RunStatus.APPROVED
    run.created_at = datetime.now(UTC) - timedelta(days=30)
    assert not run.is_expired(72.0)


def test_expire_stale_updates_and_reports(store: RunStore) -> None:
    fresh = store.create(Trigger.MANUAL)
    fresh.status = RunStatus.PENDING_APPROVAL
    store.save(fresh)

    stale = store.create(Trigger.SCHEDULED)
    stale.status = RunStatus.PENDING_APPROVAL
    stale.created_at = datetime.now(UTC) - timedelta(hours=100)
    store.save(stale)

    expired = store.expire_stale()
    assert [r.id for r in expired] == [stale.id]
    assert store.load(stale.id).status is RunStatus.EXPIRED  # type: ignore[union-attr]
    assert store.load(fresh.id).status is RunStatus.PENDING_APPROVAL  # type: ignore[union-attr]


# --- terminal states ---------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "terminal"),
    [
        (RunStatus.BUILDING, False),
        (RunStatus.PENDING_APPROVAL, False),
        (RunStatus.APPROVED, True),
        (RunStatus.REJECTED, True),
        (RunStatus.FAILED, True),
        (RunStatus.EXPIRED, True),
        (RunStatus.NO_CHANGE, True),
    ],
)
def test_terminal_classification(status: RunStatus, terminal: bool) -> None:
    assert status.is_terminal is terminal


def test_discard_keeps_the_audit_record(store: RunStore) -> None:
    """Rejected artifacts must not linger — they'd bloat the repo and could be mistaken
    for published output — but the run record stays as history."""
    run = store.create(Trigger.MANUAL)
    (store.artifacts_dir(run.id) / "resume.pdf").write_bytes(b"%PDF-fake")
    (store.pages_dir(run.id) / "page-01.png").write_bytes(b"PNG-fake")

    store.discard(run.id)

    assert not store.artifacts_dir(run.id).exists()
    assert not store.pages_dir(run.id).exists()
    assert store.load(run.id) is not None


def test_discard_is_safe_to_repeat(store: RunStore) -> None:
    """Reachable when a rejection and an expiry race on the same run."""
    run = store.create(Trigger.MANUAL)
    store.discard(run.id)
    store.discard(run.id)


# --- diff baseline -----------------------------------------------------------


def test_no_baseline_on_first_run(store: RunStore) -> None:
    assert store.load_approved_snapshot() is None


def test_baseline_roundtrips(store: RunStore) -> None:
    payload = {"lang": "en", "profile": {"name": "WU, YU-HSUAN"}}
    store.save_approved_snapshot(payload)
    assert store.load_approved_snapshot() == payload


def test_snapshot_is_written_atomically(store: RunStore) -> None:
    """Via temp file + replace, so a crash can't leave truncated JSON as the baseline.

    A corrupt baseline would make the next diff meaningless.
    """
    store.save_approved_snapshot({"lang": "en"})
    assert not list(store._settings.state_dir.glob("*.tmp"))


def test_snapshot_json_is_stable(store: RunStore) -> None:
    """Sorted keys, so re-serializing unchanged content produces no git diff."""
    store.save_approved_snapshot({"b": 2, "a": 1})
    assert store._settings.approved_snapshot.read_text(encoding="utf-8").index('"a"') < (
        store._settings.approved_snapshot.read_text(encoding="utf-8").index('"b"')
    )


# --- approval signing --------------------------------------------------------


def test_signature_is_scoped_to_the_action(store: RunStore) -> None:
    """An approve token must not be replayable as a reject, or vice versa."""
    from web.routes_runs import sign, verify

    settings = store._settings
    run_id = "20260807T030000Z-scheduled"
    approve = sign(run_id, "approve", settings)
    reject = sign(run_id, "reject", settings)

    assert approve != reject
    assert verify(run_id, "approve", approve, settings)
    assert not verify(run_id, "reject", approve, settings)


def test_signature_is_scoped_to_the_run(store: RunStore) -> None:
    """Guessing a run ID must not be enough to publish."""
    from web.routes_runs import sign, verify

    settings = store._settings
    token = sign("run-a", "approve", settings)
    assert not verify("run-b", "approve", token, settings)


def test_tampered_token_is_rejected(store: RunStore) -> None:
    from web.routes_runs import sign, verify

    settings = store._settings
    token = sign("run-a", "approve", settings)
    flipped = ("0" if token[0] != "0" else "1") + token[1:]
    assert not verify("run-a", "approve", flipped, settings)


def test_approval_links_are_absolute(store: RunStore) -> None:
    """They're opened from Slack on a phone, so a relative path is useless."""
    from web.routes_runs import approval_links

    run = store.create(Trigger.MANUAL)
    links = approval_links(run, store._settings)
    for action in ("approve", "reject"):
        assert links[action].startswith("http")
        assert f"/api/runs/{run.id}/{action}?token=" in links[action]


# --- summary formatting ------------------------------------------------------


def test_counts_summary_reads_naturally() -> None:
    assert DiffCounts(added=2, modified=3, removed=1).summary() == (
        "+2 added / ~3 modified / -1 removed"
    )


def test_zero_diff_is_detectable() -> None:
    """The runner keys "skip the notification" off this."""
    assert DiffCounts().total == 0
