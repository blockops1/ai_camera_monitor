"""
capture — Stage 1: legacy capture stub.

STATUS: legacy
THREAD SAFETY: single-threaded (one call per alert, runs inside camera semaphore)

INPUTS:
    - ctx: AlertContext (mutated: frame_paths set to empty list)

OUTPUTS:
    - mutates ctx.frame_paths = []
    - log.warning with deprecation message

PUBLIC API:
    capture_stage(ctx: AlertContext) -> None
        LEGACY capture stub. Phase.115 (2026-08-25) removed the
        6-frame RTSP capture path — the motion gate is now the sole
        producer of frames + crops. This stub exists for backward
        compat with tests that still reference capture_stage; callers
        should migrate to gate_aware_vehicle_capture (in
        _gate_aware_capture, called by process_alert) or to
        process_alert() directly.

DOES NOT DO:
    - Actually capture frames from RTSP — that path is removed in
      Phase.115. The motion gate is the sole producer now.
    - Open RTSP connections — infra.frame_capture owns that, but no
      longer called from here.
    - Populate ctx.frames, ctx.crop_a, ctx.crop_b — those are set by
      gate_aware_vehicle_capture from the gate verdict.

WHY HERE:
    Phase.115 removed the RTSP-based capture path. This stub
    exists so old tests + imports don't break. New code should call
    process_alert() (which calls gate_aware_vehicle_capture directly)
    and never touch this function.

CALLED BY:
    - (effectively dead in production — process_alert in __init__.py
      calls gate_aware_vehicle_capture directly. Kept for test fixtures
      that reference capture_stage by name.)
    - legacy tests that imported capture_stage from vehicle_event_pipeline

CALLS INTO:
    - (nothing — only stdlib logging)

RELATED:
    - _gate_aware_capture.gate_aware_vehicle_capture — the real capture
    - process_alert() in __init__.py — driver that calls gate_aware_*
"""
from __future__ import annotations

import logging

from .context import AlertContext

log = logging.getLogger(__name__)


def capture_stage(ctx: AlertContext) -> None:
    """Stage 1: LEGACY — capture 6 frames from RTSP.

    Phase.115 (2026-08-25): removed. The motion gate is now the sole
    producer of frames + crops. This function is kept as a stub for
    backward compat with tests that still reference it; any caller
    should migrate to `gate_aware_vehicle_capture()` (which uses the
    gate's 4 frames) or to `process_alert()` directly.

    Mutates ctx: frame_paths (set to empty list).
    """
    log.warning(
        f"[{ctx.alert_id}] capture_stage: DEPRECATED 2026-08-25 (Phase.115) "
        f"— gate is sole frame producer. Returning with no frames."
    )
    ctx.frame_paths = []
