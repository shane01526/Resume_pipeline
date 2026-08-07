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
