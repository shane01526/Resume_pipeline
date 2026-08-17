"""Slack surface: the /resume slash command and the approve/reject buttons.

Two hard requirements from Slack, both of which shape this module:

1. **Verify every request.** Slack signs each one; an unverified endpoint is a public
   "publish my resume" button. `verify_signature` is applied before anything else reads
   the body.
2. **Acknowledge within 3 seconds.** Slack retries anything slower, which would trigger
   duplicate runs. Every handler returns immediately and pushes work to a background
   task.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from typing import Any
from urllib.parse import parse_qs

import httpx
from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from fastapi.responses import JSONResponse

from pipeline.config import Settings, get_settings
from pipeline.state import RunStatus, RunStore, Trigger

log = logging.getLogger(__name__)

router = APIRouter(prefix="/slack", tags=["slack"])

SLACK_API = "https://slack.com/api"
# Slack's own recommendation: reject anything older than five minutes to bound replay.
MAX_TIMESTAMP_SKEW = 60 * 5


def verify_signature(body: bytes, timestamp: str, signature: str, settings: Settings) -> bool:
    """Verify Slack's v0 request signature.

    The timestamp check is not optional: without it a captured request could be replayed
    indefinitely, and the signature would still validate.
    """
    secret = settings.slack_signing_secret.get_secret_value()
    if not (secret and timestamp and signature):
        return False

    try:
        if abs(time.time() - int(timestamp)) > MAX_TIMESTAMP_SKEW:
            log.warning("rejecting slack request with stale timestamp %s", timestamp)
            return False
    except ValueError:
        return False

    basestring = b"v0:" + timestamp.encode() + b":" + body
    expected = "v0=" + hmac.new(secret.encode(), basestring, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


async def _verified_body(request: Request, settings: Settings) -> bytes:
    """Read the raw body and verify it, or raise 401.

    Raw bytes matter — re-serializing the parsed form would change them and break the MAC.
    """
    body = await request.body()
    if not verify_signature(
        body,
        request.headers.get("X-Slack-Request-Timestamp", ""),
        request.headers.get("X-Slack-Signature", ""),
        settings,
    ):
        raise HTTPException(401, "invalid slack signature")
    return body


# --- slash command ----------------------------------------------------------


@router.post("/commands")
async def slash_command(request: Request, background: BackgroundTasks) -> JSONResponse:
    """`/resume update | status | latest`."""
    settings = get_settings()
    body = await _verified_body(request, settings)
    form = {key: values[0] for key, values in parse_qs(body.decode()).items()}
    subcommand = (
        (form.get("text") or "").strip().split()[0].lower() if form.get("text") else "status"
    )

    if subcommand == "update":
        background.add_task(_start_run, settings)
        # Deliberately does not promise a preview. A run with no changes never produces one,
        # and the old wording ("完成後會通知你預覽") made `/resume update` look broken every
        # time nothing had changed. Both outcomes now post a message — see
        # pipeline/runner.py's no-change branch.
        return _in_channel("🔄 已開始更新履歷。有變更會送審核連結，沒變更也會回報。")

    if subcommand == "latest":
        return _in_channel(_latest_links(settings))

    if subcommand == "status":
        return _in_channel(_status_text(settings))

    return _in_channel(
        f"未知的指令 `{subcommand}`。可用：`/resume update`、`/resume status`、`/resume latest`"
    )


def _in_channel(text: str) -> JSONResponse:
    """A reply the whole channel sees, including the command that triggered it.

    `in_channel` rather than `ephemeral` because Slack only echoes the user's slash command
    into the channel for in-channel responses. With an ephemeral reply there is no record at
    all: the answer is visible to one person, vanishes on reload, and nothing shows that the
    command was ever run — which reads as "the command did nothing".
    """
    return JSONResponse({"response_type": "in_channel", "text": text})


async def _start_run(settings: Settings) -> None:
    """Create a manual run, reusing the HTTP trigger so both paths behave identically."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            response = await client.post(
                f"{settings.public_base_url}/api/runs",
                params={"trigger": Trigger.MANUAL.value},
                headers={"X-Trigger-Token": settings.trigger_token.get_secret_value()},
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            log.error("failed to start run from slack: %s", exc)
            await post_message(settings, f"⚠️ 無法啟動更新：{exc}")


def _status_text(settings: Settings) -> str:
    store = RunStore(settings)
    runs = store.list_runs(limit=5)
    if not runs:
        return "還沒有任何執行紀錄。用 `/resume update` 開始第一次。"

    lines = []
    for run in runs:
        marker = "⏳" if run.status is RunStatus.PENDING_APPROVAL else "•"
        line = f"{marker} `{run.id}` {run.status.value}"
        if run.counts.total:
            line += f" — {run.counts.summary()}"
        if run.status is RunStatus.PENDING_APPROVAL:
            line += f"\n   <{run.preview_url(settings.public_base_url)}|檢視 diff>"
        lines.append(line)
    return "\n".join(lines)


def _latest_links(settings: Settings) -> str:
    base = settings.public_base_url
    published = [
        (label, f"{base}/resume/{lang}.{fmt}")
        for lang, lang_label in (("en", "英文"), ("zh", "中文"))
        for fmt, fmt_label in (("pdf", "PDF"), ("latex.pdf", "PDF (LaTeX)"), ("docx", "Word"))
        for label in (f"{lang_label} {fmt_label}",)
        if (settings.output_dir / lang / _artifact_name(fmt)).is_file()
    ]
    if not published:
        return "還沒有已發布的履歷。核准一次執行後就會出現下載連結。"
    return "最新履歷：\n" + "\n".join(f"• <{url}|{label}>" for label, url in published)


def _artifact_name(fmt: str) -> str:
    return {"pdf": "resume.pdf", "latex.pdf": "resume.latex.pdf", "docx": "resume.docx"}[fmt]


# --- interactive buttons ----------------------------------------------------


@router.post("/interactions")
async def interactions(request: Request, background: BackgroundTasks) -> JSONResponse:
    """Approve / reject taps from the notification message."""
    settings = get_settings()
    body = await _verified_body(request, settings)
    form = {key: values[0] for key, values in parse_qs(body.decode()).items()}

    try:
        payload: dict[str, Any] = json.loads(form["payload"])
    except (KeyError, ValueError) as exc:
        raise HTTPException(400, "malformed interaction payload") from exc

    actions = payload.get("actions") or []
    if not actions:
        return JSONResponse({"ok": True})

    action_id = actions[0].get("action_id", "")
    run_id = actions[0].get("value", "")
    if action_id not in ("approve_run", "reject_run") or not run_id:
        return JSONResponse({"ok": True})

    store = RunStore(settings)
    run = store.load(run_id)
    if run is None:
        return JSONResponse({"text": f"找不到執行紀錄 `{run_id}`", "replace_original": False})

    if run.status is not RunStatus.PENDING_APPROVAL:
        # Idempotent: a double-tap must not publish twice.
        return JSONResponse(
            {
                "text": f"`{run_id}` 已經是 {run.status.value}，無需再處理。",
                "replace_original": False,
            }
        )

    if action_id == "approve_run":
        from pipeline.publish import publish_run

        background.add_task(publish_run, run_id, "slack")
        text = f"✅ 已核准 `{run_id}`，正在發布…"
    else:
        run.status = RunStatus.REJECTED
        run.decided_by = "slack"
        store.save(run)
        store.discard(run_id)
        text = f"🚫 已駁回 `{run_id}`，產出已丟棄。"

    # replace_original swaps the buttons out, so the decision is visible and the message
    # cannot be acted on twice.
    return JSONResponse({"text": text, "replace_original": True})


# --- outbound ---------------------------------------------------------------


async def post_message(settings: Settings, text: str, blocks: list[dict] | None = None) -> None:
    """Send a message to the configured DM channel. Never raises."""
    token = settings.slack_bot_token.get_secret_value()
    if not (token and settings.slack_dm_channel):
        log.warning("slack not configured — skipping message: %s", text[:80])
        return

    payload: dict[str, Any] = {"channel": settings.slack_dm_channel, "text": text}
    if blocks:
        payload["blocks"] = blocks

    async with httpx.AsyncClient(timeout=20.0) as client:
        try:
            response = await client.post(
                f"{SLACK_API}/chat.postMessage",
                headers={"Authorization": f"Bearer {token}"},
                json=payload,
            )
            data = response.json()
            if not data.get("ok"):
                # Slack reports failures in the body with HTTP 200, so status alone is
                # not enough to know the message landed.
                log.error("slack rejected the message: %s", data.get("error"))
        except httpx.HTTPError as exc:
            log.error("could not reach slack: %s", exc)
