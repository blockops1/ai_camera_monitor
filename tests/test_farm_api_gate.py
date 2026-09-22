"""Tests for farm_api.gate — GateConfig and SuppressionRule.

STATUS: V2-068b3
"""

import json
from pathlib import Path

from infra.farm_api.gate import GateConfig, get_gate_config, list_suppression_rules


class TestGetGateConfig:
    """Tests for get_gate_config()."""

    def test_get_gate_config_reads_env(self, tmp_path, monkeypatch):
        """get_gate_config reads all env vars and returns a GateConfig.

        Verifies:
        - motion_threshold from MOTION_GATE_CONFIDENCE_THRESHOLD
        - night_suppress_enabled from MOTION_GATE_NIGHT_SUPPRESS_ENABLED
        - global_change_max_pct from FARMSURV_GLOBAL_CHANGE_MAX_PCT
        - v2_enabled from MOTION_GATE_V2
        """
        monkeypatch.setenv("MOTION_GATE_CONFIDENCE_THRESHOLD", "0.55")
        monkeypatch.setenv("MOTION_GATE_NIGHT_SUPPRESS_ENABLED", "1")
        monkeypatch.setenv("FARMSURV_GLOBAL_CHANGE_MAX_PCT", "75")
        monkeypatch.setenv("MOTION_GATE_V2", "1")

        cfg = get_gate_config()

        assert isinstance(cfg, GateConfig)
        assert cfg.motion_threshold == 0.55
        assert cfg.night_suppress_enabled is True
        assert cfg.global_change_max_pct == 75.0
        assert cfg.v2_enabled is True

    def test_get_gate_config_defaults(self, tmp_path, monkeypatch):
        """get_gate_config returns defaults when no env vars are set."""
        monkeypatch.delenv("MOTION_GATE_CONFIDENCE_THRESHOLD", raising=False)
        monkeypatch.delenv("MOTION_GATE_NIGHT_SUPPRESS_ENABLED", raising=False)
        monkeypatch.delenv("FARMSURV_GLOBAL_CHANGE_MAX_PCT", raising=False)
        monkeypatch.delenv("MOTION_GATE_V2", raising=False)

        cfg = get_gate_config()

        assert cfg.motion_threshold == 0.50
        assert cfg.night_suppress_enabled is False
        assert cfg.global_change_max_pct == 60.0
        assert cfg.v2_enabled is False

    def test_get_gate_config_invalid_values_fallback(self, tmp_path, monkeypatch):
        """get_gate_config falls back to defaults on invalid env values."""
        monkeypatch.setenv("MOTION_GATE_CONFIDENCE_THRESHOLD", "not-a-number")
        monkeypatch.setenv("FARMSURV_GLOBAL_CHANGE_MAX_PCT", "also-not-a-number")

        cfg = get_gate_config()

        assert cfg.motion_threshold == 0.50
        assert cfg.global_change_max_pct == 60.0


class TestListSuppressionRules:
    """Tests for list_suppression_rules()."""

    def test_list_suppression_rules_empty_when_no_state(self, tmp_path):
        """list_suppression_rules returns empty list when no rules file exists.

        Verifies the function does not crash and returns [] when
        data/gate_suppression/rules.jsonl does not exist.
        """
        # Ensure no stale state
        rules_path = tmp_path / "gate_suppression" / "rules.jsonl"
        assert not rules_path.exists()

        # Use monkeypatch to point GATE_SUPPRESSION_DIR at tmp_path
        import infra.farm_api.gate as gate_mod

        original_dir = gate_mod.GATE_SUPPRESSION_DIR
        try:
            gate_mod.GATE_SUPPRESSION_DIR = Path(tmp_path) / "gate_suppression"
            rules = list_suppression_rules()
            assert rules == []
        finally:
            gate_mod.GATE_SUPPRESSION_DIR = original_dir

    def test_list_suppression_rules_parses_jsonl(self, tmp_path):
        """list_suppression_rules parses a rules.jsonl file correctly."""
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
            rules = list_suppression_rules()
            assert len(rules) == 2
            assert rules[0].camera_id == "FRONT"
            assert rules[0].class_name == "dog"
            assert rules[0].expires_at == "2027-12-31T23:59:59+00:00"
            assert rules[1].camera_id == "BACK"
            assert rules[1].class_name == "cat"
            assert rules[1].expires_at is None
        finally:
            gate_mod.GATE_SUPPRESSION_DIR = original_dir

    def test_list_suppression_rules_excludes_expired(self, tmp_path):
        """list_suppression_rules excludes rules with past expires_at."""
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
            rules = list_suppression_rules()
            assert len(rules) == 1
            assert rules[0].class_name == "bear"
        finally:
            gate_mod.GATE_SUPPRESSION_DIR = original_dir
