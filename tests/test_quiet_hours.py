"""
test_quiet_hours.py -- unit tests for infra.quiet_hours.

STATUS: new (US-062c)
THREAD SAFETY: single-threaded (pytest fixtures are single-threaded)

MOCKS:
    - time_of_day.is_night_at_edt: mocked to return predictable values

DOES NOT DO:
    - Make live network calls
    - Depend on llama-server or any external service
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _env_patch(cameras=None, enabled=None):
    """Build a dict of env-var overrides for quiet-hours tests."""
    env = {}
    if enabled is not None:
        env["FARMSURV_QUIET_HOURS_ENABLED"] = str(enabled)
    if cameras is not None:
        env["FARMSURV_QUIET_HOURS_CAMERAS"] = cameras
    return env


def _load_module():
    """Import (or reload) the quiet_hours module and return it."""
    import importlib

    import infra.quiet_hours as mod

    importlib.reload(mod)
    return mod


# ---------------------------------------------------------------------------
# AC2: Module exports
# ---------------------------------------------------------------------------


def test_module_exports():
    """AC2: module exports in_quiet_hours callable."""
    mod = _load_module()
    assert callable(mod.in_quiet_hours)


# ---------------------------------------------------------------------------
# AC3: Kill switch disables gate
# ---------------------------------------------------------------------------


def test_kill_switch_disabled():
    """AC3: FARMSURV_QUIET_HOURS_ENABLED=0 disables the gate entirely."""
    mod = _load_module()
    with patch.object(mod, "is_night_at_edt", return_value=True), patch.dict(
        "os.environ",
        _env_patch(enabled="0", cameras="OUTSIDE_FRONT_SOLAR"),
        clear=False,
    ):
        mod = _load_module()
        assert mod.in_quiet_hours(datetime(2026, 9, 19, 1, 20, tzinfo=UTC), "OUTSIDE_FRONT_SOLAR") is False


# ---------------------------------------------------------------------------
# AC4: Night + camera in list = suppressed
# ---------------------------------------------------------------------------


def test_night_camera_in_list_suppressed():
    """AC4: 01:20 AM night for camera in env list -> True (suppressed)."""
    mod = _load_module()
    with patch.object(mod, "is_night_at_edt", return_value=True), patch.dict(
        "os.environ",
        _env_patch(enabled="1", cameras="OUTSIDE_FRONT_SOLAR"),
        clear=False,
    ):
        mod = _load_module()
        assert mod.in_quiet_hours(datetime(2026, 9, 19, 1, 20, tzinfo=UTC), "OUTSIDE_FRONT_SOLAR") is True


# ---------------------------------------------------------------------------
# AC5: Day + camera in list = NOT suppressed
# ---------------------------------------------------------------------------


def test_day_camera_in_list_not_suppressed():
    """AC5: 12:00 noon for camera in env list -> False (not suppressed)."""
    mod = _load_module()
    with patch.object(mod, "is_night_at_edt", return_value=False), patch.dict(
        "os.environ",
        _env_patch(enabled="1", cameras="OUTSIDE_FRONT_SOLAR"),
        clear=False,
    ):
        mod = _load_module()
        assert mod.in_quiet_hours(datetime(2026, 9, 19, 12, 0, tzinfo=UTC), "OUTSIDE_FRONT_SOLAR") is False


# ---------------------------------------------------------------------------
# AC6: Camera NOT in env list = NOT suppressed (even at night)
# ---------------------------------------------------------------------------


def test_camera_not_in_list_not_suppressed():
    """AC6: INSIDE_GARAGE not in env list -> False (not suppressed)."""
    mod = _load_module()
    with patch.object(mod, "is_night_at_edt", return_value=True), patch.dict(
        "os.environ",
        _env_patch(enabled="1", cameras="OUTSIDE_FRONT_SOLAR"),
        clear=False,
    ):
        mod = _load_module()
        assert mod.in_quiet_hours(datetime(2026, 9, 19, 1, 20, tzinfo=UTC), "INSIDE_GARAGE") is False


# ---------------------------------------------------------------------------
# AC7: Three env vars read
# ---------------------------------------------------------------------------


def test_reads_env_vars():
    """AC7: module reads 3+ os.environ.get calls."""
    import infra.quiet_hours as mod

    with open(mod.__file__) as fh:
        source = fh.read()

    count = source.count("os.environ.get")
    assert count >= 3, f"Expected 3+ os.environ.get calls, found {count}"


# ---------------------------------------------------------------------------
# AC8: No hardcoded camera list or hour constant
# ---------------------------------------------------------------------------


def test_no_hardcoded_cameras_or_hours():
    """AC8: no QUIET_HOURS_DEFAULT_CAMERAS or QUIET_HOUR_START in source."""
    import re

    import infra.quiet_hours as mod

    with open(mod.__file__) as fh:
        source = fh.read()

    pattern = re.compile(
        r"^\s*(QUIET_HOURS_DEFAULT_CAMERAS|QUIET_HOUR_START)\s*="
    )
    assert not pattern.search(source), "Found hardcoded camera/hour constant in source"


# ---------------------------------------------------------------------------
# Multiple cameras in list
# ---------------------------------------------------------------------------


def test_multiple_cameras_in_list():
    """Multiple cameras in env list: any match gates."""
    mod = _load_module()
    with patch.object(mod, "is_night_at_edt", return_value=True), patch.dict(
        "os.environ",
        _env_patch(enabled="1", cameras="OUTSIDE_FRONT_SOLAR,OUTSIDE_BACK_SOLAR,INSIDE_GARAGE"),
        clear=False,
    ):
        mod = _load_module()
        assert mod.in_quiet_hours(datetime(2026, 9, 19, 2, 0, tzinfo=UTC), "INSIDE_GARAGE") is True
        assert mod.in_quiet_hours(datetime(2026, 9, 19, 2, 0, tzinfo=UTC), "OUTSIDE_BACK_SOLAR") is True
        assert mod.in_quiet_hours(datetime(2026, 9, 19, 2, 0, tzinfo=UTC), "OUTSIDE_FRONT_SOLAR") is True
        assert mod.in_quiet_hours(datetime(2026, 9, 19, 2, 0, tzinfo=UTC), "GARAGE_SIDE") is False


# ---------------------------------------------------------------------------
# Empty camera list = nothing gated
# ---------------------------------------------------------------------------


def test_empty_camera_list_gates_nothing():
    """Empty FARMSURV_QUIET_HOURS_CAMERAS -> no camera is gated."""
    mod = _load_module()
    with patch.object(mod, "is_night_at_edt", return_value=True), patch.dict(
        "os.environ",
        _env_patch(enabled="1", cameras=""),
        clear=False,
    ):
        mod = _load_module()
        assert mod.in_quiet_hours(datetime(2026, 9, 19, 2, 0, tzinfo=UTC), "ANY_CAMERA") is False


# ---------------------------------------------------------------------------
# Kill switch: truthy values other than "1"
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("val", ["1", "true", "yes", "on", "TRUE", "YES", "On"])
def test_kill_switch_truthy_values(val):
    """Kill switch accepts multiple truthy values."""
    mod = _load_module()
    with patch.object(mod, "is_night_at_edt", return_value=True), patch.dict(
        "os.environ",
        _env_patch(enabled=val, cameras="OUTSIDE_FRONT_SOLAR"),
        clear=False,
    ):
        mod = _load_module()
        assert mod.in_quiet_hours(datetime(2026, 9, 19, 2, 0, tzinfo=UTC), "OUTSIDE_FRONT_SOLAR") is True


@pytest.mark.parametrize("val", ["0", "false", "no", "off", "FALSE", "NO", "Off", ""])
def test_kill_switch_falsy_values(val):
    """Kill switch rejects multiple falsy values."""
    mod = _load_module()
    with patch.object(mod, "is_night_at_edt", return_value=True), patch.dict(
        "os.environ",
        _env_patch(enabled=val, cameras="OUTSIDE_FRONT_SOLAR"),
        clear=False,
    ):
        mod = _load_module()
        assert mod.in_quiet_hours(datetime(2026, 9, 19, 2, 0, tzinfo=UTC), "OUTSIDE_FRONT_SOLAR") is False


# ---------------------------------------------------------------------------
# Naive datetime handling
# ---------------------------------------------------------------------------


def test_naive_datetime_treated_as_utc():
    """Naive datetime is treated as UTC."""
    mod = _load_module()
    with patch.object(mod, "is_night_at_edt", return_value=True), patch.dict(
        "os.environ",
        _env_patch(enabled="1", cameras="OUTSIDE_FRONT_SOLAR"),
        clear=False,
    ):
        mod = _load_module()
        # Naive datetime (no tzinfo) should work
        dt = datetime(2026, 9, 19, 2, 0)  # noqa: DTZ001 (testing naive dt behavior)
        assert mod.in_quiet_hours(dt, "OUTSIDE_FRONT_SOLAR") is True


# ---------------------------------------------------------------------------
# _parse_cameras edge cases
# ---------------------------------------------------------------------------


def test_parse_cameras_empty():
    """Empty string -> empty frozenset."""
    from infra.quiet_hours import _parse_cameras

    assert _parse_cameras("") == frozenset()


def test_parse_cameras_whitespace():
    """Whitespace-only camera names are stripped, empty entries dropped."""
    from infra.quiet_hours import _parse_cameras

    result = _parse_cameras(" CAM1 , CAM2 , ")
    assert result == frozenset({"CAM1", "CAM2"})
