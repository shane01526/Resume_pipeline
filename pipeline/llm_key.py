"""Current Bedrock API key, and the plumbing to replace it without a redeploy.

Bedrock short-term API keys expire — up to 12 hours, and sooner if the underlying
credential does. That makes the key a *rotating* value, not deployment configuration, so
it can't just live in an environment variable: updating one on Cloud Run means a new
revision, and the key would expire again before the day is out.

Resolution order, first hit wins:

1. An in-process override, set by `set_key()` — what the update endpoint writes.
2. The cache file (`BEDROCK_KEY_FILE`), so a container restart doesn't lose the key.
3. `BEDROCK_API_KEY` / `AWS_BEARER_TOKEN_BEDROCK` from the environment.

The file is the reason a restart survives: Cloud Run's disk is ephemeral per instance,
but an instance restart within the same revision keeps `/tmp`. Losing it is not fatal —
you re-run the update command.

**The cache file must never be inside `state/` or `output/`.** Those are committed to a
public repo (see `pipeline/storage.py`); a key written there would be published. The
default lives in `/tmp` for exactly that reason, and `_validate_key_path` refuses a path
under the repo root.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pipeline.config import Settings

log = logging.getLogger(__name__)

# Prefixes AWS has used for Bedrock API keys. Both are accepted: long-term keys are
# `bedrock-api-key-...` (that is what the console hands you today), and `ABSK...` appears
# on short-term keys. Verified against a real key rather than assumed — an ABSK-only check
# rejects the console's own output.
#
# The point of checking at all is to catch the *wrong kind* of credential — an AWS access
# key ID (`AKIA...`) or a first-party `sk-ant-...` key — at the moment it is pasted, rather
# than as a 401 during a run.
KEY_PREFIXES = ("bedrock-api-key-", "ABSK")

# Kept for callers that display an example prefix.
KEY_PREFIX = KEY_PREFIXES[0]

# Credential prefixes that are definitely not Bedrock API keys, with the likely mistake.
_WRONG_CREDENTIALS = {
    "sk-ant-": "that is a first-party Anthropic API key - set LLM_PROVIDER=anthropic to use it",
    "AKIA": "that is an AWS access key ID, not a Bedrock API key",
    "ASIA": "that is a temporary AWS access key ID, not a Bedrock API key",
    "ghp_": "that is a GitHub token",
    "github_pat_": "that is a GitHub token",
    "xoxb-": "that is a Slack bot token",
    "ntn_": "that is a Notion integration token",
}

# AWS caps short-term keys at 12 hours. Used only to *display* a best-guess expiry when
# the key was set without one — never to reject a key, since the real lifetime is the
# lesser of the requested duration and the underlying credential's.
MAX_KEY_LIFETIME = timedelta(hours=12)

_lock = threading.Lock()
_override: _StoredKey | None = None


@dataclass(frozen=True, slots=True)
class _StoredKey:
    key: str
    set_at: datetime
    expires_at: datetime | None
    # Where the key came from, because it decides whether `set_at` means anything. For an
    # env-supplied key it is just the moment we happened to read it, so reporting an age
    # from it is worse than reporting nothing: a run failed with
    #   "It most likely expired ... and the loaded one is 0.0h old"
    # which reads as self-contradictory and sends you looking in the wrong place. The key
    # was in fact three days old, minted before the deploy that seeded it.
    source: str = "set"

    def age(self) -> timedelta:
        return datetime.now(UTC) - self.set_at

    def is_expired(self) -> bool:
        """Whether the key is past its stated expiry.

        Only meaningful when an expiry was supplied; a key with none is treated as live
        until Bedrock says otherwise. Better to attempt the call and surface a real 401
        than to refuse a key that might still work.
        """
        return self.expires_at is not None and datetime.now(UTC) >= self.expires_at


class LLMKeyError(RuntimeError):
    """The key is missing, malformed, or unwritable."""


def _validate_key_path(path: Path, settings: Settings) -> Path:
    """Refuse a cache path inside the repository.

    `state/` and `output/` are committed to a public repo. A key cached there would be
    pushed on the next publish, so this is a hard error rather than a warning.
    """
    resolved = path.expanduser().resolve()
    try:
        resolved.relative_to(settings.repo_root.resolve())
    except ValueError:
        return resolved  # outside the repo — fine
    raise LLMKeyError(
        f"BEDROCK_KEY_FILE ({resolved}) is inside the repository. state/ and output/ are "
        "committed to a public repo, so a key stored there would be published. "
        "Use a path outside the repo, such as /tmp/bedrock_key.json."
    )


def set_key(key: str, settings: Settings, *, expires_at: datetime | None = None) -> _StoredKey:
    """Install a new key for this process and cache it for restarts.

    Returns the stored record. Raises `LLMKeyError` on a malformed key — validating here
    means a typo is caught by the update command, not by a run at 3am.
    """
    key = key.strip()
    if not key:
        raise LLMKeyError("key is empty")

    # Name the specific mistake where the prefix identifies it — much faster to act on
    # than a generic "wrong format".
    for prefix, explanation in _WRONG_CREDENTIALS.items():
        if key.startswith(prefix):
            raise LLMKeyError(f"{explanation}. Bedrock keys start with {KEY_PREFIXES[0]!r}.")

    if not key.startswith(KEY_PREFIXES):
        raise LLMKeyError(
            f"that does not look like a Bedrock API key. Expected it to start with one of "
            f"{list(KEY_PREFIXES)}, got {key[:12]!r}...\n"
            "Generate one in the AWS console: Amazon Bedrock -> API keys."
        )

    now = datetime.now(UTC)
    stored = _StoredKey(key=key, set_at=now, expires_at=expires_at)

    global _override
    with _lock:
        _override = stored

    # Cache write is best-effort: the key is already usable in-process, and a read-only
    # filesystem shouldn't turn a successful rotation into a failure.
    try:
        path = _validate_key_path(settings.bedrock_key_file, settings)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "key": key,
            "set_at": now.isoformat(),
            "expires_at": expires_at.isoformat() if expires_at else None,
        }
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        # 0600 before the rename, so the key is never briefly world-readable.
        tmp.chmod(0o600)
        tmp.replace(path)
    except LLMKeyError:
        raise
    except OSError as exc:
        log.warning("could not cache the key to disk (it is still active in memory): %s", exc)

    log.info(
        "installed a new Bedrock key (…%s)%s",
        key[-4:],
        f", expires {expires_at:%Y-%m-%d %H:%M} UTC" if expires_at else "",
    )
    return stored


def _load_cached(settings: Settings) -> _StoredKey | None:
    try:
        path = _validate_key_path(settings.bedrock_key_file, settings)
    except LLMKeyError as exc:
        log.error("%s", exc)
        return None

    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        expires_raw = data.get("expires_at")
        return _StoredKey(
            key=data["key"],
            set_at=datetime.fromisoformat(data["set_at"]),
            expires_at=datetime.fromisoformat(expires_raw) if expires_raw else None,
            # A real set time, recorded when set_key() wrote the file, so the age is real.
            source="cache",
        )
    except (OSError, ValueError, KeyError) as exc:
        log.warning("ignoring unreadable key cache %s: %s", path, exc)
        return None


def current_key(settings: Settings, *, use_env: bool = True) -> _StoredKey | None:
    """The key in effect, or None if none is configured anywhere.

    `use_env=False` skips the environment tier — tests need it, because a developer
    machine with `AWS_BEARER_TOKEN_BEDROCK` exported would otherwise make a
    "no key configured" case impossible to construct.
    """
    global _override
    with _lock:
        if _override is not None:
            return _override

    if cached := _load_cached(settings):
        with _lock:
            _override = cached
        return cached

    if not use_env:
        return None

    # Settings already reads BEDROCK_API_KEY / AWS_BEARER_TOKEN_BEDROCK; check the raw
    # environment too so a key exported after startup is picked up without a restart.
    env_key = settings.bedrock_api_key.get_secret_value() or os.environ.get(
        "AWS_BEARER_TOKEN_BEDROCK", ""
    )
    if env_key:
        # Neither the expiry nor the age is knowable here: the value was set out of band,
        # possibly by a deploy days ago. set_at is the read time, flagged as such by source.
        return _StoredKey(
            key=env_key.strip(), set_at=datetime.now(UTC), expires_at=None, source="env"
        )

    return None


def require_key(settings: Settings, *, use_env: bool = True) -> str:
    """The current key, or a message that says exactly how to set one."""
    stored = current_key(settings, use_env=use_env)
    if stored is None:
        raise LLMKeyError(
            "no Bedrock API key configured. Set one with:\n"
            "  python scripts/set_bedrock_key.py <ABSK...>\n"
            "or POST it to /admin/llm-key on the deployed service."
        )
    if stored.is_expired():
        raise LLMKeyError(
            f"the Bedrock API key expired at {stored.expires_at:%Y-%m-%d %H:%M} UTC. "
            "Mint a new one and run: python scripts/set_bedrock_key.py <ABSK...>"
        )
    return stored.key


def status(settings: Settings, *, use_env: bool = True) -> dict[str, object]:
    """Non-secret key status, for /healthz and the update command.

    Never returns the key — only its last four characters, so you can tell *which* key is
    loaded without the value leaking into a log or an HTTP response.
    """
    stored = current_key(settings, use_env=use_env)
    if stored is None:
        return {"configured": False}

    out: dict[str, object] = {
        "configured": True,
        "suffix": stored.key[-4:],
        "source": stored.source,
    }
    if stored.source == "env":
        # An env key's age is unknown — it may predate the container by days.
        out["age_hours"] = None
        out["age_note"] = "set via environment at deploy time; true age unknown"
    else:
        out["age_hours"] = round(stored.age().total_seconds() / 3600, 1)
    if stored.expires_at:
        remaining = stored.expires_at - datetime.now(UTC)
        out["expires_at"] = stored.expires_at.isoformat()
        out["expired"] = remaining.total_seconds() <= 0
        out["expires_in_hours"] = round(remaining.total_seconds() / 3600, 1)
    else:
        # No expiry was recorded; show the AWS ceiling as a hint, flagged as a guess.
        out["assumed_expiry_at"] = (stored.set_at + MAX_KEY_LIFETIME).isoformat()
        out["expiry_known"] = False
    return out


def clear() -> None:
    """Drop the in-process override. For tests."""
    global _override
    with _lock:
        _override = None
