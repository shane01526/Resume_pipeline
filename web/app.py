"""FastAPI application: diff pages, approval endpoints, Slack hooks, downloads.

Startup validates configuration so a misconfigured deploy fails its health check rather
than the first cron run at 3am. Rendering imports (Playwright, Tectonic) are deliberately
deferred into the request path — importing them here would add seconds to boot and make
the health check flaky.
"""

from __future__ import annotations

import logging
import shutil
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from pipeline.config import get_settings

log = logging.getLogger(__name__)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)


@asynccontextmanager
async def lifespan(app: FastAPI):  # noqa: ANN201, ARG001
    settings = get_settings()

    # Log what is missing rather than refusing to boot: the diff page and download links
    # are useful even before Slack or GitHub are wired up, and a container that won't
    # start is harder to debug than one that reports its own gaps.
    if missing := settings.missing_for_publish():
        log.warning("publishing disabled — missing %s", ", ".join(missing))

    for tool, why in (
        ("pdftoppm", "page images on the diff page"),
        ("tectonic", "the LaTeX renderer"),
        ("git", "committing approved artifacts"),
    ):
        if shutil.which(tool) is None:
            log.warning("%s not on PATH — %s will be unavailable", tool, why)

    log.info(
        "resume-pipeline up | renderers=%s | base_url=%s",
        ",".join(settings.renderers),
        settings.public_base_url,
    )
    yield


app = FastAPI(
    title="Resume Pipeline",
    description="Notion-backed bilingual resume pipeline with human-in-the-loop approval.",
    lifespan=lifespan,
)


@app.get("/healthz")
@app.get("/health")
async def healthz() -> JSONResponse:
    """The platform health check.

    Reports degraded capabilities without failing: a missing Slack token shouldn't take
    the service down, but it should be visible. Only a broken config object is fatal.

    Registered at both paths because **Cloud Run's edge swallows `/healthz`**: it answers
    with Google's own HTML 404 and the request never reaches the container. Verified by
    elimination — `/` returns 200, every other unknown path returns this app's JSON
    `{"detail":"Not Found"}`, and `/healthz` inside the same image returns 200. There are
    no request logs for it either, so nothing arrives. `/health` is the one to curl on
    Cloud Run; `/healthz` still works locally, in the image, and on other platforms.
    """
    settings = get_settings()
    tools = {name: shutil.which(name) is not None for name in ("pdftoppm", "tectonic", "git")}
    payload: dict[str, Any] = {
        "status": "ok",
        "renderers": settings.renderers,
        "tools": tools,
        "publish_ready": not settings.missing_for_publish(),
        "llm": _llm_health(settings),
    }
    if missing := settings.missing_for_publish():
        payload["missing_credentials"] = missing
    return JSONResponse(payload)


def _llm_health(settings: Any) -> dict[str, Any]:
    """LLM provider and key state, without the key itself.

    On the health check because a short-term Bedrock key expires within 12 hours: this is
    where you notice it is about to lapse, rather than finding out from a failed 3am run.
    """
    from pipeline.llm_key import status as key_status

    info: dict[str, Any] = {"provider": settings.llm_provider, "model": settings.resolved_model()}

    if settings.llm_provider != "bedrock":
        info["key_configured"] = bool(settings.anthropic_api_key.get_secret_value())
        return info

    info["region"] = settings.aws_region
    state = key_status(settings)
    info["key"] = state

    if not state.get("configured"):
        info["warning"] = (
            "no Bedrock key loaded — set one with scripts/set_bedrock_key.py, "
            "or POST /admin/llm-key"
        )
    elif state.get("expired"):
        info["warning"] = "the Bedrock key has expired — rotate it"
    elif isinstance(remaining := state.get("expires_in_hours"), (int, float)) and remaining < 2:
        info["warning"] = f"the Bedrock key expires in {remaining}h — rotate it soon"
    elif state.get("source") == "env":
        # The gap this closes: an env-supplied key carries no expiry and no knowable age, so
        # neither branch above can fire. /health reported a clean bill of health while the
        # deployed key was three days dead, and the failure surfaced instead as a Chinese
        # resume silently left in English. A short-term key lasts at most 12 hours, so a key
        # that arrived via a deploy is suspect by construction.
        info["warning"] = (
            "the Bedrock key came from the deploy environment, so its age and expiry are "
            "unknown — short-term keys last at most 12 hours. If a run has left the Chinese "
            "resume in English, rotate it: python scripts/set_bedrock_key.py <new-key>"
        )
    return info


@app.get("/")
async def index() -> JSONResponse:
    """Minimal service description. The interesting surfaces are /runs and /resume."""
    settings = get_settings()
    from pipeline.state import RunStore

    store = RunStore(settings)
    pending = store.pending()
    return JSONResponse(
        {
            "service": "resume-pipeline",
            "pending_runs": [
                {
                    "id": run.id,
                    "created_at": run.created_at.isoformat(),
                    "url": run.preview_url(settings.public_base_url),
                }
                for run in pending
            ],
            "downloads": {
                f"{lang}.{fmt}": f"{settings.public_base_url}/resume/{lang}.{fmt}"
                for lang in ("en", "zh")
                for fmt in ("pdf", "latex.pdf", "docx", "tex")
            },
        }
    )


# Routers are imported after `app` exists so they can reference it, and last so an
# import error in one surface doesn't hide the health check.
from web import routes_admin, routes_resume, routes_runs, slack  # noqa: E402

app.include_router(routes_runs.router)
app.include_router(routes_resume.router)
app.include_router(slack.router)
app.include_router(routes_admin.router)
