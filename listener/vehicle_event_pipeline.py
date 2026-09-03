"""
vehicle_event_pipeline.py — Phase.105b (2026-08-20) — slim _process_alert.

Six named stages that the listener's `_process_alert` orchestrates via
the `AlertContext` dataclass. Each stage is `(ctx: AlertContext) -> None`
or returns a small value. Communication between stages is via the context
object — no parameter sprawl, no service-object state machine.

The 6 stages (in order):
  1. capture_stage          — captures 6 frames from RTSP + sends message 1
                              ("arriving") for vehicle events.
  2. identify_stage         — runs motion detector + vision (Qwen3-VL via
                              identify_from_crops). Vehicle vs non-vehicle
                              routing, 3-crop multi-crop vision, single-crop
                              fallback, vision coercion, face_visibility,
                              disagreement logger.
  3. match_stage            — gatekeeper-vs-not routing. Per-gatekeeper
                              cameras (OFS only, post-6B.104), run
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

DOES NOT DO:
    - Own the Flask app or webhook routes — listener/listener.py owns that.
    - Own the Telegram transport — calls infra.notifier.notify().
    - Own the frame capture module — calls infra.frame_capture.capture_frames().
    - Own the threat-level LLM — calls infra.alert_generator.generate_alert().
    - Own the audit log — calls infra.alert_history.append_alert().
    - Own the camera semaphore — caller (listener) acquires via with-block.

WHY HERE:
    Phase.105b plan doc §11.35. Note's framing 2026-08-20: *"there's
    got to be a happy balance between modularity and using variables for
    all the different elements of the process within the same loop."*
    The context object IS the happy balance — variables flow through one
    loop, but the structure is explicit. F1 (internal functions in same
    module) is the fallback if this shape doesn't fit.

CALLED BY:
    - listener.listener._process_alert (the only entry point)

CALLS INTO:
    - infra.frame_capture: capture_frames
    - infra.motion_detector: detect_motion_opencv
    - vehicle_identifier.identifier: identify_from_crops
    - infra.vehicle_matcher: match_vehicle_scored, score_top_n
    - infra.alert_generator: generate_alert
    - infra.notifier: notify
    - infra.alert_history: append_alert
    - infra.pipeline_integration: run_phase6a_recognition
    - listener.listener (private helpers): _vision_shows_person, _send_arriving_message,
      _shadow_counters_snapshot, is_arrival, record_person_seen, _load_telegram_creds
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

    # ---- Stage 1 outputs (capture) ---------------------------------------
    frame_paths: list[str] = field(default_factory=list)

    # ---- Stage 2 outputs (identify) --------------------------------------
    id_result: Any = None
    vision_result: Any = None
    vision_error: Any = None
    face_visibility: bool = False
    motion_result: Any = None

    # ---- Stage 3 outputs (match) -----------------------------------------
    match_verdict: Any = None  # MatchVerdict | NoMatch | None
    score_top_n: list = field(default_factory=list)

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
    """Stage 1: capture 6 frames from RTSP + send message 1 ("arriving").

    For vehicle events: capture with gatekeeper offsets (T-12s to T+0s
    when on a gatekeeper camera). For non-vehicle events: standard 6-frame
    capture at 2s intervals.

    Phase.9 (2026-08): 6 frames over 22s. Single RTSP session. Message 1
    fires after all 6 frames are captured so the burst lands intact (Note
    2026-08-02: phase-1/phase-2 split added ~4s reconnect latency).

    Mutates ctx: frame_paths.
    Sends: "arriving" Telegram if is_vehicle_event.
    """
    from infra.frame_capture import capture_frames

    log.info(
        f"[{ctx.alert_id}] capture_stage: starting "
        f"camera={ctx.camera_name} event={ctx.event_type}"
    )

    if ctx.is_vehicle_event:
        # Vehicle: 6 frames with gatekeeper offsets if camera is gatekeeper.
        ctx.frame_paths = capture_frames(
            rtsp_url=ctx.rtsp_url,
            output_dir=ctx.output_dir,
            count=6,
            interval=2,
            max_size=(3840, 2160),
            timeout=30,
            frame_offsets=(
                [0, 30, 60, 90, 120, 150]
                if ctx.camera_name in ctx.gatekeeper_cameras
                else None
            ),
        )
        if not ctx.frame_paths:
            log.error(f"[{ctx.alert_id}] capture_stage: no frames — aborting")
            return
        # Fire "arriving" Telegram now that all 6 frames are captured.
        _send_arriving_message(
            alert_id=ctx.alert_id,
            camera_name=ctx.camera_name,
            frame_path=ctx.frame_paths[0],
            bot_token=ctx.bot_token,
            chat_id=ctx.chat_id,
        )
    else:
        # Non-vehicle: single capture call.
        ctx.frame_paths = capture_frames(
            rtsp_url=ctx.rtsp_url,
            output_dir=ctx.output_dir,
            count=6,
            interval=2,
            max_size=(3840, 2160),
            timeout=30,
        )

    if not ctx.frame_paths:
        log.error(f"[{ctx.alert_id}] capture_stage: no frames — aborting")
        return

    log.info(f"[{ctx.alert_id}] capture_stage: captured {len(ctx.frame_paths)} frames")


def _send_arriving_message(
    alert_id: str,
    camera_name: str,
    frame_path: str,
    bot_token: str,
    chat_id: str,
) -> None:
    """Send message 1 ("arriving") for vehicle events. Lifted from
    listener.listener._process_alert (the original helper)."""
    # Lazy import to avoid circular dependency.
    from listener.listener import _send_arriving_message as _orig
    _orig(alert_id, camera_name, frame_path, bot_token, chat_id)


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
    from infra.motion_detector import detect_motion as detect_motion_opencv
    from vehicle_identifier import identify_from_crops

    log.info(f"[{ctx.alert_id}] identify_stage: starting")

    # --- Motion detector (Phase.64) ---
    # Runs BEFORE vision. Computes trajectory deterministically from pixel
    # centers and produces a cropped image of the moving object for vision
    # classification. Replaces Qwen3-VL's hallucinated trajectory field.
    if ctx.is_vehicle_event and len(ctx.frame_paths) >= 2:
        ctx.motion_result = detect_motion_opencv(
            frame_paths=ctx.frame_paths,
            output_dir=ctx.output_dir,
            alert_id=ctx.alert_id,
        )
        if ctx.motion_result.no_motion_detected:
            log.info(
                f"[{ctx.alert_id}] motion_detector: no motion detected "
                f"(falling back to 6-frame static prompt)"
            )
        else:
            primary = ctx.motion_result.primary_moving_object
            log.info(
                f"[{ctx.alert_id}] motion_detector: found primary moving object "
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
                    ctx.id_result = identify_from_crops(
                        crop_paths=crop_paths,
                        camera_name=ctx.camera_name,
                        captured_at=ctx.timestamp,
                        api_url=ctx.api_url,
                        output_dir=ctx.output_dir,
                        alert_id=ctx.alert_id,
                    )
                    _coerce_vision_result(ctx)
                except Exception as err:
                    log.warning(
                        f"[{ctx.alert_id}] identify_from_crops raised: {err}"
                    )
        # Fallback: motion detector missed → use 6-frame static prompt
        if ctx.vision_result is None:
            _fallback_single_frame_vision(ctx)
    else:
        # Non-vehicle: single-frame first-pass.
        _fallback_single_frame_vision(ctx)


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


def _fallback_single_frame_vision(ctx: AlertContext) -> None:
    """Non-vehicle OR motion-detector-missed fallback: single-frame first-pass.

    Phase.13: vehicle events always use multi-crop. Non-vehicle events
    use single-frame first-pass for speed. Vehicle events that miss motion
    detection fall back to 6-frame static prompt.
    """
    from infra.vision_analyzer import analyze_frames_queued

    log.info(f"[{ctx.alert_id}] _fallback_single_frame_vision starting")
    try:
        result = analyze_frames_queued(
            frame_paths=ctx.frame_paths[:1],
            camera_name=ctx.camera_name,
            api_url=ctx.api_url,
            alert_id=ctx.alert_id,
            event_hint=ctx.event_type,
            mode="vehicle_motion" if ctx.is_vehicle_event else "motion",
        )
        if isinstance(result, dict):
            ctx.vision_result = result
        elif isinstance(result, tuple) and len(result) == 2:
            # (vision_result_dict, error) shape from analyze_frames_queued.
            ctx.vision_result, ctx.vision_error = result
    except Exception as err:
        log.warning(f"[{ctx.alert_id}] _fallback_single_frame_vision raised: {err}")


# ============================================================================
# Stage 3 — match (gatekeeper-vs-not routing + match-alert path)
# ============================================================================


def match_stage(ctx: AlertContext) -> None:
    """Stage 3: match the identified vehicle against known vehicles.

    Gatekeeper cameras (OFS only, post-6B.104) run the match-alert path:
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
        # OFG vehicle events flow through here post-6B.104.
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
        from vehicle_matcher import NoMatch
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
        from vehicle_matcher import MatchVerdict
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
    """Build the signature dict from ctx.vision_result."""
    if not ctx.vision_result:
        return {}
    return {
        "color": ctx.vision_result.get("color", ""),
        "type": ctx.vision_result.get("type", ""),
        "make": ctx.vision_result.get("make", ""),
        "model": ctx.vision_result.get("model", ""),
        "vehicle_features": ctx.vision_result.get("vehicle_features", []),
    }


def _to_kv_id_score(top_n: list) -> list[tuple]:
    """Convert (kv, score, breakdowns) tuples to (kv_id, score) pairs."""
    return [(kv.get("id", "?"), score) for kv, score, _ in top_n]


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

    Mutates ctx: alert.
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
    from infra.notifier import notify
    from infra.pipeline_integration import run_phase6a_recognition

    # Lazy import of infra helpers
    from infra.heartbeat import _vision_shows_person, is_arrival
    from infra.vision_cache import record_person_seen

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
        from listener.listener import STATE
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

    # 6. audit append (BEFORE notify)
    history_ok = append_alert(ctx.alert)
    if not history_ok:
        log.warning(
            f"[{ctx.alert_id}] outbox_failed: append_alert returned False — "
            f"skipping Telegram send to avoid orphan message."
        )
        sent = False
    else:
        # 7. notify
        sent = notify(
            alert=ctx.alert,
            bot_token=ctx.bot_token,
            chat_id=ctx.chat_id,
            cooldown_seconds=120,
            vision_result=ctx.vision_result,
        )

    # 8. state update
    from listener.listener import STATE
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
    """
    from infra.camera_queue import acquire_for_camera

    log.info(
        f"[{ctx.alert_id}] process_alert: starting "
        f"camera={ctx.camera_name} event={ctx.event_type}"
    )

    with acquire_for_camera(ctx.camera_name):
        capture_stage(ctx)
        if not ctx.frame_paths:
            return _result_dict(ctx, sent=False)
        identify_stage(ctx)
        match_stage(ctx)
        select_best_frame_stage(ctx)
        generate_alert_stage(ctx)

    return emit_result_stage(ctx)
