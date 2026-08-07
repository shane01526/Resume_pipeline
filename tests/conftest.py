"""Shared fixtures, plus a guard against tests writing into the real repository.

The guard exists because it already happened: a test monkeypatched `Settings.repo_root`,
the patch silently had no effect (`get_settings()` is cached), and the suite wrote a dozen
files into `sources/` and overwrote its README. `repo_root` is a real settings field now,
but a checked-in tripwire is cheaper than remembering.
"""

from __future__ import annotations

import pytest

from pipeline.config import REPO_ROOT

# Directories a test must never modify. `state/` and `output/` are the pipeline's durable
# storage — the diff baseline lives there — and `sources/` is the user's document archive.
PROTECTED = ("sources", "state", "output")


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
