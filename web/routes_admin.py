"""Key rotation over HTTP, so an expiring credential doesn't need a redeploy.

Bedrock short-term API keys expire within 12 hours. On Cloud Run, changing an environment
variable means a new revision — minutes of build time for a value that expires the same
day. These endpoints let `scripts/set_bedrock_key.py` push a fresh key into the running
service instead.

Authenticated with `X-Trigger-Token`, the same shared secret the cron trigger uses. It is
already provisioned, already secret, and rotating it invalidates both surfaces together —
one fewer credential than a dedicated admin token would add.

Neither endpoint ever returns the key. Status shows the last four characters only, which
is enough to confirm *which* key is loaded without putting the value in a log or a
response body.
"""

from __future__ import annotations

import logging
import secrets
from datetime import UTC, datetime

from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from pipeline.config import get_settings
from pipeline.llm_key import LLMKeyError, set_key, status

log = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["admin"])


class KeyUpdate(BaseModel):
    key: str = Field(description="The Bedrock short-term API key (ABSK...)")
    expires_at: datetime | None = Field(
        default=None, description="When it expires, if known — used for the /healthz warning"
    )


def _authorize(token: str) -> None:
    """Reject anything without the trigger token.

    compare_digest, not `==`: a plain comparison leaks how much of the token matched
    through its timing, and this endpoint changes which credentials the service uses.
    """
    expected = get_settings().trigger_token.get_secret_value()
    if not expected or not secrets.compare_digest(token, expected):
        raise HTTPException(401, "invalid or missing X-Trigger-Token")


@router.post("/llm-key")
async def update_llm_key(
    body: KeyUpdate, x_trigger_token: str = Header(default="")
) -> JSONResponse:
    """Install a new Bedrock key for this instance.

    Scope worth understanding: this updates the instance that serves the request. With
    `--max-instances 1` (what `deploy_cloudrun.sh` sets, because concurrent instances
    would race on the git-backed state) that is the whole service. Raise max-instances and
    you would need to move the key to Secret Manager instead — noted in the README.
    """
    _authorize(x_trigger_token)
    settings = get_settings()

    try:
        stored = set_key(body.key, settings, expires_at=body.expires_at)
    except LLMKeyError as exc:
        # 400, not 500: a malformed key is the caller's input, not a server fault.
        raise HTTPException(400, str(exc)) from exc

    log.info("llm key rotated via /admin/llm-key (…%s)", stored.key[-4:])
    return JSONResponse(
        {
            "ok": True,
            "suffix": stored.key[-4:],
            "expires_at": stored.expires_at.isoformat() if stored.expires_at else None,
            "set_at": stored.set_at.isoformat(),
        }
    )


@router.get("/llm-key")
async def llm_key_status(x_trigger_token: str = Header(default="")) -> JSONResponse:
    """Which key is loaded and how long it has left. Never returns the key itself."""
    _authorize(x_trigger_token)
    settings = get_settings()
    return JSONResponse(
        {
            "provider": settings.llm_provider,
            "model": settings.resolved_model(),
            "region": settings.aws_region if settings.llm_provider == "bedrock" else None,
            "key": status(settings),
            "checked_at": datetime.now(UTC).isoformat(),
        }
    )
