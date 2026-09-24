"""test_farm_api_gate_e2e — E2E tests for farm_api gate endpoint.

E2E strategy:
    - Use monkeypatch to set env vars and override GATE_SUPPRESSION_DIR,
      then import and call gate API functions directly against real code.
    - Verify GateConfig reads env vars with correct types and defaults.
    - Verify suppression rules JSONL parsing, expired rule exclusion.

Bug classes covered:
    - Gate config reads wrong env var values.
    - Invalid env values crash instead of falling back to defaults.
    - Expired suppression rules not filtered out.
"""

from __future__ import annotations

import json
from pathlib import Path


class TestGetGateConfigE2E:
    """E2E: get_gate_config() reads env vars correctly with proper defaults."""

    def test_reads_env_vars_correctly(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MOTION_GATE_CONFIDENCE_THRESHOLD", "0.55")
        monkeypatch.setenv("MOTION_GATE_NIGHT_SUPPRESS_ENABLED", "1")
        monkeypatch.setenv("FARMSURV_GLOBAL_CHANGE_MAX_PCT", "75")
        monkeypatch.setenv("MOTION_GATE_V2", "1")

        from infra.farm_api.gate import GateConfig, get_gate_config

        cfg = get_gate_config()

        assert isinstance(cfg, GateConfig)
        assert cfg.motion_threshold == 0.55
        assert cfg.night_suppress_enabled is True
        assert cfg.global_change_max_pct == 75.0
        assert cfg.v2_enabled is True

    def test_returns_defaults(self, tmp_path, monkeypatch):
        monkeypatch.delenv("MOTION_GATE_CONFIDENCE_THRESHOLD", raising=False)
        monkeypatch.delenv("MOTION_GATE_NIGHT_SUPPRESS_ENABLED", raising=False)
        monkeypatch.delenv("FARMSURV_GLOBAL_CHANGE_MAX_PCT", raising=False)
        monkeypatch.delenv("MOTION_GATE_V2", raising=False)

        from infra.farm_api.gate import get_gate_config

        cfg = get_gate_config()

        assert cfg.motion_threshold == 0.50
        assert cfg.night_suppress_enabled is False
        assert cfg.global_change_max_pct == 60.0
        assert cfg.v2_enabled is False

    def test_invalid_values_fallback(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MOTION_GATE_CONFIDENCE_THRESHOLD", "not-a-number")
        monkeypatch.setenv("FARMSURV_GLOBAL_CHANGE_MAX_PCT", "also-not-a-number")

        from infra.farm_api.gate import get_gate_config

        cfg = get_gate_config()

        assert cfg.motion_threshold == 0.50
        assert cfg.global_change_max_pct == 60.0


class TestListSuppressionRulesE2E:
    """E2E: list_suppression_rules() parses JSONL and filters expired rules."""

    def test_empty_when_no_rules_file(self, tmp_path):
        import infra.farm_api.gate as gate_mod

        rules_path = tmp_path / "gate_suppression" / "rules.jsonl"
        assert not rules_path.exists()

        original_dir = gate_mod.GATE_SUPPRESSION_DIR
        try:
            gate_mod.GATE_SUPPRESSION_DIR = Path(tmp_path) / "gate_suppression"
            rules = gate_mod.list_suppression_rules()
            assert rules == []
        finally:
            gate_mod.GATE_SUPPRESSION_DIR = original_dir

    def test_parses_jsonl(self, tmp_path):
        rules_dir = tmp_path / "gate_suppression"
        rules_dir.mkdir()
        rules_path = rules_dir / "rules.jsonl"

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

        original_dir = gate_mod.GATE_SUPPRESSION_DIR
        try:
            gate_mod.GATE_SUPPRESSION_DIR = rules_dir
            rules = gate_mod.list_suppression_rules()
            assert len(rules) == 2
            assert rules[0].camera_id == "FRONT"
            assert rules[0].class_name == "dog"
            assert rules[0].expires_at == "2027-12-31T23:59:59+00:00"
            assert rules[1].camera_id == "BACK"
            assert rules[1].class_name == "cat"
            assert rules[1].expires_at is None
        finally:
            gate_mod.GATE_SUPPRESSION_DIR = original_dir

    def test_excludes_expired_rules(self, tmp_path):
        rules_dir = tmp_path / "gate_suppression"
        rules_dir.mkdir()
        rules_path = rules_dir / "rules.jsonl"

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

        original_dir = gate_mod.GATE_SUPPRESSION_DIR
        try:
            gate_mod.GATE_SUPPRESSION_DIR = rules_dir
            rules = gate_mod.list_suppression_rules()
            assert len(rules) == 1
            assert rules[0].class_name == "bear"
        finally:
            gate_mod.GATE_SUPPRESSION_DIR = original_dir
