"""
identify — Stage 2: motion + vision + coercion + face_visibility + trajectory + TG#1.

STATUS: stable
THREAD SAFETY: single-threaded (one call per alert; runs inside camera semaphore)

INPUTS:
    - ctx: AlertContext (gate_verdict, frames, crop_a, crop_b, frame_paths, etc.)

OUTPUTS:
    - mutates ctx: motion_result, id_result, vision_result, vision_error,
      face_visibility, shadow_disagreements, shadow_agreements,
      pairwise_diff_path (Phase.144), frame_positions (Phase.111).
    - calls _send_arriving_message — fires TG#1 Telegram if gatekeeper.

PUBLIC API:
    identify_stage(ctx: AlertContext) -> None
        Stage 2 driver. Vehicle events: motion-from-gate +
        3-crop multi-crop vision via identify_from_crops. Non-vehicle:
        single-frame first-pass via analyze_frames_queued.
    _coerce_vision_result(ctx: AlertContext) -> None
        Phase.87: handle VisionResult vs VisionError vs dict vs None
        from id_result.vision_result. Pre-fix swallowed both VisionResult
        and VisionError into {} — silently stripping Qwen's identification.
    _non_vehicle_first_pass(ctx: AlertContext) -> None
        Non-vehicle events: single-frame vision (kept intact per
        §11.54 — separate commit will redesign non-vehicle to its own gate).

DOES NOT DO:
    - Decide whether to fire TG#1 — the camera_code-in-gatekeeper_cameras
      check is built into this module (per §11.168 boundary translation).
    - Send TG#2/TG#3 — those fire from emit_result_stage.
    - Run the matcher — match_stage does that.

WHY HERE:
    Stage 2 owns all "what is this thing?" logic — motion detector
    wrapping, vision call, coercion, trajectory injection, and the
    first Telegram fire. Keeping it together makes the identify →
    match → emit flow linear.

CALLED BY:
    - process_alert (in __init__.py) — Stage 2 driver

CALLS INTO:
    - vehicle_identifier.identify_from_crops — 3-crop multi-crop vision
    - vehicle_position.build_motion_result_from_gate — motion from gate bboxes
    - vehicle_identifier.VisionResult, VisionError — coercion
    - infra.vision_analyzer.analyze_frames_queued — non-vehicle single-frame
    - notify._send_arriving_message — TG#1 Telegram (relative import)

RELATED:
    - match.match_stage — consumes ctx.vision_result from here
    - emit.emit_result_stage — consumes ctx.match_alerts populated by match
    - §11.66 in PLAN.md — pairwise_diff_path rationale
    - §11.168 — camera_code boundary translation
"""
from __future__ import annotations

import logging

from .context import AlertContext
from .notify import _send_arriving_message

log = logging.getLogger(__name__)


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
    # §11.115.11 cascade fast-path: if the cascade already populated
    # ctx.vision_result with the {vehicles[], primary_vehicle_index,
    # confidence, notable_details} shape (from §11.115.4 vehicle call-2),
    # skip the internal identify_from_crops Qwen call entirely.
    # _coerce_vision_result still runs below to normalize the dict shape
    # if needed (legacy callers may use VisionResult objects, etc.).
    cascade_provided_vision = (
        isinstance(ctx.vision_result, dict)
        and "vehicles" in ctx.vision_result
    )
    if (
        ctx.is_vehicle_event
        and len(ctx.frame_paths) > 1
        and not cascade_provided_vision
    ):
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
        # §11.115.11 cascade fast-path: skip non-vehicle first-pass
        # when the cascade already produced vehicle vision_result.
        # Without this guard, identify_stage would still trigger
        # analyze_frames_queued(mode='motion') which is removed since
        # Phase.78 (motion is owned by the gate).
        if not (
            ctx.is_vehicle_event and cascade_provided_vision
        ):
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
    # motion + vision ran. Per Note's spec (2026-08-21), TG#1 fires
    # "If crops are identified as a vehicle" — which we interpret as
    # "is_vehicle_event AND motion detector found a primary mover AND
    # vision_result is populated." The body says "Vehicle entering
    # property at <camera>, identifying..." — a heads-up that TG#2 and
    # TG#3 will follow with the identified + matched vehicle info.
    #
    # Phase.168 (2026-08-31): gate on camera_code ∈ gatekeeper_cameras.
    # Pre-fix compared camera_name (friendly from webhook) against a
    # code-keyed set, which silently failed every event. See
    # ctx.camera_code docstring.
    # Per Note's spec, TG#1 is gatekeeper-only. Non-gatekeeper vehicle
    # events skip TG#1 entirely — the entire vehicle Telegram stack is
    # the gatekeeper's exclusive job until/unless Note later adds
    # per-camera channels.
    # Failure-isolated: a TG#1 send failure logs + returns False from
    # _send_arriving_message without raising — never breaks the pipeline.
    if (
        ctx.is_vehicle_event
        and ctx.camera_code in (ctx.gatekeeper_cameras or frozenset())
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
    §11.54, Note 2026-08-26:
    "I don't want the non-crop fallback to exist. We keep working on
    designing straight paths and I keep finding that there's these
    backup systems that do something completely different and kick in
    at strange times."). The non-vehicle path (person/animal/other)
    is unchanged in this commit and keeps the legacy single-frame
    shape. A separate commit will redesign non-vehicle to its own
    gate (per Note's plan: "Let's get the vehicle system working
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
