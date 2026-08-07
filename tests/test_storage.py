"""Storage backends: both must satisfy the same contract.

The GitHub backend exists because Cloud Run has no persistent disk and can serve
consecutive requests from different instances. If it diverges from the local backend, the
symptom is a run that renders on one instance and 404s on the next — so the contract tests
run against both, and the GitHub-specific tests cover the API mechanics (sha handling,
large blobs, conflict retry) that have no local equivalent.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr

from pipeline.config import Settings
from pipeline.storage import (
    GitHubStorage,
    LocalStorage,
    Storage,
    StorageError,
    build_storage,
)

# --- the shared contract -----------------------------------------------------
# Parametrized so a change to one backend can't silently break the other.


class FakeGitHub:
    """In-memory stand-in for the Contents API, close enough to test sha handling.

    Models the parts that actually bite: a create must omit `sha`, an update must send the
    current one, and a stale sha is a 409.
    """

    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.shas: dict[str, str] = {}
        self.calls: list[tuple[str, str]] = []
        self._counter = 0

    def _next_sha(self) -> str:
        self._counter += 1
        return f"sha{self._counter:04d}"

    def handler(self, request: httpx.Request) -> httpx.Response:
        import base64
        import json as jsonlib

        path = request.url.path
        self.calls.append((request.method, path))

        if "/contents/" in path:
            key = path.split("/contents/", 1)[1]

            if request.method == "GET":
                # Directory listing when the key is a prefix of stored files.
                children = {
                    k[len(key) :].lstrip("/").split("/")[0]
                    for k in self.files
                    if k.startswith(key.rstrip("/") + "/")
                }
                if key in self.files:
                    return httpx.Response(
                        200,
                        json={
                            "sha": self.shas[key],
                            "content": base64.b64encode(self.files[key]).decode(),
                            "type": "file",
                        },
                    )
                if children:
                    return httpx.Response(
                        200,
                        json=[
                            {
                                "path": f"{key.rstrip('/')}/{c}",
                                "type": "file" if f"{key.rstrip('/')}/{c}" in self.files else "dir",
                            }
                            for c in sorted(children)
                        ],
                    )
                return httpx.Response(404, json={"message": "Not Found"})

            if request.method == "PUT":
                body = jsonlib.loads(request.content)
                existing = self.shas.get(key)
                sent = body.get("sha")
                # Reject a stale or missing sha on an update — the real API does.
                if existing and sent != existing:
                    return httpx.Response(409, json={"message": "sha mismatch"})
                self.files[key] = base64.b64decode(body["content"])
                self.shas[key] = self._next_sha()
                return httpx.Response(200, json={"content": {"sha": self.shas[key]}})

            if request.method == "DELETE":
                self.files.pop(key, None)
                self.shas.pop(key, None)
                return httpx.Response(200, json={})

        if "/git/blobs/" in path:
            sha = path.rsplit("/", 1)[1]
            for key, stored in self.shas.items():
                if stored == sha:
                    return httpx.Response(
                        200, json={"content": base64.b64encode(self.files[key]).decode()}
                    )
            return httpx.Response(404, json={})

        return httpx.Response(404, json={"message": "unhandled"})


def github_storage(fake: FakeGitHub) -> GitHubStorage:
    settings = Settings(github_token=SecretStr("test-token"), github_repo="owner/repo")
    storage = GitHubStorage(settings)
    storage._client = httpx.Client(
        base_url="https://api.github.com", transport=httpx.MockTransport(fake.handler)
    )
    return storage


@pytest.fixture(params=["local", "github"])
def storage(request: pytest.FixtureRequest, tmp_path: Path) -> Storage:
    if request.param == "local":
        return LocalStorage(tmp_path)
    return github_storage(FakeGitHub())


def test_read_missing_returns_none(storage: Storage) -> None:
    assert storage.read("state/runs/nope/run.json") is None
    assert storage.read_text("state/nope.json") is None


def test_write_then_read(storage: Storage) -> None:
    storage.write_text("state/runs/r1/run.json", '{"id": "r1"}', "create")
    assert storage.read_text("state/runs/r1/run.json") == '{"id": "r1"}'


def test_overwrite_replaces(storage: Storage) -> None:
    """An update must not append or fail — run records are rewritten on every transition."""
    storage.write_text("state/approved.json", "v1", "first")
    storage.write_text("state/approved.json", "v2", "second")
    assert storage.read_text("state/approved.json") == "v2"


def test_unicode_roundtrips(storage: Storage) -> None:
    """Resume content is bilingual; base64 and UTF-8 must both survive."""
    text = '{"name": "吳雨諠", "org": "國泰金控 — DDT AI"}'
    storage.write_text("state/runs/r1/run.json", text, "unicode")
    assert storage.read_text("state/runs/r1/run.json") == text


def test_binary_roundtrips(storage: Storage) -> None:
    data = b"%PDF-1.4\x00\x01\x02 binary \xff\xfe"
    storage.write("state/runs/r1/artifacts/resume.pdf", data, "artifact")
    assert storage.read("state/runs/r1/artifacts/resume.pdf") == data


def test_list_prefix_finds_children(storage: Storage) -> None:
    for run_id in ("20260801T030000Z-scheduled", "20260808T030000Z-manual"):
        storage.write_text(f"state/runs/{run_id}/run.json", "{}", "create")
    listed = storage.list_prefix("state/runs")
    assert len(listed) == 2
    assert all("20260801" in x or "20260808" in x for x in listed)


def test_list_prefix_empty_when_absent(storage: Storage) -> None:
    assert storage.list_prefix("state/runs") == []


def test_delete_prefix_removes_files(storage: Storage) -> None:
    storage.write("state/runs/r1/artifacts/a.pdf", b"a", "x")
    storage.write("state/runs/r1/artifacts/b.pdf", b"b", "x")
    storage.write_text("state/runs/r1/run.json", "{}", "x")

    removed = storage.delete_prefix("state/runs/r1/artifacts", "discard")

    assert removed == 2
    assert storage.read("state/runs/r1/artifacts/a.pdf") is None
    # The audit record survives — that is the point of discarding only artifacts.
    assert storage.read_text("state/runs/r1/run.json") == "{}"


def test_delete_missing_prefix_is_not_an_error(storage: Storage) -> None:
    """Reachable when a rejection and an expiry race on the same run."""
    assert storage.delete_prefix("state/runs/nope/artifacts", "discard") == 0


# --- GitHub-specific mechanics ----------------------------------------------


def test_github_create_omits_sha_and_update_sends_it() -> None:
    """The real API rejects an update without the current sha and a create with one."""
    fake = FakeGitHub()
    storage = github_storage(fake)

    storage.write_text("state/approved.json", "v1", "create")
    storage.write_text("state/approved.json", "v2", "update")

    assert fake.files["state/approved.json"] == b"v2"


def test_github_conflict_is_retried_once() -> None:
    """A 409 means the branch moved; re-read the sha and retry rather than failing a run."""
    fake = FakeGitHub()
    storage = github_storage(fake)
    storage.write_text("state/x.json", "v1", "create")

    # Simulate an outside writer: change the stored sha without touching the cache.
    fake.shas["state/x.json"] = "sha-from-elsewhere"

    storage.write_text("state/x.json", "v2", "update")
    assert fake.files["state/x.json"] == b"v2"


def test_github_large_file_uses_the_blob_api() -> None:
    """Files over 1MB come back with no inline content — normal for PDF artifacts."""
    fake = FakeGitHub()
    storage = github_storage(fake)
    big = b"%PDF" + b"x" * 2_000_000
    storage.write("state/runs/r1/artifacts/big.pdf", big, "x")

    # Drop the cache so the read goes through the API path.
    storage._cache.clear()

    original = fake.handler

    def strip_inline_content(request: httpx.Request) -> httpx.Response:
        response = original(request)
        if request.method == "GET" and "/contents/" in str(request.url):
            payload = response.json()
            if isinstance(payload, dict) and "content" in payload:
                # What GitHub actually does for files over 1MB.
                return httpx.Response(200, json={**payload, "content": ""})
        return response

    storage._client = httpx.Client(
        base_url="https://api.github.com", transport=httpx.MockTransport(strip_inline_content)
    )
    assert storage.read("state/runs/r1/artifacts/big.pdf") == big


def test_github_caches_within_a_request() -> None:
    """The diff page reads a run record several times while rendering one page."""
    fake = FakeGitHub()
    storage = github_storage(fake)
    storage.write_text("state/runs/r1/run.json", "{}", "x")

    before = sum(1 for method, _ in fake.calls if method == "GET")
    for _ in range(3):
        storage.read_text("state/runs/r1/run.json")
    after = sum(1 for method, _ in fake.calls if method == "GET")

    assert after == before, "repeated reads hit the network"


def test_github_requires_a_token() -> None:
    with pytest.raises(StorageError, match="GITHUB_TOKEN"):
        GitHubStorage(Settings(github_token=SecretStr("")))


# --- local-specific ----------------------------------------------------------


def test_local_rejects_path_traversal(tmp_path: Path) -> None:
    """Some paths reach storage from URLs; containment is enforced, not assumed."""
    storage = LocalStorage(tmp_path)
    with pytest.raises(StorageError, match="escapes"):
        storage.read("../../etc/passwd")


def test_local_write_is_atomic(tmp_path: Path) -> None:
    """Temp file plus replace, so a crash can't leave a truncated run record."""
    storage = LocalStorage(tmp_path)
    storage.write_text("state/approved.json", "content", "x")
    assert not list(tmp_path.rglob("*.tmp"))


# --- backend selection -------------------------------------------------------


def test_build_storage_defaults_to_local(tmp_path: Path) -> None:
    assert isinstance(build_storage(Settings(repo_root=tmp_path)), LocalStorage)


def test_build_storage_honours_the_github_setting(tmp_path: Path) -> None:
    settings = Settings(repo_root=tmp_path, storage_backend="github", github_token=SecretStr("t"))
    storage = build_storage(settings)
    assert isinstance(storage, GitHubStorage)
    storage.close()


def test_github_backend_without_a_token_fails_loudly(tmp_path: Path) -> None:
    """Better than silently falling back to a disk that Cloud Run will discard."""
    with pytest.raises(StorageError):
        build_storage(Settings(repo_root=tmp_path, storage_backend="github"))


# --- integration with RunStore ------------------------------------------------


def test_runstore_works_on_the_github_backend(tmp_path: Path) -> None:
    """The end-to-end property Cloud Run depends on: a run saved by one instance is
    readable by another, with no shared disk."""
    from pipeline.state import DiffCounts, RunStatus, RunStore, Trigger

    fake = FakeGitHub()
    settings = Settings(repo_root=tmp_path, github_token=SecretStr("t"))

    # Instance A creates and saves.
    store_a = RunStore(settings, storage=github_storage(fake))
    run = store_a.create(Trigger.SCHEDULED)
    run.counts = DiffCounts(added=1, modified=2)
    run.status = RunStatus.PENDING_APPROVAL
    store_a.save(run)
    store_a.save_diff(run.id, {"counts": {"added": 1, "modified": 2, "removed": 0}})

    # Instance B, sharing only the fake remote, sees it.
    store_b = RunStore(settings, storage=github_storage(fake))
    loaded = store_b.load(run.id)
    assert loaded is not None
    assert loaded.counts.total == 3
    assert loaded.status is RunStatus.PENDING_APPROVAL
    assert store_b.load_diff(run.id) is not None
    assert [r.id for r in store_b.pending()] == [run.id]


def test_runstore_snapshot_roundtrips_via_github(tmp_path: Path) -> None:
    from pipeline.state import RunStore

    fake = FakeGitHub()
    settings = Settings(repo_root=tmp_path, github_token=SecretStr("t"))
    store = RunStore(settings, storage=github_storage(fake))

    assert store.load_approved_snapshot() is None
    store.save_approved_snapshot({"lang": "en", "profile": {"name": "吳雨諠"}})
    assert store.load_approved_snapshot() == {"lang": "en", "profile": {"name": "吳雨諠"}}


def test_runstore_sources_index_roundtrips_via_github(tmp_path: Path) -> None:
    from pipeline.state import RunStore

    fake = FakeGitHub()
    settings = Settings(repo_root=tmp_path, github_token=SecretStr("t"))
    store = RunStore(settings, storage=github_storage(fake))

    assert store.load_sources_index() == {}
    store.save_sources_index({"sources/prd.md": "abc123"})
    assert store.load_sources_index() == {"sources/prd.md": "abc123"}
