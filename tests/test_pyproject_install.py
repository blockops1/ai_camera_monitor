"""
test_pyproject_install — verify runtime + dev deps are declared; .env.example files tracked.

STATUS: stable
THREAD SAFETY: single-threaded (pytest)

DOES NOT DO:
    - Check transitive dependency resolution (pip does that)
    - Import production modules (infra.frame_capture doesn't exist yet)
"""

import tomllib  # pyright: ignore[reportUndefinedVariable]
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = ROOT / "pyproject.toml"

REQUIRED_RUNTIME = [
    "flask",
    "python-telegram-bot",
    "numpy",
    "Pillow",
    "onnxruntime",
    "av",
    "httpx",
    "python-dotenv",
]


def _load_pyproject_deps() -> list[str]:
    """Parse pyproject.toml and return the list of dependency strings."""
    import tomllib  # pyright: ignore[reportUndefinedVariable]

    with open(PYPROJECT, "rb") as f:
        data = tomllib.load(f)
    return data["project"]["dependencies"]


# -- runtime deps present in pyproject.toml ----------------------------------

@pytest.mark.parametrize("name", REQUIRED_RUNTIME)
def test_pyproject_contains_runtime_dep(name):
    """Each required runtime dependency appears in pyproject.toml."""
    deps = _load_pyproject_deps()
    assert any(name in d for d in deps), f"{name} not found in dependencies"


def test_pyproject_dev_optional_deps_exist():
    """[project.optional-dependencies] dev section is present."""
    with open(PYPROJECT, "rb") as f:
        data = tomllib.load(f)
    dev = data.get("project", {}).get("optional-dependencies", {}).get("dev", [])
    assert len(dev) >= 4, f"Expected >=4 dev deps, got {len(dev)}: {dev}"


# -- .env.example files tracked by git ---------------------------------------

@pytest.mark.parametrize("fname", [
    "camera-creds.env.example",
    "telegram-creds.env.example",
    "llm-creds.env.example",
])
def test_env_example_files_exist(fname):
    """All three .env.example files exist in the repo root."""
    path = ROOT / fname
    assert path.exists(), f"{fname} missing"


def test_env_example_files_not_in_gitignore():
    """The .env.example files are excluded from .gitignore via ! negation."""
    gitignore = ROOT / ".gitignore"
    content = gitignore.read_text()
    for fname in ("camera-creds.env.example", "telegram-creds.env.example", "llm-creds.env.example"):
        assert f"!{fname}" in content, f"{fname} not negated in .gitignore"
