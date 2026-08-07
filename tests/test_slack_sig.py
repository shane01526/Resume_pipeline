"""Slack request verification.

`/slack/interactions` is a public endpoint whose payload can publish a resume. Without
signature verification it is an unauthenticated "publish my resume" button, so these tests
cover the ways a forged or replayed request must fail.
"""

from __future__ import annotations

import hashlib
import hmac
import time

import pytest
from pydantic import SecretStr

from pipeline.config import Settings
from web.slack import MAX_TIMESTAMP_SKEW, verify_signature

SECRET = "8f742914b2b1c0e5d3a6f8091e2c4b7a"


@pytest.fixture
def settings() -> Settings:
    return Settings(slack_signing_secret=SecretStr(SECRET))


def sign(body: bytes, timestamp: str, secret: str = SECRET) -> str:
    """Produce a signature the way Slack does, so the test exercises the real format."""
    basestring = b"v0:" + timestamp.encode() + b":" + body
    return "v0=" + hmac.new(secret.encode(), basestring, hashlib.sha256).hexdigest()


@pytest.fixture
def now() -> str:
    return str(int(time.time()))


# --- the happy path ----------------------------------------------------------


def test_valid_signature_accepted(settings: Settings, now: str) -> None:
    body = b"payload=%7B%22type%22%3A%22block_actions%22%7D"
    assert verify_signature(body, now, sign(body, now), settings)


def test_body_with_unicode_verifies(settings: Settings, now: str) -> None:
    """Slack payloads carry the Chinese button labels this app uses."""
    body = "text=核准並發布".encode()
    assert verify_signature(body, now, sign(body, now), settings)


# --- forgery -----------------------------------------------------------------


def test_tampered_body_rejected(settings: Settings, now: str) -> None:
    """The attack this exists to stop: a valid signature reused with a different run id."""
    original = b"payload=%7B%22value%22%3A%22run-a%22%7D"
    signature = sign(original, now)
    forged = b"payload=%7B%22value%22%3A%22run-b%22%7D"
    assert not verify_signature(forged, now, signature, settings)


def test_wrong_secret_rejected(settings: Settings, now: str) -> None:
    body = b"text=update"
    assert not verify_signature(body, now, sign(body, now, "a-different-secret"), settings)


def test_malformed_signature_rejected(settings: Settings, now: str) -> None:
    for bogus in ("", "v0=", "not-a-signature", "v1=" + "0" * 64):
        assert not verify_signature(b"text=update", now, bogus, settings)


def test_signature_from_a_different_timestamp_rejected(settings: Settings, now: str) -> None:
    """The timestamp is inside the signed basestring, so it cannot be swapped."""
    body = b"text=update"
    signature = sign(body, str(int(now) - 60))
    assert not verify_signature(body, now, signature, settings)


# --- replay ------------------------------------------------------------------


def test_stale_timestamp_rejected(settings: Settings) -> None:
    """Without this check a captured request replays forever — the signature stays valid."""
    stale = str(int(time.time()) - MAX_TIMESTAMP_SKEW - 60)
    body = b"text=update"
    assert not verify_signature(body, stale, sign(body, stale), settings)


def test_future_timestamp_rejected(settings: Settings) -> None:
    """Clock skew is bounded in both directions."""
    future = str(int(time.time()) + MAX_TIMESTAMP_SKEW + 60)
    body = b"text=update"
    assert not verify_signature(body, future, sign(body, future), settings)


def test_recent_timestamp_within_skew_accepted(settings: Settings) -> None:
    """Ordinary network latency must not reject a legitimate request."""
    recent = str(int(time.time()) - 30)
    body = b"text=update"
    assert verify_signature(body, recent, sign(body, recent), settings)


def test_non_numeric_timestamp_rejected(settings: Settings) -> None:
    body = b"text=update"
    assert not verify_signature(body, "not-a-number", sign(body, "not-a-number"), settings)


# --- misconfiguration --------------------------------------------------------


def test_unconfigured_secret_rejects_everything() -> None:
    """Fail closed. An empty secret must not mean "accept anything"."""
    settings = Settings(slack_signing_secret=SecretStr(""))
    now = str(int(time.time()))
    body = b"text=update"
    assert not verify_signature(body, now, sign(body, now, ""), settings)


def test_missing_headers_rejected(settings: Settings, now: str) -> None:
    body = b"text=update"
    assert not verify_signature(body, "", sign(body, now), settings)
    assert not verify_signature(body, now, "", settings)
