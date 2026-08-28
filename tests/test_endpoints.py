"""HTTP surface, exercised through the real app.

Covers the approval flow end to end: an unsigned request is refused, a signed one is
accepted, and a second identical request does not publish twice. Publishing itself is
stubbed — it pushes to a real repo — but everything up to the call is genuine.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

import pipeline.publish as publish_module
import pipeline.runner as runner_module
from pipeline.config import Settings
from pipeline.diff import compute_diff, write_diff
from pipeline.models import Resume
from pipeline.state import RunStatus, RunStore, Trigger
from web.app import app

FIXTURE = Path(__file__).parent / "fixtures" / "resume.sample.json"
TRIGGER_TOKEN = "test-trigger-token"


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    (tmp_path / "state").mkdir()
    (tmp_path / "output").mkdir()
    return Settings(
        repo_root=tmp_path,
        trigger_token=SecretStr(TRIGGER_TOKEN),
        approval_hmac_secret=SecretStr("test-hmac-secret"),
        public_base_url="http://testserver",
    )


@pytest.fixture
def client(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """A client whose app sees the temp-rooted settings.

    get_settings is lru_cached, so the cache is overridden rather than the class — the same
    trap that once let tests write into the production directory.
    """
    monkeypatch.setattr("pipeline.config.get_settings", lambda: settings)
    for module in ("web.app", "web.routes_runs", "web.routes_resume", "web.slack"):
        monkeypatch.setattr(f"{module}.get_settings", lambda: settings, raising=False)
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def pending_run(settings: Settings) -> str:
    """A run waiting for approval, with a real diff behind it."""
    store = RunStore(settings)
    run = store.create(Trigger.SCHEDULED)

    baseline = json.loads(FIXTURE.read_text(encoding="utf-8"))
    updated = json.loads(json.dumps(baseline))
    updated["experiences"][0]["role"] = "Machine Learning Engineer Intern"

    diff = compute_diff(baseline, Resume.model_validate(updated))
    write_diff(diff, store.run_dir(run.id) / "diff.json")
    (store.artifacts_dir(run.id) / "resume.json").write_text(
        json.dumps(updated, ensure_ascii=False), encoding="utf-8"
    )
    run.counts = diff.counts
    run.status = RunStatus.PENDING_APPROVAL
    store.save(run)
    return run.id


# --- health ------------------------------------------------------------------


def test_healthz_reports_capabilities(client: TestClient) -> None:
    """The health check. Missing credentials are reported, not fatal — the diff page
    and download links are useful before Slack is wired up."""
    payload = client.get("/healthz").json()
    assert payload["status"] == "ok"
    assert set(payload["tools"]) == {"pdftoppm", "tectonic", "git"}
    assert payload["publish_ready"] is False
    assert "NOTION_TOKEN" in payload["missing_credentials"]


def test_health_is_served_at_both_paths(client: TestClient) -> None:
    """`/health` exists because Cloud Run's edge intercepts `/healthz` and answers with
    Google's own HTML 404 — the request never reaches the container, so the documented
    post-deploy check and the GitHub Actions warm-up both failed against a healthy
    service. Both paths must return the same payload, or the two would drift.
    """
    alias, original = client.get("/health"), client.get("/healthz")
    assert alias.status_code == original.status_code == 200

    # `llm.key` carries a computed expiry timestamp that differs by microseconds between
    # two calls, so compare everything else and the llm fields that are not time-derived.
    stable = lambda payload: {k: v for k, v in payload.items() if k != "llm"}  # noqa: E731
    assert stable(alias.json()) == stable(original.json())
    for field in ("provider", "model", "region"):
        assert alias.json()["llm"][field] == original.json()["llm"][field]


def test_index_lists_pending_runs(client: TestClient, pending_run: str) -> None:
    payload = client.get("/").json()
    assert [r["id"] for r in payload["pending_runs"]] == [pending_run]


# --- trigger auth ------------------------------------------------------------


def test_trigger_without_token_is_refused(client: TestClient) -> None:
    assert client.post("/api/runs").status_code == 401


def test_trigger_with_wrong_token_is_refused(client: TestClient) -> None:
    response = client.post("/api/runs", headers={"X-Trigger-Token": "wrong"})
    assert response.status_code == 401


def test_trigger_accepts_and_returns_immediately(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """202 with a preview URL: rendering takes tens of seconds, past any HTTP timeout."""
    calls: list[tuple[str, object]] = []

    async def fake_execute(run_id: str, store: object = None) -> None:
        calls.append((run_id, store))

    monkeypatch.setattr(runner_module, "execute_run", fake_execute)

    response = client.post(
        "/api/runs", headers={"X-Trigger-Token": TRIGGER_TOKEN}, params={"trigger": "Manual"}
    )
    assert response.status_code == 202
    payload = response.json()
    assert payload["run_id"].endswith("-manual")
    assert payload["url"].endswith(f"/runs/{payload['run_id']}")
    assert [run_id for run_id, _ in calls] == [payload["run_id"]]


def test_trigger_hands_its_store_to_the_background_task(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The store must be passed, not reconstructed inside the task.

    With the GitHub backend, a fresh store re-reads the run through the Contents API, which
    is only eventually consistent for read-after-write: every run on Cloud Run died
    instantly with "execute_run called for unknown run" while the record sat committed in
    the repository. Passing the store also passes its cache, which holds the record already.
    """
    captured: list[object] = []

    async def fake_execute(run_id: str, store: object = None) -> None:
        captured.append(store)

    monkeypatch.setattr(runner_module, "execute_run", fake_execute)
    client.post("/api/runs", headers={"X-Trigger-Token": TRIGGER_TOKEN})

    assert captured, "execute_run was never scheduled"
    assert isinstance(captured[0], RunStore), (
        f"expected the request's RunStore to be handed over, got {captured[0]!r}"
    )


def test_unknown_trigger_is_rejected(client: TestClient) -> None:
    response = client.post(
        "/api/runs", headers={"X-Trigger-Token": TRIGGER_TOKEN}, params={"trigger": "Whenever"}
    )
    assert response.status_code == 400


# --- diff page ---------------------------------------------------------------


def test_diff_page_renders(client: TestClient, pending_run: str) -> None:
    response = client.get(f"/runs/{pending_run}")
    assert response.status_code == 200
    body = response.text
    # The change, the counts, and both decision buttons.
    assert "Machine Learning Engineer Intern" in body
    assert "~1 修改" in body
    assert "核准並發布" in body
    assert "駁回" in body


def test_diff_page_needs_no_javascript(client: TestClient, pending_run: str) -> None:
    """Tabs are radio inputs plus CSS: if the page loads, it works."""
    body = client.get(f"/runs/{pending_run}").text
    assert "<script" not in body
    assert 'type="radio"' in body


def test_unknown_run_page_is_404(client: TestClient) -> None:
    assert client.get("/runs/20260101T000000Z-manual").status_code == 404


# --- approval ----------------------------------------------------------------


def test_approval_requires_a_signature(client: TestClient, pending_run: str) -> None:
    """Otherwise guessing a run id is enough to publish to a public repo."""
    assert client.get(f"/api/runs/{pending_run}/approve?token=nope").status_code == 403


def test_approve_token_cannot_reject(
    client: TestClient, pending_run: str, settings: Settings
) -> None:
    from web.routes_runs import sign

    token = sign(pending_run, "approve", settings)
    assert client.get(f"/api/runs/{pending_run}/reject?token={token}").status_code == 403


def test_signed_approval_publishes(
    client: TestClient, pending_run: str, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    published: list[tuple[str, str]] = []

    async def fake_publish(run_id: str, decided_by: str = "web") -> None:
        published.append((run_id, decided_by))

    monkeypatch.setattr(publish_module, "publish_run", fake_publish)

    from web.routes_runs import sign

    token = sign(pending_run, "approve", settings)
    response = client.get(f"/api/runs/{pending_run}/approve?token={token}")

    assert response.status_code == 202
    assert published == [(pending_run, "web")]


def test_signed_rejection_discards_artifacts(
    client: TestClient, pending_run: str, settings: Settings
) -> None:
    store = RunStore(settings)
    (store.artifacts_dir(pending_run) / "resume.pdf").write_bytes(b"%PDF-fake")

    from web.routes_runs import sign

    token = sign(pending_run, "reject", settings)
    response = client.get(f"/api/runs/{pending_run}/reject?token={token}")

    assert response.status_code == 200
    assert response.json()["status"] == "Rejected"
    # Rejected artifacts must not linger where they could be mistaken for published output.
    assert not store.artifacts_dir(pending_run).exists()
    # The run record survives as history.
    assert store.load(pending_run) is not None


def test_second_approval_is_a_no_op(
    client: TestClient, pending_run: str, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A link opened twice, or a double-tapped Slack button, must not publish twice."""
    published: list[str] = []

    async def fake_publish(run_id: str, decided_by: str = "web") -> None:
        published.append(run_id)
        run = RunStore(settings).load(run_id)
        assert run is not None
        run.status = RunStatus.APPROVED
        RunStore(settings).save(run)

    monkeypatch.setattr(publish_module, "publish_run", fake_publish)

    from web.routes_runs import sign

    token = sign(pending_run, "approve", settings)
    first = client.get(f"/api/runs/{pending_run}/approve?token={token}")
    second = client.get(f"/api/runs/{pending_run}/approve?token={token}")

    assert first.status_code == 202
    assert second.status_code == 200
    assert "already" in second.json()["message"]
    assert len(published) == 1


def test_decided_run_shows_no_buttons(
    client: TestClient, pending_run: str, settings: Settings
) -> None:
    store = RunStore(settings)
    run = store.load(pending_run)
    assert run is not None
    run.status = RunStatus.APPROVED
    run.commit_sha = "abc123def456"
    store.save(run)

    body = client.get(f"/runs/{pending_run}").text
    assert "核准並發布" not in body
    assert "abc123def456" in body


# --- downloads ---------------------------------------------------------------


def test_download_before_first_publish_is_404(client: TestClient) -> None:
    response = client.get("/resume/en.pdf")
    assert response.status_code == 404
    # The message has to say what to do about it.
    assert "Approve a run first" in response.json()["detail"]


def test_published_artifact_is_served(client: TestClient, settings: Settings) -> None:
    target = settings.output_dir / "en" / "resume.pdf"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"%PDF-1.4 published")
    (settings.output_dir / "resume.json").write_text(
        json.dumps({"profile": {"name": "WU, YU-HSUAN"}}), encoding="utf-8"
    )

    response = client.get("/resume/en.pdf")
    assert response.status_code == 200
    assert response.content == b"%PDF-1.4 published"
    # A recruiter-facing filename, derived from the published resume itself.
    assert "WU_YU-HSUAN_Resume_EN.pdf" in response.headers["content-disposition"]


def test_unknown_language_and_format_are_404(client: TestClient) -> None:
    assert client.get("/resume/fr.pdf").status_code == 404
    assert client.get("/resume/en.rtf").status_code == 404


def test_latex_variant_route_resolves(client: TestClient, settings: Settings) -> None:
    """ "latex.pdf" contains a dot, which the default path converter would split."""
    target = settings.output_dir / "zh" / "resume.latex.pdf"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"%PDF-1.4 latex")
    assert client.get("/resume/zh.latex.pdf").status_code == 200


def test_latex_source_is_downloadable(client: TestClient, settings: Settings) -> None:
    """The .tex is what you upload to Overleaf, so it needs the same stable link."""
    target = settings.output_dir / "en" / "resume.tex"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("% !TEX program = xelatex\n", encoding="utf-8")
    (settings.output_dir / "resume.json").write_text(
        json.dumps({"profile": {"name": "WU, YU-HSUAN"}}), encoding="utf-8"
    )

    response = client.get("/resume/en.tex")
    assert response.status_code == 200
    assert "WU_YU-HSUAN_Resume_EN.tex" in response.headers["content-disposition"]


def test_index_lists_the_latex_source(client: TestClient) -> None:
    """Every published format is advertised here; a missing one is invisible otherwise."""
    downloads = client.get("/").json()["downloads"]
    assert downloads["en.tex"].endswith("/resume/en.tex")
    assert downloads["zh.tex"].endswith("/resume/zh.tex")


# --- page images -------------------------------------------------------------


def test_page_image_is_served(client: TestClient, settings: Settings, pending_run: str) -> None:
    pages = RunStore(settings).pages_dir(pending_run) / "en"
    pages.mkdir(parents=True, exist_ok=True)
    (pages / "resume-01.png").write_bytes(b"\x89PNG fake")

    response = client.get(f"/runs/{pending_run}/pages/en/resume-01.png")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "public, max-age=86400, immutable"


def test_page_image_traversal_is_blocked(
    client: TestClient, settings: Settings, pending_run: str
) -> None:
    """The path comes from the URL and is read off disk, so containment is enforced."""
    secret = settings.repo_root / "secret.png"
    secret.write_bytes(b"\x89PNG secret")

    for attempt in ("../../secret.png", "..%2f..%2fsecret.png", "....//secret.png"):
        response = client.get(f"/runs/{pending_run}/pages/en/{attempt}")
        assert response.status_code == 404, attempt
        assert b"secret" not in response.content


def test_non_png_page_request_is_404(
    client: TestClient, settings: Settings, pending_run: str
) -> None:
    pages = RunStore(settings).pages_dir(pending_run) / "en"
    pages.mkdir(parents=True, exist_ok=True)
    (pages / "notes.txt").write_text("not an image", encoding="utf-8")
    assert client.get(f"/runs/{pending_run}/pages/en/notes.txt").status_code == 404


# --- slack -------------------------------------------------------------------


def test_slack_endpoints_reject_unsigned_requests(client: TestClient) -> None:
    """These are public endpoints whose payloads can publish."""
    assert client.post("/slack/commands", data={"text": "update"}).status_code == 401
    assert client.post("/slack/interactions", data={"payload": "{}"}).status_code == 401


# --- what the API publish path records ----------------------------------------


def test_api_publish_records_a_commit_and_creates_the_tag(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The Cloud Run publish path must record a *commit* sha and actually tag it.

    Both were wrong on the deployed service: `commit_sha` held the last file's blob sha, so
    `GET /git/commits/<sha>` returned 404 for a value the diff page presents as the published
    commit; and no tag was ever created, while the log still announced "published run X as
    resume-YYYYMMDD-HHMM" on every publish. `git tag -l` was empty after a real publish.
    """
    import pipeline.publish as pub

    calls: list[tuple[str, str]] = []

    class _FakeClient:
        def get(self, path: str):  # noqa: ANN202
            calls.append(("GET", path))
            return _FakeResponse(200, {"sha": "c" * 40})

        def post(self, path: str, json: dict):  # noqa: A002, ANN202
            calls.append(("POST", path))
            assert json["sha"] == "c" * 40, "the tag must point at the head commit"
            return _FakeResponse(201, {})

        def patch(self, path: str, json: dict):  # noqa: A002, ANN202
            calls.append(("PATCH", path))
            return _FakeResponse(200, {})

    class _FakeResponse:
        def __init__(self, status: int, payload: dict) -> None:
            self.status_code = status
            self._payload = payload

        def json(self) -> dict:
            return self._payload

    class _FakeStorage:
        def __init__(self, *_a: object, **_k: object) -> None:
            self._client = _FakeClient()
            self.written: list[str] = []

        def write(self, key: str, data: bytes, message: str) -> None:  # noqa: ARG002
            self.written.append(key)

        def close(self) -> None:
            pass

    monkeypatch.setattr("pipeline.storage.GitHubStorage", _FakeStorage)

    artifact = settings.output_dir / "en" / "resume.pdf"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_bytes(b"%PDF fake")

    run = RunStore(settings).create(Trigger.MANUAL)
    sha = pub._publish_via_api(run, [artifact], settings, "resume-20260814-0347")

    assert sha == "c" * 40, "expected the head commit sha, not a file blob sha"
    assert ("POST", "/repos/shane01526/Resume_pipeline/git/refs") in calls, (
        f"no tag ref was created; calls were {calls}"
    )


# --- the slash command's visibility contract ----------------------------------
#
# These exist because "/resume update 沒有用" turned out to be a reporting problem, not a
# broken command: the run was created and completed, but the reply was ephemeral (nobody
# could see a record) and a no-change run never said anything at all.


def _slash(client: TestClient, text: str, secret: str) -> dict:
    """POST a correctly-signed slash command and return the JSON body."""
    import hashlib
    import hmac
    import time
    from urllib.parse import urlencode

    body = urlencode({"command": "/resume", "text": text, "user_id": "U0"}).encode()
    ts = str(int(time.time()))
    sig = (
        "v0="
        + hmac.new(secret.encode(), b"v0:" + ts.encode() + b":" + body, hashlib.sha256).hexdigest()
    )
    response = client.post(
        "/slack/commands",
        content=body,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "X-Slack-Request-Timestamp": ts,
            "X-Slack-Signature": sig,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


@pytest.fixture
def slack_client(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """A client whose app has a Slack signing secret configured."""
    signed = settings.model_copy(update={"slack_signing_secret": SecretStr("slack-test-secret")})
    monkeypatch.setattr("pipeline.config.get_settings", lambda: signed)
    for module in ("web.app", "web.routes_runs", "web.routes_resume", "web.slack"):
        monkeypatch.setattr(f"{module}.get_settings", lambda: signed, raising=False)
    with TestClient(app) as client:
        yield client


@pytest.mark.parametrize("text", ["update", "status", "latest", "", "banana"])
def test_every_slash_reply_is_visible_in_the_channel(
    slack_client: TestClient, text: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Slack only echoes the user's command into the channel for in_channel responses.

    With an ephemeral reply there is no record at all — visible to one person, gone on
    reload, nothing showing the command ran. That is why it read as "the command did
    nothing".
    """

    async def noop(*_a: object, **_k: object) -> None:
        return None

    monkeypatch.setattr("web.slack._start_run", noop)

    payload = _slash(slack_client, text, "slack-test-secret")

    assert payload["response_type"] == "in_channel", f"{text or '(no args)'} replied privately"
    assert payload["text"]


def test_update_ack_does_not_promise_a_preview(
    slack_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The old ack said "完成後會通知你預覽", which is false when nothing changed — and that
    broken promise is what made the command look dead."""

    async def noop(*_a: object, **_k: object) -> None:
        return None

    monkeypatch.setattr("web.slack._start_run", noop)

    text = _slash(slack_client, "update", "slack-test-secret")["text"]

    assert "沒變更也會回報" in text


def test_unknown_subcommand_lists_the_valid_ones(slack_client: TestClient) -> None:
    """So a typo like `/resume lastest` teaches you the right spelling."""
    text = _slash(slack_client, "lastest", "slack-test-secret")["text"]

    for valid in ("update", "status", "latest"):
        assert valid in text


# --- Slack download links ----------------------------------------------------


def _publish_all_formats(settings: Settings) -> None:
    for lang in ("en", "zh"):
        for name in ("resume.pdf", "resume.latex.pdf", "resume.docx", "resume.tex"):
            target = settings.output_dir / lang / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"x")


async def test_publish_notification_links_every_format(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The publish confirmation is the message you are sure to read, so nothing may be
    missing from it — it once linked only the two PDFs and pointed at the rest with "⋯".
    """
    from pipeline.notify_slack import notify_published
    from web.slack import DOWNLOAD_FORMATS

    _publish_all_formats(settings)
    posted: list[str] = []

    async def fake_post(_settings: Settings, text: str, **_kwargs: object) -> None:
        posted.append(text)

    monkeypatch.setattr("web.slack.post_message", fake_post)

    run = RunStore(settings).create(Trigger.MANUAL)
    run.commit_sha = "0d489977deadbeef"
    await notify_published(run, settings)

    assert len(posted) == 1
    for lang in ("en", "zh"):
        for fmt, _label in DOWNLOAD_FORMATS:
            assert f"/resume/{lang}.{fmt}" in posted[0], f"{lang}.{fmt} is not linked"


async def test_publish_notification_omits_formats_that_were_not_rendered(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No Tectonic means no .latex.pdf; linking it anyway would hand you a 404."""
    from pipeline.notify_slack import notify_published

    _publish_all_formats(settings)
    (settings.output_dir / "en" / "resume.latex.pdf").unlink()
    posted: list[str] = []

    async def fake_post(_settings: Settings, text: str, **_kwargs: object) -> None:
        posted.append(text)

    monkeypatch.setattr("web.slack.post_message", fake_post)

    await notify_published(RunStore(settings).create(Trigger.MANUAL), settings)

    assert "/resume/en.latex.pdf" not in posted[0]
    assert "/resume/zh.latex.pdf" in posted[0]
    assert "/resume/en.tex" in posted[0]


# --- downloads survive an instance that never published ----------------------
#
# The Dockerfile creates output/en and output/zh empty and never copies published files
# into the image, so on Cloud Run only the instance that ran the publish has them. These
# use a storage backend whose contents are NOT on the local disk, which is the whole
# point: with the real LocalStorage the durable copy is the same file, so a test would
# "find" what a fresh container cannot.


class _RepoStorage:
    """Durable storage holding published files that never touched this container's disk."""

    def __init__(self, files: dict[str, bytes]) -> None:
        self.files = files
        self.reads: list[str] = []

    def read(self, path: str) -> bytes | None:
        self.reads.append(path)
        return self.files.get(path)

    def list_prefix(self, prefix: str) -> list[str]:
        return [key for key in self.files if key.startswith(f"{prefix}/")]


@pytest.fixture
def repo_storage(monkeypatch: pytest.MonkeyPatch) -> _RepoStorage:
    storage = _RepoStorage(
        {
            "output/resume.json": json.dumps({"profile": {"name": "WU, YU-HSUAN"}}).encode(),
            "output/en/resume.tex": b"% !TEX program = xelatex\n",
            "output/en/resume.pdf": b"%PDF-1.4 from the repo",
        }
    )
    for module in ("web.routes_resume", "web.slack"):
        monkeypatch.setattr(f"{module}.build_storage", lambda _s: storage)
    return storage


def test_download_falls_back_to_storage(
    client: TestClient, settings: Settings, repo_storage: _RepoStorage
) -> None:
    """A redeploy turned four working links into 404s; the repo is the durable copy."""
    assert not (settings.output_dir / "en" / "resume.tex").exists()

    response = client.get("/resume/en.tex")

    assert response.status_code == 200
    assert response.content == b"% !TEX program = xelatex\n"
    # The recruiter-facing filename needs resume.json, which is equally absent locally.
    assert "WU_YU-HSUAN_Resume_EN.tex" in response.headers["content-disposition"]


def test_restored_download_is_cached_on_disk(
    client: TestClient, settings: Settings, repo_storage: _RepoStorage
) -> None:
    """Otherwise every download costs a GitHub API round trip."""
    client.get("/resume/en.tex")
    assert (settings.output_dir / "en" / "resume.tex").is_file()

    repo_storage.reads.clear()
    assert client.get("/resume/en.tex").status_code == 200
    assert repo_storage.reads == [], "the second request went back to storage"


def test_download_still_404s_when_nothing_was_published(
    client: TestClient, repo_storage: _RepoStorage
) -> None:
    """Absent from disk *and* from the repo is the real "not published yet"."""
    response = client.get("/resume/zh.docx")
    assert response.status_code == 404
    assert "Approve a run first" in response.json()["detail"]


def test_latest_links_read_storage_on_a_fresh_instance(
    settings: Settings, repo_storage: _RepoStorage
) -> None:
    """`/resume latest` reported "還沒有已發布的履歷" on any instance that had not published."""
    from web.slack import _latest_links

    reply = _latest_links(settings)

    assert "/resume/en.tex" in reply
    assert "/resume/en.pdf" in reply
    # Not published at all, so it must not be offered.
    assert "/resume/zh.pdf" not in reply


def test_latest_links_prefer_the_local_disk(settings: Settings, repo_storage: _RepoStorage) -> None:
    """The publishing instance has the files, so the common case costs no API call."""
    from web.slack import _available_artifacts

    target = settings.output_dir / "en" / "resume.docx"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"x")

    assert _available_artifacts(settings, "en") == {"resume.docx"}
