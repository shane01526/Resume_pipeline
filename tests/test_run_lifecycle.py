"""Run lifecycle: state transitions, expiry, approval signing, and idempotency.

These are the behaviours that decide whether a stale run can still publish, whether a
double-tap publishes twice, and whether a forgotten run disappears silently — all of which
matter more than any single stage working.
"""

from __future__ import annotations

import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from pipeline.config import Settings
from pipeline.state import DiffCounts, RunStatus, RunStore, Trigger, new_run_id
from pipeline.storage import Storage


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


async def test_execute_run_uses_the_store_it_is_given(
    store: RunStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A store whose backing data is not yet readable must still work if it is passed in.

    Simulates GitHub's read-after-write lag, which is what actually happened: the run was
    committed, `load()` through a *fresh* store returned None, and every run died at
    "execute_run called for unknown run". The regression is invisible with LocalStorage —
    a filesystem read-after-write always succeeds — so the lag is injected here.
    """
    from pipeline import runner as runner_module

    run = store.create(Trigger.MANUAL)

    fresh_stores: list[RunStore] = []
    real_init = RunStore.__init__

    def tracking_init(self: RunStore, *args: object, **kwargs: object) -> None:
        real_init(self, *args, **kwargs)  # type: ignore[arg-type]
        fresh_stores.append(self)

    executed: list[str] = []

    async def fake_execute_body(run_arg, store_arg, settings_arg) -> None:  # noqa: ANN001
        executed.append(run_arg.id)

    monkeypatch.setattr(RunStore, "__init__", tracking_init)
    monkeypatch.setattr(runner_module, "_execute", fake_execute_body)

    await runner_module.execute_run(run.id, store)

    assert executed == [run.id], "the run was not executed from the store it was handed"
    # No new store may be constructed: constructing one is what triggers the stale read.
    assert fresh_stores == [], f"execute_run built {len(fresh_stores)} extra store(s)"


# --- surviving an instance that no longer has the files -----------------------
#
# These use _DictStorage rather than the default LocalStorage. That is the whole point:
# with LocalStorage the durable copy and the scratch copy are the *same file*, so a test
# can delete the scratch copy and still "find" the durable one — which is precisely why the
# production bug was invisible to a green suite. An earlier version of these tests passed
# with the persistence step removed for exactly that reason.


@pytest.fixture
def split_store(tmp_path: Path) -> RunStore:
    """A store whose durable layer is separate from its local scratch disk, like Cloud Run."""
    (tmp_path / "state").mkdir()
    settings = Settings(repo_root=tmp_path, approval_timeout_hours=72.0)
    return RunStore(settings, storage=_DictStorage({}))


# Not a real PNG — nothing decodes it here, only the byte-for-byte round trip matters.
PNG_BYTES = b"fake-png-bytes-for-round-trip"


def _render_fake_output(store: RunStore, run_id: str) -> None:
    page = store.pages_dir(run_id) / "en" / "resume-1.png"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_bytes(PNG_BYTES)
    for name in ("resume.json", "en/resume.pdf", "zh/resume.pdf"):
        path = store.artifacts_dir(run_id) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"{}" if name.endswith(".json") else b"%PDF fake")


def _wipe_local_disk(store: RunStore, run_id: str) -> RunStore:
    """Drop the scratch copies, keeping the durable layer — an instance being recycled."""
    shutil.rmtree(store.run_dir(run_id))
    return RunStore(store._settings, storage=store._storage)  # noqa: SLF001


def test_page_images_survive_a_recycled_instance(split_store: RunStore) -> None:
    """The measured failure: a run sat Pending Approval while every page image 404'd,
    because the PNGs existed only on the disk of the instance that rendered them."""
    from pipeline.runner import _persist_rendered_files

    run = split_store.create(Trigger.SCHEDULED)
    _render_fake_output(split_store, run.id)
    _persist_rendered_files(split_store, run.id)

    fresh = _wipe_local_disk(split_store, run.id)

    assert fresh.load_run_file(run.id, "pages/en/resume-1.png") == PNG_BYTES


def test_publish_restores_artifacts_from_storage(split_store: RunStore) -> None:
    """Approval is a separate request, so publish cannot depend on local disk.

    Before this, approving after a recycle failed with "no artifacts directory — it may have
    been discarded already", which blames rejection rather than ephemeral disk.
    """
    from pipeline.runner import _persist_rendered_files

    run = split_store.create(Trigger.SCHEDULED)
    _render_fake_output(split_store, run.id)
    _persist_rendered_files(split_store, run.id)

    fresh = _wipe_local_disk(split_store, run.id)
    assert fresh.materialize_artifacts(run.id) is True

    restored = fresh.artifacts_dir(run.id)
    assert {p.relative_to(restored).as_posix() for p in restored.rglob("*") if p.is_file()} == {
        "resume.json",
        "en/resume.pdf",
        "zh/resume.pdf",
    }


def test_materialize_reports_when_nothing_was_ever_rendered(split_store: RunStore) -> None:
    """So the caller can tell "not on this instance" from "never existed"."""
    run = split_store.create(Trigger.SCHEDULED)
    fresh = _wipe_local_disk(split_store, run.id)

    assert fresh.materialize_artifacts(run.id) is False


def test_run_json_is_not_duplicated_by_the_file_sweep(split_store: RunStore) -> None:
    """run.json and diff.json already go through the store with their own commit messages;
    sweeping them again would double every run's commits."""
    from pipeline.runner import _persist_rendered_files

    run = split_store.create(Trigger.SCHEDULED)
    _render_fake_output(split_store, run.id)
    _persist_rendered_files(split_store, run.id)

    keys = split_store._storage.walk(f"state/runs/{run.id}")  # noqa: SLF001
    swept = [k for k in keys if k.endswith(("/run.json", "/diff.json"))]

    assert swept == [f"state/runs/{run.id}/run.json"], (
        "expected only the store's own run.json write, not a second copy from the sweep"
    )


class _DictStorage(Storage):
    """Durable-only storage with no filesystem behind it."""

    def __init__(self, data: dict[str, bytes]) -> None:
        self._data = dict(data)

    def read(self, path: str) -> bytes | None:
        return self._data.get(path)

    def write(self, path: str, data: bytes, message: str) -> None:  # noqa: ARG002
        self._data[path] = data

    def delete_prefix(self, prefix: str, message: str) -> int:  # noqa: ARG002
        keys = [k for k in self._data if k.startswith(prefix)]
        for key in keys:
            del self._data[key]
        return len(keys)

    def list_prefix(self, prefix: str) -> list[str]:
        return sorted({k for k in self._data if k.startswith(prefix)})

    def walk(self, prefix: str) -> list[str]:
        return sorted(k for k in self._data if k.startswith(prefix))


# --- no-change notification: manual answers, scheduled stays quiet -------------


async def _run_with_no_changes(store: RunStore, trigger: Trigger, monkeypatch) -> list[str]:
    """Drive a run whose Notion content matches the approved snapshot. Returns Slack texts.

    Everything before the diff is stubbed: this asserts the notification decision, not the
    pipeline. The approved snapshot is set to exactly what stage 4 returns, which is how a
    real no-change run arises.
    """
    import json

    from pipeline import runner as runner_module
    from pipeline.models import Resume

    fixture = Path(__file__).parent / "fixtures" / "resume.sample.json"
    resume = Resume.model_validate_json(fixture.read_text(encoding="utf-8"))
    store.save_approved_snapshot(json.loads(resume.model_dump_json(by_alias=True)))

    posted: list[str] = []

    async def fake_post(_settings, text, blocks=None):  # noqa: ANN001, ANN202, ARG001
        posted.append(text)

    class _FakeReader:
        async def __aenter__(self):  # noqa: ANN202
            return self

        async def __aexit__(self, *_exc: object) -> None:
            return None

        async def read_resume(self):  # noqa: ANN202
            return resume

    async def fake_translate(res, _reader, _settings):  # noqa: ANN001, ANN202
        return res

    monkeypatch.setattr("web.slack.post_message", fake_post)
    monkeypatch.setattr("pipeline.notion_client.NotionReader", lambda _s: _FakeReader())
    monkeypatch.setattr("pipeline.translate.translate_resume", fake_translate)
    # Stages 1-3 need Notion and an LLM; irrelevant to the notification decision.
    monkeypatch.setattr("pipeline.ingest.ingest_sources", lambda *_a, **_k: [])

    run = store.create(trigger)
    await runner_module.execute_run(run.id, store)

    reloaded = store.load(run.id)
    assert reloaded is not None
    assert reloaded.status is RunStatus.NO_CHANGE, f"expected No Change, got {reloaded.status}"
    return posted


async def test_manual_no_change_run_reports_back(
    store: RunStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`/resume update` with nothing to change must say so.

    Silence in answer to a direct request is indistinguishable from the command being
    broken — which is exactly how it was reported.
    """
    posted = await _run_with_no_changes(store, Trigger.MANUAL, monkeypatch)

    assert len(posted) == 1, f"expected one message, got {posted}"
    assert "沒有需要更新" in posted[0]


async def test_scheduled_no_change_run_stays_silent(
    store: RunStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The original invariant, which must survive the change above: a weekly "nothing
    changed" ping trains you to ignore the channel."""
    posted = await _run_with_no_changes(store, Trigger.SCHEDULED, monkeypatch)

    assert posted == [], f"a scheduled no-change run must not notify, but sent {posted}"


def test_bad_signature_is_rejected_before_the_run_is_looked_up(
    store: RunStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Auth first, then work.

    With the lookup first, an unauthenticated caller could distinguish "this run exists"
    (403) from "it does not" (404) by status code alone, and every unsigned request cost a
    storage read — a GitHub API call on the deployed service.
    """
    from web import routes_runs

    loads: list[str] = []
    original = RunStore.load

    def counting_load(self: RunStore, run_id: str):  # noqa: ANN202
        loads.append(run_id)
        return original(self, run_id)

    monkeypatch.setattr(RunStore, "load", counting_load)

    run = store.create(Trigger.MANUAL)
    run.status = RunStatus.PENDING_APPROVAL
    store.save(run)
    loads.clear()

    import asyncio

    from fastapi import BackgroundTasks, HTTPException

    monkeypatch.setattr(routes_runs, "get_settings", lambda: store._settings)  # noqa: SLF001
    with pytest.raises(HTTPException) as caught:
        asyncio.run(routes_runs._decide(run.id, "approve", "0" * 32, BackgroundTasks()))  # noqa: SLF001

    assert caught.value.status_code == 403
    assert loads == [], "the run was loaded despite an invalid signature"
