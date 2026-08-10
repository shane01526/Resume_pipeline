"""Shared fixtures, plus three guards that keep the suite honest about its environment.

The repo-write guard exists because it already happened: a test monkeypatched
`Settings.repo_root`, the patch silently had no effect (`get_settings()` is cached), and
the suite wrote a dozen files into `sources/` and overwrote its README. `repo_root` is a
real settings field now, but a checked-in tripwire is cheaper than remembering.

The `.env` isolation guard is the same class of bug seen from the other side: `Settings`
reads `.env` by default, so once a developer creates one, "this credential is absent"
becomes impossible to express in a test. That failure is silent and environment-dependent
— the suite passes on CI and fails on the machine that just configured a deployment.

The no-network guard is the third instance of the same lesson, and the most expensive:
the translate tests were written to exercise the *passthrough* path, on the assumption
that no key would be present. Once `AWS_BEARER_TOKEN_BEDROCK` was exported in the
developer's shell, they started making real, paid Bedrock calls — the suite went from 23
seconds to 340, and two tests failed because the model translated text they expected to
come back untouched. Disabling the `.env` file was not enough: the key also arrives from
the raw environment and from the `/tmp` key cache. So this blocks the SDK's transport
directly. A test that wants to exercise a call substitutes a fake client.
"""

from __future__ import annotations

import pytest

from pipeline.config import REPO_ROOT, Settings

# Directories a test must never modify. `state/` and `output/` are the pipeline's durable
# storage — the diff baseline lives there — and `sources/` is the user's document archive.
PROTECTED = ("sources", "state", "output")


@pytest.fixture(autouse=True)
def _isolate_settings_from_dotenv(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stop `Settings()` reading the developer's real `.env` during tests.

    Without this, a test asserting "no GITHUB_TOKEN raises" passes on a clean checkout and
    fails as soon as someone creates a `.env` with placeholder values — the credential is
    non-empty, so the guard it is testing never fires. Tests that *want* a credential pass
    it explicitly.
    """
    # pydantic-settings resolves `_env_file` per instantiation, so defaulting it to None
    # disables the file source without touching the real `.env` on disk.
    original_init = Settings.__init__

    def _init_without_dotenv(self: Settings, **kwargs: object) -> None:
        kwargs.setdefault("_env_file", None)  # type: ignore[arg-type]
        original_init(self, **kwargs)

    monkeypatch.setattr(Settings, "__init__", _init_without_dotenv)


@pytest.fixture(autouse=True)
def _block_real_llm_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail, loudly and instantly, if a test reaches Bedrock or the Anthropic API.

    Patched at the SDK's transport method rather than at `pipeline.llm.structured`, because
    the leak came in through paths that never touch `structured()` — `extract.py` builds
    its own request for PDFs, and `_client()` finds a key in the environment or the `/tmp`
    cache even with `.env` disabled.

    Not a mock returning canned data: a test that silently gets a fake translation is how
    the original problem hid. This raises, so the call site is named in the traceback.
    """
    try:
        from anthropic import _base_client
    except ImportError:  # pragma: no cover - anthropic is a hard dependency
        return

    def refuse(*args: object, **kwargs: object) -> object:
        raise AssertionError(
            "a test tried to call the Claude API for real (this costs money and is slow).\n"
            "Substitute a fake client, or assert on the request via "
            "pipeline.llm.schema_kwargs() / parse_response() instead."
        )

    monkeypatch.setattr(_base_client.SyncAPIClient, "request", refuse)
    monkeypatch.setattr(_base_client.AsyncAPIClient, "request", refuse)


@pytest.fixture(autouse=True)
def _guard_repo_directories() -> object:
    """Fail any test that adds, removes, or edits a file under a protected directory.

    Autouse, so no test has to opt in. Compares a name+size+mtime snapshot rather than
    hashing: fast enough to run per-test, and precise enough to catch a truncation or an
    overwrite.
    """
    before = _snapshot()
    yield
    after = _snapshot()

    if before != after:
        added = sorted(set(after) - set(before))
        removed = sorted(set(before) - set(after))
        modified = sorted(k for k in set(before) & set(after) if before[k] != after[k])
        raise AssertionError(
            "a test modified the real repository — use Settings(repo_root=tmp_path)\n"
            f"  added:    {added}\n"
            f"  removed:  {removed}\n"
            f"  modified: {modified}"
        )


def _snapshot() -> dict[str, tuple[int, int]]:
    snapshot: dict[str, tuple[int, int]] = {}
    for directory in PROTECTED:
        root = REPO_ROOT / directory
        if not root.is_dir():
            continue
        for path in root.rglob("*"):
            if path.is_file():
                stat = path.stat()
                snapshot[path.relative_to(REPO_ROOT).as_posix()] = (
                    stat.st_size,
                    stat.st_mtime_ns,
                )
    return snapshot
