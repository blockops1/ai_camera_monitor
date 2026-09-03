"""
pipeline_integration.py — Phase 6A glue (face recognition + property state).

STATUS: stable
THREAD SAFETY: thread-safe (no shared mutable state; Phase 6A modules
    are lazy-loaded per-call to avoid the import-at-startup cost)

INPUTS:
    - function arg frames: list[str] (required) — JPEG paths
    - function arg vision_result: dict (required) — Qwen-Vision output
    - function arg camera_name: str (required)
    - env PHASE6A_ENABLED (default "true") — toggle; "false" disables

OUTPUTS:
    - return value: dict | None — Phase 6A result (None if disabled,
      person-not-seen, or any error)
    - network call: indirect via Phase 6A modules (InsightFace for
      face recognition, Telegram for response dispatch)
    - side effect: Phase 6A modules are loaded from ~/<sibling-project>/
      on first call (sys.path injection is module-scoped)

PUBLIC API:
    run_phase6a_recognition(frames: list[str], vision_result: dict,
                            camera_name: str) -> dict | None
        Run the Phase 6A recognition pipeline. MUST NEVER raise — any
        error is caught and logged, returns None so the caller continues
        with the existing alert path.
    PHASE6A_ENABLED — module constant (bool, derived from env at import)

DOES NOT DO:
    - Send alerts directly — Phase 6A's response_engine handles that
      via the listener's notifier (passed in by the caller)
    - Persist identity data — Phase 6A's property_state owns that
    - Run when vision_result doesn't show a person — short-circuits to None

WHY HERE:
    Phase 6A lives in the separate ~/<sibling-project>/ repo per the
    Aug-2026 split (commit d23a8f1). This module is the listener's
    adapter — it lazy-loads the Phase 6A modules only when needed
    (PHASE6A_ENABLED=true) and never propagates exceptions back to
    the listener's main pipeline.

    Pipeline placement (in alert_listener._process_alert):
        capture → vision (LLM) → generate alert (LLM)
            → run_phase6a_recognition(frames, vision_result, camera)
                - identify faces, ingest to property_state, dispatch to Telegram
            → notify (Telegram)

CALLED BY:
    - listener.listener: run_phase6a_recognition() in _process_alert()

CALLS INTO:
    - ~/<sibling-project>/infra/face_recognition (lazy)
    - ~/<sibling-project>/infra/property_state (lazy)
    - ~/<sibling-project>/infra/response_engine (lazy)
    - sys.path.insert: scope-limited to the function call

RELATED:
    - ~/<sibling-project>/ — the separate repo that owns Phase 6A
    - infra.alert_generator — runs before this in the pipeline
    - infra.notifier — runs after this in the pipeline
"""

from __future__ import annotations

import logging
import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

from infra.frame_capture import crop_face_region_from_4k
from infra.vision_queue import PHASE6A_ELIGIBLE_CAMERAS, code_for

# Phase 6A dependencies (face_recognition, property_state, response_engine)
# live in the <sibling-project> project, not here. They're imported lazily inside
# run_phase6a_recognition() because the listener may disable Phase 6A entirely
# via PHASE6A_ENABLED=false. If Phase 6A is off, these modules are never loaded.


log = logging.getLogger(__name__)


_cached_notifier: object | None = None


# Allow killing the whole Phase 6A pipeline with an env var. Default: ON.
PHASE6A_ENABLED = os.environ.get("PHASE6A_ENABLED", "true").lower() in (
    "1",
    "true",
    "yes",
    "on",
)


def _load_phase6a_modules():
    """Lazy-load Phase 6A modules from <sibling-project>.

    Returns (face_recognition, property_state, response_engine) or None
    if any module is missing (e.g. <sibling-project> project not yet present).
    Caller MUST handle None — never assume the modules loaded.
    """
    try:
        # Phase 6A modules (face_recognition, property_state, response_engine)
        # are lazy-loaded from ~/<sibling-project>/ via sys.path injection.
        # mypy can't see them because they live in a separate repo; usage
        # sites carry # type: ignore[name-defined]. See SKILL.md.
        import face_recognition
        import property_state
        import response_engine
        return face_recognition, property_state, response_engine
    except ImportError as err:
        log.warning(f"Phase 6A modules not available (<sibling-project> project not installed?): {err}")
        return None


def run_phase6a_recognition(
    frame_paths: list[str],
    vision_result: dict,
    camera: str,
) -> str | None:
    """
    Run the Phase 6A recognition + state + response pipeline.

    Args:
        frame_paths: list of frame paths captured for this alert
        vision_result: dict from vision_analyzer.analyze_frames
        camera: human-readable camera name

    Returns:
        The Telegram message that was sent (if any), or None.

    Raises:
        Never. All exceptions are caught and logged.
    """
    try:
        if not PHASE6A_ENABLED:
            log.info("Phase 6A disabled via PHASE6A_ENABLED — skipping")
            return None

        # Phase 6A modules live in the <sibling-project> project. Load them
        # lazily so a refactor-only deployment (no <sibling-project>) doesn't
        # fail at import time. If they're missing, skip the whole pipeline.
        modules = _load_phase6a_modules()
        if modules is None:
            return None
        face_recognition, property_state, response_engine = modules  # noqa: RUF059 — used in nested helpers

        # Gate 0: skip non-eligible cameras. Phase 6A face recognition
        # only runs on outside cameras (security perimeter). Inside
        # cameras (workshop interior) are descriptive-only — running
        # InsightFace on them burned cycles on the same person walking
        # between rooms and produced double-identifications.
        #
        # Source of truth for the eligible set is vision_queue.PHASE6A_ELIGIBLE_CAMERAS.
        # Keep the two in sync — the queue uses the same set to assign
        # higher priority to these cameras' vision calls.
        #
        # Phase.167 §13.5 Commit 12: PHASE6A_ELIGIBLE_CAMERAS now
        # contains CameraSpec.code values; callers still pass friendly
        # names, so we translate via code_for() first.
        if code_for(camera) not in PHASE6A_ELIGIBLE_CAMERAS:
            log.info(
                f"Phase 6A: camera {camera!r} is not on the eligible "
                f"list {sorted(PHASE6A_ELIGIBLE_CAMERAS)} — skipping "
                f"face recognition (descriptive-only camera)"
            )
            return None

        # Gate 1: skip non-person scenes. Wildlife/vehicles/empty scenes
        # don't get face recognition (no point).
        if not _vision_shows_person(vision_result):
            log.info(
                f"Phase 6A: vision says non-person ({vision_result.get('primary_subject')}), skipping"
            )
            return None

        # Gate 1.5 (removed 2026-07-20): the previous version skipped
        # Phase 6A when Qwen said "face is too small / not front-facing".
        # But that left the entire pipeline at the mercy of Qwen's frame-
        # level confidence estimate — which often returns "no" even when
        # the actual face is usable after a tight crop. User directive
        # 2026-07-20: keep trying frames until InsightFace finds a face.
        # Gate 1.5 above was the failure mode; removed so iteration runs.

        # Gate 2: need at least one frame
        if not frame_paths:
            log.info("Phase 6A: no frames to analyze, skipping")
            return None

        log.info(
            f"Phase 6A: starting face recognition on {len(frame_paths)} frames"
        )

        # Reorder frames by Qwen's per-frame face visibility. The best
        # frame (largest, most front-facing face) is identified by index,
        # not by reordered list position — best_idx below is used directly.
        # This block remains only for logging context: if Qwen reorders
        # priorities versus the capture order, we surface that here.
        original_order = list(frame_paths)
        reordered_paths = _rank_frames_by_qwen_face_visibility(vision_result, frame_paths)
        if reordered_paths != original_order:
            log.info(
                f"Phase 6A: Qwen-ranked frame order: "
                f"capture=[{','.join(os.path.basename(p) for p in original_order)}] → "
                f"qwen=[{','.join(os.path.basename(p) for p in reordered_paths)}]"
            )

        # NEW FLOW (2026-07-20): Qwen reports a face bbox per frame in
        # the DOWNSAMPLED image's coords. We pick the BEST frame (highest
        # face_fraction), convert that bbox's center to relative coords,
        # and crop a 640x640 region from the ORIGINAL 4K frame for
        # InsightFace. InsightFace runs ONCE (not per-frame).
        #
        # Burned 2026-07-20: the old design ran InsightFace on every
        # full-resolution frame in the alert, returning 0 faces because
        # the face was a tiny fraction of the 4K frame and InsightFace
        # downsamples to 640x640 internally anyway. The new design sends
        # InsightFace a TIGHT CROP where the face is the main subject.
        #
        # We use Qwen's per_frame[i] face_fraction/front_facing to rank
        # frames directly — no separate reordering step needed since
        # best_idx points to the right frame in the original frame_paths
        # list.
        fv = vision_result.get("face_visibility") or {}
        per_frame = fv.get("per_frame") or []

        # Rank frames by Qwen's face visibility, best first. We then
        # iterate and try InsightFace on each crop until one returns a
        # face — picking the single best frame and giving up otherwise
        # burned us when the best crop had no usable face (turn angle,
        # occlusion, etc). User directive 2026-07-20: keep trying.
        ranked_indices = _rank_frame_indices_by_face_visibility(
            per_frame, len(frame_paths)
        )

        # Build the (frame_idx, bbox_small) pairs to try, in order.
        candidates: list[tuple[int, list | None]] = []
        for idx in ranked_indices:
            entry = per_frame[idx] if idx < len(per_frame) else None
            if not isinstance(entry, dict):
                candidates.append((idx, None))
                continue
            bbox = entry.get("bbox")
            if bbox and len(bbox) == 4:
                candidates.append((idx, bbox))
            else:
                candidates.append((idx, None))

        if not candidates:
            log.info(
                "Phase 6A: no usable frame index from Qwen "
                f"(per_frame had {len(per_frame)} entries) — falling back "
                "to full-frame InsightFace on frame_paths[0]."
            )
            return _run_insightface_on_frame(
                frame_path=frame_paths[0],
                camera=camera,
                vision_result=vision_result,
                bbox_in_crop_coords=None,
                source_log="no per_frame from Qwen — full-frame fallback",
            )

        # Iterate through ranked candidates until InsightFace finds a face.
        for attempt, (idx, bbox_small) in enumerate(candidates, start=1):
            frame_name = os.path.basename(frame_paths[idx])
            if bbox_small is None:
                # No usable bbox on this frame — try full-frame InsightFace
                # as a last-ditch attempt for that frame.
                log.info(
                    f"Phase 6A: attempt {attempt}/{len(candidates)} — "
                    f"frame={frame_name} has no valid bbox from Qwen, "
                    f"trying full-frame InsightFace"
                )
                result_msg = _run_insightface_on_frame(
                    frame_path=frame_paths[idx],
                    camera=camera,
                    vision_result=vision_result,
                    bbox_in_crop_coords=None,
                    source_log=f"attempt {attempt}/{len(candidates)} — full-frame",
                )
                if result_msg is not None:
                    return result_msg
                continue

            # Crop a 640x640 region from the frame at Qwen's bbox center.
            # Phase.131 (Note 2026-08-26): Qwen now receives the frame at
            # NATIVE resolution (no downscale), so bbox_small is in the
            # frame's pixel coords — read the actual native size from the
            # file and pass it to crop_face_region_from_4k as small_size.
            # Place the crop in the SAME directory as the source frame so it
            # survives /tmp cleanup — user directive 2026-07-20 to keep crops
            # for review.
            try:
                crop_dir = os.path.dirname(frame_paths[idx])
                from PIL import Image  # PIL is a hard dep already loaded by infra.image_prep
                with Image.open(frame_paths[idx]) as _img:
                    native_size = _img.size  # (width, height) in native px
                crop_path = crop_face_region_from_4k(
                    frame_paths[idx],
                    bbox_small,
                    small_size=native_size,
                    output_dir=crop_dir,
                )
            except Exception as crop_err:
                log.warning(
                    f"Phase 6A: attempt {attempt}/{len(candidates)} — "
                    f"crop failed on {frame_name}: {crop_err}"
                )
                continue

            log.info(
                f"Phase 6A: attempt {attempt}/{len(candidates)} — "
                f"running InsightFace on crop from {frame_name}"
            )
            result_msg = _run_insightface_on_frame(
                frame_path=frame_paths[idx],
                camera=camera,
                vision_result=vision_result,
                crop_path=crop_path,
                source_log=(
                    f"attempt {attempt}/{len(candidates)} — "
                    f"crop from {frame_name} using Qwen bbox"
                ),
            )
            if result_msg is not None:
                return result_msg

        # All frames tried, no face found in any of them.
        log.info(
            f"Phase 6A: tried {len(candidates)} frame(s) in Qwen-ranked "
            f"order — no face found in any crop. Falling back to full-frame "
            f"InsightFace on frame_paths[0] as a final attempt."
        )
        return _run_insightface_on_frame(
            frame_path=frame_paths[0],
            camera=camera,
            vision_result=vision_result,
            bbox_in_crop_coords=None,
            source_log="final fallback — full-frame after all crops failed",
        )

    except Exception as err:
        # Phase 6A MUST NEVER break the existing pipeline.
        log.warning(f"Phase 6A swallowed exception: {err}")
        return None


def _run_insightface_on_frame(
    frame_path: str,
    camera: str,
    vision_result: dict,
    crop_path: str | None = None,
    bbox_in_crop_coords: list | None = None,
    source_log: str = "",
) -> str | None:
    """Run InsightFace on a single frame (or pre-computed crop) and dispatch.

    Args:
        frame_path: 4K source frame path.
        camera: human-readable camera name.
        vision_result: dict from vision_analyzer.analyze_frames (for logging).
        crop_path: if pre-computed (by crop_face_region_from_4k), use this
            directly. If None, run InsightFace on the full frame_path.
        bbox_in_crop_coords: if crop_path is None but Qwen provided a bbox,
            crop first then run. Otherwise run on the full frame.
        source_log: human-readable tag for log messages (which frame, what crop).

    Returns:
        The Telegram message that was dispatched (if any), or None.
    """
    if crop_path is None and bbox_in_crop_coords is not None:
        # Compute the crop on the fly. Place alongside the source frame so
        # it survives /tmp cleanup — user directive 2026-07-20.
        try:
            # §11.88 (2026-09-01) — crop uses NATIVE_RES by default. The
            # Qwen scene-classify pass receives the native-resolution
            # frame directly from infra.image_prep.downscale_for_qwen
            # (which is now a pass-through). Passing QWEN_INPUT_SIZE
            # here was redundant and removed.
            crop_path = crop_face_region_from_4k(
                frame_path,
                bbox_in_crop_coords,
                output_dir=os.path.dirname(frame_path),
            )
        except Exception as crop_err:
            log.warning(f"Phase 6A: crop failed: {crop_err}")
            return None

    image_to_recognize = crop_path if crop_path else frame_path

    try:
        # face_recognition is lazy-loaded from ~/<sibling-project>/ at runtime;
        # mypy can't see it because it's not on the path. See SKILL.md.
        result = face_recognition.recognize_faces(image_to_recognize)  # type: ignore[name-defined]
    except Exception as recog_err:
        log.warning(f"Phase 6A: InsightFace error on {image_to_recognize}: {recog_err}")
        return None

    if not result or not result.get("faces"):
        log.info(
            f"Phase 6A: InsightFace found 0 faces in {image_to_recognize} ({source_log})"
        )
        return None

    # Pick the largest bbox * confidence as the best face
    def _face_score(face: dict) -> float:
        bbox = face.get("bbox") or []
        if len(bbox) != 4:
            return 0.0
        x1, y1, x2, y2 = bbox
        area = max(0.0, float(x2 - x1)) * max(0.0, float(y2 - y1))
        conf = float(face.get("confidence", 0.0))
        return area * conf

    best_face = max(result["faces"], key=_face_score)
    log.info(
        f"Phase 6A: best face — name={best_face.get('identified_name')!r} "
        f"confidence={best_face.get('confidence', 0.0):.2f} "
        f"is_known={best_face.get('is_known', False)} "
        f"frame={os.path.basename(frame_path)} ({source_log})"
    )

    # Build evidence dict for property_state. Use the source frame that
    # actually contained the best face, not frame_paths[0].
    evidence = {
        "modality": "face",
        "camera": camera,
        "timestamp": time.time(),
        "frame_path": frame_path,
        "face": {
            "name": best_face.get("identified_name"),
            "confidence": best_face.get("confidence", 0.0),
            "is_known": best_face.get("is_known", False),
            "bbox": best_face.get("bbox"),
        },
    }

    # Ingest into property state machine
    # property_state is lazy-loaded from ~/<sibling-project>/ at runtime;
    # mypy can't see it. See SKILL.md.
    updates = property_state.ingest_evidence(evidence)  # type: ignore[name-defined]
    if not updates:
        return None

    # Dispatch to response engine for Telegram + audit
    state_change = {"type": "added", "occupant": updates[0]}
    # response_engine is lazy-loaded from ~/<sibling-project>/ at runtime.
    msg: str | None = response_engine.dispatch(state_change, notifier=_default_notifier())  # type: ignore[name-defined]
    return msg


# Minimum face size (as fraction of total frame area) for which we trust
# InsightFace to produce a usable embedding. Below this, the embedding is
# noisy enough that matches are unreliable — better to skip and rely on
# clothing/gait/vehicle features from the vision model.
#
# Calibration note (2026-07-20): the 4K cameras stream at 3840x2160.
# A face at ~1% of frame = 82944 pixels at 4K — well above InsightFace's
# minimum working size (~10x10 pixels for a usable embedding). We keep
# Minimum face size (as fraction of total frame area) for which we trust
# InsightFace to produce a usable embedding. At 4K with a person 30m from
# the camera, the face is well under 1% of the image — even the 640x640
# detector window will miss it. 5% = ~4000 px^2 in a 4K frame, enough
# for InsightFace to detect. Below this, the gate skips InsightFace.
#
# Burned 2026-07-20: 0.01 was too permissive — every distant/back-turned
# face still ran InsightFace and wasted GPU cycles returning 0 faces.
# User confirmed raising to 0.05.
MIN_USABLE_FACE_FRACTION = 0.05


def _vision_shows_usable_face(vision_result: dict) -> bool:
    """True if the vision model reports a face big enough and front-facing
    enough that InsightFace is worth running.

    Burned 2026-07-20: every captured frame from a back-turned or distant
    walk was sent to InsightFace anyway, returning zero faces. The
    upstream signal (Qwen) can cheaply filter these out.

    Defaults to False when the field is missing or malformed, so that an
    absence of information means "skip face recognition" rather than
    "attempt it blindly".
    """
    fv = vision_result.get("face_visibility") or {}
    if not isinstance(fv, dict):
        return False
    if not fv.get("any_face_visible"):
        return False
    if not fv.get("front_facing"):
        return False
    fraction = float(fv.get("best_frame_face_fraction", 0.0) or 0.0)
    return fraction >= MIN_USABLE_FACE_FRACTION


def _rank_frame_indices_by_face_visibility(
    per_frame: list, n_frames: int
) -> list[int]:
    """Return frame indices in Qwen-ranked order (best face first).

    Same scoring as _rank_frames_by_qwen_face_visibility but returns
    indices for the caller to use as keys into both frame_paths and
    per_frame in lockstep. Falls back to original order when per_frame
    is empty or shape mismatches.

    User directive 2026-07-20: keep trying frames until a face is found.
    This function provides the order to iterate in.
    """
    if not isinstance(per_frame, list) or len(per_frame) != n_frames:
        return list(range(n_frames))

    indexed = []
    for i, entry in enumerate(per_frame):
        if not isinstance(entry, dict):
            indexed.append((i, 0.0))
            continue
        frac = float(entry.get("face_fraction", 0.0) or 0.0)
        front = bool(entry.get("front_facing", False))
        score = frac * (1.5 if front else 0.5)
        indexed.append((i, score))

    # Sort by score descending; stable so ties preserve original order.
    indexed.sort(key=lambda x: (-x[1], x[0]))
    return [i for i, _ in indexed]


def _rank_frames_by_qwen_face_visibility(
    vision_result: dict, frame_paths: list[str]
) -> list[str]:
    """Reorder frame_paths so the frame Qwen thinks has the best face is
    first. Falls back to original order if per_frame info is missing.

    Why this matters: when a person walks toward the camera, the last
    captured frame usually has the largest, clearest face. Running
    InsightFace on the worst frame first wastes the cycle and may miss
    the face entirely. Qwen sees the whole batch in one call and can tell
    us which frame to prioritize.

    Returns the reordered list (does NOT mutate the input).
    """
    fv = vision_result.get("face_visibility") or {}
    per_frame = fv.get("per_frame") or []
    if not isinstance(per_frame, list) or len(per_frame) != len(frame_paths):
        return list(frame_paths)

    # Build (index, score) — index is 1-based in the per_frame array
    indexed = []
    for i, entry in enumerate(per_frame):
        if not isinstance(entry, dict):
            indexed.append((i, 0.0))
            continue
        frac = float(entry.get("face_fraction", 0.0) or 0.0)
        front = bool(entry.get("front_facing", False))
        # Score: face area dominates, front_facing is a bonus
        score = frac * (1.5 if front else 0.5)
        indexed.append((i, score))

    # Sort by score descending; stable so ties preserve original order
    indexed.sort(key=lambda x: (-x[1], x[0]))
    return [frame_paths[i] for i, _ in indexed]


def _vision_shows_person(vision_result: dict) -> bool:
    """True if the vision model detected a person.

    YOLO-World returns one of several person-typed tokens depending on the
    scene composition:
      - "person"   — generic person
      - "man"      — adult male
      - "woman"    — adult female
      - "boy"      — younger male
      - "girl"     — younger female
      - "people"   — plural / crowd

    All of these are people. We also accept any object whose label contains
    a person token as a fallback (e.g. "person on bicycle"). Burned
    2026-07-20: an outside-camera walk where YOLO-World returned "man"
    caused Phase 6A to skip face recognition entirely, defeating the
    purpose of the system.
    """
    person_tokens = (
        "person", "people",
        "man", "woman",
        "boy", "girl",
    )
    primary = (vision_result.get("primary_subject") or "").lower()
    if any(tok in primary for tok in person_tokens):
        return True
    for obj in vision_result.get("objects_detected", []) or []:
        label = (obj or "").lower()
        if any(tok in label for tok in person_tokens):
            return True
    return False


def _default_notifier():
    """
    Build a notifier object that calls the real Telegram send API.
    Cached at module level so we don't re-import on every call.
    """
    global _cached_notifier
    if _cached_notifier is not None:
        return _cached_notifier

    from notifier import _send_message
    from paths import TELEGRAM_CREDS_FILE
    from telegram_creds import load_telegram_creds

    bot_token = ""
    chat_id = ""
    if os.path.exists(TELEGRAM_CREDS_FILE):
        try:
            tg = load_telegram_creds(TELEGRAM_CREDS_FILE)
            bot_token = tg.bot_token
            chat_id = tg.chat_id
        except Exception as err:
            log.warning(f"Phase 6A: failed to load Telegram creds: {err}")

    class _Notifier:
        def send_message(self, text):
            try:
                return _send_message(bot_token, chat_id, text)
            except Exception as err:
                log.warning(f"Phase 6A: Telegram send failed: {err}")
                return False

    _cached_notifier = _Notifier()
    return _cached_notifier
