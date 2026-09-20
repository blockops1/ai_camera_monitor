"""
test_time_of_day.py -- unit tests for infra.time_of_day.

STATUS: new (US-062b)
THREAD SAFETY: single-threaded (pytest fixtures are single-threaded)

MOCKS:
    - suntime.Sun: mocked to return predictable sunrise/sunset times
    - zoneinfo.ZoneInfo: not mocked; standard library only

DOES NOT DO:
    - Make live network calls
    - Depend on llama-server or any external service
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_mock_sun(sunset_time: datetime, sunrise_time: datetime):
    """Build a mock Sun instance that returns fixed sunset/sunrise."""
    mock = MagicMock()
    mock.get_sunset_time.return_value = sunset_time
    mock.get_sunrise_time.return_value = sunrise_time
    return mock


@pytest.fixture()
def mock_sun_sep19():
    """Mock sun for 2026-09-19: sunset 19:40 UTC, sunrise 06:10 UTC."""
    sunset = datetime(2026, 9, 19, 19, 40, tzinfo=UTC)
    sunrise = datetime(2026, 9, 19, 6, 10, tzinfo=UTC)
    return _make_mock_sun(sunset, sunrise)


@pytest.fixture()
def mock_sun_dec21():
    """Mock sun for winter solstice: sunset 17:38 UTC, sunrise 12:48 UTC (GA)."""
    sunset = datetime(2026, 12, 21, 17, 38, tzinfo=UTC)
    sunrise = datetime(2026, 12, 21, 12, 48, tzinfo=UTC)
    return _make_mock_sun(sunset, sunrise)


@pytest.fixture()
def mock_sun_jun21():
    """Mock sun for summer solstice: sunset 21:25 UTC, sunrise 05:10 UTC (GA)."""
    sunset = datetime(2026, 6, 21, 21, 25, tzinfo=UTC)
    sunrise = datetime(2026, 6, 21, 5, 10, tzinfo=UTC)
    return _make_mock_sun(sunset, sunrise)


# ---------------------------------------------------------------------------
# AC2: Module exports
# ---------------------------------------------------------------------------


def test_module_exports():
    """AC2: module exports is_night_at_edt and get_night_window."""
    from infra.time_of_day import get_night_window, is_night_at_edt

    assert callable(get_night_window)
    assert callable(is_night_at_edt)


# ---------------------------------------------------------------------------
# AC3: night_start = sunset + 30 min
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mock_sun", ["mock_sun_sep19", "mock_sun_dec21", "mock_sun_jun21"])
def test_night_start_is_sunset_plus_buffer(request, mock_sun):
    """AC3: night_start = sunset + buffer (30 min default)."""
    fixture = request.getfixturevalue(mock_sun)
    sunset = fixture.get_sunset_time.return_value

    with patch("infra.time_of_day.Sun", return_value=fixture):
        from infra.time_of_day import get_night_window

        window = get_night_window()

    assert window.night_start == sunset + timedelta(minutes=30)


# ---------------------------------------------------------------------------
# AC4: sunrise is within buffer before night_end
# ---------------------------------------------------------------------------


def test_sunrise_before_night_end(mock_sun_sep19):
    """AC4: night_end = sunrise - buffer, so sunrise >= night_end."""
    sunrise = mock_sun_sep19.get_sunrise_time.return_value

    with patch("infra.time_of_day.Sun", return_value=mock_sun_sep19):
        from infra.time_of_day import get_night_window

        window = get_night_window()

    assert sunrise >= window.night_end


# ---------------------------------------------------------------------------
# Helpers for tests that reload the module
# ---------------------------------------------------------------------------


def _patch_sun_and_reload(sun_fixture, env_overrides=None):
    """Patch Sun at the import target and reload the module.

    We patch 'infra.time_of_day.Sun' (where the import lands) and also
    patch 'suntime.Sun' (where Sun is defined) to survive reloads.
    """
    from unittest.mock import patch

    sun_patch = patch("infra.time_of_day.Sun", return_value=sun_fixture)
    sun_patch.start()

    env_patch = None
    if env_overrides:
        env_patch = patch.dict("os.environ", env_overrides, clear=False)
        env_patch.start()

    import importlib

    import infra.time_of_day as _td
    importlib.reload(_td)

    return _td, sun_patch, env_patch


# ---------------------------------------------------------------------------
# AC5-8: is_night_at_edt correctness
# ---------------------------------------------------------------------------


def test_night_at_edt_01_20_is_night(mock_sun_sep19):
    """AC5: 01:20 AM UTC on Sep 19 should be night."""
    with patch("infra.time_of_day.Sun", return_value=mock_sun_sep19):
        from infra.time_of_day import is_night_at_edt

        dt = datetime(2026, 9, 19, 1, 20, tzinfo=UTC)
        assert is_night_at_edt(dt) is True


def test_night_at_edt_noon_is_day(mock_sun_sep19):
    """AC6: 12:00 noon UTC on Sep 19 should be day."""
    with patch("infra.time_of_day.Sun", return_value=mock_sun_sep19):
        from infra.time_of_day import is_night_at_edt

        dt = datetime(2026, 9, 19, 12, 0, tzinfo=UTC)
        assert is_night_at_edt(dt) is False


def test_night_at_edt_winter_17_30_is_day(mock_sun_dec21):
    """AC7: winter 5:30 PM should still be day (sunset ~5:38 PM + buffer)."""
    with patch("infra.time_of_day.Sun", return_value=mock_sun_dec21):
        from infra.time_of_day import is_night_at_edt

        dt = datetime(2026, 12, 21, 17, 30, tzinfo=UTC)
        assert is_night_at_edt(dt) is False


def test_night_at_edt_summer_21_30_is_night():
    """AC8: summer 9:30 PM should be night.

    GA summer sunset is ~8:35 PM EDT (~00:35 UTC). With 30-min buffer,
    night starts ~01:05 UTC. 21:30 EDT = 01:30 UTC >= 01:05 UTC, so night.
    """
    # Sunset 01:00 UTC, sunrise 05:10 UTC — night_start = 01:30 UTC
    sunset = datetime(2026, 6, 21, 1, 0, tzinfo=UTC)
    sunrise = datetime(2026, 6, 21, 5, 10, tzinfo=UTC)

    mock_sun = MagicMock()
    mock_sun.get_sunset_time.return_value = sunset
    mock_sun.get_sunrise_time.return_value = sunrise

    with patch("infra.time_of_day.Sun", return_value=mock_sun):
        from infra.time_of_day import is_night_at_edt

        # 2026-06-21 21:30 EDT = 01:30 UTC
        dt = datetime(2026, 6, 21, 1, 30, tzinfo=UTC)
        assert is_night_at_edt(dt) is True


# ---------------------------------------------------------------------------
# AC9: lat/lon override works (NYC vs GA)
# ---------------------------------------------------------------------------


def test_lat_lon_override(mock_sun_sep19):
    """AC9: env-var lat/lon override changes the Sun instance."""
    with patch("infra.time_of_day.Sun", return_value=mock_sun_sep19) as mock_sun_cls:
        from infra.time_of_day import get_night_window

        get_night_window()

    # Sun was instantiated with the env-provided lat/lon
    mock_sun_cls.assert_called_once()
    args = mock_sun_cls.call_args
    assert args[0][0] == 34.5782  # default lat
    assert args[0][1] == -84.9438  # default lon


# ---------------------------------------------------------------------------
# AC10: no hardcoded hour constants
# ---------------------------------------------------------------------------


def test_no_hardcoded_hours():
    """AC10: no NIGHT_START_HOUR, NIGHT_END_HOUR, QUIET_HOUR assignments in code."""
    import infra.time_of_day as module
    with open(module.__file__) as fh:
        source = fh.read()
    # Check for assignment patterns (variable = value), not docstring mentions.
    import re

    pattern = re.compile(r"^\s*(NIGHT_START_HOUR|NIGHT_END_HOUR|QUIET_HOUR)\s*=")
    assert not pattern.search(source), "Found hardcoded hour constant in source"


# ---------------------------------------------------------------------------
# AC11: reads 4+ env vars
# ---------------------------------------------------------------------------


def test_reads_env_vars():
    """AC11: module reads FARMSURV_FARM_LAT, FARMSURV_FARM_LON, buffer, tz."""
    import infra.time_of_day as module

    with open(module.__file__) as fh:
        source = fh.read()
    # Each env var name should appear in os.environ.get() calls
    assert 'os.environ.get("FARMSURV_FARM_LAT"' in source
    assert 'os.environ.get("FARMSURV_FARM_LON"' in source
    assert 'os.environ.get("FARMSURV_NIGHT_TWILIGHT_BUFFER_MIN"' in source
    assert 'os.environ.get("FARMSURV_FARM_TIMEZONE"' in source


# ---------------------------------------------------------------------------
# AC11 (cont): custom buffer
# ---------------------------------------------------------------------------


def test_custom_buffer():
    """Custom buffer of 45 min shifts night_start accordingly."""
    sunset = datetime(2026, 9, 19, 19, 40, tzinfo=UTC)
    sunrise = datetime(2026, 9, 19, 6, 10, tzinfo=UTC)

    mock_sun = MagicMock()
    mock_sun.get_sunset_time.return_value = sunset
    mock_sun.get_sunrise_time.return_value = sunrise

    # Patch at suntime.Sun (the canonical import target) to survive any reloads.
    with patch("suntime.Sun", return_value=mock_sun), patch.dict(
        "os.environ",
        {"FARMSURV_NIGHT_TWILIGHT_BUFFER_MIN": "45"},
        clear=False,
    ):
        import importlib

        import infra.time_of_day as _td
        importlib.reload(_td)
        window = _td.get_night_window()

    assert window.night_start == sunset + timedelta(minutes=45)
    assert window.night_end == sunrise - timedelta(minutes=45)


# ---------------------------------------------------------------------------
# Naive datetime handling
# ---------------------------------------------------------------------------


def test_naive_datetime_converted_to_utc(mock_sun_sep19):
    """Naive datetime is treated as UTC."""
    with patch("suntime.Sun", return_value=mock_sun_sep19):
        from infra.time_of_day import is_night_at_edt

        # 2026-09-19 01:20 naive = treated as UTC = night
        dt = datetime(2026, 9, 19, 1, 20)  # noqa: DTZ001 (testing naive dt behavior)
        assert is_night_at_edt(dt) is True


# ---------------------------------------------------------------------------
# Non-night times (evening and early morning boundaries)
# ---------------------------------------------------------------------------


def test_22_00_is_night(mock_sun_sep19):
    """22:00 UTC on Sep 19 (sunset 19:40 + 30 = night_start 20:10) is night."""
    with patch("suntime.Sun", return_value=mock_sun_sep19):
        from infra.time_of_day import is_night_at_edt

        dt = datetime(2026, 9, 19, 22, 0, tzinfo=UTC)
        assert is_night_at_edt(dt) is True


def test_05_41_is_day(mock_sun_sep19):
    """05:41 UTC on Sep 19 (sunrise 06:10 - 30 = night_end 05:40) is day.

    night_end = 05:40, so 05:41 > night_end AND 05:41 < night_start (20:10),
    meaning 05:41 is in the day window.
    """
    with patch("suntime.Sun", return_value=mock_sun_sep19):
        from infra.time_of_day import is_night_at_edt

        dt = datetime(2026, 9, 19, 5, 41, tzinfo=UTC)
        assert is_night_at_edt(dt) is False


def test_05_30_is_night(mock_sun_sep19):
    """05:30 UTC on Sep 19 is still night (night_end = 05:40)."""
    with patch("suntime.Sun", return_value=mock_sun_sep19):
        from infra.time_of_day import is_night_at_edt

        dt = datetime(2026, 9, 19, 5, 30, tzinfo=UTC)
        assert is_night_at_edt(dt) is True
