"""Stage 9: publish an approved run.

This is the only module that changes anything outside `state/`. Order matters, and it is
chosen so a partial failure leaves a recoverable state:

1. Copy artifacts into `output/` and update the approved snapshot.
2. Commit and push. **The commit is the source of truth** — if this fails, nothing was
   published and the run stays pending for a retry.
3. Upload to Notion and notify Slack. Both are best-effort: a Notion outage must not
   undo a successful publish, because the files are already committed and the download
   links already serve them.

Git is the database here (see `pipeline/state.py`), so this function also pushes the run
record itself — meaning the audit trail and the artifacts it describes land together.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from pipeline.config import Settings, get_settings
from pipeline.state import Run, RunStatus, RunStore

log = logging.getLogger(__name__)


class PublishError(RuntimeError):
    pass


# What gets published from a run's artifacts, besides the two JSON snapshots (which are
# handled separately because they must be written last). One constant rather than the same
# tuple in `_copy_artifacts` and `_publish_via_api`: a format present in one and missing
# from the other is rendered and then never committed, and only on Cloud Run — where
# nobody is reading the log.
#
# `.tex` is here because it is the Overleaf-editable source of the LaTeX PDF.
PUBLISHED_SUFFIXES = (".pdf", ".docx", ".tex")


async def publish_run(run_id: str, decided_by: str = "web") -> None:
    """Publish `run_id`. Never raises — failures are recorded on the run."""
    settings = get_settings()
    store = RunStore(settings)

    run = store.load(run_id)
    if run is None:
        log.error("publish_run called for unknown run %s", run_id)
        return

    if run.status is not RunStatus.PENDING_APPROVAL:
        # Guards a double-tap in Slack and a link opened twice.
        log.info("run %s is %s, not pending — nothing to publish", run_id, run.status.value)
        return

    try:
        await _publish(run, store, settings, decided_by)
    except Exception as exc:  # noqa: BLE001 - a background task must not die silently
        log.exception("publishing run %s failed", run_id)
        run.status = RunStatus.FAILED
        run.error = f"publish failed: {type(exc).__name__}: {exc}"[:1000]
        store.save(run)
        from web.slack import post_message

        await post_message(settings, f"⚠️ 發布失敗 `{run_id}`\n```{run.error}```")


async def _publish(run: Run, store: RunStore, settings: Settings, decided_by: str) -> None:
    # Approval arrives in its own request, minutes or days after the render, so on Cloud Run
    # it is usually served by an instance that has never seen this run's files. Pull them
    # back from durable storage first; without this, every approval after an instance
    # recycle failed with "no artifacts directory — it may have been discarded already",
    # which blames the wrong thing and reads as if the run had been rejected.
    store.materialize_artifacts(run.id)

    artifacts = store.artifacts_dir(run.id)
    if not artifacts.is_dir():
        raise PublishError(
            f"run {run.id} has no artifacts, on disk or in storage — nothing was rendered, "
            "or the run was already discarded"
        )

    # --- 1. Move artifacts into output/ ------------------------------------
    published = _copy_artifacts(artifacts, settings.output_dir)
    if not published:
        raise PublishError("no artifacts to publish")
    log.info("staged %d artifact(s) into output/", len(published))

    # The new diff baseline. Written before the commit so both land together — a snapshot
    # that disagreed with output/ would make the next run's diff wrong.
    resume_json = artifacts / "resume.json"
    if resume_json.is_file():
        store.save_approved_snapshot(json.loads(resume_json.read_text(encoding="utf-8")))

    # --- 2. Commit (the point of no return) --------------------------------
    run.status = RunStatus.APPROVED
    run.decided_by = decided_by
    store.save(run)

    tag = f"resume-{datetime.now(UTC):%Y%m%d-%H%M}"
    # Two paths, because the two hosts differ in what they can offer. With a working copy
    # (Render, local) git gives one atomic commit for all eight artifacts plus a tag. On
    # Cloud Run there is no working copy, so each file is a separate API commit — less
    # tidy, but the alternative is cloning the repo on every cold start.
    if settings.storage_backend == "github":
        run.commit_sha = _publish_via_api(run, published, settings, tag)
    else:
        run.commit_sha = _commit_and_push(run, tag, settings)
    store.save(run)
    log.info("published run %s as %s (%s)", run.id, tag, (run.commit_sha or "?")[:8])

    # --- 3. Best-effort side effects ---------------------------------------
    # Past this point the publish has succeeded. Failures here are logged, not raised.
    try:
        await _upload_to_notion(run, published, settings)
    except Exception as exc:  # noqa: BLE001
        log.warning("could not attach artifacts to Notion: %s", exc)

    from pipeline.notify_slack import notify_published

    await notify_published(run, settings)


def _copy_artifacts(source: Path, output_dir: Path) -> list[Path]:
    """Copy a run's artifacts into `output/`, preserving the en/ zh/ split."""
    written: list[Path] = []

    for name in ("resume.json", "resume.zh.json"):
        if (path := source / name).is_file():
            target = output_dir / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)
            written.append(target)

    for lang in ("en", "zh"):
        lang_dir = source / lang
        if not lang_dir.is_dir():
            continue
        for path in sorted(lang_dir.iterdir()):
            if path.suffix in PUBLISHED_SUFFIXES:
                target = output_dir / lang / path.name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, target)
                written.append(target)

    return written


def _publish_via_api(run: Run, published: list[Path], settings: Settings, tag: str) -> str:
    """Write the artifacts through the GitHub Contents API, one commit per file.

    Used where there is no working copy. Ordering matters: the JSON snapshots go last, so
    if a document write fails the baseline still describes the previously published state
    and the next run's diff stays meaningful.

    Returns the head commit sha and creates `tag`, so this path records the same two things
    the working-copy path does.
    """
    from pipeline.storage import GitHubStorage

    storage = GitHubStorage(settings)
    try:
        documents = [p for p in published if p.suffix in PUBLISHED_SUFFIXES]
        snapshots = [p for p in published if p.suffix == ".json"]

        for path in [*documents, *snapshots]:
            key = path.relative_to(settings.repo_root).as_posix()
            storage.write(key, path.read_bytes(), f"Publish {key} (run {run.id})")
            log.info("published %s", key)

        # The head commit after the writes. This used to return the last file's *blob* sha,
        # which is not a commit and cannot be looked up as one — `git/commits/<sha>` gave a
        # 404 for a value stored in a field called `commit_sha` and shown on the diff page as
        # the published commit. Verified against the real published sha.
        head = _head_commit(storage, settings)
        if head:
            _create_tag(storage, settings, tag, head)
        return head
    finally:
        storage.close()


def _head_commit(storage: object, settings: Settings) -> str:
    """The branch's current head commit sha, or "" if it cannot be read."""
    response = storage._client.get(  # noqa: SLF001 - same package, one HTTP client
        f"/repos/{settings.github_repo}/commits/{settings.git_branch}"
    )
    if response.status_code != 200:
        log.warning("could not read the head commit (%s)", response.status_code)
        return ""
    return response.json().get("sha", "")


def _create_tag(storage: object, settings: Settings, tag: str, sha: str) -> None:
    """Tag the published commit. Best-effort: a failed tag must not fail the publish.

    The Cloud Run path silently skipped tagging while the log still announced
    "published run X as resume-YYYYMMDD-HHMM", so every publish claimed a tag that did not
    exist — `git tag -l` came back empty after a successful publish.
    """
    client = storage._client  # noqa: SLF001
    repo = settings.github_repo
    response = client.post(f"/repos/{repo}/git/refs", json={"ref": f"refs/tags/{tag}", "sha": sha})
    if response.status_code == 422:
        # Already exists (a second publish in the same minute). Move it.
        response = client.patch(
            f"/repos/{repo}/git/refs/tags/{tag}", json={"sha": sha, "force": True}
        )
    if response.status_code >= 400:
        log.warning("could not create tag %s (%s)", tag, response.status_code)
    else:
        log.info("tagged %s at %s", tag, sha[:8])


def _commit_and_push(run: Run, tag: str, settings: Settings) -> str:
    """Commit `output/` and `state/`, tag, and push. Returns the commit SHA."""
    repo = settings.repo_root

    if not settings.github_token.get_secret_value():
        raise PublishError("GITHUB_TOKEN is empty — cannot push")

    # Identity is set per-invocation rather than assumed: a fresh container has no
    # git config, and a commit without one fails.
    _git(repo, "config", "user.name", settings.git_author_name)
    _git(repo, "config", "user.email", settings.git_author_email)

    # Rebase on pull: the cron container and the web container both read this repo, and a
    # merge commit in the audit history is noise.
    _git(repo, "remote", "set-url", "origin", settings.git_remote_url())
    _git(repo, "pull", "--rebase", "--autostash", "origin", settings.git_branch, check=False)

    _git(repo, "add", "output", "state")

    if not _git(repo, "diff", "--cached", "--quiet", check=False).returncode:
        # Nothing staged. Reachable if the same content was already published — treat it
        # as success and report the current HEAD.
        log.info("no staged changes; reporting current HEAD")
        return _git(repo, "rev-parse", "HEAD").stdout.strip()

    message = (
        f"Publish resume {tag}\n\n"
        f"{run.counts.summary()}\n"
        f"run: {run.id}\n"
        f"trigger: {run.trigger.value}\n"
        f"approved via: {run.decided_by or 'unknown'}\n"
    )
    _git(repo, "commit", "-m", message)
    sha = _git(repo, "rev-parse", "HEAD").stdout.strip()

    _git(repo, "tag", "-f", tag)
    _git(repo, "push", "origin", f"HEAD:{settings.git_branch}")
    # Tags pushed separately and non-fatally: a rejected tag must not fail a publish whose
    # commit already landed.
    _git(repo, "push", "--force", "origin", f"refs/tags/{tag}", check=False)

    return sha


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(  # noqa: S603 - fixed binary, no shell
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )
    if check and result.returncode != 0:
        # Never echo the command: the remote URL carries the token.
        raise PublishError(f"git {args[0]} failed ({result.returncode}): {result.stderr[:400]}")
    return result


async def _upload_to_notion(run: Run, artifacts: list[Path], settings: Settings) -> None:
    """Attach the artifacts to the run's Notion row.

    Notion's file upload is a three-step flow (create upload → send bytes → attach), so
    this is the most failure-prone part of publishing — hence best-effort, after the commit.
    """
    import httpx

    token = settings.notion_token.get_secret_value()
    if not (token and run.notion_run_page_id):
        log.info("no Notion run page recorded — skipping upload")
        return

    headers = {
        "Authorization": f"Bearer {token}",
        "Notion-Version": settings.notion_version,
    }

    uploaded: list[dict] = []
    async with httpx.AsyncClient(base_url="https://api.notion.com/v1", timeout=60.0) as client:
        for path in artifacts:
            if path.suffix == ".json":
                continue  # the JSON snapshots live in git, not as Notion attachments
            try:
                create = await client.post(
                    "/file_uploads",
                    headers={**headers, "Content-Type": "application/json"},
                    json={"filename": f"{path.parent.name}-{path.name}"},
                )
                create.raise_for_status()
                upload_id = create.json()["id"]

                send = await client.post(
                    f"/file_uploads/{upload_id}/send",
                    headers=headers,
                    files={"file": (path.name, path.read_bytes())},
                )
                send.raise_for_status()
                uploaded.append({"type": "file_upload", "file_upload": {"id": upload_id}})
            except (httpx.HTTPError, KeyError) as exc:
                log.warning("upload of %s failed: %s", path.name, exc)

        if not uploaded:
            return

        patch = await client.patch(
            f"/pages/{run.notion_run_page_id}",
            headers={**headers, "Content-Type": "application/json"},
            json={
                "properties": {
                    "Artifacts": {"files": uploaded},
                    "Status": {"select": {"name": RunStatus.APPROVED.value}},
                    "Commit SHA": {"rich_text": [{"text": {"content": run.commit_sha or ""}}]},
                }
            },
        )
        patch.raise_for_status()
        log.info("attached %d artifact(s) to the Notion run row", len(uploaded))
