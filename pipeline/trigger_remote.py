"""Cron entrypoint. Does one thing: ask the web service to start a run.

Deliberately thin. If the cron container ran pipeline stages itself, it and the web
container would both write to `state/` — which is the git repo — and one side's commit
would be rejected. One writer, one scheduler.

Exit codes matter here: Render surfaces a non-zero exit as a failed cron job, which is
how a broken trigger becomes visible instead of silently never running.
"""

from __future__ import annotations

import logging
import sys

import httpx

from pipeline.config import get_settings
from pipeline.state import Trigger

log = logging.getLogger("trigger")


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(name)s: %(message)s")
    settings = get_settings()

    token = settings.trigger_token.get_secret_value()
    if not token:
        log.error("TRIGGER_TOKEN is empty — the web service will reject this request")
        return 1

    url = f"{settings.public_base_url}/api/runs"
    log.info("triggering scheduled run at %s", url)

    try:
        # The endpoint returns 202 immediately and renders in the background, so a short
        # timeout is correct — a slow response means the service is unhealthy, not busy.
        response = httpx.post(
            url,
            params={"trigger": Trigger.SCHEDULED.value},
            headers={"X-Trigger-Token": token},
            timeout=30.0,
        )
    except httpx.HTTPError as exc:
        log.error("could not reach the web service: %s", exc)
        return 1

    if response.status_code != 202:
        log.error("unexpected response %s: %s", response.status_code, response.text[:300])
        return 1

    payload = response.json()
    log.info("run %s accepted — %s", payload.get("run_id"), payload.get("url"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
