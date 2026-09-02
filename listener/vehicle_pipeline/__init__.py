"""
vehicle_pipeline — 6-stage vehicle event pipeline (split from vehicle_event_pipeline.py).

STATUS: stable
THREAD SAFETY: single-threaded (one AlertContext per alert; runs inside camera
    semaphore for stages 1-5, OUTSIDE for stage 6)

INPUTS:
    - ctx: AlertContext (constructed by listener, passed to process_alert)

OUTPUTS:
    - process_alert: returns dict for the listener with
      {event_type, vehicle_match, telegram_sent, telegram_error, alert_id}

PUBLIC API:
    process_alert(ctx: AlertContext) -> dict
        Drive the 6 stages in sequence: capture → identify → match →
        select_best_frame → generate_alert (inside the camera-semaphore
        with-block) → emit_result (outside the block).
    AlertContext
        The per-alert carrier dataclass. Re-exported from .context for
        callers that just want `from listener.vehicle_pipeline import AlertContext`.

PACKAGE STRUCTURE:
    context.py       — AlertContext dataclass (carrier through all 6 stages)
    capture.py       — capture_stage (Phase 6B.115 legacy stub)
    identify.py      — identify_stage + _coerce_vision_result + _non_vehicle_first_pass
    match.py         — match_stage + _extract_signature + _to_kv_id_score
                        + _vision_summary_str + _emit_match_loop + VISION_CONFIDENCE_FLOOR
    select_frame.py  — select_best_frame_stage
    alert.py         — generate_alert_stage (LLM threat-level call)
    emit.py          — emit_result_stage + _result_dict
    notify.py        — _send_arriving_message + _format_vehicle_summary

DOES NOT DO:
    - Decide which event type routes here — the listener's _process_alert
      owns that. (Phase 6B.106 + §11.111: gate is suppress-only, routing
      is camera+event based.)
    - Run the motion gate — _motion_gate_dispatch owns that.
    - Construct the AlertContext — listener._process_alert does that.

WHY HERE:
    Split from listener/vehicle_event_pipeline.py in Phase 6B.170 (§11.111,
    2026-09-02). Mirror pattern of §11.106 (person pipeline) and §11.86
    (animal pipeline scaffold). Goal: each stage becomes a 1-purpose
    module that can be edited independently without merge conflicts on
    the 1554-line monolith.

    Module-load imports (vehicle_matcher.matcher, infra.cameras.by_code/
    code_for) are pinned at the TOP of this __init__.py because of the
    package-shadowing bug fixed in Phase 6B.116. If anything in the
    production listener registers `vehicle_matcher` as a module in
    sys.modules (via `from infra.vehicle_matcher import X`), the bare
    name `vehicle_matcher` is shadowed and Python refuses to look for
    `vehicle_matcher.matcher` as a subpackage — raising
    `ModuleNotFoundError: 'vehicle_matcher' is not a package` even
    though `vehicle_matcher/` is right there on disk. Importing the
    package form at module load (below) ensures
    `sys.modules['vehicle_matcher']` is the package (not a module)
    before any function runs. The lazy imports inside match_stage /
    _emit_match_loop become safety-net fallbacks that always succeed
    because the package is already cached.

CALLED BY:
    - listener.listener._process_alert — for non-person, non-animal
      events (the gatekeeper vehicle dispatch)

CALLS INTO:
    - infra.camera_queue.acquire_for_camera — per-camera semaphore
    - _gate_aware_capture.gate_aware_vehicle_capture, SkipEvent — capture stage
    - All 6 stage submodules in this package

RELATED:
    - §11.111 in PLAN.md — extraction plan
    - §11.106 — person pipeline extraction precedent
    - §11.86 — animal pipeline scaffold precedent
    - listener/vehicle_event_pipeline.py — legacy monolith (kept as shim for §11.111;
      removed in follow-up commit)
"""
from __future__ import annotations

import logging

# Phase 6B.116: module-load imports. MUST be at module-load time, not
# lazy, because of the package-shadowing bug described in the
# docstring above. These two lines ensure sys.modules has the package
# form before any function in this package runs.
from vehicle_matcher.matcher import (  # module-load registration
    MatchVerdict,
    NoMatch,
)
from infra.cameras import (  # module-load registration
    by_code as _by_camera_code,
    code_for as _code_for_camera,
)

from .context import AlertContext
from .match import VISION_CONFIDENCE_FLOOR
from . import capture, identify, match, select_frame, alert, emit, notify  # noqa: F401

log = logging.getLogger(__name__)


def process_alert(ctx: AlertContext) -> dict:
    """Drive the 6 stages in sequence.

    The order is:
        capture → identify → match → select_best_frame → generate_alert
        (inside the with acquire_for_camera block)
        emit_result (outside the block — uses no per-camera semaphore)

    Returns: the alert result dict (see emit_result_stage).

    Phase 6B.115 (§11.46, 2026-08-25): The motion gate is the sole
    producer of frames + crops on the vehicle path. Stage 1 now uses
    `gate_aware_vehicle_capture(ctx)` which ONLY consumes the gate's
    4 frames (no legacy fallback). If the gate's frames are missing
    on disk for any reason, ctx.frame_paths stays empty and we return
    a `sent=False` result.
    """
    from infra.camera_queue import acquire_for_camera
    # Dual-context import (matches the pattern in listener.py for
    # vehicle_event_pipeline / person_event_pipeline): bare import works
    # when listener.py runs as __main__ (sys.path[0] = listener/);
    # package import works in tests (pytest adds repo root + initializes
    # listener as a package). Fixes ModuleNotFoundError: 'listener' is not
    # a package in production.
    try:
        from _gate_aware_capture import SkipEvent, gate_aware_vehicle_capture
    except ImportError:
        from listener._gate_aware_capture import (
            SkipEvent,
            gate_aware_vehicle_capture,
        )

    log.info(
        f"[{ctx.alert_id}] process_alert: starting "
        f"camera={ctx.camera_name} event={ctx.event_type}"
    )

    with acquire_for_camera(ctx.camera_name):
        try:
            gate_aware_vehicle_capture(ctx)
        except SkipEvent:
            # Phase 6B.115: gate didn't produce frames; no legacy
            # fallback. Skip the alert, return sent=False.
            return emit._result_dict(ctx, sent=False)
        identify.identify_stage(ctx)
        match.match_stage(ctx)
        select_frame.select_best_frame_stage(ctx)
        alert.generate_alert_stage(ctx)

    return emit.emit_result_stage(ctx)


__all__ = [
    "process_alert",
    "AlertContext",
    "VISION_CONFIDENCE_FLOOR",
]
