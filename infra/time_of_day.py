"""
time_of_day.py -- suntime-driven night window for headlight FP suppression.

STATUS: new (US-062b)
THREAD SAFETY: single-threaded, no shared mutable state
IMPORT ORDER: stdlib -> third-party -> local (infra.)

INPUTS:
    - Env vars: FARMSURV_FARM_LAT, FARMSURV_FARM_LON, FARMSURV_NIGHT_TWILIGHT_BUFFER_MIN

OUTPUTS:
    - NightWindow: sunset, sunrise, night_start, night_end datetimes (tz-aware, UTC)
    - is_night_at_edt(dt) -> bool: whether a UTC datetime falls in the night window

DOES NOT DO:
    - Hardcoded hour constants (NIGHT_START_HOUR, NIGHT_END_HOUR, QUIET_HOUR)
    - Fallback / try-except-pass / compat shims
    - Reading from config files or databases

CALLED BY:
    - listener/pipeline.py (night-time headlight FP suppression)
"""

from __future__ import annotations

import os
import zoneinfo
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone

from suntime import Sun

# ---------------------------------------------------------------------------
# NightWindow -- immutable result of get_night_window()
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class NightWindow:
    """Sunrise/sunset window with 30-min twilight buffer.

    - sunset: when sun dips below horizon (UTC)
    - sunrise: when sun rises above horizon (UTC)
    - night_start: sunset + buffer (start of headlight suppression)
    - night_end:   sunrise - buffer (end of headlight suppression)
    """

    sunset: datetime
    sunrise: datetime
    night_start: datetime
    night_end: datetime


# ---------------------------------------------------------------------------
# Env-var defaults
# ---------------------------------------------------------------------------

_DEFAULT_LAT = 34.5782  # Georgia, US
_DEFAULT_LON = -84.9438
_DEFAULT_BUFFER_MIN = 30
_DEFAULT_TZ = "UTC"


def _read_env() -> tuple[float, float, int, str]:
    """Read configuration from environment. One os.environ.get per line."""
    lat = float(os.environ.get("FARMSURV_FARM_LAT", str(_DEFAULT_LAT)))
    lon = float(os.environ.get("FARMSURV_FARM_LON", str(_DEFAULT_LON)))
    buffer_min = int(os.environ.get("FARMSURV_NIGHT_TWILIGHT_BUFFER_MIN", str(_DEFAULT_BUFFER_MIN)))
    tz_str = os.environ.get("FARMSURV_FARM_TIMEZONE", _DEFAULT_TZ)
    return lat, lon, buffer_min, tz_str


# ---------------------------------------------------------------------------
# Core API
# ---------------------------------------------------------------------------

def get_night_window(at_date: datetime | None = None) -> NightWindow:
    """Compute today's sunrise/sunset + twilight buffer.

    Args:
        at_date: Optional date override. Defaults to today.

    Returns:
        NightWindow with sunset, sunrise, night_start, night_end.

    Raises:
        SunTimeException: if no sunrise/sunset on this date at this lat/lon.
    """
    lat, lon, buffer_min, tz_str = _read_env()

    if tz_str == "UTC" or tz_str is None:
        tz: timezone | zoneinfo.ZoneInfo = UTC
    else:
        tz = zoneinfo.ZoneInfo(tz_str)

    sun = Sun(lat, lon)

    if at_date is None:
        at_date = datetime.now(tz)
    elif at_date.tzinfo is not None:
        at_date = at_date.astimezone(UTC)

    # Calculate in the configured timezone, then convert to UTC for NightWindow.
    sunset_local = sun.get_sunset_time(at_date, time_zone=tz)  # type: ignore[arg-type]
    sunrise_local = sun.get_sunrise_time(at_date, time_zone=tz)  # type: ignore[arg-type]

    sunset_utc = sunset_local.astimezone(UTC)
    sunrise_utc = sunrise_local.astimezone(UTC)

    buffer = timedelta(minutes=buffer_min)

    return NightWindow(
        sunset=sunset_utc,
        sunrise=sunrise_utc,
        night_start=sunset_utc + buffer,
        night_end=sunrise_utc - buffer,
    )


def is_night_at_edt(dt: datetime) -> bool:
    """Check if a UTC datetime falls within the night window.

    The night window starts at sunset + buffer and ends at sunrise - buffer.
    Because the window crosses midnight, "in night" means dt >= night_start
    (evening) OR dt <= night_end (early morning).

    Args:
        dt: UTC datetime to check. Must be tz-aware or naive UTC.

    Returns:
        True if dt is in the night window.
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    else:
        dt = dt.astimezone(UTC)

    window = get_night_window(at_date=dt)
    return dt >= window.night_start or dt <= window.night_end


if __name__ == "__main__":
    window = get_night_window()
    print(f"Sunset:   {window.sunset.strftime('%H:%M UTC')}")
    print(f"Sunrise:  {window.sunrise.strftime('%H:%M UTC')}")
    print(f"Night:    {window.night_start.strftime('%H:%M')} -> {window.night_end.strftime('%H:%M')} UTC")
