"""Run endpoints: trigger a run, view its diff, approve or reject it.

Approval is authenticated by an HMAC signature over the run ID rather than by a session.
That means the link in a Slack message works from any device, still works if Slack is
down, and cannot be forged by guessing a run ID — which matters because approving
publishes to a public repo.

The diff page itself lands in the next commit; these endpoints and the signing scheme
are what the Slack surface and the cron trigger depend on.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets

from fastapi import APIRouter, BackgroundTasks, Header, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from pipeline.config import Settings, get_settings
from pipeline.state import Run, RunStatus, RunStore, Trigger

log = logging.getLogger(__name__)

router = APIRouter(tags=["runs"])


# --- approval signing -------------------------------------------------------


def sign(run_id: str, action: str, settings: Settings) -> str:
    """HMAC-SHA256 over "<run_id>:<action>", truncated to 32 hex chars.

    Scoped to the action as well as the run, so an approve token cannot be replayed as a
    reject. 128 bits is far beyond what a one-shot, 72-hour-lived token needs.
    """
    message = f"{run_id}:{action}".encode()
    key = settings.approval_hmac_secret.get_secret_value().encode()
    return hmac.new(key, message, hashlib.sha256).hexdigest()[:32]


def verify(run_id: str, action: str, token: str, settings: Settings) -> bool:
    return hmac.compare_digest(sign(run_id, action, settings), token)


def approval_links(run: Run, settings: Settings) -> dict[str, str]:
    """Signed approve and reject URLs for one run."""
    base = f"{settings.public_base_url}/api/runs/{run.id}"
    return {
        action: f"{base}/{action}?token={sign(run.id, action, settings)}"
        for action in ("approve", "reject")
    }


# --- trigger ----------------------------------------------------------------


@router.post("/api/runs")
async def create_run(
    background: BackgroundTasks,
    x_trigger_token: str = Header(default=""),
    trigger: str = "Scheduled",
) -> JSONResponse:
    """Start a pipeline run. Called by the cron job and by Slack's /resume update.

    Returns immediately and does the work in the background: rendering six artifacts
    takes tens of seconds, well past any sane HTTP timeout.
    """
    settings = get_settings()
    expected = settings.trigger_token.get_secret_value()
    if not expected or not secrets.compare_digest(x_trigger_token, expected):
        raise HTTPException(401, "invalid or missing X-Trigger-Token")

    try:
        trigger_enum = Trigger(trigger)
    except ValueError:
        raise HTTPException(400, f"unknown trigger {trigger!r}") from None

    store = RunStore(settings)

    # Expire anything stale first, so a forgotten pending run doesn't sit there while a
    # newer one is created alongside it.
    for expired in store.expire_stale():
        log.info("expired run %s after %sh", expired.id, settings.approval_timeout_hours)

    run = store.create(trigger_enum)
    log.info("created run %s (trigger=%s)", run.id, trigger_enum.value)

    from pipeline.runner import execute_run

    # Hand over this store, not just the id. A fresh store would re-read the run through the
    # GitHub Contents API, which is only eventually consistent for read-after-write — so the
    # task logged "unknown run" and exited while the record sat committed in the repo.
    background.add_task(execute_run, run.id, store)

    return JSONResponse(
        {
            "run_id": run.id,
            "status": run.status.value,
            "url": run.preview_url(settings.public_base_url),
        },
        status_code=202,
    )


# --- inspect ----------------------------------------------------------------


@router.get("/api/runs")
async def list_runs(limit: int = 20) -> JSONResponse:
    settings = get_settings()
    store = RunStore(settings)
    return JSONResponse(
        {
            "runs": [
                {
                    "id": run.id,
                    "status": run.status.value,
                    "trigger": run.trigger.value,
                    "created_at": run.created_at.isoformat(),
                    "diff": run.counts.summary(),
                    "commit": run.commit_sha,
                    "error": run.error,
                }
                for run in store.list_runs(limit=limit)
            ]
        }
    )


@router.get("/runs/{run_id}", response_class=HTMLResponse)
async def diff_page(run_id: str) -> HTMLResponse:
    """The before/after review page.

    Full implementation (content diff, page-image comparison, raw JSON) comes with the
    diff stage; this renders the run's current state so the endpoint and its signed
    approval links are usable now.
    """
    settings = get_settings()
    store = RunStore(settings)
    run = store.load(run_id)
    if run is None:
        raise HTTPException(404, f"no such run: {run_id}")

    from web.diff_page import render_diff_page

    return HTMLResponse(render_diff_page(run, store, settings))


@router.get("/runs/{run_id}/pages/{lang}/{filename}")
async def page_image(run_id: str, lang: str, filename: str) -> FileResponse:
    """Serve a rasterized page image for the diff page's visual tab."""
    settings = get_settings()
    store = RunStore(settings)

    # Path traversal guard: run_id, lang, and filename all come from the URL, and this
    # endpoint reads from disk. Resolving and then checking containment blocks `..`
    # segments and symlinks that a name-only check would miss.
    base = store.pages_dir(run_id).resolve()
    try:
        path = (base / lang / filename).resolve()
        path.relative_to(base)
    except (ValueError, OSError):
        raise HTTPException(404, "not found") from None

    if not path.is_file() or path.suffix != ".png":
        raise HTTPException(404, "not found")

    # Immutable: a run's page images never change once written.
    return FileResponse(
        path, media_type="image/png", headers={"Cache-Control": "public, max-age=86400, immutable"}
    )


# --- decide -----------------------------------------------------------------


@router.post("/api/runs/{run_id}/approve")
async def approve(run_id: str, token: str, background: BackgroundTasks) -> JSONResponse:
    return await _decide(run_id, "approve", token, background)


@router.post("/api/runs/{run_id}/reject")
async def reject(run_id: str, token: str, background: BackgroundTasks) -> JSONResponse:
    return await _decide(run_id, "reject", token, background)


# GET variants so a plain link in an email or Slack message works. Publishing from a GET
# is not ideal REST, but the token is single-purpose, unguessable, and expires with the
# run — and a link that needs a form to submit defeats the point of one-tap approval.
@router.get("/api/runs/{run_id}/approve")
async def approve_via_link(run_id: str, token: str, background: BackgroundTasks) -> JSONResponse:
    return await _decide(run_id, "approve", token, background)


@router.get("/api/runs/{run_id}/reject")
async def reject_via_link(run_id: str, token: str, background: BackgroundTasks) -> JSONResponse:
    return await _decide(run_id, "reject", token, background)


async def _decide(
    run_id: str, action: str, token: str, background: BackgroundTasks
) -> JSONResponse:
    settings = get_settings()
    store = RunStore(settings)

    run = store.load(run_id)
    if run is None:
        raise HTTPException(404, f"no such run: {run_id}")
    if not verify(run_id, action, token, settings):
        raise HTTPException(403, "invalid signature")

    # Idempotent: a double-tap on the Slack button, or a link opened twice, must not
    # publish twice.
    if run.status is not RunStatus.PENDING_APPROVAL:
        return JSONResponse(
            {
                "run_id": run.id,
                "status": run.status.value,
                "message": f"already {run.status.value.lower()}; nothing to do",
            }
        )

    if action == "approve":
        from pipeline.publish import publish_run

        background.add_task(publish_run, run.id, "web")
        return JSONResponse({"run_id": run.id, "status": "publishing"}, status_code=202)

    run.status = RunStatus.REJECTED
    run.decided_by = "web"
    store.save(run)
    store.discard(run.id)
    log.info("run %s rejected", run.id)
    return JSONResponse({"run_id": run.id, "status": run.status.value})
