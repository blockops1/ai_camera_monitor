"""Unit tests for infra/quiet_hours.py — Time-window suppression for outside cameras.

Phase.112 (Note 2026-08-24): refactored from fixed 21:00-07:00 wall-clock
window using ZoneInfo to astro night detection via infra.time_of_day.

Coverage:
  - QUIET_HOURS_CAMERAS membership is unchanged (4 outside cameras)
  - Cameras outside the set are NEVER silenced
  - Inside the set: silenced during night, NOT silenced during day
  - Boundary: pre-dawn civil twilight still silenced; civil twilight ends unblock it
  - Naive datetimes handled (treated as UTC)
  - Module is timezone-consistent: never uses ZoneInfo("America/New_York")
    (memory rule: EDT fixed UTC-4, no auto-fallback)
"""

from __future__ import annotations

import ast
import sys
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

_root = Path(__file__).resolve().parent.parent.parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

from infra.quiet_hours import (  # noqa: E402
    QUIET_HOURS_CAMERAS,
    in_quiet_hours,
)

EDT = timezone(timedelta(hours=-4))


def _edt(hour: int, minute: int = 0, day: int = 24, year: int = 2026, month: int = 8) -> datetime:
    """Helper: build a tz-aware UTC datetime for the given EDT wall time."""
    if hour < 20:
        utc_day = day
        utc_hour = hour + 4
    else:
        utc_day = day + 1
        utc_hour = (hour + 4) % 24
    return datetime(year, month, utc_day, utc_hour, minute, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# QUIET_HOURS_CAMERAS — membership contract
# ---------------------------------------------------------------------------


def test_quiet_hours_cameras_contains_4_outside_cams():
    """The 4 outside cameras are in the silenced set.

    Phase.167 §13.4 Commit 17 (T3 C17): CAM{N} codes per
    infra.cameras._LEGACY_PREFIX_TO_CODE. CAM3/CAM4/CAM5/CAM6 = the
    4 exterior yard/driveway cameras.
    """
    expected = {
        "CAM5",  # OUTSIDE_FRONT_SOLAR
        "CAM4",  # OUTSIDE_FRONT_POWER
        "CAM6",  # OUTSIDE_BACK_SOLAR
        "CAM3",  # OUTSIDE_FRONT_GARAGE
    }
    assert set(QUIET_HOURS_CAMERAS) == expected


def test_quiet_hours_cameras_is_frozenset():
    """Camera set must be immutable so callers can't accidentally add to it."""
    assert isinstance(QUIET_HOURS_CAMERAS, frozenset)


def test_quiet_hours_cameras_excludes_doors():
    """CAM1 and CAM2 (doorbells) are NOT in the silenced set.

    Phase.167 §13.4 Commit 17 (T3 C17): §13.4-migration refactor;
    doorbells remain security-relevant at any hour.
    """
    assert "CAM1" not in QUIET_HOURS_CAMERAS
    assert "CAM2" not in QUIET_HOURS_CAMERAS


# ---------------------------------------------------------------------------
# Cameras outside QUIET_HOURS_CAMERAS — NEVER silenced
# ---------------------------------------------------------------------------


def test_front_door_outside_never_silenced_during_night():
    """FDO is not in the set — even at 02:00, we don't suppress."""
    dt = _edt(2)  # deep night
    assert in_quiet_hours(dt, "Front Door Outside") is False


def test_back_door_inside_never_silenced_during_night():
    dt = _edt(2)
    assert in_quiet_hours(dt, "Back Door Inside") is False


def test_unknown_camera_never_silenced():
    """Unknown camera name → False (not in the set)."""
    dt = _edt(2)
    assert in_quiet_hours(dt, "Some Unmapped Camera Name") is False


# ---------------------------------------------------------------------------
# QUIET_HOURS_CAMERAS members — silenced at night, NOT silenced during day
# ---------------------------------------------------------------------------


def test_ofs_silenced_at_midnight():
    """OFS at EDT 00:00 (deep night) → silenced."""
    dt = _edt(0)
    assert in_quiet_hours(dt, "Outside Front Solar") is True


def test_ofs_silenced_at_2am():
    dt = _edt(2)
    assert in_quiet_hours(dt, "Outside Front Solar") is True


def test_ofp_silenced_at_civil_twilight_begin():
    """OFP at EDT 20:46 (sunset + 30 min, civil twilight) → silenced."""
    dt = _edt(20, 46)
    assert in_quiet_hours(dt, "Outside Front Power") is True


def test_ofp_not_silenced_at_sunset():
    """OFP at EDT 20:15 (astronomical sunset — IR LEDs still off) → NOT silenced."""
    dt = _edt(20, 15)
    assert in_quiet_hours(dt, "Outside Front Power") is False


def test_obs_not_silenced_at_civil_twilight_end():
    """OBS at EDT 06:38 = 1 min after civil twilight ends → NOT silenced."""
    dt = _edt(6, 38)
    assert in_quiet_hours(dt, "Outside Back Solar") is False


def test_obs_silenced_at_pre_dawn():
    """OBS at EDT 06:00 = pre-dawn twilight → silenced."""
    dt = _edt(6)
    assert in_quiet_hours(dt, "Outside Back Solar") is True


def test_ofg_silenced_at_22():
    """OFG at EDT 22:00 (full dark) → silenced."""
    dt = _edt(22)
    assert in_quiet_hours(dt, "Outside Front Garage") is True


def test_ofs_not_silenced_at_7am():
    """OFS at EDT 07:07 (sunrise) → NOT silenced."""
    dt = _edt(7, 7)
    assert in_quiet_hours(dt, "Outside Front Solar") is False


def test_ofs_not_silenced_at_noon():
    dt = _edt(12)
    assert in_quiet_hours(dt, "Outside Front Solar") is False


def test_ofs_not_silenced_at_evening_before_sunset():
    """OFS at EDT 19:00 (1 hour before sunset) → NOT silenced."""
    dt = _edt(19)
    assert in_quiet_hours(dt, "Outside Front Solar") is False


# ---------------------------------------------------------------------------
# Naive datetime handling
# ---------------------------------------------------------------------------


def test_naive_datetime_treated_as_utc_at_night():
    """A naive datetime (no tzinfo) is treated as UTC. 02:00 UTC = 22:00 EDT
    (evening) — that's night in EDT, so silenced."""
    naive = datetime(2026, 8, 24, 6, 0, 0)  # noqa: DTZ001 (intentionally naive to test the defensive branch)
    assert in_quiet_hours(naive, "Outside Front Solar") is True


def test_naive_datetime_treated_as_utc_during_day():
    """Naive datetime at 16:00 UTC = 12:00 EDT = noon → NOT silenced."""
    naive = datetime(2026, 8, 24, 16, 0, 0)  # noqa: DTZ001 (intentionally naive to test the defensive branch)
    assert in_quiet_hours(naive, "Outside Front Solar") is False


def test_aware_datetime_in_other_tz_is_converted():
    """A datetime with a non-EDT tzinfo should still produce correct EDT result."""
    # 02:00 UTC of Aug 25 → 22:00 EDT of Aug 24 (still night)
    aware_utc = datetime(2026, 8, 25, 2, 0, 0, tzinfo=UTC)
    assert in_quiet_hours(aware_utc, "Outside Front Solar") is True


# ---------------------------------------------------------------------------
# Module-purity contract: no ZoneInfo("America/New_York")
# ---------------------------------------------------------------------------


def test_no_zoneinfo_used():
    """Per memory rule: never import ZoneInfo('America/New_York').
    That auto-switches to EST in November and breaks the night-window logic."""
    import infra.quiet_hours as qh

    with open(qh.__file__) as f:
        src = f.read()
    parsed = ast.parse(src)
    zoneinfo_imports: list[Any] = []
    for node in ast.walk(parsed):
        if isinstance(node, ast.Import):
            zoneinfo_imports.extend(a.name for a in node.names if "zoneinfo" in a.name.lower())
        if isinstance(node, ast.ImportFrom) and node.module and "zoneinfo" in node.module.lower():
            zoneinfo_imports.append(node.module)
    assert zoneinfo_imports == [], (
        f"Found zoneinfo imports in quiet_hours.py — violates EDT-fixed rule: "
        f"{zoneinfo_imports}"
    )


def test_no_hardcoded_quiet_hours_window_constants():
    """Phase.112: time logic moved to infra.time_of_day. Quiet hours module
    should NOT define QUIET_HOURS_START / QUIET_HOURS_END wall-clock constants
    anymore — those would conflict with the sun-driven logic."""
    import infra.quiet_hours as qh
    assert not hasattr(qh, "QUIET_HOURS_START"), (
        "QUIET_HOURS_START should be removed — daylight/sunset is now driven by infra.time_of_day"
    )
    assert not hasattr(qh, "QUIET_HOURS_END"), (
        "QUIET_HOURS_END should be removed — same reason"
    )
    assert not hasattr(qh, "QUIET_HOURS_TZ"), (
        "QUIET_HOURS_TZ should be removed — EDT fixed UTC-4, no ZoneInfo"
    )
