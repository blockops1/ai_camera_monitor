"""Tests for infra/gate_cooldown.py — per-camera × per-event-type cooldown (Phase.154, PLAN §11.77).

Coverage:
  - get_gate_cooldown_seconds() resolves the correct window from config
    (exact key → per-camera default → module default → 0)
  - is_in_gate_cooldown() suppresses the second alert within the window
  - is_in_gate_cooldown() allows the next alert after the window expires
  - "people" event_type is normalized to "person" key
  - per-camera default applies to event_types not listed
  - caller-provided window_seconds overrides config
  - module default (no config) = no cooldown (backward-compatibility)
  - clear_all_gate_cooldowns() resets both map and config cache
  - thread safety: concurrent calls under the lock don't corrupt the map
"""

from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

import pytest

_root = Path(__file__).resolve().parent.parent.parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

from infra.gate_cooldown import (
    clear_all_gate_cooldowns,
    get_gate_cooldown_seconds,
    is_in_gate_cooldown,
)


@pytest.fixture(autouse=True)
def reset_state(tmp_path, monkeypatch):
    """Reset gate_cooldown state before each test."""
    # Point PROJECT_ROOT at a tmp_path so each test sees its own config
    monkeypatch.setattr("infra.paths.PROJECT_ROOT", tmp_path)
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    yield
    clear_all_gate_cooldowns()


def _write_config(tmp_path: Path, data: dict) -> None:
    cfg = tmp_path / "config" / "motion_gate_thresholds.json"
    cfg.write_text(json.dumps(data))


class TestResolveCooldownSeconds:
    """get_gate_cooldown_seconds() resolution order."""

    def test_returns_zero_when_config_missing(self, tmp_path):
        """No config file → module default 0 (backward-compat)."""
        assert get_gate_cooldown_seconds("Any Camera", "vehicle") == 0

    def test_returns_zero_when_camera_missing(self, tmp_path):
        """Camera not in config → module default 0."""
        _write_config(tmp_path, {"Other Camera": {"gate_cooldown": {"vehicle": 60}}})
        assert get_gate_cooldown_seconds("Unknown", "vehicle") == 0

    def test_returns_exact_key_value(self, tmp_path):
        """Camera + event_type → exact config value."""
        _write_config(tmp_path, {
            "OFG": {"gate_cooldown": {"vehicle": 60, "person": 30}}
        })
        assert get_gate_cooldown_seconds("OFG", "vehicle") == 60
        assert get_gate_cooldown_seconds("OFG", "person") == 30

    def test_uses_per_camera_default_for_unknown_event(self, tmp_path):
        """Camera + gate_cooldown.default → applies to event_types not listed."""
        _write_config(tmp_path, {
            "OFG": {"gate_cooldown": {"default": 45, "vehicle": 60}}
        })
        assert get_gate_cooldown_seconds("OFG", "vehicle") == 60
        assert get_gate_cooldown_seconds("OFG", "person") == 45  # default applies
        assert get_gate_cooldown_seconds("OFG", "motion") == 45   # default applies

    def test_normalizes_people_to_person_key(self, tmp_path):
        """'people' (Reolink payload) reads the 'person' config key."""
        _write_config(tmp_path, {
            "OFG": {"gate_cooldown": {"person": 30}}
        })
        assert get_gate_cooldown_seconds("OFG", "people") == 30
        assert get_gate_cooldown_seconds("OFG", "person") == 30

    def test_zero_value_means_no_cooldown(self, tmp_path):
        """Explicit 0 → no cooldown (don't suppress)."""
        _write_config(tmp_path, {
            "OFG": {"gate_cooldown": {"vehicle": 0, "person": 30}}
        })
        assert get_gate_cooldown_seconds("OFG", "vehicle") == 0
        assert get_gate_cooldown_seconds("OFG", "person") == 30


class TestIsInGateCooldown:
    """is_in_gate_cooldown() behavior."""

    def test_first_call_returns_false_records_timestamp(self, tmp_path):
        """First alert for (camera, event_type) → False, records timestamp."""
        _write_config(tmp_path, {"OFG": {"gate_cooldown": {"vehicle": 60}}})
        in_cooldown, last_seen = is_in_gate_cooldown("OFG", "vehicle")
        assert in_cooldown is False
        assert last_seen == 0.0  # no previous alert

    def test_second_call_within_window_returns_true(self, tmp_path):
        """Second alert within window → True (suppressed)."""
        _write_config(tmp_path, {"OFG": {"gate_cooldown": {"vehicle": 60}}})
        is_in_gate_cooldown("OFG", "vehicle")  # first
        in_cooldown, last_seen = is_in_gate_cooldown("OFG", "vehicle")  # second
        assert in_cooldown is True
        assert last_seen > 0.0  # has a previous timestamp

    def test_third_call_after_window_returns_false(self, tmp_path, monkeypatch):
        """Alert after window expires → False (allowed through)."""
        _write_config(tmp_path, {"OFG": {"gate_cooldown": {"vehicle": 1}}})
        is_in_gate_cooldown("OFG", "vehicle")  # first
        time.sleep(1.1)  # window is 1s
        in_cooldown, _ = is_in_gate_cooldown("OFG", "vehicle")  # after window
        assert in_cooldown is False

    def test_caller_window_overrides_config(self, tmp_path, monkeypatch):
        """Explicit window_seconds arg beats config."""
        _write_config(tmp_path, {"OFG": {"gate_cooldown": {"vehicle": 9999}}})
        # Config says 9999s but caller says 1s
        is_in_gate_cooldown("OFG", "vehicle", window_seconds=1)
        time.sleep(1.1)
        in_cooldown, _ = is_in_gate_cooldown("OFG", "vehicle", window_seconds=1)
        assert in_cooldown is False  # 1s caller window applied, not 9999s config

    def test_per_camera_independence(self, tmp_path):
        """Cooldown on camera A does not affect camera B."""
        _write_config(tmp_path, {
            "OFG": {"gate_cooldown": {"vehicle": 60}},
            "OFP": {"gate_cooldown": {"vehicle": 60}},
        })
        is_in_gate_cooldown("OFG", "vehicle")  # first alert OFG
        in_cooldown_OFP, _ = is_in_gate_cooldown("OFP", "vehicle")  # OFP is fresh
        assert in_cooldown_OFP is False

    def test_per_event_type_independence(self, tmp_path):
        """Cooldown on (cam, vehicle) does not affect (cam, person)."""
        _write_config(tmp_path, {
            "OFG": {"gate_cooldown": {"vehicle": 60, "person": 60}}
        })
        is_in_gate_cooldown("OFG", "vehicle")  # first alert vehicle
        in_cooldown_person, _ = is_in_gate_cooldown("OFG", "person")  # person is fresh
        assert in_cooldown_person is False

    def test_no_cooldown_when_window_zero(self, tmp_path):
        """window=0 (no config) → always False, never suppress."""
        # No config written → default 0
        for _ in range(5):
            in_cooldown, _ = is_in_gate_cooldown("OFG", "vehicle")
            assert in_cooldown is False


class TestClearAll:
    """clear_all_gate_cooldowns() resets state."""

    def test_clear_resets_in_memory_map(self, tmp_path):
        """After clear, the previously-suppressed combination is fresh again."""
        _write_config(tmp_path, {"OFG": {"gate_cooldown": {"vehicle": 60}}})
        is_in_gate_cooldown("OFG", "vehicle")
        clear_all_gate_cooldowns()
        in_cooldown, _ = is_in_gate_cooldown("OFG", "vehicle")
        assert in_cooldown is False  # map was wiped

    def test_clear_resets_config_cache(self, tmp_path):
        """After clear, config changes are picked up on next read."""
        _write_config(tmp_path, {"OFG": {"gate_cooldown": {"vehicle": 60}}})
        assert get_gate_cooldown_seconds("OFG", "vehicle") == 60
        # Change config under tmp_path
        _write_config(tmp_path, {"OFG": {"gate_cooldown": {"vehicle": 30}}})
        # Cached value still applies
        assert get_gate_cooldown_seconds("OFG", "vehicle") == 60
        clear_all_gate_cooldowns()
        # Cache cleared, fresh read
        assert get_gate_cooldown_seconds("OFG", "vehicle") == 30


class TestThreadSafety:
    """Concurrent calls don't corrupt the map."""

    def test_concurrent_calls_no_corruption(self, tmp_path):
        """Many threads calling is_in_gate_cooldown at once → consistent state."""
        _write_config(tmp_path, {"OFG": {"gate_cooldown": {"vehicle": 60}}})
        results: list[tuple[bool, float]] = []
        errors: list[Exception] = []

        def worker():
            try:
                for _ in range(50):
                    res = is_in_gate_cooldown("OFG", "vehicle")
                    results.append(res)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []
        # At least one False (the first call) and one True (a follow-up)
        # — depending on race timing we may see many False-then-True pairs.
        # The strict invariant: no call returns (True, 0.0) which would
        # mean "in cooldown but no previous timestamp".
        for in_cooldown, last_seen in results:
            if in_cooldown:
                assert last_seen > 0.0


class TestMalformedConfig:
    """Defensive: malformed config doesn't crash the listener."""

    def test_malformed_json_returns_zero(self, tmp_path):
        """Bad JSON → 0 cooldown (don't break the listener)."""
        cfg = tmp_path / "config" / "motion_gate_thresholds.json"
        cfg.write_text("{ this is not valid json")
        assert get_gate_cooldown_seconds("OFG", "vehicle") == 0
        in_cooldown, _ = is_in_gate_cooldown("OFG", "vehicle")
        assert in_cooldown is False  # no cooldown → no suppression

    def test_non_dict_camera_value_returns_zero(self, tmp_path):
        """Camera value is a string, not a dict → 0 cooldown."""
        _write_config(tmp_path, {"OFG": "not a dict"})
        assert get_gate_cooldown_seconds("OFG", "vehicle") == 0

    def test_non_dict_gate_cooldown_returns_zero(self, tmp_path):
        """gate_cooldown value is a string, not a dict → 0 cooldown."""
        _write_config(tmp_path, {"OFG": {"gate_cooldown": "not a dict"}})
        assert get_gate_cooldown_seconds("OFG", "vehicle") == 0

    def test_non_numeric_window_value_returns_zero(self, tmp_path):
        """gate_cooldown[vehicle] = "abc" → 0 cooldown."""
        _write_config(tmp_path, {"OFG": {"gate_cooldown": {"vehicle": "abc"}}})
        assert get_gate_cooldown_seconds("OFG", "vehicle") == 0
