"""Packaging consistency: requirements.txt must not drift from pyproject.toml.

The Dockerfile installs from requirements.txt so the dependency layer caches independently
of the source (setuptools can't build the package before the code is copied). That leaves
two lists of the same thing, and a dependency added to only one of them produces an
ImportError at runtime in the container — long after the build looked fine.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
PYPROJECT = REPO_ROOT / "pyproject.toml"
REQUIREMENTS = REPO_ROOT / "requirements.txt"


def declared_dependencies() -> set[str]:
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    return set(data["project"]["dependencies"])


def pinned_requirements() -> set[str]:
    lines = REQUIREMENTS.read_text(encoding="utf-8").splitlines()
    return {line.strip() for line in lines if line.strip() and not line.startswith("#")}


def test_requirements_match_pyproject() -> None:
    """Identical sets, so the container installs exactly what the package declares."""
    declared, pinned = declared_dependencies(), pinned_requirements()

    assert declared == pinned, (
        "requirements.txt has drifted from pyproject.toml\n"
        f"  only in pyproject.toml: {sorted(declared - pinned)}\n"
        f"  only in requirements.txt: {sorted(pinned - declared)}"
    )


def test_dockerfile_installs_from_requirements() -> None:
    """Guards the reason this file exists.

    Reverting to `pip install .` before the COPY of the source reintroduces the
    "package directory 'pipeline' does not exist" build failure.
    """
    dockerfile = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "pip install -r requirements.txt" in dockerfile

    # The editable install of the package itself must come after the source is copied.
    install_index = dockerfile.index("pip install --no-deps -e .")
    copy_index = dockerfile.index("COPY pipeline/")
    assert copy_index < install_index, "the package is installed before its source is copied"


def test_every_setting_is_documented() -> None:
    """Every env var the code reads must appear in .env.example.

    Otherwise a field added to Settings is invisible to whoever deploys this — it silently
    takes its default, and the failure shows up as wrong behaviour rather than a missing
    variable. .env.example is the canonical list; render.yaml only carries what the
    deployment overrides.
    """
    import re

    from pipeline.config import Settings

    documented = set(
        re.findall(
            r"^#?\s*([A-Z][A-Z0-9_]+)=",
            (REPO_ROOT / ".env.example").read_text(encoding="utf-8"),
            re.M,
        )
    )
    # repo_root is a test seam, set by constructing Settings directly, never via the env.
    expected = {name.upper() for name in Settings.model_fields} - {"REPO_ROOT"}

    assert expected <= documented, (
        f"config fields missing from .env.example: {sorted(expected - documented)}"
    )


def test_no_secret_values_are_committed() -> None:
    """No deployment config may carry a literal credential.

    Everything in this repo is public, so a token pasted into a config file is a leak. The
    Cloud Run script reads from Secret Manager and the Render blueprint uses `sync: false`;
    this checks neither regressed into an inline value.
    """
    import re

    # Patterns that identify a real credential rather than a placeholder.
    real_secret = re.compile(
        r"(?:ntn_[A-Za-z0-9]{20,}"  # Notion integration token
        r"|xoxb-[0-9]{5,}-[0-9]{5,}"  # Slack bot token
        r"|github_pat_[A-Za-z0-9_]{20,}"  # GitHub fine-grained PAT
        r"|sk-ant-[A-Za-z0-9-]{20,})"  # Anthropic API key
    )

    for path in (
        REPO_ROOT / "docs" / "render.yaml",
        REPO_ROOT / "scripts" / "deploy_cloudrun.sh",
        REPO_ROOT / ".env.example",
        REPO_ROOT / "README.md",
        *(REPO_ROOT / ".github" / "workflows").glob("*.yml"),
    ):
        if not path.is_file():
            continue
        content = path.read_text(encoding="utf-8")
        found = real_secret.findall(content)
        assert not found, f"{path.name} contains what looks like a real credential: {found}"


def test_cloudrun_script_uses_secret_manager() -> None:
    """Cloud Run env vars are readable by anyone with viewer access to the service.

    These credentials grant push access to a repo and posting rights in Slack, so they must
    come from Secret Manager (`--set-secrets`), never `--set-env-vars`.
    """
    script = (REPO_ROOT / "scripts" / "deploy_cloudrun.sh").read_text(encoding="utf-8")
    assert "--set-secrets" in script

    for name in ("NOTION_TOKEN", "GITHUB_TOKEN", "SLACK_BOT_TOKEN", "APPROVAL_HMAC_SECRET"):
        assert name in script, f"{name} is not wired into the Cloud Run deploy"

    # STORAGE_BACKEND=github is mandatory there: the disk is ephemeral and consecutive
    # requests can land on different instances.
    assert "STORAGE_BACKEND=github" in script


def test_every_declared_package_is_importable() -> None:
    """A dependency that can't be imported under its real module name is misconfigured."""
    module_names = {
        "anthropic": "anthropic",
        "fastapi": "fastapi",
        "httpx": "httpx",
        "jinja2": "jinja2",
        "pydantic": "pydantic",
        "pydantic-settings": "pydantic_settings",
        "pypdf": "pypdf",
        "python-docx": "docx",
        "python-pptx": "pptx",
        "uvicorn": "uvicorn",
        "playwright": "playwright",
    }
    import importlib.util

    for requirement in declared_dependencies():
        # Strip the version specifier and any extras: "uvicorn[standard]>=0.37.0" -> "uvicorn"
        name = requirement.split(">=")[0].split("[")[0].strip()
        module = module_names.get(name)
        assert module, f"no module mapping for {name!r} — add it to this test"
        assert importlib.util.find_spec(module) is not None, f"{name} is not installed"
