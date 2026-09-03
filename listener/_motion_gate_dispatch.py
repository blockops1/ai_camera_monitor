"""_motion_gate_dispatch — listener.py integration for the motion gate.

STATUS: provisional (Phase.107 §11.37, 2026-08-23)
THREAD SAFETY: single-threaded (per-webhook, called from _process_alert)
INPUTS:
  - alert_id, camera_name, rtsp_url (from webhook)
  - output_dir for crop files
OUTPUTS:
  - gate verdict (vehicle/person/suppress) → routes to existing pipeline
  - or None → fall back to legacy path (no gate run)
PUBLIC API:
  - maybe_run_motion_gate(...) -> GateVerdict | None
  - is_motion_gate_enabled() -> bool (env var lookup)
DOES NOT DO:
  - Does NOT replace the existing pipelines (those stay unchanged this phase)
  - Does NOT replace the legacy _process_alert path (env var gate)
  - Does NOT save Telegram / audit / alert files
  - Does NOT orchestrate vehicle_event_pipeline or person_event_pipeline
    (listener.py calls them directly based on verdict.decision)
CALLED BY: listener/listener.py _process_alert (and _process_person_alert)
CALLS INTO:
  - listener/motion_gate_pipeline.py (gate logic)
  - infra/frame_capture.capture_frames (capture 4 frames from RTSP)
  - infra/paths (paths)
RELATED:
  - PLAN.md §11.37 (locked architecture)
  - AGENTS.md §4 cutover (env-var flag pattern)

This module is the wiring between the gate (a pure function) and the
listener (which does orchestration). It owns:
  - Capturing 4 frames from RTSP for the gate
  - Calling motion_gate_pipeline.run()
  - Logging the verdict
  - Returning it to listener.py so it can route

listener.py is responsible for the routing decision — this module
returns the verdict, it does NOT call vehicle_event_pipeline or
person_event_pipeline directly. Per module-purity: this module is a
listener-side helper, motion_gate_pipeline is the gate itself, and
listener.py orchestrates.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, cast

from infra.frame_capture import capture_frames

# Phase.152 — per-camera gate_enabled lookup. Dual-context import
# pattern matching the run_gate() import below (works when run as
# __main__ with sys.path[0]=listener/ and when imported as
# listener._motion_gate_dispatch from tests).
try:
    from motion_gate_pipeline import is_gate_enabled
except ImportError:
    from listener.motion_gate_pipeline import is_gate_enabled

if TYPE_CHECKING:
    from listener.motion_gate_pipeline import GateVerdict

log = logging.getLogger(__name__)


# Env var to enable the motion gate. Default 0 = legacy path (no gate).
# Set to 1 to use the gate (PLAN §11.37 cutover step 2).
# Set to 2 in production after week 1 of parallel-run validation.
ENV_VAR_NAME = "MOTION_GATE_ENABLED"


# Phase.128 — OFS gatekeeper gets a 4-frame pre-event motion trail
# (indices [0, 30, 60, 90] at 15fps → T-6s through T+0s of camera time,
# 2s spacing). All other cameras keep the trailing-tail behavior.
#
# Phase.143 (2026-08-27, Note): OFG (Outside Front Garage) joins
# the gatekeeper list for the SAME trail semantics. Reason: the person
# pipeline's Telegram album (§11.64, 6B.141) shows 4 wide frames, and
# Note observed they were all at the same instant (recent_frames pulled
# 4 consecutive frames at ~67ms apart) instead of the spaced trail he
# expects. Adding OFG here makes its gate capture use the offset path,
# mirroring OFS. Qwen still gets frames[1]+frames[2] bracketing the
# motion event, so the spacing change is invisible to vision but
# visible in the Telegram album.
#
# Phase.160 (2026-08-28, Note): the offsets now anchor the
# gate trail at the WEBHOOK moment, not at the ring's newest frame.
# At 2fps with ring=32, capture_delay=8s, the indices (11, 15, 19, 23)
# correspond to T_w-2s, T_w+0s, T_w+2s, T_w+4s — a 6-second window
# centered on the camera-side detection. At 15fps with ring=180 the
# equivalent is (29, 59, 89, 119).
#
# The earlier formula (89, 85, 81, 77) at 2fps/ring=90 gave frames
# ending at "newest in ring" — but newest at gate-time is T_w + 8s,
# not T_w. So the trail was POST-webhook by 1.5-7.5s: the gate was
# looking at the AFTER-state, not the motion that triggered the
# camera. Fixed by anchoring at T_w with capture_delay offset.
#
# Phase.167 §13.4 Commit 17 (T3 C17): codes are CAM{N} per
# infra.cameras._LEGACY_PREFIX_TO_CODE (FRONT→CAM1, BACK→CAM2,
# OUTSIDE_FRONT_GARAGE→CAM3, OUTSIDE_FRONT_POWER→CAM4,
# OUTSIDE_FRONT_SOLAR→CAM5, OUTSIDE_BACK_SOLAR→CAM6). The operator's
# legacy env gets translated to CAM{N} codes by the legacy env parser;
# a fresh env with CAM{N}_IP keys uses CAM{N} directly.
# Why locally duplicated instead of importing from listener.listener:
# _motion_gate_dispatch is a hot-path helper called from listener.py
# before the gate has run. Importing listener.listener here would create
# a circular import (listener → _motion_gate_dispatch → listener).
# Mirrors listener/listener.py to keep them in sync.
GATEKEEPER_CAMERAS = frozenset({
    "CAM5",  # OUTSIDE_FRONT_SOLAR — original gatekeeper (§11.79)
    "CAM3",  # OUTSIDE_FRONT_GARAGE — promoted §11.79
    "CAM2",  # BACK                      — promoted §11.79
    "CAM1",  # FRONT                     — promoted §11.79
    "CAM6",  # OUTSIDE_BACK_SOLAR        — promoted §11.79
    "CAM4",  # OUTSIDE_FRONT_POWER       — promoted §11.79
})
PERSON_GATEKEEPER_CAMERAS = frozenset({
    "CAM5",  # OUTSIDE_FRONT_SOLAR
    "CAM3",  # OUTSIDE_FRONT_GARAGE
    "CAM2",  # BACK
    "CAM1",  # FRONT
    "CAM6",  # OUTSIDE_BACK_SOLAR
    "CAM4",  # OUTSIDE_FRONT_POWER
})
# Phase.143: capture the same trail (offsets counting backward from
# ring newest, 2s spacing) for both vehicle and person gatekeepers.
# Used at the call site.
ALL_GATEKEEPER_CAMERAS = GATEKEEPER_CAMERAS | PERSON_GATEKEEPER_CAMERAS

# 4 evenly-spaced deque indices for the gate. Phase.160 (2026-08-28):
# the offsets count BACKWARD from the ring's newest frame, not forward
# from index 0. With ring=90 (FARMSV_RTSP_RING_SIZE in the launchd plist):
#   At 2fps:  (89, 85, 81, 77)  = T+0s, T-2s, T-4s, T-6s
#   At 15fps: (89, 59, 29, 0)   = T+0s, T-2s, T-4s, T-6s
# Both produce a 6-second pre-event motion trail at 2s spacing. The
# default constant below (GATEKEEPER_FRAME_OFFSETS) is only consulted
# when stream_fps is unknown; the live path calls _compute_gatekeeper_offsets().
#
# The motion_gate_pipeline.run() consumer takes exactly 4 frames
# (pairwise diff between f[1]/f[2] and f[2]/f[3]), so 4 is the cap.
# Phase.128 replaced the previous "trailing 4 frames from the
# ring buffer" path which produced 4 frames at the same wall-clock
# millisecond — functionally useless for trajectory detection.
# Default offsets kept for back-compat — only used if stream_fps=0
# (camera hasn't reported its fps yet, rare race at startup).
# Phase.160 — Back-compat constant. The live path computes offsets at
# runtime via _compute_gatekeeper_offsets(stream_fps, ring_len). This
# constant is only used when stream_fps is unknown (rare startup race)
# AND ring_len isn't passed either — i.e., dead code now. Kept for
# back-compat with any external consumers that import this name.
GATEKEEPER_FRAME_OFFSETS = (89, 59, 29, 0)  # 15fps × ring=90 default

# Pre-event trail length and inter-frame spacing in seconds (camera-time,
# not wall-clock). Kept here as constants so the math is auditable.
GATEKEEPER_TRAIL_SECONDS = 6.0
GATEKEEPER_FRAME_SPACING_SECONDS = 2.0
# Deferred capture wait (seconds between webhook fire and gate run).
# Mirrors listener/listener.py:360 — duplicated here to avoid a circular
# import (listener → _motion_gate_dispatch → listener).
GATEKEEPER_CAPTURE_DELAY_S = 8.0

def _compute_gatekeeper_offsets(
    stream_fps: float,
    ring_len: int = 90,
    capture_delay_seconds: float = 8.0,
    webhook_offsets_seconds: tuple[float, ...] = (2.0, 4.0, 6.0, 8.0),
) -> tuple[int, ...]:
    """Compute deque indices for the gate trail.

    Note (2026-09-01, Phase.174): trail shifted +4s from previous
    defaults `(-2, 0, 2, 4)` to `(2, 4, 6, 8)`. Reason: live test
    alert 61fcee70 (Front Door Outside) showed frames 1+2 were empty
    because the camera's "person detected" webhook fires AFTER motion
    starts. Pre-webhook anchors captured no motion. All four anchors
    are now POST-webhook so the trail always has the subject.

    Previous (2026-08-28): Note wanted pre+post webhook context
    (`T_w-2s, T_w+0s, T_w+2s, T_w+4s`). That assumption was wrong for
    RLC-510A with `delay_person=0` — the camera fires the webhook
    immediately on detection, so pre-webhook frames have nothing.

    New anchors: `T_w+2s, T_w+4s, T_w+6s, T_w+8s` — 6-second window at
    2-second spacing, all post-webhook. Subject guaranteed in at least
    the first two frames; last two frames may show subject leaving or
    already gone (honest `no_subject_detected` suppression if so).

    Math:
      - The listener runs the gate `capture_delay_seconds` (default 8s)
        AFTER the webhook fires. So at gate-time, "now" in the ring is
        actually T_w + 8s, not T_w.
      - ring_time(X) = T_gate − (ring_len − 1 − X) × (1/fps)
        = T_w + capture_delay − (ring_len − 1 − X)/fps
      - Solving for X given ring_time = T_w + offset:
        X = ring_len − 1 − (capture_delay − offset) × fps

    Examples (ring_len=32, fps=2, capture_delay=8s):
      webhook_offsets_seconds = (2, 4, 6, 8)
        → indices = (19, 23, 27, 31)
        = T_w+2s, T_w+4s, T_w+6s, T_w+8s

    The previous version used offsets (-0s, -2s, -4s, -6s) relative to
    "newest in ring" — i.e., the trail ended at T_w + 8s. That was
    wrong direction (POST-webhook only) AND didn't account for the
    deferred capture wait. Fixed 2026-08-28.
    """
    if stream_fps <= 0:
        # Camera hasn't reported its fps yet (rare race at startup).
        # Use 15fps defaults — caller will retry once stream_fps stabilises.
        stream_fps = 15.0
    indices = []
    for off_s in webhook_offsets_seconds:
        # Distance from newest (index ring_len-1) in frames
        frames_back = (capture_delay_seconds - off_s) * stream_fps
        idx = ring_len - 1 - round(frames_back)
        # Clamp to valid range; out-of-range means "ring not warm yet"
        idx = max(0, min(ring_len - 1, idx))
        indices.append(idx)
    # Sort ASCENDING so frame_001.jpg = earliest anchor (e.g. T_w-2s) and
    # frame_NNN.jpg = latest anchor (e.g. T_w+4s). The motion_gate_pipeline
    # diff-pair logic assumes frame_N is older than frame_N+1, so this
    # ordering matches "older→newer" which is the diff's intended direction.
    indices.sort()
    return tuple(indices)


def is_motion_gate_enabled() -> bool:
    """Check the env var. Returns True if MOTION_GATE_ENABLED is set to
    1, true, yes, or on. Returns False otherwise (default).

    The cutover plan (§11.37):
      - Default 0: legacy path (no gate, today's behavior)
      - Set 1: gate runs; verdict-based routing to vehicle/person/suppress
      - After 1 week of validation: Note approves, default flips to 1
    """
    val = os.environ.get(ENV_VAR_NAME, "0").strip().lower()
    return val in ("1", "true", "yes", "on")


def maybe_run_motion_gate(
    alert_id: str,
    camera_name: str,
    rtsp_url: str,
    output_dir: str,
    timestamp: datetime | str | None = None,
    event_type: str | None = None,
) -> GateVerdict | None:
    """Run the motion gate if enabled.

    Returns:
      - GateVerdict if the gate ran (regardless of decision: vehicle/person/suppress)
      - None if the gate is disabled (env var not set / 0) — caller falls back
        to legacy path
      - None if the gate is disabled for this (camera, event_type) combination
        via motion_gate_thresholds.json gate_enabled — caller falls back to
        legacy path (Phase.152, PLAN §11.75)

    Cost when enabled:
      - Capture 4 frames from RTSP (~3s if persistent buffer, ~12s if cold)
      - Run motion_gate_pipeline.run (~50-100ms)
      - Returns verdict

    Cost when disabled: 0 (no work done)

    Args:
      timestamp: Phase.116 timestamp-fix. ISO string (webhook format) or
        datetime. Forwarded to run_gate() so the night-suppression heuristic
        can check `is_night_at_edt(timestamp)`. When None, the heuristic
        falls back to file mtime (works for legacy GATE_KEEP_DISK_ARTIFACTS=true)
        and skips suppression entirely when mtime is also unavailable.
      event_type: Phase.152. Webhook event type (vehicle/person/md/...).
        When provided, the per-camera gate_enabled matrix is consulted
        (motion_gate_thresholds.json). When None, the env-var-only check
        applies (backward compat).
    """
    if not is_motion_gate_enabled():
        return None

    # Phase.152 — per-camera × per-event-type gate configuration.
    # Default if the camera or event_type is missing from the config is
    # True (gate enabled). Operators disable by writing:
    #   "CAM5": {"gate_enabled": {"vehicle": false, ...}}
    if event_type is not None and not is_gate_enabled(camera_name, event_type):
        log.info(
            f"[{alert_id}] motion_gate: disabled for "
            f"camera={camera_name} event={event_type!r} "
            "(config/motion_gate_thresholds.json gate_enabled) — "
            "skipping gate, alert routes direct to pipeline"
        )
        return None

    # Lazy import — only load the gate module when enabled. Avoids loading
    # onnxruntime + YOLO model at startup when gate is off.
    # Dual-context import (matches listener.py's vehicle_event_pipeline pattern).
    try:
        from motion_gate_pipeline import run as run_gate
    except ImportError:
        from listener.motion_gate_pipeline import run as run_gate

    log.info(
        f"[{alert_id}] motion_gate enabled — capturing 4 frames from {camera_name}"
    )

    # Ensure output dir exists
    Path(output_dir).mkdir(parents=True, exist_ok=True)

    # Phase.128 — gatekeeper cameras (OFS) sample 4 evenly-spaced frames
    # from the persistent ring buffer at indices [0, 30, 60, 90] = a 6-second
    # pre-event motion trail at 2s spacing. All other cameras use the
    # trailing-tail path (last 4 frames in the buffer).
    # Phase.143 (2026-08-27, Note): gatekeeper-trail capture now
    # applies to BOTH vehicle gatekeepers (OFS) and person gatekeepers
    # (OFG). Previously only OFS got the [0, 30, 60, 90] pre-event
    # trail — OFG was using recent_frames which gave 4 consecutive
    # frames at ~67ms apart. Now both use the spaced trail.
    # Phase.167 §13.4 Commit 17 (T3 C17): resolved via _code_for_camera
    # to translate the friendly name from the webhook payload to the
    # CAM{N} code in ALL_GATEKEEPER_CAMERAS (the frozenset is code-keyed).
    # If camera_name is an unknown alias (e.g. an unenrolled camera in
    # tests), _code_for_camera returns the input unchanged, so this check
    # still returns False on miss rather than crashing.
    try:
        from infra.cameras import code_for as _code_for_camera
    except ImportError:
        from infra import cameras
        _code_for_camera = cameras.code_for
    camera_code = _code_for_camera(camera_name)
    is_gatekeeper = camera_code in ALL_GATEKEEPER_CAMERAS
    # Phase.160 — query the persistent reader's actual fps so the offsets
    # work at any main-stream frame rate. With Reolink main streams lowered
    # from 15fps → 2fps, the old (0, 30, 60, 90) constants exceed the ring
    # buffer (90 frames × 0.5s = 45s, not 6s). We now compute
    # (0, 4, 8, 12) at 2fps, keeping the same 4-frame / 2s-spacing trail.
    if is_gatekeeper:
        from infra.persistent_rtsp import get_reader
        _reader = get_reader(camera_name)
        _fps = _reader.stream_fps if _reader is not None else 0.0
        # 2026-08-28 bugfix: pass ring_len so offsets count BACKWARD from the
        # newest frame. Without this, offsets [0, 4, 8, 12] at 2fps would
        # index into the OLDEST frames in the ring (39-45s ago), not the
        # 0-6s pre-event trail we want.
        _ring_len = _reader.ring_size if _reader is not None else 90
        # Phase.160 (2026-08-28): use the user-specified trail anchors
        # (T_w-2s, T_w, T_w+2s, T_w+4s) rather than the previous
        # (T-6s..T+0s relative to ring newest) which gave a POST-webhook
        # trail by 1.5-7.5s. The gate was looking at the AFTER-state,
        # not the motion that triggered the camera.
        capture_frame_offsets: list[int] | None = list(
            _compute_gatekeeper_offsets(
                stream_fps=_fps,
                ring_len=_ring_len,
                capture_delay_seconds=GATEKEEPER_CAPTURE_DELAY_S,
            )
        )
        log.info(
            f"[{alert_id}] motion_gate: gatekeeper offsets computed at fps={_fps:.1f} ring_len={_ring_len} "
            f"capture_delay={GATEKEEPER_CAPTURE_DELAY_S:.1f}s: {capture_frame_offsets} "
            f"(T_w-2s, T_w+0s, T_w+2s, T_w+4s)"
        )
    else:
        capture_frame_offsets = None
    capture_count = 4  # motion_gate_pipeline.run() requires exactly 4

    # Capture 4 frames at 2s intervals (matches §11.37 spec).
    # For gatekeeper cameras the `frame_offsets` argument drives the
    # capture path through reader.get_frames_by_offset() (pre-event trail).
    # Otherwise we fall through to reader.get_recent_frames(n=4) (trailing tail).
    try:
        frame_paths = capture_frames(
            rtsp_url=rtsp_url,
            output_dir=output_dir,
            count=capture_count,
            interval=2,
            max_size=(3840, 2160),
            timeout=30,
            frame_offsets=capture_frame_offsets,
        )
    except Exception as e:
        log.error(f"[{alert_id}] motion_gate: frame capture failed: {e!r}")
        # If capture fails, return a suppress verdict so we don't block the
        # alert — better to call Qwen on legacy path than to drop the alert.
        try:
            from motion_gate_pipeline import GateVerdict as _GateVerdict
        except ImportError:
            from listener.motion_gate_pipeline import GateVerdict as _GateVerdict
        # `_GateVerdict` is bound at runtime above; the cast below is a
        # type-only narrowing (TYPE_CHECKING branch). Using `_GateVerdict`
        # as both the constructor and the cast target would be more
        # idiomatic but the runtime symbol shadows the import-as alias.
        return _GateVerdict(  # type: ignore[no-any-return]
            decision="suppress",
            class_label=None,
            confidence=0.0,
            crop_a_path=None,
            crop_b_path=None,
            bbox_a=None,
            bbox_b=None,
            reason=f"capture_failed:{type(e).__name__}",
        )

    if not frame_paths or len(frame_paths) < 4:
        log.warning(
            f"[{alert_id}] motion_gate: only got {len(frame_paths)} frames, "
            "falling back to legacy path"
        )
        return None  # let legacy path handle it

    # Run the gate
    try:
        # Phase.116 timestamp-fix: parse ISO string from webhook to a
        # tz-aware datetime so the night heuristic can check is_night_at_edt().
        # The webhook's "timestamp" field is the actual motion-detected time,
        # which is what we want for day/night classification (NOT now()).
        gate_timestamp: datetime | None = None
        if isinstance(timestamp, str):
            try:
                # Python 3.11+ fromisoformat handles 'Z' suffix; older needs replace.
                ts_str = timestamp.replace("Z", "+00:00")
                gate_timestamp = datetime.fromisoformat(ts_str)
            except (ValueError, TypeError):
                log.warning(
                    f"[{alert_id}] motion_gate: could not parse timestamp {timestamp!r}; "
                    f"night heuristic will fall back to file mtime"
                )
                gate_timestamp = None
        elif isinstance(timestamp, datetime):
            gate_timestamp = timestamp
        # else: leave gate_timestamp=None, heuristic will skip if mtime unavailable too

        verdict = cast(
            "GateVerdict | None",
            run_gate(
                frame_paths=frame_paths,
                camera_name=camera_name,
                alert_id=alert_id,
                output_dir=output_dir,
                timestamp=gate_timestamp,
            ),
        )
        return verdict
    except Exception as e:
        # Gate failed — log and fall back to legacy path (don't lose alerts)
        log.error(f"[{alert_id}] motion_gate: gate execution failed: {e!r}")
        return None
