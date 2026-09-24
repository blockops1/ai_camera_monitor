"""test_farm_api_gate_e2e — E2E tests for farm_api gate endpoint.

E2E strategy:
    - Use os.environ to set env vars and override GATE_SUPPRESSION_DIR
      directly, then import and call gate API functions against real code.
    - Verify GateConfig reads env vars with correct types and defaults.
    - Verify suppression rules JSONL parsing, expired rule exclusion.

Bug classes covered:
    - Gate config reads wrong env var values.
    - Invalid env values crash instead of falling back to defaults.
    - Expired suppression rules not filtered out.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Fixture — gate config env vars
# ---------------------------------------------------------------------------

@pytest.fixture()
def _gate_config_env(tmp_path: Path):
    """Set gate config env vars and clear cached modules."""
    old_env = {}
    for key in (
        "MOTION_GATE_CONFIDENCE_THRESHOLD",
        "MOTION_GATE_NIGHT_SUPPRESS_ENABLED",
        "FARMSURV_GLOBAL_CHANGE_MAX_PCT",
        "MOTION_GATE_V2",
    ):
        old_env[key] = os.environ.pop(key, None)

    yield tmp_path

    for key, val in old_env.items():
        if val is not None:
            os.environ[key] = val


@pytest.fixture()
def _gate_suppression_env(tmp_path: Path):
    """Override GATE_SUPPRESSION_DIR to a temp dir."""
    import infra.farm_api.gate as gate_mod

    rules_dir = tmp_path / "gate_suppression"
    rules_dir.mkdir()

    old_dir = gate_mod.GATE_SUPPRESSION_DIR
    old_env_dir = os.environ.get("FARMSURV_GATE_SUPPRESSION_DIR")

    gate_mod.GATE_SUPPRESSION_DIR = rules_dir
    if old_env_dir is not None:
        os.environ["FARMSURV_GATE_SUPPRESSION_DIR"] = old_env_dir
    else:
        os.environ.pop("FARMSURV_GATE_SUPPRESSION_DIR", None)

    yield rules_dir

    gate_mod.GATE_SUPPRESSION_DIR = old_dir
    if old_env_dir is not None:
        os.environ["FARMSURV_GATE_SUPPRESSION_DIR"] = old_env_dir
    else:
        os.environ.pop("FARMSURV_GATE_SUPPRESSION_DIR", None)


# ---------------------------------------------------------------------------
# E2E tests — GateConfig env-var reading
# ---------------------------------------------------------------------------

class TestGetGateConfigE2E:
    """E2E: get_gate_config() reads env vars correctly with proper defaults."""

    def test_reads_env_vars_correctly(self, _gate_config_env):
        os.environ["MOTION_GATE_CONFIDENCE_THRESHOLD"] = "0.55"
        os.environ["MOTION_GATE_NIGHT_SUPPRESS_ENABLED"] = "1"
        os.environ["FARMSURV_GLOBAL_CHANGE_MAX_PCT"] = "75"
        os.environ["MOTION_GATE_V2"] = "1"

        from infra.farm_api.gate import GateConfig, get_gate_config

        cfg = get_gate_config()

        assert isinstance(cfg, GateConfig)
        assert cfg.motion_threshold == 0.55
        assert cfg.night_suppress_enabled is True
        assert cfg.global_change_max_pct == 75.0
        assert cfg.v2_enabled is True

    def test_returns_defaults(self, _gate_config_env):
        # All env vars cleared by fixture
        from infra.farm_api.gate import get_gate_config

        cfg = get_gate_config()

        assert cfg.motion_threshold == 0.50
        assert cfg.night_suppress_enabled is False
        assert cfg.global_change_max_pct == 60.0
        assert cfg.v2_enabled is False

    def test_invalid_values_fallback(self, _gate_config_env):
        os.environ["MOTION_GATE_CONFIDENCE_THRESHOLD"] = "not-a-number"
        os.environ["FARMSURV_GLOBAL_CHANGE_MAX_PCT"] = "also-not-a-number"

        from infra.farm_api.gate import get_gate_config

        cfg = get_gate_config()

        assert cfg.motion_threshold == 0.50
        assert cfg.global_change_max_pct == 60.0


# ---------------------------------------------------------------------------
# E2E tests — suppression rules JSONL parsing
# ---------------------------------------------------------------------------

class TestListSuppressionRulesE2E:
    """E2E: list_suppression_rules() parses JSONL and filters expired rules."""

    def test_empty_when_no_rules_file(self, _gate_suppression_env):
        import infra.farm_api.gate as gate_mod

        rules_path = _gate_suppression_env / "rules.jsonl"
        assert not rules_path.exists()

        rules = gate_mod.list_suppression_rules()
        assert rules == []

    def test_parses_jsonl(self, _gate_suppression_env):
        rules_path = _gate_suppression_env / "rules.jsonl"

        rules_data = [
            {
                "camera_id": "FRONT",
                "class_name": "dog",
                "reason": "Too many false positives on dogs at night",
                "expires_at": "2027-12-31T23:59:59+00:00",
            },
            {
                "camera_id": "BACK",
                "class_name": "cat",
                "reason": "Cat in backyard, expected",
            },
        ]
        rules_path.write_text(
            "\n".join(json.dumps(r) for r in rules_data) + "\n",
            encoding="utf-8",
        )

        import infra.farm_api.gate as gate_mod

        rules = gate_mod.list_suppression_rules()
        assert len(rules) == 2
        assert rules[0].camera_id == "FRONT"
        assert rules[0].class_name == "dog"
        assert rules[0].expires_at == "2027-12-31T23:59:59+00:00"
        assert rules[1].camera_id == "BACK"
        assert rules[1].class_name == "cat"
        assert rules[1].expires_at is None

    def test_excludes_expired_rules(self, _gate_suppression_env):
        rules_path = _gate_suppression_env / "rules.jsonl"

        rules_data = [
            {
                "camera_id": "FRONT",
                "class_name": "bird",
                "reason": "Temporary suppression",
                "expires_at": "2020-01-01T00:00:00+00:00",
            },
            {
                "camera_id": "FRONT",
                "class_name": "bear",
                "reason": "Permanent suppression",
            },
        ]
        rules_path.write_text(
            "\n".join(json.dumps(r) for r in rules_data) + "\n",
            encoding="utf-8",
        )

        import infra.farm_api.gate as gate_mod

        rules = gate_mod.list_suppression_rules()
        assert len(rules) == 1
        assert rules[0].class_name == "bear"
