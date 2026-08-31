"""
person_event_pipeline.py — Phase.139 (2026-08-27) — slim person-event pipeline.

Four named stages that drive a person-event alert from capture to Telegram.
Mirror of vehicle_event_pipeline.py's pattern, scoped to person events on
PERSON_GATEKEEPER_CAMERAS (the registry-defined set, §11.61 / 6B.140).

The 4 stages (in order):
  1. person_capture_stage   — DEPRECATED stub (Phase.139). The motion
                              gate is the sole frame producer; see
                              gate_aware_person_capture in
                              listener/_gate_aware_capture.py.
                              This stub exists only for backward compat
                              with tests that still reference it.
  2. person_identify_stage  — single Qwen3-VL call with mode="person"
                              (reuses infra.person_prompt_template); then
                              optional ArcFace match via
                              infra.face_recognition.recognize_faces().
  3. person_match_stage     — runs infra.person_matcher.match_person() with
                              (vision_result, known_persons, face_recognition).
                              Returns MatchVerdict | NoMatch.
  4. person_emit_stage      — builds structured Telegram body; sends body
                              via send_message + 2-image media group
                              (just the 2 YOLO crops from
                              _collect_person_album_paths — wide frames
                              excluded since Phase.153 / 2026-08-28);
                              calls infra.camera_audio if PERSON_AUDIO_ENABLED.

Communication between stages is via PersonContext (per-alert carrier
object) — same context-object pattern as vehicle_event_pipeline.

STATUS: provisional (Phase.139; will stabilize after live telemetry)
THREAD SAFETY: thread-safe (no module-level mutable state; reads camera
    semaphores from infra.camera_queue)

INPUTS:
    - PersonContext fields set by the listener before calling
      process_person_event(). Includes alert_id, camera_name, timestamp,
      event_type, rtsp_url, output_dir, known_persons list, face_recognition
      output (optional), bot_token, chat_id, api_url, feature flags,
      gate_verdict (set by listener.py — 6B.139 contract).

OUTPUTS:
    - return value from process_person_event: dict  (alert_id,
      person_match, telegram_sent, telegram_error, face_recognitions,
      structured_body, plus `suppressed` and `suppressed_reason` when
      gate=person but Qwen=no_person_in_frame — Phase.162)
    - One Telegram sent (structured body, no LLM prose)
    - Suppressed events: logged to audit only, no Telegram
      (Phase.162)
    - One audio dispatch attempted (if PERSON_AUDIO_ENABLED env is set
      and a clip is selected by infra.camera_audio)
    - Structured log lines (Phase.164): vision_attrs (full
      stable-attribute block Qwen returned) and best_confidence (top
      matcher's score vs STABLE_ATTRIBUTES_MATCH_THRESHOLD) so matching
      failures are debuggable from logs/listener.log alone

PUBLIC API:
    process_person_event(ctx: PersonContext) -> dict
        Drive the 4 stages in sequence. Returns the alert result dict.
        Stage 1 calls gate_aware_person_capture (Phase.139).
    PersonContext (dataclass)
        Per-alert carrier object. See below.

DOES NOT DO:
    - Own the Flask app or webhook routes — listener/listener.py owns that.
    - Override Telegram transport — uses infra.send_telegram directly.
    - Detect motion — Qwen handles that via the person prompt's
      action enum ("walking" / "standing" / "leaving" etc.).
    - Re-prompt for height / bbox ratio — parked to §11.36b.
    - Multi-face handling — v1 assumes one person per event.
    - Capture frames from RTSP — Phase.139: that responsibility was
      removed from this module. The motion gate (in
      listener/_motion_gate_dispatch.py) is the sole frame producer;
      gate_aware_person_capture in listener/_gate_aware_capture.py is
      the bridge that copies gate outputs onto PersonContext.

WHY HERE:
    Mirrors vehicle_event_pipeline's context-object pattern per the operator's
    2026-08-20 directive: "got to be a happy balance between modularity
    and using variables for all the different elements within the same
    loop." The 4 stages are tiny wrappers around domain modules
    (frame_capture, person_prompt_template, face_recognition, person_matcher).

CALLED BY:
    - listener.listener._process_alert (when (camera, event) is in
      PERSON_GATEKEEPER_CAMERAS × {person, people})

CALLS INTO:
    - listener._gate_aware_capture: gate_aware_person_capture (Phase.139 —
      sole frame producer for person events)
    - infra.face_recognition: recognize_faces (when face_visible)
    - infra.faces: list_identities (to build known_persons list)
    - infra.person_matcher: match_person (vision_result → MatchVerdict | NoMatch)
    - infra.person_prompt_template: build_person_prompt (Qwen prompt)
    - infra.vision_analyzer: analyze_frames_queued (single Qwen call)
    - infra.send_telegram: send_photo_with_caption (Telegram transport)
    - infra.camera_audio: dispatch_audio_clip (env-gated)
    - infra.alert_history: append_alert (audit log)
    - infra.image_prep: crop_face_region_from_4k (face crop source for ArcFace)

RELATED:
    - listener.vehicle_event_pipeline.AlertContext — sibling pattern
    - PLAN.md §11.36 — design plan
    - PLAN.md §11.60 — Phase.139 (this commit): gate-aware person capture
    - PLAN.md §11.61 — Phase.140 (2026-08-27): person-gatekeeper swap
    - PLAN.md §11.64 — Phase.141 (2026-08-27): 6-image Telegram media group (album)
    - PLAN.md §11.65 — Phase.143 (2026-08-27): person-gatekeeper joins pre-event trail capture
    - PLAN.md §11.76 — Phase.153 (2026-08-28): crops-only (2 images) — wide frames excluded
    - telegram_formatter/match_telegram.py — same pattern (send_message + send_photo_group)
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, cast

# Phase.167 §13.5 Commit 15: resolve operator-flavored friendly
# names through the cameras registry instead of literal camera-name
# strings. The two helpers below translate friendly-name input into
# stable code values that downstream stages can compare.
from infra.cameras import (
    by_code as _by_camera_code,
    code_for as _code_for_camera,
)

log = __import__("logging").getLogger(__name__)


# Number of frames to capture for the multi-frame Qwen call.
# Two-frame input matches the person_prompt_template's "both frames"
# instruction (same pattern as 6B.100 multi-crop).
PERSON_CAPTURE_FRAME_COUNT = 2

# Phase.153 (2026-08-28) — person-emit sends a 2-image Telegram
# media group: just the 2 YOLO crops (frame_NNN_crop*.jpg). Wide
# frames excluded by request ("I want to send few images"). Phase
# 6B.141 (2026-08-27) previously sent 4 wide + 2 crops; Phase.153
# reduced to crops-only.


def _collect_person_album_paths(output_dir: str) -> list[str]:
    """Build the 2-image Telegram media group path list (CROPS ONLY).

    Reads disk artifacts written by the motion gate under
    `output_dir` (typically `data/frames/<alert_id>/`). The gate
    writes 4 wide frames (`frame_001.jpg`..`frame_004.jpg`) plus
    YOLO tight crops named `frame_00X_crop<x>_<y>_<w>x<h>.jpg`.

    Returns paths:
      [frame_NNN_crop*.jpg, frame_NNN_crop*.jpg]

    Wide frames are intentionally excluded. Phase.153
    (2026-08-28, Note): "I think we are going to get a lot
    of notifications today. First, for the person pipeline, I
    want to send few images. Just the two cropped images would
    be fine." A person event is informative at face/crop scale;
    the wide frames are redundant for recognition decisions and
    flood the Telegram album at scale.

    Tolerates missing files gracefully — if a crop is absent
    (bbox was None or gate didn't produce crops), that crop is
    skipped. The album shrinks to whatever's available.
    Returns [] if `output_dir` is missing or empty.

    Phase.141 (2026-08-27): prior version sent 4 wide frames
    + 2 crops. Phase.153 reduced to crops-only.
    """
    if not output_dir or not os.path.isdir(output_dir):
        return []

    result: list[str] = []

    # Crops: scan output_dir for files matching frame_NNN_crop*.jpg,
    # preserve the gate's natural ordering (frame_003_crop before
    # frame_004_crop, multiple crops per frame sort by bbox coords).
    crop_paths = sorted(
        p for p in os.listdir(output_dir)
        if p.startswith("frame_") and "_crop" in p
        and p.endswith(".jpg")
        and not p.endswith("_tight.jpg")  # skip _tight variants (Phase.142)
        and os.path.isfile(os.path.join(output_dir, p))
    )
    result.extend(os.path.join(output_dir, p) for p in crop_paths)

    return result


@dataclass
class PersonContext:
    """Per-alert carrier for person events.

    Mirrors AlertContext but is much smaller — person events don't need
    gatekeeper-vs-not routing, vehicle matching, motion trail composite,
    or threat-level LLM calls. Single Telegram, single path.

    Attributes:
        alert_id: UUID for this alert run (matches alert_id used in
            audit log + Telegram caption).
        camera_name: Human-readable camera label (e.g. the operator's
            friendly name from cameras.env).
        timestamp: ISO 8601 event timestamp from the webhook.
        event_type: "person" or "people" (lowercased by listener).
        rtsp_url: Full RTSP URL for this camera (used for capture).
        output_dir: Where to write captured frames (data/frames/<alert_id>/).
        bot_token: Telegram bot token (loaded by listener).
        chat_id: Telegram chat ID.
        api_url: Vision API URL (Qwen3-VL endpoint).

    State populated by stages:
        frame_paths: list[str] — paths to captured frames (after capture).
            6B.139: these are the gate's selected frames (frames[1] +
            frames[2]) — either reused from verdict.frame_paths or
            written by gate_aware_person_capture to disk.
        frames: list[PIL.Image.Image] — 6B.139: full 4-frame copy from
            gate verdict (mirrors vehicle's AlertContext.frames).
        crop_a, crop_b: PIL.Image.Image | None — 6B.139: YOLO crops
            from gate verdict.
        selected_frames: list[PIL.Image.Image] — 6B.139: 2-frame subset
            (frames[1], frames[2]) passed to Qwen. Reserved for
            Phase.140 best-frame selection.
        vision_result: dict — Qwen output (after identify).
        face_recognition: dict | None — recognize_faces() result
            (None if not run, e.g. face not visible).
        person_match: MatchVerdict | NoMatch — match_stage output.
        matched_name: str | None — convenience accessor for matched_name.
        matched_via: str | None — convenience accessor for "face_recognition"
            or "clothing_color" (None if NoMatch).
        structured_body: str — formatted Telegram body.
        result: dict — final result dict returned by process_person_event.

    Side effects:
        telegram_sent: bool — whether a Telegram was sent.
        telegram_error: str | None — error message if send failed.
    """

    # --- Required inputs (set by listener) ---
    alert_id: str
    camera_name: str
    timestamp: str
    event_type: str
    rtsp_url: str
    output_dir: str
    bot_token: str
    chat_id: str
    api_url: str

    # --- Phase.139 (§11.60): gate-aware capture inputs ---
    # Same fields as AlertContext. See vehicle_event_pipeline.py for the
    # contract. The listener sets ctx.gate_verdict before calling
    # process_person_event.
    # (Phase.115: legacy_capture_avoided was removed — see vehicle path.
    #  Phase.139: the 6-second-late fresh RTSP pull is removed —
    #  gate_aware_person_capture is the only path.)
    gate_verdict: Any = None  # GateVerdict | None
    capture_source: str = "rtsp"  # "gate" after 6B.139

    # --- State populated by stages ---
    frame_paths: list[str] = field(default_factory=list)
    frames: list = field(default_factory=list)        # 6B.139: gate's 4 PIL.Image
    crop_a: Any = None                                # 6B.139: gate's crop_a PIL.Image
    crop_b: Any = None                                # 6B.139: gate's crop_b PIL.Image
    selected_frames: list = field(default_factory=list)  # 6B.139: 2 PIL frames for Qwen
    vision_result: dict = field(default_factory=dict)
    face_recognition: dict | None = None
    person_match: Any = None  # MatchVerdict | NoMatch
    matched_name: str | None = None
    matched_via: str | None = None
    structured_body: str = ""
    result: dict = field(default_factory=dict)

    # --- Side effects ---
    telegram_sent: bool = False
    telegram_error: str | None = None


# ---------------------------------------------------------------------------
# Stage 1: Capture
# ---------------------------------------------------------------------------


def person_capture_stage(ctx: PersonContext) -> None:
    """Stage 1: LEGACY — capture 2 frames from RTSP. removed in 6B.139.

    Phase.139 (§11.60, 2026-08-27): the motion gate is now the sole
    producer of frames for person events. `gate_aware_person_capture`
    (in `listener/_gate_aware_capture.py`) reads 4 PIL frames + crops
    from the gate verdict, selects the bracketing 2, and writes them
    to ctx.output_dir. This function is kept as a stub for backward
    compat with tests that still reference it; any caller should
    migrate to `process_person_event()` directly (which calls
    `gate_aware_person_capture`).

    Mutates ctx: frame_paths (set to empty list), capture_source
    unchanged.

    See PLAN §11.60 for the rationale (Qwen was analyzing fresh RTSP
    frames 6+ seconds after the alert fired — wrong moment).
    """
    log.warning(
        f"[{ctx.alert_id}] person_capture_stage: DEPRECATED 2026-08-27 "
        f"(Phase.139) — gate is sole frame producer. Returning with no frames."
    )
    ctx.frame_paths = []


# ---------------------------------------------------------------------------
# Stage 2: Identify
# ---------------------------------------------------------------------------


def person_identify_stage(ctx: PersonContext) -> None:
    """Single Qwen3-VL call returning the full PERSON_SCHEMA_JSON shape.

    Optional ArcFace recognition: if vision_result reports
    face_visible=true, run infra.face_recognition.recognize_faces on the
    primary frame to identify by face. Result stored on ctx.face_recognition.

    The face crop is taken from frame_paths[0] using the face_bbox in
    Qwen's pixel space (the prompt's explicit coordinate reminder).
    """
    from infra.vision_analyzer import analyze_frames_queued

    log.info(
        f"[{ctx.alert_id}] person_identify: calling Qwen mode=person "
        f"frames={len(ctx.frame_paths)}"
    )

    try:
        # analyze_frames_queued selects the prompt template via mode="person"
        # (see infra.person_prompt_template). The prompt is built by
        # select_prompt_template internally — callers don't construct it.
        result = analyze_frames_queued(
            frame_paths=ctx.frame_paths,
            camera_name=ctx.camera_name,
            api_url=ctx.api_url,
            alert_id=ctx.alert_id,
            mode="person",
        )
    except Exception as err:
        log.exception(
            f"[{ctx.alert_id}] person_identify: vision call failed"
        )
        ctx.vision_result = {"persons": [], "error": str(err)}
        return

    ctx.vision_result = result if isinstance(result, dict) else {}

    # Log stable_attributes the matcher will see, plus face visibility and
    # primary scene description. Without this log the matcher scoring is
    # opaque — we only see the final NoMatch reason, not the inputs. (6B.164)
    _log_vision_attrs(ctx, result if isinstance(result, dict) else {})

    # Optional: if face is visible, run ArcFace on the primary frame.
    primary = _extract_primary_person(ctx.vision_result)
    if primary and primary.get("face_visible"):
        ctx.face_recognition = _run_face_recognition(ctx, primary)
    else:
        ctx.face_recognition = None
        log.info(
            f"[{ctx.alert_id}] person_identify: face not visible, "
            f"skipping ArcFace"
        )


def _extract_primary_person(
    vision_result: dict[str, Any],
) -> dict[str, Any] | None:
    """Pick primary person from PERSON_SCHEMA_JSON output."""
    persons = vision_result.get("persons") or []
    if not persons:
        return None
    idx = vision_result.get("primary_person_index", 0)
    if not isinstance(idx, int) or idx < 0 or idx >= len(persons):
        idx = 0
    selected = persons[idx]
    if isinstance(selected, dict):
        return cast(dict[str, Any], selected)
    return None


def _log_vision_attrs(ctx: PersonContext, vision_result: dict[str, Any]) -> None:
    """Emit a single structured log line capturing what Qwen returned for
    the primary person — every stable-attribute field the matcher will
    score against, plus face_visible and scene_description. Also logs
    the whole persons list length and the overall confidence.

    Phase.164 (2026-08-29): added so the matcher scoring becomes
    debuggable. Previously only the final NoMatch reason was logged,
    leaving us blind to whether Qwen returned nulls (back-to-camera),
    wrong values (misread), or the schema was being passed through
    correctly.

    Output format (single line, key=val pairs):
        [alert_id] vision_attrs: face_visible=... persons=... conf=...
            silhouette.build=... .height=... skin_tone=... age_range=...
            hair.color=... .length=... .style=... facial_hair=...
            glasses=... scene="..."
    """
    primary = _extract_primary_person(vision_result) or {}
    sil = primary.get("silhouette") or {}
    hair = primary.get("hair") or {}
    parts = [
        f"face_visible={primary.get('face_visible')}",
        f"persons={len(vision_result.get('persons') or [])}",
        f"conf={vision_result.get('confidence')}",
        f"silhouette.build={sil.get('build') if isinstance(sil, dict) else None}",
        f"silhouette.height={sil.get('height') if isinstance(sil, dict) else None}",
        f"skin_tone={primary.get('skin_tone')}",
        f"age_range={primary.get('age_range')}",
        f"hair.color={hair.get('color') if isinstance(hair, dict) else None}",
        f"hair.length={hair.get('length') if isinstance(hair, dict) else None}",
        f"hair.style={hair.get('style') if isinstance(hair, dict) else None}",
        f"facial_hair={primary.get('facial_hair')}",
        f"glasses={primary.get('glasses')}",
        f"scene={(vision_result.get('scene_description') or '')[:120]!r}",
    ]
    log.info(f"[{ctx.alert_id}] vision_attrs: {' '.join(parts)}")


def _run_face_recognition(ctx: PersonContext, person: dict) -> dict | None:
    """Run ArcFace recognition on the primary frame.

    The face_bbox from Qwen is in pixel coords of the image Qwen sees
    (typically the resized version). We re-resolve it against the
    original frame in frame_paths[0] using PIL to map coords.

    Falls through to None if any step fails — matcher will use clothing
    color instead.
    """
    try:
        from infra.face_recognition import recognize_faces
        from infra.image_prep import crop_face_region_from_4k
    except ImportError as err:
        log.warning(
            f"[{ctx.alert_id}] person_identify: face_recognition not available: {err}"
        )
        return None

    face_bbox = person.get("face_bbox")
    if not face_bbox or not isinstance(face_bbox, (list, tuple)) or len(face_bbox) != 4:
        log.warning(
            f"[{ctx.alert_id}] person_identify: face_visible=true but no "
            f"face_bbox from Qwen"
        )
        return None

    if not ctx.frame_paths:
        return None

    try:
        # crop_face_region_from_4k takes the original-frame bbox and crops.
        # Qwen's face_bbox is in Qwen's image space (resized); we map it
        # back to original-frame space using PIL to read the image
        # dimensions. If the dimensions match, pass through; otherwise
        # scale.
        from PIL import Image
        frame_path = ctx.frame_paths[0]
        with Image.open(frame_path) as img:
            w, h = img.size
        # Assume Qwen's face_bbox is in the same coord space as the
        # original frame (the prompt asks for tight pixel bbox). If
        # downstream vision resized the image before sending to Qwen,
        # this assumption breaks — §11.36b will scale via
        # infra.image_prep.
        face_img = crop_face_region_from_4k(frame_path, list(face_bbox))
        if face_img is None:
            log.warning(
                f"[{ctx.alert_id}] person_identify: face crop returned None "
                f"(bbox={face_bbox}, frame={w}x{h})"
            )
            return None

        result = recognize_faces(face_img)
        log.info(
            f"[{ctx.alert_id}] person_identify: face_recognition found "
            f"{len(result.get('faces') or [])} face(s), "
            f"identified={result.get('identified_person')!r}"
        )
        return result
    except Exception:
        log.exception(
            f"[{ctx.alert_id}] person_identify: face_recognition failed"
        )
        return None


# ---------------------------------------------------------------------------
# Stage 3: Match
# ---------------------------------------------------------------------------


def person_match_stage(ctx: PersonContext) -> None:
    """Match vision_result against known_persons via infra.person_matcher.

    Loads known_persons from infra.faces.list_identities() (returns
    list of {name, role}; we add a stub clothing_upper_color field
    since face-recognition is the primary matcher path and clothing
    is fallback). If no known persons enrolled, the matcher always
    returns NoMatch(reason="no_known_persons").

    Stores MatchVerdict | NoMatch on ctx.person_match and convenience
    accessors (matched_name, matched_via).
    """
    from infra.person_matcher import MatchVerdict, match_person

    log.info(
        f"[{ctx.alert_id}] person_match: starting "
        f"face_recognition={ctx.face_recognition is not None}"
    )

    known_persons = _load_known_persons_for_matching()
    log.info(
        f"[{ctx.alert_id}] person_match: {len(known_persons)} known person(s) enrolled"
    )

    result = match_person(
        vision_result=ctx.vision_result,
        known_persons=known_persons,
        face_recognition=ctx.face_recognition,
    )

    if isinstance(result, MatchVerdict):
        ctx.matched_name = result.matched_name
        ctx.matched_via = result.matched_via
        log.info(
            f"[{ctx.alert_id}] person_match: MATCHED name={result.matched_name} "
            f"via={result.matched_via} confidence={result.confidence:.2f}"
        )
    else:
        # Phase.164: also log best_candidate_confidence so we can see
        # how close the top candidate got to STABLE_ATTRIBUTES_MATCH_THRESHOLD
        # (0.65) without having to re-run the matcher offline.
        bc = result.best_candidate_confidence
        bc_str = f"{bc:.3f}" if isinstance(bc, (int, float)) else "None"
        log.info(
            f"[{ctx.alert_id}] person_match: NO MATCH reason={result.reason} "
            f"best_candidate={result.best_candidate_name!r} "
            f"best_confidence={bc_str}"
        )

    ctx.person_match = result


def _load_known_persons_for_matching() -> list[dict]:
    """Build the known_persons list for the matcher.

    Reads identity files via infra.faces.list_identities() + load_identity.
    Each known person is projected to {name, clothing_upper_color, role}
    for the matcher's clothing-color fallback.

    clothing_upper_color is NOT yet stored in identity JSONs (current
    schema has face_embedding only). Returns the name + role now;
    clothing-color matching will be effective once v1.5 extends the
    schema. v1 match is face_recognition only for enrolled persons.
    """
    try:
        from infra.faces import list_identities, load_identity
    except ImportError:
        return []

    out = []
    for name in list_identities():
        identity = load_identity(name)
        if not identity:
            continue
        out.append({
            "name": identity.get("name", name),
            "role": identity.get("role", "unknown"),
            # clothing_upper_color not yet in schema — v1.5 will add
            "clothing_upper_color": identity.get("clothing_upper_color", "unknown"),
        })
    return out


# ---------------------------------------------------------------------------
# Stage 4: Emit
# ---------------------------------------------------------------------------


def person_emit_stage(ctx: PersonContext) -> dict:
    """Build structured Telegram body and send one Telegram.

    The body includes:
      - Person matched (with name + match method) OR no-match reason
      - Clothing attributes (upper/lower + type + color)
      - Carrying items
      - Action
      - Face visibility + face_bbox size
      - Scene description from Qwen

    Sends via infra.send_telegram (photo if frames available, text-only
    fallback). Audio dispatch attempted via infra.camera_audio if the
    env gate is set (currently off until clips are recorded).

    Returns the alert result dict (event_type, matched, telegram_sent,
    telegram_error, alert_id).
    """
    body = _build_structured_body(ctx)

    ctx.structured_body = body

    log.info(
        f"[{ctx.alert_id}] person_emit: body length={len(body)} chars; "
        f"sending Telegram"
    )

    # Send Telegram: text body first, then a 2-image media group
    # (just the 2 YOLO crops from the motion gate; wide frames excluded
    # per Phase.153 / 2026-08-28, Note: "I think we are going
    # to get a lot of notifications today. First, for the person
    # pipeline, I want to send few images. Just the two cropped
    # images would be fine.").
    # Phase.141 (2026-08-27, Note): separate text + album
    # because sendMediaGroup only attaches the caption to the first
    # image, and the body is too long for a caption. Text arrives
    # as one message, album arrives as one notification with
    # swipeable images.
    try:
        from infra.send_telegram import (
            send_message,
            send_photo_group,
        )

        album_paths = _collect_person_album_paths(ctx.output_dir)
        body_sent = send_message(
            bot_token=ctx.bot_token,
            chat_id=ctx.chat_id,
            text=body,
            alert_id=ctx.alert_id,
            channel="person_tracker",
            event="person_emit",
        )

        if album_paths:
            # Media group: 4 wide frames + 2 crops (when present),
            # no caption (body already went via send_message above).
            group_ok = send_photo_group(
                bot_token=ctx.bot_token,
                chat_id=ctx.chat_id,
                frame_paths=album_paths,
                caption="",
                alert_id=ctx.alert_id,
                channel="person_tracker",
                event="person_emit",
            )
            log.info(
                f"[{ctx.alert_id}] person_emit: album sent "
                f"({len(album_paths)} images, body_sent={body_sent})"
            )
            ctx.telegram_sent = bool(body_sent and group_ok)
        else:
            # Fallback: no gate frames on disk (defensive — shouldn't
            # happen for the person-gatekeeper camera since the gate is
            # gatekeeper-tier, but
            # keep text-only path in case GATE_KEEP_DISK_ARTIFACTS=false
            # or output_dir is missing).
            log.warning(
                f"[{ctx.alert_id}] person_emit: no gate frames on disk "
                f"under {ctx.output_dir!r}; sent text-only"
            )
            ctx.telegram_sent = bool(body_sent)
    except Exception as err:
        log.exception(
            f"[{ctx.alert_id}] person_emit: Telegram send failed"
        )
        ctx.telegram_sent = False
        ctx.telegram_error = str(err)

    # Audio dispatch (env-gated)
    try:
        _try_dispatch_audio(ctx)
    except Exception as err:
        log.warning(
            f"[{ctx.alert_id}] person_emit: audio dispatch skipped: {err}"
        )

    # Audit log
    try:
        _append_audit(ctx)
    except Exception as err:
        log.warning(
            f"[{ctx.alert_id}] person_emit: audit log failed: {err}"
        )

    return _result_dict(ctx)


def _build_structured_body(ctx: PersonContext) -> str:
    """Build the structured Telegram body from vision + match results."""
    primary = _extract_primary_person(ctx.vision_result)
    lines = []
    lines.append(f"🚶 Person at {ctx.camera_name}")
    lines.append(f"  Time: {ctx.timestamp}")

    # Match result (top of body — most actionable)
    if ctx.matched_name:
        lines.append("")
        lines.append(
            f"  👤 Identified: {ctx.matched_name} "
            f"(via {ctx.matched_via or 'unknown'}, "
            f"confidence {(_match_confidence_str(ctx))})"
        )
    else:
        reason = _no_match_reason(ctx)
        lines.append("")
        lines.append(f"  ⚠️ Unknown person (reason: {reason})")

    # Vision attributes (structured)
    if primary:
        lines.append("")
        upper = primary.get("clothing_upper") or {}
        lower = primary.get("clothing_lower") or {}
        if upper.get("color") or upper.get("type"):
            lines.append(
                f"  Upper: {upper.get('color', 'unknown')} "
                f"{upper.get('type', '') or ''}".strip()
            )
        if lower.get("color") or lower.get("type"):
            lines.append(
                f"  Lower: {lower.get('color', 'unknown')} "
                f"{lower.get('type', '') or ''}".strip()
            )
        carrying = primary.get("carrying") or []
        if carrying:
            lines.append(f"  Carrying: {', '.join(carrying)}")
        action = primary.get("action")
        if action:
            lines.append(f"  Action: {action}")

        # Face visibility
        if primary.get("face_visible"):
            face_str = "Face: visible"
            if ctx.face_recognition:
                # If ArcFace ran but didn't identify, surface that
                faces = ctx.face_recognition.get("faces") or []
                known_faces = [f for f in faces if f.get("is_known")]
                if not known_faces and faces:
                    face_str += " — not identified by ArcFace"
                elif known_faces:
                    face_str += f" — identified as {ctx.matched_name}"
            lines.append(f"  {face_str}")
        else:
            lines.append("  Face: not visible")

    # Scene description from Qwen (1-2 sentences)
    scene = ctx.vision_result.get("scene_description")
    if scene:
        lines.append("")
        lines.append(f"  Scene: {scene}")

    return "\n".join(lines)


def _match_confidence_str(ctx: PersonContext) -> str:
    """Format the match confidence as a human-readable string."""
    if ctx.person_match is None:
        return "n/a"
    conf = getattr(ctx.person_match, "confidence", None)
    if conf is None:
        return "n/a"
    return f"{conf:.2f}"


def _no_match_reason(ctx: PersonContext) -> str:
    """Get the human-readable no-match reason."""
    if ctx.person_match is None:
        return "no_match_run"
    return getattr(ctx.person_match, "reason", "unknown")


def _try_dispatch_audio(ctx: PersonContext) -> None:
    """Optional audio dispatch — env-gated off until clips are recorded.

    Lazily imports infra.camera_audio (added in §11.36 step 9). Until
    then, dispatch is silently skipped — no error, just a log line.
    """
    if os.environ.get("PERSON_AUDIO_ENABLED", "").lower() not in ("1", "true", "yes"):
        return

    try:
        from infra.camera_audio import dispatch_audio_clip
    except ImportError:
        log.info(
            f"[{ctx.alert_id}] person_emit: audio gate on but "
            f"infra.camera_audio not yet implemented (post-§11.36 step 9)"
        )
        return

    try:
        dispatch_audio_clip(
            camera_name=ctx.camera_name,
            event_type=ctx.event_type,
            matched_name=ctx.matched_name,
        )
        log.info(
            f"[{ctx.alert_id}] person_emit: audio dispatched for "
            f"camera={ctx.camera_name}"
        )
    except Exception as err:
        log.warning(
            f"[{ctx.alert_id}] person_emit: audio dispatch failed: {err}"
        )


def _append_audit(ctx: PersonContext) -> None:
    """Append to the alert history audit log."""
    try:
        from infra.alert_history import append_alert
    except ImportError:
        return

    payload = {
        "alert_id": ctx.alert_id,
        "camera": ctx.camera_name,
        "timestamp": ctx.timestamp,
        "event_type": ctx.event_type,
        "matched_name": ctx.matched_name,
        "matched_via": ctx.matched_via,
        "telegram_sent": ctx.telegram_sent,
        "telegram_error": ctx.telegram_error,
    }
    try:
        append_alert(payload)
    except Exception:
        log.exception(
            f"[{ctx.alert_id}] person_emit: append_alert raised"
        )


def _result_dict(ctx: PersonContext) -> dict:
    """Build the alert result dict returned to the listener."""
    return {
        "alert_id": ctx.alert_id,
        "camera_name": ctx.camera_name,
        "event_type": ctx.event_type,
        "matched_name": ctx.matched_name,
        "matched_via": ctx.matched_via,
        "structured_body": ctx.structured_body,
        "telegram_sent": ctx.telegram_sent,
        "telegram_error": ctx.telegram_error,
        # Phase.162 — suppression tracking (for early-return path).
        "suppressed": getattr(ctx.person_match, "suppress", False)
            if hasattr(ctx, "person_match") and ctx.person_match is not None
            else False,
        "suppressed_reason": (
            getattr(ctx.person_match, "reason", None)
            if hasattr(ctx, "person_match") and ctx.person_match is not None
            else None
        ),
    }


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


def process_person_event(ctx: PersonContext) -> dict:
    """Drive the 4 stages in sequence. Returns the alert result dict.

    Order:
        capture → identify → match → emit

    Each stage may short-circuit on failure (empty frame_paths after
    capture, vision error after identify, etc.). The orchestrator
    handles each short-circuit gracefully — emit_stage always runs to
    produce a structured result dict for the listener.

    Phase.139 (§11.60, 2026-08-27): Stage 1 (capture) is now
    `gate_aware_person_capture(ctx)` exclusively. The gate is the sole
    producer of frames for person events. The 6-second-late fresh RTSP
    pull (`person_capture_stage` calling `capture_frames`) is removed.
    There is no env-var opt-out — gate-aware is the only path, matching
    the 6B.115 contract for the vehicle pipeline.

    the operator 2026-08-27: *"I'd like it to work more like the vehicle
    pipeline does."* Phase.139 implements that alignment.
    """
    log.info(
        f"[{ctx.alert_id}] process_person_event: starting "
        f"camera={ctx.camera_name} event={ctx.event_type}"
    )

    # Dual-context import (matches the pattern in listener.py for
    # vehicle_event_pipeline / person_event_pipeline): bare import works
    # when listener.py runs as __main__ (sys.path[0] = listener/);
    # package import works in tests (pytest adds repo root + initializes
    # listener as a package). Fixes ModuleNotFoundError: 'listener' is not
    # a package in production.
    try:
        from _gate_aware_capture import SkipEvent, gate_aware_person_capture
    except ImportError:
        from listener._gate_aware_capture import (
            SkipEvent,
            gate_aware_person_capture,
        )

    try:
        gate_aware_person_capture(ctx)
    except SkipEvent:
        # Phase.139: gate didn't produce frames; no legacy fallback.
        # Skip the alert, return sent=False. Mirrors the vehicle path
        # contract from 6B.115.
        log.warning(
            f"[{ctx.alert_id}] process_person_event: gate produced no frames, "
            f"short-circuiting"
        )
        ctx.result = _result_dict(ctx)
        return ctx.result

    if not ctx.frame_paths:
        log.warning(
            f"[{ctx.alert_id}] process_person_event: no frames captured, "
            f"short-circuiting"
        )
        ctx.result = _result_dict(ctx)
        return ctx.result

    person_identify_stage(ctx)

    person_match_stage(ctx)

    # Phase.162: suppress Telegram if gate says person but Qwen says
    # no_person_in_frame (verified false positive — spiderweb, shadow, etc.)
    if getattr(ctx.person_match, "suppress", False):
        log.info(
            f"[{ctx.alert_id}] process_person_event: suppressed "
            f"(reason={ctx.person_match.reason})"
        )
        ctx.result = _result_dict(ctx)
        return ctx.result

    return person_emit_stage(ctx)


__all__ = [
    "PERSON_CAPTURE_FRAME_COUNT",
    "PersonContext",
    "person_capture_stage",
    "person_emit_stage",
    "person_identify_stage",
    "person_match_stage",
    "process_person_event",
]
