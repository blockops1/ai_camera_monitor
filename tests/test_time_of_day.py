"""Unit tests for infra/time_of_day.py — Day/night classification for Resaca, GA farm.

Phase.111 §11.41.

These tests use FIXED datetime inputs (not real "now") so they're deterministic.
The function we're testing is `is_night_at_edt(dt_utc)` plus the night-window
helper. We're checking the civil-twilight boundaries work right across the full
24-hour cycle, including the trickier pre-dawn case.

Per AGENTS.md §3.4: tests must pass before commit. Per memory: EDT = fixed UTC-4
(no ZoneInfo("America/New_York"), no auto-fallback to EST in November).
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta, timezone

_root = __import__("pathlib").Path(__file__).resolve().parent.parent.parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

from infra.time_of_day import (
    FARM_LATITUDE,
    FARM_LONGITUDE,
    get_night_window_edt,
    is_night_at_edt,
    next_sunrise_edt,
    next_sunset_edt,
)

EDT = timezone(timedelta(hours=-4), name="EDT")


def _edt_to_utc(hour: int, minute: int = 0, day: int = 24) -> datetime:
    """Helper: build a UTC datetime for the given EDT hour/min on Aug 24, 2026.

    EDT is UTC-4. EDT hour 0-15 maps to UTC hour 4-19 same day.
    EDT hour 16-19 maps to UTC hour 20-23 same day.
    EDT hour 20-23 maps to UTC hour 0-3 NEXT day.
    """
    if hour < 20:
        utc_day = day
        utc_hour = hour + 4
    else:
        utc_day = day + 1
        utc_hour = (hour + 4) % 24
    return datetime(2026, 8, utc_day, utc_hour, minute, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Constants sanity
# ---------------------------------------------------------------------------


def test_farm_coords_match_resaca_ga():
    """Sanity check — Resaca, GA is at (34.5782, -84.9438)."""
    assert 34.0 < FARM_LATITUDE < 36.0  # Northern Georgia range
    assert -86.0 < FARM_LONGITUDE < -83.0  # Western-ish Georgia range


# ---------------------------------------------------------------------------
# is_night_at_edt() — 24-hour cycle
# ---------------------------------------------------------------------------


def test_midnight_is_night():
    """EDT 00:00 = deep night → True."""
    dt = _edt_to_utc(0)
    assert is_night_at_edt(dt) is True


def test_deep_night_is_night():
    """EDT 02:00 = deep night → True."""
    dt = _edt_to_utc(2)
    assert is_night_at_edt(dt) is True


def test_pre_dawn_twilight_is_night():
    """EDT 06:00 = pre-dawn → True."""
    dt = _edt_to_utc(6)
    assert is_night_at_edt(dt) is True


def test_civil_twilight_end_at_06_37_is_still_night():
    """EDT 06:37 = civil twilight end (07:07 - 30 min) → True (IR LEDs still on)."""
    dt = _edt_to_utc(6, 37)
    assert is_night_at_edt(dt) is True


def test_just_after_civil_twilight_is_day():
    """EDT 07:00 = 7 minutes past civil twilight → False (day)."""
    dt = _edt_to_utc(7)
    assert is_night_at_edt(dt) is False


def test_sunrise_is_day():
    """EDT 07:07 = astronomical sunrise → False."""
    dt = _edt_to_utc(7, 7)
    assert is_night_at_edt(dt) is False


def test_mid_morning_is_day():
    """EDT 10:00 = mid-morning → False."""
    dt = _edt_to_utc(10)
    assert is_night_at_edt(dt) is False


def test_noon_is_day():
    """EDT 12:00 = noon → False."""
    dt = _edt_to_utc(12)
    assert is_night_at_edt(dt) is False


def test_afternoon_is_day():
    """EDT 17:00 = afternoon → False."""
    dt = _edt_to_utc(17)
    assert is_night_at_edt(dt) is False


def test_evening_before_sunset_is_day():
    """EDT 19:00 = 1 hour before sunset → False."""
    dt = _edt_to_utc(19)
    assert is_night_at_edt(dt) is False


def test_astronomical_sunset_is_day():
    """EDT 20:15 = astronomical sunset → False (IR LEDs still off)."""
    dt = _edt_to_utc(20, 15)
    assert is_night_at_edt(dt) is False


def test_civil_twilight_begin_is_night():
    """EDT 20:46 = sunset + 30 min → True (civil twilight begins)."""
    dt = _edt_to_utc(20, 46)
    assert is_night_at_edt(dt) is True


def test_full_dark_is_night():
    """EDT 22:00 = full dark → True."""
    dt = _edt_to_utc(22)
    assert is_night_at_edt(dt) is True


# ---------------------------------------------------------------------------
# Boundary cases — exact transition times (test that we don't have off-by-one)
# ---------------------------------------------------------------------------


def test_one_minute_before_twilight_begin_is_day():
    """EDT 20:45 = 1 min before civil twilight → False (day)."""
    dt = _edt_to_utc(20, 45)
    assert is_night_at_edt(dt) is False


def test_one_minute_after_twilight_begin_is_night():
    """EDT 20:47 = 1 min after civil twilight → True."""
    dt = _edt_to_utc(20, 47)
    assert is_night_at_edt(dt) is True


def test_one_minute_before_twilight_end_is_night():
    """EDT 06:36 = 1 min before civil twilight ends → True."""
    dt = _edt_to_utc(6, 36)
    assert is_night_at_edt(dt) is True


def test_one_minute_after_twilight_end_is_day():
    """EDT 06:38 = 1 min after civil twilight ends → False (day)."""
    dt = _edt_to_utc(6, 38)
    assert is_night_at_edt(dt) is False


# ---------------------------------------------------------------------------
# Window shape — spans midnight EDT correctly
# ---------------------------------------------------------------------------


def test_get_night_window_from_morning_returns_tonight_window():
    """Window from EDT 10:00 morning should return tonight's window.
    The window crosses midnight EDT, so end's date is the day after start's date.
    Duration is ~10 hours."""
    morning = _edt_to_utc(10)
    start, end = get_night_window_edt(morning)
    assert start.date() < end.date()  # spans midnight
    duration_hours = (end - start).total_seconds() / 3600
    assert 9.5 < duration_hours < 10.5, f"expected ~10h, got {duration_hours:.2f}h"


def test_get_night_window_from_afternoon_returns_tonight_window():
    """Same window — returns upcoming tonight."""
    afternoon = _edt_to_utc(15)
    start, end = get_night_window_edt(afternoon)
    duration_hours = (end - start).total_seconds() / 3600
    assert 9.5 < duration_hours < 10.5


def test_get_night_window_from_evening_returns_current_window():
    """At EDT 22:00 we should still get the current window (lasted ~1.5h)."""
    evening = _edt_to_utc(22)
    start, end = get_night_window_edt(evening)
    duration_hours = (end - start).total_seconds() / 3600
    assert 9.5 < duration_hours < 10.5  # Same as upcoming, just different "current" framing


def test_get_night_window_from_pre_dawn_returns_current_window():
    """At EDT 02:00 (deep pre-dawn) we should still get a ~10h window."""
    predawn = _edt_to_utc(2)
    start, end = get_night_window_edt(predawn)
    duration_hours = (end - start).total_seconds() / 3600
    assert 9.5 < duration_hours < 10.5


# ---------------------------------------------------------------------------
# next_sunset_edt() and next_sunrise_edt() — diagnostic helpers
# ---------------------------------------------------------------------------


def test_next_sunset_from_morning_returns_today():
    """At EDT 10:00 morning, next sunset should be today's (~20:15 EDT)."""
    morning = _edt_to_utc(10)
    next_set = next_sunset_edt(morning)
    assert next_set.hour == 20
    # Sunset is at 20:15-16 EDT; accept any minute in that range with
    # a small tolerance for suntime's precision (returns ±2 sec).
    assert 14 <= next_set.minute <= 17


def test_next_sunset_from_afternoon_returns_today():
    """At EDT 17:00, next sunset should still be today's."""
    afternoon = _edt_to_utc(17)
    next_set = next_sunset_edt(afternoon)
    assert next_set.hour == 20


def test_next_sunset_from_night_returns_tomorrow():
    """At EDT 22:00, today's sunset has passed → next sunset is tomorrow."""
    night = _edt_to_utc(22)
    next_set = next_sunset_edt(night)
    assert next_set.date() > night.astimezone(EDT).date()


def test_next_sunrise_from_evening_returns_tomorrow():
    """At EDT 22:00, next sunrise is tomorrow morning."""
    night = _edt_to_utc(22)
    next_rise = next_sunrise_edt(night)
    assert next_rise.hour == 7
    assert next_rise.date() > night.astimezone(EDT).date()


def test_next_sunrise_from_morning_returns_today():
    """At EDT 02:00 deep night, next sunrise is today's morning (07:07)."""
    predawn = _edt_to_utc(2)
    next_rise = next_sunrise_edt(predawn)
    assert next_rise.hour == 7


# ---------------------------------------------------------------------------
# EDT fixity — never use ZoneInfo
# ---------------------------------------------------------------------------


def test_no_zoneinfo_used():
    """Per memory rule: never import ZoneInfo('America/New_York')."""
    import ast

    import infra.time_of_day as tod

    with open(tod.__file__) as f:
        src = f.read()
    parsed = ast.parse(src)
    zoneinfo_imports = []
    for node in ast.walk(parsed):
        if isinstance(node, ast.Import):
            zoneinfo_imports.extend(a.name for a in node.names if "zoneinfo" in a.name.lower())
        if isinstance(node, ast.ImportFrom) and node.module and "zoneinfo" in node.module.lower():
            zoneinfo_imports.append(node.module)
    assert zoneinfo_imports == [], f"Found zoneinfo imports: {zoneinfo_imports}"
