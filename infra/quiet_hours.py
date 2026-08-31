"""
quiet_hours.py — Phase.52 / 6B.112 — Quiet-hours Telegram suppression for outside cameras.

STATUS: stable
THREAD SAFETY: thread-safe (pure functions, no shared state)

INPUTS:
    - function arg now: datetime (required) — current time (any tz)
    - function arg camera_name: str (required)

OUTPUTS:
    - return value: bool — True if Telegram should be suppressed
    - log line on suppression (info level): "QUIET_HOURS_SUPPRESSED
      channel=... camera=... event=... local_time=HH:MM reason=quiet_hours"

PUBLIC API:
    in_quiet_hours(now: datetime, camera_name: str) -> bool
        True if `now` falls in the camera's quiet window AND the camera is in
        QUIET_HOURS_CAMERAS. False otherwise.
    QUIET_HOURS_CAMERAS — frozenset[str] of camera names subject to the filter

DOES NOT DO:
    - Decide whether to actually suppress a Telegram — the caller
      (notifier.py) does that
    - Read camera config — the camera list is hard-coded (4 cameras)
    - Skip frame capture or vision — those run normally; only Telegram is silenced

WHY HERE:
    the operator 2026-08-04 OOB: "I am thinking that maybe we need to turn off
    alerts from CAM1 and CAM3 at night." Investigation showed the noise
    was county-road headlights triggering AI vehicle/people/animal
    detection on the four outside cameras (CAM1, CAM3, CAM4, CAM2).
    During the night window, all Telegram emissions from these four
    cameras are suppressed. Frames still captured, vision still runs,
    state still updates — only Telegram is silenced.

    Phase.112 (the operator 2026-08-24): time logic refactored from a fixed
    21:00–07:00 wall-clock window with ZoneInfo("America/New_York") to
    astro/sunrise-sunset-based night detection via infra.time_of_day.
    Keeps QUIET_HOURS_CAMERAS unchanged. Window now tracks actual dusk/dawn
    at the farm, not the wall-clock — and avoids the auto-EST-fallback bug
    from ZoneInfo("America/New_York") that would shift this module by 1h
    against the rest of the system's EDT-fixed logic each November.

CALLED BY:
    - infra.notifier: in_quiet_hours() check before every send

CALLS INTO:
    - infra.time_of_day.is_night_at_edt: astro night check
    - logging: suppression audit line

RELATED:
    - infra.notifier — the caller that suppresses Telegram on True
    - infra.time_of_day — night-window source of truth (Phase.111 §11.41)
"""
from __future__ import annotations

from datetime import datetime

# Cameras subject to quiet-hours Telegram suppression (Phase.52).
# Phase.167 §13.4 Commit 17 (T3 C17): CAM{N} codes per
# infra.cameras._LEGACY_PREFIX_TO_CODE — CAM3/CAM4/CAM5/CAM6 are the
# 4 exterior yard/driveway cameras (CAM1/CAM2 are the doorbell cameras
# and are NOT in this set — doorbell signals are security-relevant at
# any hour, see §11.6 PLAN commentary).
QUIET_HOURS_CAMERAS: frozenset[str] = frozenset({
    "CAM5",  # → CAM1
    "CAM4",  # → CAM3
    "CAM6",  # → CAM4
    "CAM3",  # → CAM2
})


def in_quiet_hours(now: datetime, camera_name: str) -> bool:
    """Return True if Telegram should be suppressed for this camera now.

    A camera is silenced iff (a) it's in QUIET_HOURS_CAMERAS AND (b) it's
    nighttime at the farm. Daytime → never silenced. Cameras outside
    QUIET_HOURS_CAMERAS (CAM1, CAM2) are never silenced regardless of hour.

    Phase.112: "night" is now derived from astronomical sunrise/sunset
    at the farm (Resaca, GA) via infra.time_of_day.is_night_at_edt, not from
    a fixed wall-clock window. Civil twilight is built into the night
    window (sunset + 30 min → sunrise − 30 min EDT).

    Phase.167 §13.4 (T3 C17): camera_name is translated via
    infra.cameras.code_for() because QUIET_HOURS_CAMERAS uses CAM{N}
    codes (not friendly names). Falls back to literal string comparison
    on miss so test fixtures that pass synthetic codes still work.

    Args:
        now: A timezone-aware datetime. Naive datetimes are treated as UTC.
        camera_name: The camera name as it appears in the alert payload.

    Returns:
        True iff the camera is in QUIET_HOURS_CAMERAS AND it's nighttime.
    """
    from infra.cameras import code_for  # §13.4: name → CAM{N}
    if code_for(camera_name) not in QUIET_HOURS_CAMERAS:
        return False

    # Late import to avoid circular dependency (quiet_hours → time_of_day → suntime).
    from infra.time_of_day import is_night_at_edt

    return is_night_at_edt(now)
