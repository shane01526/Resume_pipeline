"""Packaging consistency: requirements.txt must not drift from pyproject.toml.

The Dockerfile installs from requirements.txt so the dependency layer caches independently
of the source (setuptools can't build the package before the code is copied). That leaves
two lists of the same thing, and a dependency added to only one of them produces an
ImportError at runtime in the container — long after the build looked fine.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from pipeline.config import Settings

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


def test_cloudrun_deploy_disables_cpu_throttling() -> None:
    """The background-task design does not work on Cloud Run without this flag.

    POST /api/runs returns 202 and renders in a FastAPI background task. Cloud Run throttles
    an instance's CPU to nearly nothing outside a request, so that work crawls and then dies
    when the idle instance is reclaimed: a real run wrote its diff counts 615s after being
    triggered (~90s of work locally) and stopped at status "Building" with error: null.

    Asserted here because the failure is silent and remote — no exception, no failed deploy,
    just a run that never reaches Pending Approval and a Slack notification that never
    arrives.
    """
    script = (REPO_ROOT / "scripts" / "deploy_cloudrun.sh").read_text(encoding="utf-8")

    assert "--no-cpu-throttling" in script


def test_every_env_var_the_deploy_script_sets_is_accepted_by_settings() -> None:
    """Parse `--set-env-vars` out of the deploy script and feed it to Settings.

    This is the test that was missing. `deploy_cloudrun.sh` sets
    `RENDERERS=html,latex,docx`, and pydantic-settings JSON-decodes complex-typed fields
    at the source — before validators run — so Settings() raised SettingsError and every
    Cloud Run container died during startup. The suite passed: nothing connected the
    script's literal values to the config that has to accept them.

    Found by running the built image, not by reading the code.
    """
    import re

    script = (REPO_ROOT / "scripts" / "deploy_cloudrun.sh").read_text(encoding="utf-8")

    # The env_vars=( ... ) array literal, which is what reaches --set-env-vars.
    block = re.search(r"env_vars=\((.*?)\n\)", script, re.DOTALL)
    assert block, "could not find the env_vars array in deploy_cloudrun.sh"

    pairs = re.findall(r'"([A-Z_]+)=([^"]*)"', block.group(1))
    assert pairs, "no NAME=value pairs found; did the script's format change?"

    env = {name: value for name, value in pairs if "${" not in value}
    assert "RENDERERS" in env, "expected the script to still set RENDERERS"

    # Through the ENVIRONMENT, not as kwargs. Passing `renderers="html,latex,docx"` as a
    # keyword argument skips the env source that does the JSON decoding, so it would pass
    # even with the bug present — verified by reverting the fix and watching it pass.
    with pytest.MonkeyPatch.context() as patch:
        for name, value in env.items():
            patch.setenv(name, value)
        settings = Settings(_env_file=None)

    assert settings.renderers == ["html", "latex", "docx"]
    assert settings.storage_backend == "github"


def _settings_with_renderers(value: str) -> Settings:
    """Build Settings with RENDERERS set in the environment.

    Every renderers test goes through here rather than passing a kwarg: pydantic-settings
    only JSON-decodes values arriving from an env source, so a kwarg-based test cannot see
    the bug this whole group exists to prevent.
    """
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("RENDERERS", value)
        return Settings(_env_file=None)


def test_renderers_accepts_a_comma_separated_string() -> None:
    """The only form a Cloud Run / Render env var can take."""
    assert _settings_with_renderers("html,docx").renderers == ["html", "docx"]
    assert _settings_with_renderers("html, latex , docx").renderers == ["html", "latex", "docx"]


def test_renderers_accepts_a_json_list_rather_than_mangling_it() -> None:
    """Splitting `["html","docx"]` on commas yields '["html"' — a name that matches no
    renderer, so the run produces zero artifacts and reports success."""
    assert _settings_with_renderers('["html","docx"]').renderers == ["html", "docx"]


def test_empty_renderers_is_rejected() -> None:
    """`RENDERERS=` would otherwise mean "render nothing" and pass silently."""
    with pytest.raises(ValueError, match="RENDERERS is empty"):
        _settings_with_renderers("")


def test_unknown_renderer_name_is_rejected() -> None:
    """A typo is invisible otherwise: `"latex" in renderers` is just False, the artifact
    goes missing, and nothing is logged."""
    with pytest.raises(ValueError, match="unknown renderer"):
        _settings_with_renderers("html,latext")


def test_deploy_script_disables_msys_path_conversion() -> None:
    """Git Bash must not rewrite the container's POSIX paths into Windows ones.

    Without this, `BEDROCK_KEY_FILE=/tmp/bedrock_key.json` reached Cloud Run as
    `C:/Users/<you>/AppData/Local/Temp/bedrock_key.json`. Inside the Linux container that is
    a *relative* path, so it resolved under /app — the repository — and llm_key.py's guard
    correctly refused to write a credential there. POST /admin/llm-key answered 400 and
    in-place rotation was dead, with nothing failing at deploy time to say so.
    """
    script = (REPO_ROOT / "scripts" / "deploy_cloudrun.sh").read_text(encoding="utf-8")

    assert "MSYS_NO_PATHCONV=1" in script
    assert 'MSYS2_ARG_CONV_EXCL="*"' in script


def test_container_paths_in_the_deploy_script_are_posix() -> None:
    """Every path handed to the container must be POSIX, whatever OS deploys it."""
    import re

    script = (REPO_ROOT / "scripts" / "deploy_cloudrun.sh").read_text(encoding="utf-8")
    block = re.search(r"env_vars=\((.*?)\n\)", script, re.DOTALL)
    assert block

    for name, value in re.findall(r'"([A-Z_]+)=([^"]*)"', block.group(1)):
        assert not re.match(r"^[A-Za-z]:[\/]", value), f"{name} carries a Windows path: {value}"
        assert "\\" not in value, f"{name} carries a backslash: {value}"


def test_key_cache_default_is_outside_the_repo() -> None:
    """The guard that caught the bug above only works if the default is sane too.

    state/ and output/ are committed to a public repo, so a credential cached there would be
    published — hence the hard error rather than a warning.
    """
    from pipeline.llm_key import LLMKeyError, _validate_key_path

    settings = Settings(_env_file=None)
    resolved = _validate_key_path(settings.bedrock_key_file, settings)
    assert resolved.is_absolute()

    with pytest.raises(LLMKeyError, match="inside the repository"):
        _validate_key_path(settings.repo_root / "state" / "key.json", settings)
