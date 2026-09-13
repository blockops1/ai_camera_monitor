"""Smoke test for scripts/release/config.yaml schema.

Validates that config.yaml:
  - parses as YAML
  - has version: 1
  - has all required keys (strip_paths, tests_to_strip, required_files, scanners, push_remotes, license)
  - strip_paths are non-empty
  - license block has type/author/year
  - push_modes format is well-formed (each uses {branch}; at least one uses {version})

This is a low-cost regression test — the cut script depends on this config
being well-formed. Run before modifying config.yaml.
"""

from __future__ import annotations

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = REPO_ROOT / "scripts" / "release" / "config.yaml"


def _load_config() -> dict:
    assert CONFIG_PATH.exists(), f"missing {CONFIG_PATH}"
    return yaml.safe_load(CONFIG_PATH.read_text())


def test_config_parses_and_has_version():
    cfg = _load_config()
    assert cfg["version"] == 1, f"unsupported config version: {cfg['version']}"


def test_config_has_required_keys():
    cfg = _load_config()
    required = ["strip_paths", "tests_to_strip", "required_files",
                "scanners", "push_remotes", "license"]
    for key in required:
        assert key in cfg, f"missing required config key: {key}"


def test_strip_paths_nonempty():
    cfg = _load_config()
    assert len(cfg["strip_paths"]) >= 5, \
        f"strip_paths too small ({len(cfg['strip_paths'])}); expected at least 5"


def test_license_block_well_formed():
    cfg = _load_config()
    lic = cfg["license"]
    for key in ("type", "author", "year"):
        assert key in lic, f"license.{key} missing"
    assert lic["type"] in ("MIT", "Apache-2.0", "BSD-3-Clause"), \
        f"unsupported license type: {lic['type']}"


def test_push_remotes_have_command_format():
    cfg = _load_config()
    remotes = cfg["push_remotes"]
    assert len(remotes) >= 1
    has_version_mode = False
    for remote in remotes:
        assert "name" in remote, f"remote missing name: {remote}"
        modes = remote.get("push_modes", [])
        assert len(modes) >= 1, f"remote {remote['name']!r} has no push_modes"
        for mode in modes:
            assert "{branch}" in mode["command"], \
                f"push command missing {{branch}}: {mode['command']}"
            if "{version}" in mode["command"]:
                has_version_mode = True
    # at least one mode should reference the version tag
    assert has_version_mode, \
        "no push_modes reference {version} — at least one (tag-only) must use the version"


def test_tests_to_strip_have_rationale():
    cfg = _load_config()
    for t in cfg["tests_to_strip"]:
        path = t["path"]
        assert path.startswith("tests/"), f"unexpected test path: {path}"
        assert path.endswith(".py"), f"non-python test path: {path}"
        assert "why" in t and len(t["why"]) > 10, \
            f"tests_to_strip entry missing 'why' rationale: {t}"
