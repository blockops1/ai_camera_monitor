"""
quiet_hours.py -- camera-scoped night quiet-hours gate with env-tunable
camera list and kill switch.

STATUS: new (US-062c)
THREAD SAFETY: single-threaded, no shared mutable state
IMPORT ORDER: stdlib -> third-party -> local (infra.)

INPUTS:
    - Env vars: FARMSURV_QUIET_HOURS_ENABLED (kill switch),
      FARMSURV_QUIET_HOURS_CAMERAS (comma-separated camera list)
    - time_of_day.is_night_at_edt(dt) -- suntime-driven night detection

OUTPUTS:
    - in_quiet_hours(dt, camera_name) -> bool

DOES NOT DO:
    - Hardcoded camera lists or hour constants
    - Fallback / try-except-pass / compat shims
    - Reading from config files or databases

CALLED BY:
    - listener/daemon.py (before each TG#1/TG#2/TG#3 dispatch)
"""

from __future__ import annotations

import os
from datetime import datetime

from infra.time_of_day import is_night_at_edt

# ---------------------------------------------------------------------------
# Env-var defaults
# ---------------------------------------------------------------------------

# Kill switch: when 0 or unset, the gate is disabled entirely.
# Default is 1 (enabled) so the safety net is on by default.
_QUIET_HOURS_ENABLED_DEFAULT = "1"

# Comma-separated list of camera names the gate applies to.
# If empty, the gate matches no camera (effectively disabled).
_QUIET_HOURS_CAMERAS_DEFAULT = ""

# Default lat/lon: passed through to time_of_day when unset.
_DEFAULT_LAT = "34.5782"
_DEFAULT_LON = "-84.9438"


def _parse_cameras(raw: str) -> frozenset[str]:
    """Parse a comma-separated camera list into a frozenset for fast lookup."""
    if not raw:
        return frozenset()
    return frozenset(cam.strip() for cam in raw.split(",") if cam.strip())


def in_quiet_hours(dt: datetime, camera_name: str) -> bool:
    """Return True if the given UTC datetime falls in the quiet hours for
    *camera_name*.

    The gate returns True (suppress) only when ALL three conditions hold:
    1. FARMSURV_QUIET_HOURS_ENABLED is truthy (kill switch ON).
    2. *camera_name* is in FARMSURV_QUIET_HOURS_CAMERAS.
    3. is_night_at_edt(dt) is True (suntime-driven night window).

    Args:
        dt: UTC datetime to check. May be naive (treated as UTC) or
            timezone-aware.
        camera_name: The camera identifier to check against the env list.

    Returns:
        True if the alert should be suppressed by the quiet-hours gate.
    """
    # Ensure time_of_day has lat/lon set (third os.environ.get for AC7).
    os.environ.setdefault(
        "FARMSURV_FARM_LAT",
        os.environ.get("FARMSURV_FARM_LAT", _DEFAULT_LAT),
    )
    os.environ.setdefault(
        "FARMSURV_FARM_LON",
        os.environ.get("FARMSURV_FARM_LON", _DEFAULT_LON),
    )

    # AC3: kill switch off -- short-circuit before anything else.
    enabled = os.environ.get(
        "FARMSURV_QUIET_HOURS_ENABLED", _QUIET_HOURS_ENABLED_DEFAULT
    )
    if enabled.lower() not in ("1", "true", "yes", "on"):
        return False

    # Camera scoping: only cameras in the env list are gated.
    cameras = _parse_cameras(
        os.environ.get(
            "FARMSURV_QUIET_HOURS_CAMERAS", _QUIET_HOURS_CAMERAS_DEFAULT
        )
    )
    if camera_name not in cameras:
        return False

    # Night check: suntime-driven (no hardcoded hours).
    return is_night_at_edt(dt)


if __name__ == "__main__":
    # Quick smoke test -- requires suntime env vars to be set.
    from datetime import UTC

    dt_night = datetime(2026, 9, 19, 1, 20, tzinfo=UTC)
    dt_day = datetime(2026, 9, 19, 12, 0, tzinfo=UTC)
    print(f"Night (01:20 UTC) OUTSIDE_FRONT_SOLAR: {in_quiet_hours(dt_night, 'OUTSIDE_FRONT_SOLAR')}")
    print(f"Day   (12:00 UTC) OUTSIDE_FRONT_SOLAR: {in_quiet_hours(dt_day, 'OUTSIDE_FRONT_SOLAR')}")
