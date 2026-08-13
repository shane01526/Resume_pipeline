"""Bedrock key rotation: storage, validation, and the safety guard.

The key rotates on a sub-daily cadence, so these paths run far more often than most of
the pipeline. Two properties matter most and are asserted directly: the key never appears
in a status payload, and it can never be written where the repo would publish it.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import SecretStr

from pipeline.config import Settings
from pipeline.llm_key import (
    KEY_PREFIXES,
    LLMKeyError,
    clear,
    current_key,
    require_key,
    set_key,
    status,
)

# A realistically-shaped key. AWS issues `bedrock-api-key-` prefixed values (verified
# against a real one) — an ABSK-only check would reject the console's own output.
VALID_KEY = "bedrock-api-key-YmVkcm9jay1leGFtcGxlLWtleQ==TAIL"


@pytest.fixture(autouse=True)
def _isolated_key_state():
    """Drop the process-wide override between tests."""
    clear()
    yield
    clear()


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    repo = tmp_path / "repo"
    repo.mkdir()
    return Settings(
        repo_root=repo,
        bedrock_key_file=tmp_path / "bedrock_key.json",
        llm_provider="bedrock",
    )


# --- the two properties that matter most -------------------------------------


def test_status_never_returns_the_key(settings: Settings) -> None:
    """Status goes into logs and HTTP responses — the key must not ride along."""
    set_key(VALID_KEY, settings)
    payload = json.dumps(status(settings), default=str)

    assert VALID_KEY not in payload
    # The suffix is intentional: enough to tell which key is loaded, not enough to use.
    assert status(settings)["suffix"] == VALID_KEY[-4:]


def test_key_file_inside_the_repo_is_refused(tmp_path: Path) -> None:
    """state/ and output/ are committed to a public repo.

    A key cached there would be pushed by the next publish, so this is a hard error
    rather than a warning — and nothing is written before it raises.
    """
    repo = tmp_path / "repo"
    (repo / "state").mkdir(parents=True)
    target = repo / "state" / "key.json"
    settings = Settings(repo_root=repo, bedrock_key_file=target)

    with pytest.raises(LLMKeyError, match="inside the repository"):
        set_key(VALID_KEY, settings)
    assert not target.exists()


def test_key_file_outside_the_repo_is_allowed(tmp_path: Path, settings: Settings) -> None:
    set_key(VALID_KEY, settings)
    assert settings.bedrock_key_file.is_file()


# --- validation ---------------------------------------------------------------


@pytest.mark.parametrize("prefix", KEY_PREFIXES)
def test_both_documented_prefixes_accepted(prefix: str, settings: Settings) -> None:
    """`bedrock-api-key-` is what the console issues; `ABSK` appears on short-term keys."""
    set_key(f"{prefix}abc123XYZW", settings)
    assert status(settings)["configured"] is True


@pytest.mark.parametrize(
    ("wrong_key", "expected"),
    [
        ("sk-ant-api03-abc", "first-party"),
        ("AKIAIOSFODNN7EXAMPLE", "access key ID"),
        ("ASIAIOSFODNN7EXAMPLE", "access key ID"),
        ("ghp_abcdefghijklmnop", "GitHub"),
        ("github_pat_abcdefghij", "GitHub"),
        ("xoxb-1111-2222-abcd", "Slack"),
        ("ntn_abcdefghijklmnop", "Notion"),
    ],
)
def test_wrong_credential_types_name_the_mistake(
    wrong_key: str, expected: str, settings: Settings
) -> None:
    """Every other credential in this project is a plausible paste-o.

    Naming the specific mistake is much faster to act on than "invalid format".
    """
    with pytest.raises(LLMKeyError, match=expected):
        set_key(wrong_key, settings)


def test_empty_key_rejected(settings: Settings) -> None:
    with pytest.raises(LLMKeyError, match="empty"):
        set_key("   ", settings)


def test_unrecognized_key_names_where_to_get_one(settings: Settings) -> None:
    with pytest.raises(LLMKeyError, match="Amazon Bedrock"):
        set_key("some-random-string", settings)


def test_surrounding_whitespace_is_stripped(settings: Settings) -> None:
    """Keys arrive via copy-paste and shell pipes; a stray newline must not break auth."""
    set_key(f"  {VALID_KEY}\n", settings)
    assert require_key(settings) == VALID_KEY


# --- persistence --------------------------------------------------------------


def test_key_survives_a_process_restart(settings: Settings) -> None:
    """The cache file is why a container restart doesn't need a manual re-rotation."""
    set_key(VALID_KEY, settings)
    clear()  # simulates a fresh process

    restored = current_key(settings, use_env=False)
    assert restored is not None
    assert restored.key == VALID_KEY


def test_unreadable_cache_is_ignored_not_fatal(settings: Settings) -> None:
    """A corrupt cache should degrade to "no key", not crash the service at startup."""
    settings.bedrock_key_file.write_text("{ not json", encoding="utf-8")
    assert current_key(settings, use_env=False) is None


def test_missing_key_message_names_the_command(settings: Settings) -> None:
    with pytest.raises(LLMKeyError, match="set_bedrock_key.py"):
        require_key(settings, use_env=False)


def test_no_key_reports_unconfigured(settings: Settings) -> None:
    """`use_env=False` matters here: a developer machine may export the real key."""
    assert status(settings, use_env=False) == {"configured": False}


# --- expiry -------------------------------------------------------------------


def test_expired_key_is_refused_with_the_rotation_command(settings: Settings) -> None:
    set_key(VALID_KEY, settings, expires_at=datetime.now(UTC) - timedelta(minutes=1))
    with pytest.raises(LLMKeyError, match="expired"):
        require_key(settings)


def test_live_key_reports_remaining_hours(settings: Settings) -> None:
    set_key(VALID_KEY, settings, expires_at=datetime.now(UTC) + timedelta(hours=12))
    state = status(settings)
    assert state["expired"] is False
    assert 11 < state["expires_in_hours"] <= 12


def test_key_without_an_expiry_is_treated_as_live(settings: Settings) -> None:
    """Real lifetime is the lesser of the request and the underlying credential.

    With no expiry recorded, attempting the call and surfacing a genuine 401 beats
    refusing a key that might still work.
    """
    set_key(VALID_KEY, settings)
    assert require_key(settings) == VALID_KEY
    state = status(settings)
    assert state["expiry_known"] is False
    assert "assumed_expiry_at" in state


# --- provider wiring ----------------------------------------------------------


def test_bedrock_model_id_gets_the_provider_prefix() -> None:
    """Bedrock rejects a bare ID; the first-party API rejects a prefixed one."""
    assert Settings(llm_provider="bedrock", llm_model="claude-opus-5").resolved_model() == (
        "anthropic.claude-opus-5"
    )
    assert Settings(llm_provider="anthropic", llm_model="claude-opus-5").resolved_model() == (
        "claude-opus-5"
    )


def test_model_id_resolution_is_idempotent() -> None:
    """Config may already carry a prefix; resolving twice must not double it."""
    assert (
        Settings(llm_provider="bedrock", llm_model="anthropic.claude-opus-5").resolved_model()
        == "anthropic.claude-opus-5"
    )
    assert (
        Settings(llm_provider="anthropic", llm_model="anthropic.claude-opus-5").resolved_model()
        == "claude-opus-5"
    )


def test_bedrock_client_uses_the_mantle_endpoint(settings: Settings) -> None:
    """Mantle is the Messages-API endpoint; the legacy InvokeModel path takes a
    different request shape and would break `structured()`."""
    from pipeline.llm import _client

    set_key(VALID_KEY, settings)
    client = _client(settings)

    assert type(client).__name__ == "AsyncAnthropicBedrockMantle"
    assert "bedrock-mantle" in str(client.base_url)
    assert settings.aws_region in str(client.base_url)
    # Passing api_key selects API-key auth over SigV4 in the SDK's resolver.
    assert client.api_key == VALID_KEY


def test_missing_bedrock_key_fails_with_guidance(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The env var must be cleared explicitly.

    `_client` intentionally falls back to AWS_BEARER_TOKEN_BEDROCK so a key exported
    after startup works without a restart — which means a developer machine that has one
    exported would find it and this case would be unreachable.
    """
    from pipeline.llm import LLMError, _client

    monkeypatch.delenv("AWS_BEARER_TOKEN_BEDROCK", raising=False)
    monkeypatch.delenv("BEDROCK_API_KEY", raising=False)
    no_key = settings.model_copy(update={"bedrock_api_key": SecretStr("")})

    with pytest.raises(LLMError, match="set_bedrock_key.py"):
        _client(no_key)


# --- error messaging ----------------------------------------------------------


class _HTTPError(Exception):
    def __init__(self, status_code: int) -> None:
        super().__init__(f"HTTP {status_code}")
        self.status_code = status_code


@pytest.mark.parametrize("code", [401, 403])
def test_auth_failure_explains_expiry_and_the_fix(code: int, settings: Settings) -> None:
    """An expired short-term key surfaces as a bare 401 mid-run.

    Without this, "401 Unauthorized" reads as a misconfiguration rather than a routine
    rotation — the wrong diagnosis to reach for at 3am.
    """
    from pipeline.llm import _call_failure_message

    set_key(VALID_KEY, settings)
    message = _call_failure_message("anthropic.claude-opus-5", settings, _HTTPError(code))

    assert "expired" in message
    assert "set_bedrock_key.py" in message
    assert VALID_KEY not in message  # only the suffix may appear


def test_non_auth_failures_stay_plain(settings: Settings) -> None:
    """A 500 is not a key problem; suggesting rotation would misdirect."""
    from pipeline.llm import _call_failure_message

    message = _call_failure_message("anthropic.claude-opus-5", settings, _HTTPError(500))
    assert "set_bedrock_key.py" not in message


def test_auth_failure_on_first_party_stays_plain() -> None:
    """The rotation hint is Bedrock-specific — a first-party key doesn't expire this way."""
    from pipeline.llm import _call_failure_message

    settings = Settings(llm_provider="anthropic")
    message = _call_failure_message("claude-opus-5", settings, _HTTPError(401))
    assert "set_bedrock_key.py" not in message


# --- honest provenance --------------------------------------------------------


def test_env_supplied_key_reports_no_age(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """An env key's age is unknowable, so status must not invent one.

    A real run failed with "It most likely expired ... and the loaded one is 0.0h old" —
    self-contradictory, and it sends you looking at the wrong thing. 0.0h was the time since
    the *process read* the variable; the key itself was three days old, seeded by a deploy.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setenv("BEDROCK_API_KEY", VALID_KEY)
    settings = Settings(
        repo_root=repo,
        bedrock_key_file=tmp_path / "absent.json",
        llm_provider="bedrock",
        bedrock_api_key=SecretStr(VALID_KEY),
    )

    payload = status(settings)

    assert payload["configured"] is True
    assert payload["source"] == "env"
    assert payload["age_hours"] is None
    assert "unknown" in str(payload["age_note"])
    assert VALID_KEY not in json.dumps(payload, default=str)


def test_explicitly_set_key_does_report_an_age(settings: Settings) -> None:
    """set_key() records a real timestamp, so the age there is meaningful."""
    set_key(VALID_KEY, settings)

    payload = status(settings)

    assert payload["source"] in {"set", "cache"}
    assert isinstance(payload["age_hours"], float)


def test_401_message_does_not_claim_an_age_it_cannot_know(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The 401 text is what someone reads when a scheduled run fails."""
    from pipeline.llm import _call_failure_message

    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setenv("BEDROCK_API_KEY", VALID_KEY)
    settings = Settings(
        repo_root=repo,
        bedrock_key_file=tmp_path / "absent.json",
        llm_provider="bedrock",
        bedrock_api_key=SecretStr(VALID_KEY),
    )

    message = _call_failure_message("anthropic.claude-opus-5", settings, _HTTPError(401))

    assert "0.0h old" not in message
    assert "real age is unknown" in message
    assert "set_bedrock_key.py" in message
    assert VALID_KEY not in message
