"""
vehicle_event_pipeline.py — Phase.105b (2026-08-20) — slim _process_alert.

Six named stages that the listener's `_process_alert` orchestrates via
the `AlertContext` dataclass. Each stage is `(ctx: AlertContext) -> None`
or returns a small value. Communication between stages is via the context
object — no parameter sprawl, no service-object state machine.

The 6 stages (in order):
  1. (frame capture happens via `_gate_aware_vehicle_capture`)
                              — uses the motion gate's 4 frames + 2 crops.
                              No legacy 6-frame RTSP capture.
  2. identify_stage         — builds MotionResult from gate verdict
                              (bbox_a + bbox_b → 4-cell trajectory) + runs
                              vision (Qwen3-VL via identify_from_crops)
                              on the gate's 2 crops. NO non-crop fallback
                              (§11.54): vehicle events without crops from
                              the gate are suppressed. vision coercion +
                              face_visibility + disagreement logger
                              continue to operate. Non-vehicle events
                              (person/animal) use _non_vehicle_first_pass
                              — a separate path, redesign pending.
  3. match_stage            — gatekeeper-vs-not routing. Per-gatekeeper
                              camera (post-6B.104), run
                              match_vehicle_scored + score_top_n + match-
                              alert gate. For non-gatekeeper cameras, skip
                              the match-alert path.
  4. select_best_frame_stage — frame selection with face_visibility priority.
  5. generate_alert_stage   — Qwen3.5-9B threat-level LLM call (generate_alert).
  6. emit_result_stage      — alert_id suffix, frame_path, error handling,
                              arrival detection, phase 6A recognition,
                              notify, audit, state update.

The 6 listener-specific concerns from §11.34 (gatekeeper-vs-not match
routing, face_visibility integration, shadow counters, arrival detection,
threat-level LLM call, override application) become named functions inside
the appropriate stage — not separate modules. Each is ~50-100 lines of
code lifted from the listener's original `_process_alert`.

STATUS: stable
THREAD SAFETY: uses threading.Lock where state-mutating (the listener's
acquire_for_camera semaphore per camera). The pipeline itself is a thread-
local sequence of stages; AlertContext is a per-alert dataclass.

INPUTS:
    - AlertContext fields (set by the listener before calling process_alert).
    - The listener passes:
        alert_id, camera_name, timestamp, event_type, rtsp_url, output_dir,
        is_vehicle_event, known_vehicles, bot_token, chat_id, api_url,
        gatekeeper_cameras, feature_flags (overrides, etc.)

OUTPUTS:
    - return value from process_alert: dict  # the listener's per-alert
      result dict (event_type, vehicle_match, telegram_sent, telegram_error,
      alert_id). All fields populated by emit_result_stage.

PUBLIC API:
    process_alert(ctx: AlertContext) -> dict
        Drive the 6 stages in sequence. Returns the alert result dict.
    AlertContext (dataclass)
        Per-alert carrier object. See below.
    _non_vehicle_first_pass(ctx: AlertContext) -> None
        Phase.132: the ONLY single-frame first-pass helper. Used
        for non-vehicle events (person/animal). Vehicle events without
        crops from the gate are suppressed — no fallback (§11.54).

DOES NOT DO:
    - Own the Flask app or webhook routes — listener/listener.py owns that.
    - Own the Telegram transport — uses telegram_formatter/* (send_photo_with_caption)
      + audit_telegram (audit log). Does NOT call infra.notifier.notify() — that path
      is reserved for the legacy alert_notifier channel (heartbeat escalation only).
    - Own the frame capture module — the motion gate handles capture; this
      pipeline only consumes the gate's frame_paths + crops via AlertContext.
    - Own the pairwise-diff motion detection — Phase.115 removed
      detect_motion() + _crop_top_n + _load_grayscale. The gate's diff bboxes
      are the source of truth; identify_stage just stitches them into a
      MotionResult via build_motion_result_from_gate().
    - Own the threat-level LLM — calls infra.alert_generator.generate_alert().
    - Own the audit log — calls infra.alert_history.append_alert().
    - Own the camera semaphore — caller (listener) acquires via with-block.

WHY HERE:
    Phase.105b plan doc §11.35. the operator's framing 2026-08-20: *"there's
    got to be a happy balance between modularity and using variables for
    all the different elements of the process within the same loop."*
    The context object IS the happy balance — variables flow through one
    loop, but the structure is explicit. F1 (internal functions in same
    module) is the fallback if this shape doesn't fit.

CALLED BY:
    - listener.listener._process_alert (the only entry point)

CALLS INTO:
    - vehicle_position.motion_detector_impl: build_motion_result_from_gate
    - infra.frame_capture: capture_frames (only the gate calls this)
    - vehicle_identifier.identifier: identify_from_crops
    - infra.vehicle_matcher: match_vehicle_scored, score_top_n
    - infra.alert_generator: generate_alert
    - infra.notifier: notify
    - infra.alert_history: append_alert
    - infra.pipeline_integration: run_phase6a_recognition
    - listener.state: STATE singleton (total_alerts, by_threat_level, last_alert,
      last_webhook_at, start_time)
    - infra.alert_overrides: (apply overrides — not yet extracted; see TODOs)

RELATED:
    - listener.listener._process_alert (the slimmed driver that calls this)
    - pipeline/orchestrator.py (parallel cross-domain pipeline; not wired)
    - pipeline/_legacy_match_adapter.py (15-dim legacy scorer adapter)
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, ClassVar

# Phase.116 — module-level import of vehicle_matcher.matcher (the
# new modular matcher). MUST be at module-load time, not lazy, because:
#
# The codebase has both `infra/vehicle_matcher.py` (legacy orchestrator,
# exposes match_vehicle_scored + score_top_n + match_with_details) AND
# `vehicle_matcher/` (a new package with `matcher.py` exposing the
# MatchVerdict + NoMatch dataclasses). Both share the top-level name
# `vehicle_matcher`. If anything in the production listener registers
# `vehicle_matcher` as a module in sys.modules (via
# `from infra.vehicle_matcher import X`), the bare name `vehicle_matcher`
# is shadowed and Python refuses to look for `vehicle_matcher.matcher`
# as a subpackage — raising `ModuleNotFoundError: 'vehicle_matcher' is
# not a package` even though `vehicle_matcher/` is right there on disk.
#
# Importing the package form at module load (below) ensures
# `sys.modules['vehicle_matcher']` is the package (not a module) before
# any function runs. The lazy imports inside match_stage / _emit_match_loop
# become safety-net fallbacks that always succeed because the package is
# already cached.
from vehicle_matcher.matcher import (  # module-load registration
    MatchVerdict,
    NoMatch,
)

# Phase.167 §13.5 Commit 15: replace operator-flavored camera
# literals (CAM1/CAM2 and similar) with cameras.by_code().name / .zone
# lookups. The two helpers below resolve friendly-name input through
# the registry, so the rest of the pipeline compares against stable
# code/zone values rather than hard-coded strings.
from infra.cameras import (
    by_code as _by_camera_code,
    code_for as _code_for_camera,
)

# Lazy imports: the listener passes context, but the heavy infrastructure
# imports (Qwen client, RTSP, etc.) are deferred to the first stage that
# needs them. This keeps module import time low for the webhook entry
# point.

log = logging.getLogger(__name__)


# ============================================================================
# AlertContext — the per-alert carrier object that flows through the 6 stages
# ============================================================================


@dataclass
class AlertContext:
    """Per-alert carrier object. Set by the listener before calling
    process_alert; mutated by each stage in sequence.

    Field groups:
        Inputs (set by the listener, never mutated):
            alert_id, camera_name, timestamp, event_type, rtsp_url,
            output_dir, is_vehicle_event, known_vehicles, bot_token,
            chat_id, api_url, gatekeeper_cameras
        Stage 1 outputs (capture):
            frame_paths
        Stage 2 outputs (identify):
            id_result, vision_result, vision_error, face_visibility,
            motion_result
        Stage 3 outputs (match):
            match_verdict, score_top_n
        Stage 4 outputs (select_best_frame):
            best_frame_path
        Stage 5 outputs (generate_alert):
            alert
        Telemetry (counters updated as a side effect):
            shadow_disagreements, shadow_agreements
    """

    # ---- Inputs (set by the listener, never mutated) -----------------------
    alert_id: str
    camera_name: str
    timestamp: str
    event_type: str
    rtsp_url: str
    output_dir: str
    is_vehicle_event: bool
    known_vehicles: list[dict]
    bot_token: str
    chat_id: str
    api_url: str
    gatekeeper_cameras: frozenset[str]

    # ---- Phase.108a (§11.38.5): gate-aware capture inputs ---------------
    # gate_verdict: populated by listener.py when MOTION_GATE_ENABLED=1 and
    #     the gate ran. None means the gate did not run (legacy path).
    #     Captured by gate_aware_capture in stage 1.
    # capture_source: observability — "gate" (4 frames reused) or "rtsp"
    #     (legacy 6-frame capture). Default "rtsp" so legacy callers/tests
    #     don't need to set it.
    # (Phase.115: legacy_capture_avoided was removed — there's no
    # legacy capture path anymore. capture_source is the only observability
    # field for capture routing.)
    gate_verdict: Any = None  # GateVerdict | None — late import to avoid cycle
    capture_source: str = "rtsp"

    # ---- Stage 1 outputs (capture) ---------------------------------------
    # Phase.115: in-memory frames + crops from the gate (authoritative).
    frames: list = field(default_factory=list)        # list[PIL.Image.Image]
    crop_a: Any = None                                # PIL.Image.Image | None
    crop_b: Any = None                                # PIL.Image.Image | None
    # Disk paths — populated only when GATE_KEEP_DISK_ARTIFACTS=true.
    frame_paths: list[str] = field(default_factory=list)

    # ---- Stage 2 outputs (identify) --------------------------------------
    id_result: Any = None
    vision_result: Any = None
    vision_error: Any = None
    face_visibility: bool = False
    motion_result: Any = None
    # Phase.144 (§11.66): path to the gate's pairwise differential
    # image (abs(frame_3 − frame_4) JPEG with bbox overlays). Qwen
    # uses this to disambiguate the moving subject from stationary
    # vehicles in the same frame (e.g. tractor moving in front of
    # parked Sequoia). None when the gate didn't run or didn't write
    # the diff (e.g. GATE_KEEP_DISK_ARTIFACTS=false).
    pairwise_diff_path: str | None = None

    # ---- Stage 3 outputs (match) -----------------------------------------
    match_verdict: Any = None  # MatchVerdict | NoMatch | None
    score_top_n: list = field(default_factory=list)
    # Phase.121 (2026-08-22): list of MatchTelegramInput the match
    # loop built (one per matched vehicle). Used by emit_result_stage
    # to replace ctx.alert with the match body before append_alert().
    match_alerts: list = field(default_factory=list)

    # ---- Stage 4 outputs (select_best_frame) -----------------------------
    best_frame_path: str = ""

    # ---- Stage 5 outputs (generate_alert) --------------------------------
    alert: dict = field(default_factory=dict)

    # ---- Telemetry --------------------------------------------------------
    shadow_disagreements: int = 0
    shadow_agreements: int = 0

    # ---- Class-level constants --------------------------------------------
    GATEKEEPER_VEHICLE_EVENT: ClassVar[bool] = True


# ============================================================================
# Stage 1 — capture
# ============================================================================


def capture_stage(ctx: AlertContext) -> None:
    """Stage 1: LEGACY — capture 6 frames from RTSP.

    Phase.115 (2026-08-25): removed. The motion gate is now the sole
    producer of frames + crops. This function is kept as a stub for
    backward compat with tests that still reference it; any caller
    should migrate to `_gate_aware_vehicle_capture()` (which uses the
    gate's 4 frames) or to `process_alert()` directly.

    Mutates ctx: frame_paths (set to empty list).
    """
    log.warning(
        f"[{ctx.alert_id}] capture_stage: DEPRECATED 2026-08-25 (Phase.115) "
        f"— gate is sole frame producer. Returning with no frames."
    )
    ctx.frame_paths = []


def _send_arriving_message(
    alert_id: str,
    camera_name: str,
    frame_path: str,
    bot_token: str,
    chat_id: str,
    captured_at: str = "",
) -> bool:
    """
    Phase.9 message 1: instant heads-up that a vehicle is on the
    property and the system is identifying it.

    Inlined from listener.listener._process_alert (Phase.105c, 2026-08-21)
    so the pipeline module has zero cross-listener dependencies. The
    listener.py version remains as the original source of truth for the
    comment history; if either copy drifts, prefer updating this one since
    it's what the production tree calls.

    Phase.112 (2026-08-21): the operator spec calls for TG#1 ("vehicle
    detected, identifying...") on every gatekeeper-camera vehicle event
    where motion detector confirms motion. The previous VEHICLE_ARRIVING_ENABLED env
    gate (default OFF, set via FARM_VEHICLE_ARRIVING_ENABLED=1) was
    RETIRED — it predated the operator's current spec. The Telegram now fires
    unconditionally when called from identify_stage. Callers gate on
    `is_vehicle_event AND motion_result.primary_moving_object is not
    None` so it doesn't fire for non-vehicle events or no-motion events.

    Phase.113 (2026-08-21): Switched from `infra.notifier.notify()`
    to `infra.send_telegram.send_photo_with_caption()`. The notify()
    path adds an `[CAMERA_ALERT]` prefix and emits a redundant
    `channel=alert_notifier` audit line; we want a clean TG#1 with
    only the `channel=vehicle_arriving` audit line. Mirrors the
    pattern used by send_composite_alert and send_match_alert.

    Returns True if Telegram accepted the message (or if there were no
    creds to send with), False if the send failed.

    Failure-isolated: any exception is logged, never raised.

    Phase.114 (2026-08-25): Removed the [alert_id] prefix (diagnostic
    noise to the user) and added a footer line with the captured_at
    webhook time so the operator can correlate "when did this fire?"
    with logs. Event time, not send time, per Note correction
    ("it is actually fine to leave it as the webhook time").
    """
    from infra.audit_telegram import log_outbound_telegram

    # Build the body (no CHANNEL_LABEL prefix; this is TG#1).
    # Phase.114: footer with event time at the end.
    body_lines = [
        f"🚗 <b>[VEHICLE_IN_MOTION]</b> Vehicle moving on property at {camera_name}, identifying...",
    ]
    if captured_at:
        body_lines.append("")
        # captured_at already includes "EDT" suffix (infra.timezone.to_edt_string).
        body_lines.append(captured_at)
    body = "\n".join(body_lines)

    try:
        from infra.send_telegram import send_photo_with_caption as _tg_send
        photo_ok = bool(_tg_send(
            bot_token, chat_id, frame_path, body,
            alert_id=alert_id,
            channel="vehicle_arriving",
            event="vehicle_arriving",
        ))
    except Exception as err:
        log.warning(
            f"[{alert_id}] message 1 (arriving) send failed: {err}"
        )
        photo_ok = False

    log.info(
        f"[{alert_id}] message 1 (arriving) → telegram: "
        f"camera={camera_name}, sent={photo_ok}"
    )
    log_outbound_telegram(
        channel="vehicle_arriving",
        alert_id=alert_id,
        v_id="",
        event="vehicle_arriving",
        body=body,
        sent=bool(photo_ok),
        extra=f"camera={camera_name}",
        image_paths=[frame_path] if frame_path and photo_ok else [],
    )
    return photo_ok


# ============================================================================
# Stage 2 — identify (motion + vision + coercion + face_visibility + telemetry)
# ============================================================================


def identify_stage(ctx: AlertContext) -> None:
    """Stage 2: identify the vehicle or motion subject.

    Vehicle events: motion detector (frame-differencing) + 3-crop multi-crop
    vision via identify_from_crops. Falls back to single-crop vision when
    motion detector finds no moving object. VisionResult coercion (Phase
    6B.87) handles the dict vs VisionResult vs VisionError shapes.

    Non-vehicle events: single-frame first-pass via analyze_frames_queued.

    Mutates ctx: motion_result, id_result, vision_result, vision_error,
    face_visibility, shadow_disagreements, shadow_agreements.
    """
    from vehicle_identifier import identify_from_crops
    from vehicle_position import build_motion_result_from_gate

    log.info(f"[{ctx.alert_id}] identify_stage: starting")

    # --- Motion detector (Phase.115: gate-only path, in-memory) ---
    # Phase.115 (§11.46.6): the motion gate is the sole producer of
    # diff bboxes + crops + frames. We pass PIL.Image objects (from
    # verdict.frames / verdict.crop_a / verdict.crop_b) directly. The
    # build_motion_result_from_gate function derives frame dims from
    # frames[2], so no cv2.imread round-trip.
    if ctx.is_vehicle_event and len(getattr(ctx, "frames", [])) >= 4:
        verdict = getattr(ctx, "gate_verdict", None)
        bbox_a = getattr(verdict, "bbox_a", None) if verdict else None
        bbox_b = getattr(verdict, "bbox_b", None) if verdict else None
        crop_paths: list[str] = []
        if verdict is not None:
            cap = getattr(verdict, "crop_a_path", None)
            cbp = getattr(verdict, "crop_b_path", None)
            crop_paths = [p for p in (cap, cbp) if p]
            # Phase.144 (§11.66): pull the gate's pairwise diff
            # image path so Qwen can identify the MOVING subject.
            ctx.pairwise_diff_path = getattr(
                verdict, "pairwise_diff_path", None
            )
        ctx.motion_result = build_motion_result_from_gate(
            frames=ctx.frames,
            crop_a=getattr(ctx, "crop_a", None),
            crop_b=getattr(ctx, "crop_b", None),
            bbox_a=bbox_a,
            bbox_b=bbox_b,
            alert_id=ctx.alert_id,
            crop_paths=crop_paths,
        )
        if ctx.motion_result.no_motion_detected:
            log.info(
                f"[{ctx.alert_id}] motion_detector: no motion detected "
                f"(gate bboxes both None)"
            )
        else:
            primary = ctx.motion_result.primary_moving_object
            log.info(
                f"[{ctx.alert_id}] motion_detector: built from gate "
                f"(avg_area={primary.avg_area if primary else 0}, "
                f"trajectory={primary.trajectory if primary else []})"
            )

    # --- Vision ---
    if ctx.is_vehicle_event and len(ctx.frame_paths) > 1:
        # 6B.65 + 6B.100: 3-crop multi-crop vision via identify_from_crops.
        if ctx.motion_result is not None and not ctx.motion_result.no_motion_detected:
            crop_paths = ctx.motion_result.crop_paths  # up to 3 crops (6B.65)
            if crop_paths:
                try:
                    # Phase.144 (§11.66): pass the gate's pairwise
                    # differential image so Qwen can pick the MOVING
                    # subject (tractor in front of parked Sequoia, etc.).
                    ctx.id_result = identify_from_crops(
                        crop_paths=crop_paths,
                        camera_name=ctx.camera_name,
                        captured_at=ctx.timestamp,
                        api_url=ctx.api_url,
                        output_dir=ctx.output_dir,
                        alert_id=ctx.alert_id,
                        pairwise_diff_path=ctx.pairwise_diff_path,
                    )
                    _coerce_vision_result(ctx)
                except Exception as err:
                    log.warning(
                        f"[{ctx.alert_id}] identify_from_crops raised: {err}"
                    )
        # Phase.132 (§11.54): no fallback. Vehicle events without
        # crops from the gate are suppressed — no full-frame Qwen send,
        # no degraded ID, no generic scene. Downstream stages handle
        # ctx.vision_result=None cleanly (match_stage skips on empty
        # signature; generate_alert_stage gets vision_result={}).
    else:
        # Non-vehicle: single-frame first-pass. Phase.132 keeps this
        # path intact — redesign lands in a separate commit.
        _non_vehicle_first_pass(ctx)

    # --- Phase.111: trajectory injection (POST-vision) ---
    # Inject AFTER vision + _coerce_vision_result so the trajectory
    # survives into the final ctx.vision_result. (Pre-vision injection
    # gets clobbered by the vision-result coercion step.) Restored from
    # the legacy archive (`_process_alert_archive_6B105b.py` L282 —
    # `vehicles[0]["frame_positions"] = trajectory`). The slim pre-6B.111
    # computed trajectory but never surfaced it to the alert body.
    if (
        ctx.is_vehicle_event
        and ctx.motion_result is not None
        and not ctx.motion_result.no_motion_detected
        and ctx.motion_result.primary_moving_object is not None
    ):
        primary = ctx.motion_result.primary_moving_object
        trajectory = list(primary.trajectory or [])
        if trajectory:
            # Ensure vision_result is a dict before mutating.
            if not isinstance(ctx.vision_result, dict):
                ctx.vision_result = {}
            ctx.vision_result["frame_positions"] = trajectory
            # Also inject into the first vehicle dict if present (matches
            # legacy archive shape). Don't fabricate a vehicle dict if
            # vision hasn't returned one — the top-level frame_positions
            # is the surface the alert generator uses.
            vehicles = ctx.vision_result.get("vehicles") or []
            if vehicles and isinstance(vehicles[0], dict):
                vehicles[0].setdefault("frame_positions", trajectory)
            log.info(
                f"[{ctx.alert_id}] trajectory_inject: "
                f"frame_positions={trajectory}"
            )

    # --- Phase.112: TG#1 ("arriving" Telegram) ---
    # Fires at the end of identify_stage, AFTER motion detector confirms
    # motion + vision ran. Per the operator's spec (2026-08-21), TG#1 fires
    # "If crops are identified as a vehicle" — which we interpret as
    # "is_vehicle_event AND motion detector found a primary mover AND
    # vision_result is populated." The body says "Vehicle entering
    # property at <camera>, identifying..." — a heads-up that TG#2 and
    # TG#3 will follow with the identified + matched vehicle info.
    #
    # Phase.113 (2026-08-21): GATE on camera_name ∈ gatekeeper_cameras.
    # Per the operator's spec, TG#1 is gatekeeper-only. Non-gatekeeper vehicle
    # events skip TG#1 entirely — the entire vehicle Telegram stack is
    # the gatekeeper's exclusive job until/unless the operator later adds
    # per-camera channels.
    # Failure-isolated: a TG#1 send failure logs + returns False from
    # _send_arriving_message without raising — never breaks the pipeline.
    if (
        ctx.is_vehicle_event
        and ctx.camera_name in (ctx.gatekeeper_cameras or frozenset())
        and ctx.motion_result is not None
        and ctx.motion_result.primary_moving_object is not None
        and ctx.frame_paths
    ):
        # Phase.121 (2026-08-22): pick the FIRST frame where the vehicle
        # is actually visible, not frame_paths[0]. Frame 1 is captured
        # ~8s after the webhook and usually has motion='absent' (the
        # deferred-capture window misses the very first approach).
        # Sending an empty frame as TG#1 made Qwen say "No Activity
        # Detected" and showed the user an empty driveway.
        #
        # Strategy: trust the motion.json trajectory — find the first
        # index where the cell is not 'absent'. Falls back to
        # frame_paths[0] if no motion cells (shouldn't happen since we
        # gate on primary_moving_object, but defensive).
        arriving_frame = ctx.frame_paths[0]
        try:
            traj = ctx.motion_result.primary_moving_object.trajectory
            for i, cell in enumerate(traj):
                if cell != "absent" and i < len(ctx.frame_paths):
                    arriving_frame = ctx.frame_paths[i]
                    break
        except (AttributeError, TypeError):
            # motion_result or primary_moving_object shaped unexpectedly;
            # fall back to frame_paths[0] — better than crashing.
            pass

        _send_arriving_message(
            alert_id=ctx.alert_id,
            camera_name=ctx.camera_name,
            frame_path=arriving_frame,
            bot_token=ctx.bot_token,
            chat_id=ctx.chat_id,
            captured_at=ctx.timestamp,
        )


def _coerce_vision_result(ctx: AlertContext) -> None:
    """Coerce the IdentifierResult.vision_result into a dict.

    Phase.87: identify_from_crops returns vision_result as a
    VisionResult object on success, VisionError on failure, None when
    no crops. Pre-6B.87 code did isinstance(_vr, dict) which swallowed
    both VisionResult and VisionError into {} — silently stripping
    Qwen's identification. This explicitly handles each case.
    """
    from vehicle_identifier import VisionError, VisionResult

    if ctx.id_result is None:
        return
    _vr = ctx.id_result.vision_result
    if isinstance(_vr, VisionResult):
        ctx.vision_result = _vr.to_dict()
    elif isinstance(_vr, VisionError):
        ctx.vision_error = _vr
        ctx.vision_result = {}
    elif isinstance(_vr, dict):
        # Legacy pre-6B.87 shape (older fixtures).
        ctx.vision_result = _vr
    else:
        ctx.vision_result = {}


def _non_vehicle_first_pass(ctx: AlertContext) -> None:
    """Non-vehicle events only: single-frame first-pass vision (§11.54).

    Phase.132: the previous vehicle-without-crops caller was deleted
    (vehicle events without crops from the gate are suppressed — see
    §11.54, the operator 2026-08-26:
    "I don't want the non-crop fallback to exist. We keep working on
    designing straight paths and I keep finding that there's these
    backup systems that do something completely different and kick in
    at strange times."). The non-vehicle path (person/animal/other)
    is unchanged in this commit and keeps the legacy single-frame
    shape. A separate commit will redesign non-vehicle to its own
    gate (per the operator's plan: "Let's get the vehicle system working
    correctly, then we can get the person system working").
    """
    from infra.vision_analyzer import analyze_frames_queued

    log.info(f"[{ctx.alert_id}] _non_vehicle_first_pass starting")
    try:
        # mode="motion" — non-vehicle motion-guidance hint for the
        # single-frame prompt template.
        result = analyze_frames_queued(
            frame_paths=ctx.frame_paths[:1],
            camera_name=ctx.camera_name,
            api_url=ctx.api_url,
            alert_id=ctx.alert_id,
            event_hint=ctx.event_type,
            mode="motion",
        )
        if isinstance(result, dict):
            ctx.vision_result = result
        elif isinstance(result, tuple) and len(result) == 2:
            # (vision_result_dict, error) shape from analyze_frames_queued.
            ctx.vision_result, ctx.vision_error = result
    except Exception as err:
        log.warning(f"[{ctx.alert_id}] _non_vehicle_first_pass raised: {err}")


# ============================================================================
# Stage 3 — match (gatekeeper-vs-not routing + match-alert path)
# ============================================================================


def match_stage(ctx: AlertContext) -> None:
    """Stage 3: match the identified vehicle against known vehicles.

    Gatekeeper cameras (post-6B.104) run the match-alert path:
        match_vehicle_scored + score_top_n + match-alert gate.
    Non-gatekeeper cameras skip the match-alert path (the lead-motion
    Telegram has already been sent; no match-alert Telegram stack).

    Mutates ctx: match_verdict, score_top_n.
    """
    if not ctx.is_vehicle_event:
        # Non-vehicle events don't match.
        return

    if ctx.camera_name not in ctx.gatekeeper_cameras:
        # Non-gatekeeper cameras skip the match-alert path entirely.
        # Other gatekeeper vehicles flow through here post-6B.104.
        log.info(
            f"[{ctx.alert_id}] match_stage: {ctx.camera_name} is not a "
            f"gatekeeper — skipping match-alert path"
        )
        return

    from infra.vehicle_matcher import match_vehicle_scored, score_top_n

    # Lazily load the known-vehicles list if the caller didn't pass one in.
    if not ctx.known_vehicles:
        from known_vehicles import load_known_vehicles
        ctx.known_vehicles = load_known_vehicles()

    # Build the signature from the vision_result.
    signature = _extract_signature(ctx)

    if not signature:
        log.info(f"[{ctx.alert_id}] match_stage: no signature — no match")
        return

    # Primary match
    match_result = match_vehicle_scored(
        sig=signature,
        known=ctx.known_vehicles,
    )

    # Top-N for the no-match Telegram body
    ctx.score_top_n = score_top_n(
        sig=signature,
        known=ctx.known_vehicles,
        n=3,
    )

    if match_result is None:
        log.info(f"[{ctx.alert_id}] match_stage: no match")
        from vehicle_matcher.matcher import (
            NoMatch,  # 6B.90 package-form (per farm-surveillance-workflow skill)
        )
        ctx.match_verdict = NoMatch(
            reason="below_threshold",
            top_candidates=_to_kv_id_score(ctx.score_top_n),
        )
    else:
        # Legacy returns (matched_kv, top_score, gap, all_breakdowns)
        # where all_breakdowns[kv_id] = {dim: score}.
        # Modular MatchVerdict wants breakdowns for the top candidate only.
        matched_kv, top_score, gap, all_breakdowns = match_result
        top_kv_id = matched_kv.get("id", "?")
        top_breakdowns = all_breakdowns.get(top_kv_id, {})
        from vehicle_matcher.matcher import MatchVerdict
        ctx.match_verdict = MatchVerdict(
            known_vehicle=matched_kv,
            score=top_score,
            gap=gap,
            breakdowns=top_breakdowns,
            rank=0,
            all_scores=_to_kv_id_score(ctx.score_top_n),
        )
        log.info(
            f"[{ctx.alert_id}] match_stage: matched "
            f"{top_kv_id} (score={top_score:.2f})"
        )


def _extract_signature(ctx: AlertContext) -> dict:
    """Build the signature dict from ctx.vision_result.

    Phase.129b (§11.52): now reads from vehicles[] first (the
    multi-vehicle schema), falling back to top-level fields (the
    legacy single-vehicle schema). For backward compat with old
    Qwen responses that only populate top-level fields, we keep
    the legacy fallback intact.
    """
    if not ctx.vision_result:
        return {}
    vr = ctx.vision_result
    # Multi-vehicle schema: pick vehicles[primary_vehicle_index] if present.
    vehicles = vr.get("vehicles") or []
    if vehicles and isinstance(vehicles, list) and isinstance(vehicles[0], dict):
        pvi = vr.get("primary_vehicle_index", 0)
        if not isinstance(pvi, int) or pvi < 0 or pvi >= len(vehicles):
            pvi = 0
        v = vehicles[pvi]
        return {
            "color": v.get("color", ""),
            "type": v.get("type", "") or v.get("body_style_hint", ""),
            "make": v.get("make", ""),
            "model": v.get("model", ""),
            "vehicle_features": v.get("vehicle_features", []),
        }
    # Single-vehicle schema (top-level fields, legacy compat)
    return {
        "color": vr.get("color", ""),
        "type": vr.get("type", ""),
        "make": vr.get("make", ""),
        "model": vr.get("model", ""),
        "vehicle_features": vr.get("vehicle_features", []),
    }


def _to_kv_id_score(top_n: list) -> list[tuple]:
    """Convert (kv, score, breakdowns) tuples to (kv_id, score) pairs."""
    return [(kv.get("id", "?"), score) for kv, score, _ in top_n]


def _vision_summary_str(vision_result) -> str:
    """Build a brief verbatim identification string from vision_result.

    Used by TG#2 (composite Telegram body) — the "identified as:" line.

    Phase.129 (initial): looked at vision_result["vehicles"][0] first
    (multi-vehicle schema) then fell back to top-level fields.

    Phase.130 (§11.53): handles multi-vehicle results correctly.
      1. Reads vehicles[] (multi-vehicle schema)
      2. Reorders so primary_vehicle_index's vehicle comes FIRST
      3. Joins each non-empty identification with ", plus " so the operator
         sees every vehicle Qwen identified (the primary first, then
         secondary/incidental vehicles in the order Qwen emitted them).
         Example: "red Kubota M7 tractor, plus silver Toyota 4Runner SUV"
      4. Falls back to single-vehicle top-level fields when vehicles[] is
         missing (legacy compat for responses from older code paths)

    Returns:
        A short string like "red Kubota M7 tractor, plus silver Toyota
        4Runner SUV" or "white Honda Civic sedan" for a single-vehicle
        result, or "" if no identification is present.
    """
    if not isinstance(vision_result, dict):
        return ""

    # Multi-vehicle schema (Phase.129 + 6B.130)
    vehicles = vision_result.get("vehicles") or []
    if isinstance(vehicles, list) and vehicles:
        primary_idx = vision_result.get("primary_vehicle_index", 0)
        # Clamp to list bounds (defensive — primary idx may be out of range)
        if not isinstance(primary_idx, int) or primary_idx < 0:
            primary_idx = 0
        if primary_idx >= len(vehicles):
            primary_idx = 0

        # Order: primary first, then the rest in their original order
        ordered: list = []
        primary = vehicles[primary_idx]
        if isinstance(primary, dict):
            ordered.append(primary)
        for i, v in enumerate(vehicles):
            if i == primary_idx:
                continue
            if isinstance(v, dict):
                ordered.append(v)

        # Format each, drop empties, join with ", plus "
        parts = [s for s in (_format_vehicle_summary(v) for v in ordered) if s]
        if parts:
            return ", plus ".join(parts)

    # Single-vehicle schema (top-level fields — legacy compat)
    return _format_vehicle_summary(vision_result)


def _format_vehicle_summary(v) -> str:
    """Format a vehicle dict as a short identification string.

    Defensive against None and non-dict inputs (LSP-safe: caller may
    pass None when vision_result has unexpected shape).
    """
    if not isinstance(v, dict):
        return ""
    parts: list[str] = []
    color = (v.get("color") or "").strip()
    make = (v.get("make") or "").strip()
    model = (v.get("model") or "").strip()
    body_style = (v.get("type") or v.get("body_style_hint") or "").strip()
    if color:
        parts.append(color)
    if make and model:
        parts.append(f"{make} {model}")
    elif make:
        parts.append(make)
    elif model:
        parts.append(model)
    if body_style:
        parts.append(body_style)
    return " ".join(parts)


def _emit_match_loop(ctx: AlertContext) -> None:
    """Phase.112: per-vehicle match loop — TG#3a / TG#3b in the gatekeeper stack.

    Fires AFTER TG#1 (arriving) and TG#2 (vehicle in motion + composite).
    Per Note 2026-08-21: "the matcher should run after the other two
    alerts are sent to me."

    For each vehicle in `vision_result["vehicles"]` (or wrapped single-
    vehicle if absent), extract a signature via `extract_signature`,
    run `match_with_details`, and send a match_alert (TG#3a) or
    no_match_alert (TG#3b) accordingly. Each TG#3 attachment is the
    vertical 3-crop composite from `_concat_crops_vertical`.

    The slim match_stage already ran in stage 3 and populated
    `ctx.match_verdict` for ONE vehicle (the top-identified one). This
    function ALSO handles multi-vehicle cases where vision returned
    >1 vehicles — it loops over each, ignoring ctx.match_verdict for
    vehicles that aren't already scored.

    Failure-isolated: per-vehicle failure logs and continues; never
    raises to the caller.
    """
    from infra.vehicle_matcher import match_with_details, score_top_n
    from telegram_formatter.match_telegram import (
        MatchTelegramInput,
        send_match_alert,
        send_no_match_alert,
    )
    from telegram_formatter.no_match_telegram import (
        NoMatchTelegramInput,
    )
    from vehicle_identifier.signature import extract_signature

    # Build vehicle list. Multi-vehicle schema first; fallback to wrap.
    vr = ctx.vision_result if isinstance(ctx.vision_result, dict) else {}
    vehicles = vr.get("vehicles") or []
    if not vehicles:
        # Wrap top-level fields as a single-vehicle list
        single = {k: v for k, v in vr.items() if k != "vehicles"}
        if single.get("make") or single.get("type") or single.get("color"):
            vehicles = [single]

    if not vehicles:
        log.info(
            f"[{ctx.alert_id}] match_loop: no vehicles in vision_result "
            f"— skipping TG#3"
        )
        return

    # Crop paths for the 3-crop composite photo
    crop_paths: list[str] = []
    if ctx.motion_result is not None and ctx.motion_result.crop_paths:
        crop_paths = ctx.motion_result.crop_paths[:3]

    # Thresholds: use ctx.score_top_n's first candidate score as a hint,
    # otherwise default. The matcher's actual threshold is read inside
    # match_with_details — these are for the body only.
    confidence_threshold = 0.6
    gap_threshold = 0.15

    sent_count = 0
    for v_idx, veh in enumerate(vehicles):
        if not isinstance(veh, dict):
            continue
        # Wrap so extract_signature picks vehicles[v_idx] as primary
        wrap = {
            "vehicles": [veh],
            "primary_vehicle_index": 0,
            "frame_positions": vr.get("frame_positions", []),
        }
        sig = extract_signature(wrap)
        if not sig:
            log.info(
                f"[{ctx.alert_id}] match_loop: vehicle[{v_idx}] "
                f"no signature — skipping"
            )
            continue

        try:
            match_detail = match_with_details(sig, ctx.known_vehicles)
        except Exception as e:
            log.warning(
                f"[{ctx.alert_id}] match_loop: vehicle[{v_idx}] "
                f"matcher raised {e!r} — skipping"
            )
            continue

        if match_detail is None:
            # No match — compute top-3 for the no-match body
            top_n = score_top_n(
                sig=sig, known=ctx.known_vehicles, n=3,
            )
            top_n_breakdowns = [
                (kv.get("id", "?"), score, breakdowns)
                for kv, score, breakdowns in top_n
            ]
            no_match = NoMatch(
                reason="below_threshold",
                top_candidates=[
                    (kv.get("id", "?"), score) for kv, score, _ in top_n
                ],
            )
            no_match_telegram_input = NoMatchTelegramInput(
                camera_name=ctx.camera_name,
                captured_at_iso=ctx.timestamp,
                no_match=no_match,
                top_n_breakdowns=top_n_breakdowns,
                match_threshold=confidence_threshold,
                gap_threshold=gap_threshold,
                alert_id=f"{ctx.alert_id}-v{v_idx}",
            )
            # Phase.122 (2026-08-22, Note): stash the
            # NoMatchTelegramInput so emit_result_stage can write its
            # body to alert.jsonl (the LLM-fallback title "Normal
            # Daytime Scene - No Activity Detected" was a lie).
            # b079e97a (red Jeep, 11:23:24 EDT) was the canary.
            ctx.match_alerts.append(no_match_telegram_input)
            sent = send_no_match_alert(
                alert_id=f"{ctx.alert_id}-v{v_idx}",
                camera_name=ctx.camera_name,
                no_match_telegram_input=no_match_telegram_input,
                crop_paths=crop_paths,
                bot_token=ctx.bot_token,
                chat_id=ctx.chat_id,
                captured_at=ctx.timestamp,
            )
            sent_count += int(sent)
        else:
            # match_detail is MatchDetail (kv, score, gap, reasons,
            # matched_dim_weights). Build a MatchVerdict for the body.
            verdict = MatchVerdict(
                known_vehicle=match_detail.kv,
                score=match_detail.score,
                gap=match_detail.gap,
                breakdowns=getattr(match_detail, "matched_dim_weights", {}) or {},
                rank=0,
                all_scores=[
                    (kv.get("id", "?"), score)
                    for kv, score, _ in score_top_n(
                        sig=sig, known=ctx.known_vehicles, n=3,
                    )
                ],
            )
            match_input = MatchTelegramInput(
                camera_name=ctx.camera_name,
                captured_at_iso=ctx.timestamp,
                verdict=verdict,
                match_threshold=confidence_threshold,
                gap_threshold=gap_threshold,
                alert_id=f"{ctx.alert_id}-v{v_idx}",
            )
            # Phase.121 (2026-08-22): stash the MatchTelegramInput so
            # emit_result_stage can use its body as the alert.jsonl record
            # instead of the LLM-fallback's generic L0 title.
            ctx.match_alerts.append(match_input)
            sent = send_match_alert(
                alert_id=f"{ctx.alert_id}-v{v_idx}",
                camera_name=ctx.camera_name,
                match_telegram_input=match_input,
                crop_paths=crop_paths,
                bot_token=ctx.bot_token,
                chat_id=ctx.chat_id,
                captured_at=ctx.timestamp,
            )
            sent_count += int(sent)

    log.info(
        f"[{ctx.alert_id}] match_loop: {sent_count}/{len(vehicles)} TG#3 "
        f"sent for {len(vehicles)} vehicles"
    )


# ============================================================================
# Stage 4 — select_best_frame (face_visibility priority chain)
# ============================================================================


def select_best_frame_stage(ctx: AlertContext) -> None:
    """Stage 4: pick the best frame for the alert photo.

    Default: first captured frame. Updated priority chain when
    face_visibility is on (Phase.86).

    Mutates ctx: best_frame_path.
    """
    if not ctx.frame_paths:
        ctx.best_frame_path = ""
        return

    # Default: first captured frame.
    ctx.best_frame_path = ctx.frame_paths[0]

    # If vision selected a frame, use that.
    if ctx.vision_result and ctx.vision_result.get("selected_frame"):
        selected = ctx.vision_result.get("selected_frame")
        if selected in ctx.frame_paths:
            ctx.best_frame_path = selected


# ============================================================================
# Stage 5 — generate_alert (threat-level LLM)
# ============================================================================


def generate_alert_stage(ctx: AlertContext) -> None:
    """Stage 5: call the threat-level LLM (Qwen3.5-9B at :8081) to
    produce the alert body (title, summary, threat_level).

    Phase.91: alert_overrides may downgrade threat_level for baseline
    windows (e.g. delivery during business hours) or offhours (e.g. all
    quiet hours). Overrides applied AFTER LLM response.

    Phase.121 (2026-08-22): the match-alert Telegram body override
    for confident matches is applied LATER, in emit_result_stage, after
    the match_loop has populated ctx.match_alerts. Doing it here would
    require running the matcher early (before the LLM call), which
    breaks the existing 6B.112 ordering (composite TG#2 fires before
    matcher loop per Note 2026-08-21). So we just leave a
    placeholder here; emit_result_stage replaces ctx.alert with the
    match body before append_alert().

    Mutates ctx: alert (placeholder set; real value applied in
    emit_result_stage after match loop).
    """
    from infra.alert_generator import generate_alert

    log.info(f"[{ctx.alert_id}] generate_alert_stage: starting")

    # Determine the "source" for the LLM. Vehicle+match: "match"; vehicle
    # without match: "rtsp_frames" (Qwen-9B re-derives a more specific
    # class); non-vehicle: "rtsp_frames".
    source = "match" if (ctx.is_vehicle_event and ctx.match_verdict is not None) else "rtsp_frames"

    try:
        ctx.alert = generate_alert(
            vision_result=ctx.vision_result or {},
            camera_name=ctx.camera_name,
            timestamp=ctx.timestamp,
            source=source,
            api_url=ctx.api_url,
        )
    except Exception as err:
        log.warning(f"[{ctx.alert_id}] generate_alert raised: {err}")
        ctx.alert = {"title": "error", "threat_level": 0, "summary": ""}


# ============================================================================
# Stage 6 — emit_result (alert_id suffix + frame_path + arrival detection +
# phase 6A + notify + audit + state update)
# ============================================================================


def emit_result_stage(ctx: AlertContext) -> dict:
    """Stage 6: finalize the alert and emit it.

    Order matters:
        1. alert_id suffix (vehicle: "-identified" so cooldown treats msg 2 as independent)
        2. frame_path attachment
        3. error handling (alert["title"] == "error")
        4. arrival detection (L0 → L1 bump if person + arrival)
        5. phase 6A recognition (face recognition + property state)
        6. audit append (BEFORE notify per 6B.24 §5)
        7. notify (skip if audit failed)
        7.5. composite Telegram (Phase.111 — motion-trail visualization
             with bbox overlays; fires AFTER the lead motion Telegram so
             the lead motion body + photo don't get delayed by the ~140ms
             composite render. Failure-tolerant: never suppresses the
             lead motion Telegram that already went out.)
        8. state update (total_alerts, by_threat_level, last_alert)

    Returns: result dict for the listener.
        {
            "event_type": final_event_type,
            "vehicle_match": match_verdict,
            "telegram_sent": sent,
            "telegram_error": None,
            "alert_id": alert_id,
        }
    """
    from infra.alert_history import append_alert

    # Lazy import of infra helpers
    from infra.arrival import _vision_shows_person, is_arrival
    from infra.pipeline_integration import run_phase6a_recognition
    from infra.vision_cache import record_person_seen

    # Phase.112: notify() (the LLM-generated alert body Telegram)
    # was removed from this stage. The slim previously sent a single
    # Telegram with the Qwen9B-generated title+summary + best-frame
    # the operator's spec (2026-08-21) defines a 3-Telegram gatekeeper stack:
    #   TG#1 = "vehicle detected" (arriving) — fires from identify_stage
    #   TG#2 = "vehicle in motion" + composite motion-trail photo — fires HERE
    #   TG#3 = match/no-match + 3-crop composite photo — fires HERE (per vehicle)
    # The LLM-generated ctx.alert is still used by:
    #   - state counters (STATE["by_threat_level"])
    #   - audit (append_alert) — persists the LLM summary to history
    #   - arrival detection (L0 → L1 bump if person + arrival)
    # But it is NOT sent to Telegram anymore. Per-vehicle match loop
    # handles TG#3.

    log.info(f"[{ctx.alert_id}] emit_result_stage: starting")

    # 1. alert_id suffix
    if ctx.is_vehicle_event:
        ctx.alert["alert_id"] = f"{ctx.alert_id}-identified"
    else:
        ctx.alert["alert_id"] = ctx.alert_id

    # 2. frame_path attachment
    ctx.alert["frame_path"] = ctx.best_frame_path

    # 3. error handling
    if ctx.alert.get("title") == "error":
        log.error(f"[{ctx.alert_id}] emit_result_stage: alert generation failed")
        try:
            from state import STATE
        except ImportError:
            from listener.state import STATE
        STATE["by_threat_level"][-1] += 1
        return _result_dict(ctx, sent=False)

    log.info(
        f"[{ctx.alert_id}] emit_result_stage: alert generated "
        f"Level {ctx.alert.get('threat_level')} — {ctx.alert.get('title')}"
    )

    # 4. arrival detection
    if ctx.alert.get("threat_level") == 0 and _vision_shows_person(ctx.vision_result):
        if is_arrival(ctx.camera_name):
            log.info(f"[{ctx.alert_id}] Arrival detected — bumping L0 → L1")
            ctx.alert["threat_level"] = 1
            ctx.alert["source"] = "arrival"
            title_lower = (ctx.alert.get("title") or "").lower()
            if any(
                kw in title_lower
                for kw in ["routine", "no threat", "all clear", "no concern"]
            ):
                ctx.alert["title"] = (
                    f"Arrival detected — Person present in {ctx.camera_name}"
                )
        # Always record the person-seen timestamp so future motion events
        # within the gap are NOT classified as arrivals.
        record_person_seen(ctx.camera_name, when_iso=ctx.timestamp)

    # 5. phase 6A face recognition
    try:
        run_phase6a_recognition(
            frame_paths=ctx.frame_paths,
            vision_result=ctx.vision_result,
            camera=ctx.camera_name,
        )
    except Exception as err:
        log.warning(f"[{ctx.alert_id}] Phase 6A swallowed at caller: {err}")

    # Phase.122 (2026-08-22, Note): defer append_alert until
    # AFTER _emit_match_loop, so alert.jsonl can capture the match
    # body that TG#3 actually sent (not the LLM-fallback's generic L0
    # title).
    #
    # History: 6B.121 placed a swap BEFORE append_alert, but that was
    # wrong because _emit_match_loop runs AFTER append_alert in this
    # stage. The swap saw an empty match_alerts list and was a silent
    # no-op. Confirmed via b079e97a (red Jeep, 11:23 EDT): TG#3 said
    # "❌ No match" but alert.jsonl said "Normal Daytime Scene - No
    # Activity Detected".
    #
    # Defer append_alert until after match_loop. Track via _defer_alert.
    _defer_alert = ctx.is_vehicle_event and ctx.camera_name in (
        ctx.gatekeeper_cameras or frozenset()
    )
    if not _defer_alert:
        # 6. audit append (BEFORE Telegram sends) — non-vehicle and
        # non-gatekeeper paths take this branch immediately.
        history_ok = append_alert(ctx.alert)
    else:
        # Vehicle/gatekeeper path — append_alert deferred until after
        # match_loop below so we can capture the match body.
        history_ok = True
    if not history_ok:
        log.warning(
            f"[{ctx.alert_id}] outbox_failed: append_alert returned False — "
            f"skipping Telegram sends to avoid orphan messages."
        )

    # Phase.112: split the single notify() (LLM body Telegram) into
    # the 3-Telegram gatekeeper message stack (TG#2 + TG#3). TG#1 already fired
    # from identify_stage. The legacy notify() call is GONE — the LLM
    # body is no longer sent as a Telegram; it's used for state +
    # audit + arrival detection only.
    sent = bool(history_ok)  # Only False if audit failed

    # 7. composite motion-trail Telegram (TG#2 in the gatekeeper stack).
    # Fires AFTER TG#1 (arriving — fired in identify_stage). Per the operator
    # OOB 2026-08-21: "the matcher should run after the other two
    # alerts are sent to me." So this fires BEFORE the matcher loop.
    # Failure-tolerant: any failure logs + skips; doesn't block match
    # loop or state update. Skipped if motion_result has no primary.
    # Phase.113: GATE on camera_name ∈ gatekeeper_cameras — same
    # gate as TG#1. Non-gatekeeper vehicle events skip TG#2 too.
    if (
        sent
        and ctx.is_vehicle_event
        and ctx.camera_name in (ctx.gatekeeper_cameras or frozenset())
        and ctx.motion_result is not None
    ):
        try:
            from telegram_formatter.composite_telegram import (
                send_composite_alert,
            )
            primary = ctx.motion_result.primary_moving_object
            # Phase.115: read bbox_a + bbox_b from the gate verdict
            # (gate's diff bboxes in native coords). trajectory comes
            # from motion_result.primary_moving_object.trajectory
            # which is now built from the gate's bboxes (4 cells).
            verdict = getattr(ctx, "gate_verdict", None)
            bbox_a = getattr(verdict, "bbox_a", None)
            bbox_b = getattr(verdict, "bbox_b", None)
            trajectory = list(primary.trajectory) if primary else []
            # Build a brief vision summary for TG#2 body. Take the top
            # identified vehicle's make/model/color from vision_result.
            vision_summary = _vision_summary_str(ctx.vision_result)
            composite_sent = send_composite_alert(
                alert_id=ctx.alert_id,
                camera_name=ctx.camera_name,
                frames=ctx.frames,                # Phase.115: in-memory PIL images
                output_dir=ctx.output_dir,         # composite.jpg written here
                bbox_a=bbox_a,
                bbox_b=bbox_b,
                bot_token=ctx.bot_token,
                chat_id=ctx.chat_id,
                captured_at=ctx.timestamp,
                trajectory=trajectory,
                vision_summary=vision_summary,
            )
            log.info(
                f"[{ctx.alert_id}] composite_alert (TG#2): "
                f"{'sent' if composite_sent else 'skipped/failed'}"
            )
        except Exception as err:
            log.warning(
                f"[{ctx.alert_id}] composite_alert: caller caught {err!r}"
            )

    # 8. per-vehicle match loop (TG#3 in the gatekeeper stack).
    # Runs AFTER TG#1 + TG#2 (per Note 2026-08-21). For each vehicle
    # in vision_result["vehicles"] (or wrapped single-vehicle if absent):
    #   - extract signature
    #   - run match_with_details
    #   - send match_alert or no_match_alert
    # Non-gatekeeper cameras skip this entirely (no matcher fires for them).
    # Skipped if match_verdict is missing or no known_vehicles.
    if (
        sent
        and ctx.is_vehicle_event
        and ctx.camera_name in ctx.gatekeeper_cameras
        and ctx.known_vehicles
    ):
        _emit_match_loop(ctx)

    # Phase.122 (2026-08-22, Note): if the match_loop populated
    # ctx.match_alerts with one or more MatchTelegramInput, replace
    # ctx.alert with the first match body so alert.jsonl stays consistent
    # with what TG#3 told the user. Then call append_alert (which was
    # deferred from above so the swap could run first).
    #
    # History: this was originally placed BEFORE append_alert() in
    # 6B.121, but that was wrong because _emit_match_loop runs AFTER
    # append_alert in this stage. The swap saw an empty match_alerts
    # list and was a silent no-op. Confirmed bug via b079e97a (red
    # Jeep, 11:23 EDT): TG#3 said "❌ No match" but alert.jsonl said
    # "Normal Daytime Scene - No Activity Detected".
    if _defer_alert:
        if ctx.match_alerts:
            try:
                first = ctx.match_alerts[0]
                # Phase.122 (2026-08-22): handle BOTH match and
                # no-match Telegram inputs. b079e97a (red Jeep) was a
                # no-match event, and 6B.121's swap only knew about
                # MatchTelegramInput — which is why the no-match case
                # was always going to get the LLM-fallback title.
                # Detect by duck-typing (NoMatchTelegramInput has a
                # `no_match` attribute; MatchTelegramInput has `verdict`).
                if hasattr(first, "no_match"):
                    from telegram_formatter.no_match_telegram import (
                        build_no_match_telegram_body,
                    )
                    body = build_no_match_telegram_body(first)
                    is_match = False
                else:
                    from telegram_formatter.match_telegram import (
                        build_match_telegram_body,
                    )
                    body = build_match_telegram_body(first)
                    is_match = True
                first_line = body.split("\n", 1)[0]
                # Phase.123 (2026-08-22, Note): vehicle threat
                # level routing per the operator's spec:
                #   L1 = known vehicle at the gatekeeper (you want Telegram alert)
                #   L2 = unknown vehicle at the gatekeeper (higher alert — no match)
                #   L3 = emergency vehicle (police/ambulance/firetruck)
                #       — NOT YET WIRED — see plan §emergency-vehicles.
                #       Will require (a) emergency-vehicle entries in
                #       known_vehicles.json with vehicle_class=
                #       "emergency" and (b) routing logic in this swap.
                #       Per the operator 2026-08-22: "we can add other
                #       categories later." Deferred.
                if is_match:
                    # Known vehicle — match found.
                    # TODO(6B.123+): if known_vehicle has vehicle_class=
                    # "emergency", route to L3 instead of L1. No
                    # entries have that field yet, so all matches → L1
                    # today.
                    threat_level = 1  # L1 — known vehicle
                else:
                    # No match — unknown vehicle. L2 per the operator spec.
                    threat_level = 2  # L2 — unknown vehicle
                ctx.alert = {
                    "title": first_line,
                    "threat_level": threat_level,
                    "summary": body,
                }
                if is_match:
                    ctx.alert["matched_vehicle_id"] = (
                        first.verdict.known_vehicle.get("id")
                    )
                    ctx.alert["match_score"] = first.verdict.score
                    ctx.alert["match_gap"] = first.verdict.gap
                log.info(
                    f"[{ctx.alert_id}] emit_result_stage: alert.jsonl "
                    f"replaced with {'match' if is_match else 'no-match'} "
                    f"body "
                    f"level={threat_level} "
                    f"({first_line[:60]!r})"
                )
            except Exception as err:
                log.warning(
                    f"[{ctx.alert_id}] emit_result_stage match-body swap "
                    f"raised {err!r}; persisting original ctx.alert"
                )
        # Append once — with the swapped body if the swap ran, else
        # with the original ctx.alert (e.g. no known_vehicles, etc).
        history_ok = append_alert(ctx.alert)

    # 9. state update
    try:
        from state import STATE
    except ImportError:
        from listener.state import STATE
    STATE["total_alerts"] += 1
    threat_level = ctx.alert.get("threat_level", -1)
    STATE["by_threat_level"][threat_level] = (
        STATE["by_threat_level"].get(threat_level, 0) + 1
    )
    STATE["last_alert"] = {
        "alert_id": ctx.alert_id,
        "camera": ctx.camera_name,
        "timestamp": ctx.timestamp,
        "threat_level": threat_level,
        "title": ctx.alert.get("title"),
        "sent_to_telegram": sent,
        "persisted_to_history": history_ok,
    }

    log.info(
        f"[{ctx.alert_id}] emit_result_stage: complete. "
        f"Telegram: {sent}, History: {history_ok}"
    )

    return _result_dict(ctx, sent=sent)


def _result_dict(ctx: AlertContext, sent: bool) -> dict:
    """Build the listener's per-alert result dict."""
    return {
        "event_type": ctx.event_type,
        "vehicle_match": ctx.match_verdict,
        "telegram_sent": sent,
        "telegram_error": None,
        "alert_id": ctx.alert_id,
    }


# ============================================================================
# process_alert — the driver
# ============================================================================


def process_alert(ctx: AlertContext) -> dict:
    """Drive the 6 stages in sequence.

    The order is:
        capture → identify → match → select_best_frame → generate_alert
        (inside the with acquire_for_camera block)
        emit_result (outside the block — uses no per-camera semaphore)

    Returns: the alert result dict (see emit_result_stage).

    Phase.115 (§11.46, 2026-08-25): The motion gate is the sole
    producer of frames + crops on the vehicle path. Stage 1 now uses
    `_gate_aware_vehicle_capture(ctx)` which ONLY consumes the gate's
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
            # Phase.115: gate didn't produce frames; no legacy
            # fallback. Skip the alert, return sent=False.
            return _result_dict(ctx, sent=False)
        identify_stage(ctx)
        match_stage(ctx)
        select_best_frame_stage(ctx)
        generate_alert_stage(ctx)

    return emit_result_stage(ctx)
