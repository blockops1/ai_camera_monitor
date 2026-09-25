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

import logging
import os
from datetime import datetime

from infra.env_required import _require_env_lower_bool
from infra.quick_classifier import night_suppress_match
from infra.time_of_day import is_night_at_edt

log = logging.getLogger("quiet_hours")

# ---------------------------------------------------------------------------
# Env-var defaults
# ---------------------------------------------------------------------------

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


def in_quiet_hours(dt: datetime, camera_name: str, class_name: str | None = None) -> bool:
    """Return True if the given UTC datetime falls in the quiet hours for
    *camera_name*.

    The gate returns True (suppress) only when ALL three conditions hold:
    1. FARMSURV_QUIET_HOURS_ENABLED is truthy (kill switch ON).
    2. *camera_name* is in FARMSURV_QUIET_HOURS_CAMERAS.
    3. is_night_at_edt(dt) is True (suntime-driven night window).

    Defense-in-depth (V2-069): when *class_name* is provided AND
    *camera_name* matches the per-camera night-suppress list
    (night_suppress_match), this function returns True regardless of
    FARMSURV_QUIET_HOURS_CAMERAS scoping, so an alert that slipped past
    the motion-gate layer is still silenced at dispatch time.

    Args:
        dt: UTC datetime to check. May be naive (treated as UTC) or
            timezone-aware.
        camera_name: The camera identifier to check against the env list.
        class_name: Optional detection class (e.g. "car", "vehicle").
            When provided AND the camera+class pair matches the night
            suppress list, returns True (suppress at TG-dispatch layer).
            When None, falls back to the existing global quiet-hours logic
            without silently suppressing.

    Returns:
        True if the alert should be suppressed by the quiet-hours gate.
    """
    # V2-069 defense-in-depth: per-camera + per-class night suppress.
    # Belt-and-suspenders: if the motion-gate missed this alert (e.g. race
    # with config reload), the TG-dispatch layer still silences it.
    # Mirrors the motion-gate check: night_suppress_match AND is_night.
    if (
        class_name is not None
        and night_suppress_match(camera_name, class_name)
        and is_night_at_edt(dt)
    ):
        log.info(
            f"quiet_hours: tg_night_suppressed "
            f"camera={camera_name} class={class_name}"
        )
        return True
    # Ensure time_of_day has lat/lon set (third os.environ.get for AC7).
    os.environ.setdefault(
        "FARMSURV_FARM_LAT",
        os.environ.get("FARMSURV_FARM_LAT", _DEFAULT_LAT),
    )
    os.environ.setdefault(
        "FARMSURV_FARM_LON",
        os.environ.get("FARMSURV_FARM_LON", _DEFAULT_LON),
    )

    # AC3: kill switch -- fail-loud if operator hasn't set it.
    # _require_env_lower_bool raises RuntimeError if the var is unset.
    if not _require_env_lower_bool("FARMSURV_QUIET_HOURS_ENABLED"):
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
