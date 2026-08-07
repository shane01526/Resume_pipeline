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
async def healthz() -> JSONResponse:
    """Render's health check.

    Reports degraded capabilities without failing: a missing Slack token shouldn't take
    the service down, but it should be visible. Only a broken config object is fatal.
    """
    settings = get_settings()
    tools = {name: shutil.which(name) is not None for name in ("pdftoppm", "tectonic", "git")}
    payload: dict[str, Any] = {
        "status": "ok",
        "renderers": settings.renderers,
        "tools": tools,
        "publish_ready": not settings.missing_for_publish(),
    }
    if missing := settings.missing_for_publish():
        payload["missing_credentials"] = missing
    return JSONResponse(payload)


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
                for fmt in ("pdf", "latex.pdf", "docx")
            },
        }
    )


# Routers are imported after `app` exists so they can reference it, and last so an
# import error in one surface doesn't hide the health check.
from web import routes_resume, routes_runs, slack  # noqa: E402

app.include_router(routes_runs.router)
app.include_router(routes_resume.router)
app.include_router(slack.router)
