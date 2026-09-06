"""
motion_detector_impl — Build MotionResult from motion-gate outputs (vehicle path).

Phase 6B.115 (§11.46, §11.46.6): the motion gate is the sole producer
of frames + crops + diff bboxes on the vehicle path. This module no
longer reads from disk or runs any frame analysis. It just stitches
the gate's outputs into a MotionResult shape so the rest of the
pipeline (identify_stage, match_telegram, etc.) doesn't need to change.

Phase 6B.115 (§11.46.6): the gate now hands in-memory PIL.Image objects
via `frames` + `crop_a` + `crop_b`. No filesystem reads on the hot path.

Phase §11.90 (2026-09-01): MovingObject + MotionResult moved to
infra.motion_types as the single source of truth. This module
re-exports them from there instead of redefining. Field shapes are
unchanged.

STATUS: stable
THREAD SAFETY: thread-safe (pure functions; no shared mutable state)

INPUTS:
    - function arg frames: list[PIL.Image.Image] (required) — 4 gate frames
    - function arg crop_a: PIL.Image.Image | None (required) — gate's pre-cropped bbox_a region
    - function arg crop_b: PIL.Image.Image | None (required) — gate's pre-cropped bbox_b region
    - function arg bbox_a: tuple | None (required) — diff(frame_2, frame_3) bbox @ native res
    - function arg bbox_b: tuple | None (required) — diff(frame_3, frame_4) bbox @ native res
    - function arg alert_id: str (required) — for log lines
    - function arg crop_paths: list[str] (optional, default []) — disk paths for postmortem
      (only present when GATE_KEEP_DISK_ARTIFACTS=true)

OUTPUTS:
    - return value: MotionResult
        - moving_objects: [MovingObject] (single primary)
        - primary_moving_object: MovingObject (the truck/car)
        - best_crop_path: first crop path (or None if disk writes off)
        - crop_paths: 2 crop paths (or empty list if disk writes off)
        - no_motion_detected: bool (True if bbox_a and bbox_b are both None)
        - reference_method: "gate" (always)
        - elapsed_ms: float

PUBLIC API:
    build_motion_result_from_gate(
        frames, crop_a, crop_b, bbox_a, bbox_b, alert_id, crop_paths=None,
    ) -> MotionResult
        Build MotionResult from gate outputs. No frame resize, no diff,
        no crop extraction — the gate already did that. This module just
        stitches the trajectory labels (4 cells) and wraps the existing
        gate crops in a MotionResult so the rest of the pipeline stays
        unchanged.

    _center_to_label(cx, cy, frame_w, frame_h) -> str
        Map a center coordinate to one of 16 grid labels (4x4). Caller
        passes native frame dims so the labels reflect the actual frame
        size, not a fixed resize target.

DOES NOT DO:
    - Capture frames from RTSP → that lives in infra/frame_capture (called by the gate)
    - Resize frames → removed Phase 6B.115 (gate already works at native)
    - Run pairwise diff on the frames → gate already produced bbox_a + bbox_b
    - Save crops from the frames → gate already saved crop_a + crop_b (when env var on)
    - Run YOLO → that lives in infra/quick_classifier (called by the gate)
    - Read from disk on the hot path → all inputs are in-memory PIL.Image
    - Compose multiple crops into a single image → infra/motion_visualization

CALLED BY:
    - listener.vehicle_event_pipeline.identify_stage (Phase 6B.115)

CALLS INTO:
    - numpy — bbox area calculations only

RELATED:
    - listener.motion_gate_pipeline.GateVerdict (input source)
    - infra.frame_diff.diff_pair_with_bbox (the gate's diff produces bbox_a + bbox_b)
    - infra.motion_visualization.render_motion_composite (uses the same 4 frames)
"""

from __future__ import annotations

import logging
import time

import numpy as np

from infra.frame_diff import pairwise_diff, subject_bbox_from_mask
from infra.motion_types import MotionResult, MovingObject

log = logging.getLogger("motion_detector")


# Phase 6B.72: 4x4 grid (16 cells) for trajectory labels.
# Row 0: T (y=0..h/4), Row 1: UM, Row 2: LM, Row 3: B
# Col 0..3: 1..4
TRAJECTORY_LABELS = (
    # Row 0: T (y=0..h/4)
    "T1", "T2", "T3", "T4",
    # Row 1: UM (y=h/4..h/2)
    "UM1", "UM2", "UM3", "UM4",
    # Row 2: LM (y=h/2..3h/4)
    "LM1", "LM2", "LM3", "LM4",
    # Row 3: B (y=3h/4..h)
    "B1", "B2", "B3", "B4",
)


# MovingObject + MotionResult moved to infra.motion_types (Phase §11.90, 2026-09-01).
# Re-exported at module top via `from infra.motion_types import MovingObject, MotionResult`.
# Field shapes are unchanged.


def _center_to_label(cx: int, cy: int, frame_w: int, frame_h: int) -> str:
    """Map a center coordinate to one of 16 grid labels (4x4).

    Phase 6B.115: caller passes native frame dims instead of relying on
    a hardcoded RESIZE_W=1280/RESIZE_H=960. The gate's bboxes are at
    native resolution (e.g., 2304x1296 for the front camera) so the labels need to
    scale with the actual frame.
    """
    if frame_w <= 0 or frame_h <= 0:
        return "absent"
    col = min(3, cx * 4 // frame_w)
    row = min(3, cy * 4 // frame_h)
    return TRAJECTORY_LABELS[row * 4 + col]


def _bbox_center_area(bbox: tuple[int, int, int, int]) -> tuple[tuple[int, int], int]:
    """Return (center_x, center_y), area for a (x, y, w, h) bbox."""
    x, y, w, h = bbox
    return ((x + w // 2, y + h // 2), w * h)


def _frame_size(pil_image) -> tuple[int, int]:
    """Return (width, height) for a PIL.Image, or (0, 0) if missing."""
    if pil_image is None:
        return (0, 0)
    return (pil_image.width, pil_image.height)


def build_motion_result_from_gate(
    frames: list,
    crop_a,
    crop_b,
    bbox_a: tuple[int, int, int, int] | None,
    bbox_b: tuple[int, int, int, int] | None,
    alert_id: str,
    crop_paths: list[str] | None = None,
    *,
    subject_bbox_a: tuple[int, int, int, int] | None = None,
    subject_bbox_b: tuple[int, int, int, int] | None = None,
) -> MotionResult:
    """Build a MotionResult from the motion-gate's in-memory outputs.

    Phase 6B.115 (§11.46.6): all inputs are in-memory PIL.Image objects.
    No filesystem reads on the hot path. crop_paths is optional — only
    present when GATE_KEEP_DISK_ARTIFACTS=true (postmortem convenience).

    The gate has already:
      - captured 4 frames @ native resolution
      - run diff(frame_2, frame_3) → bbox_a (motion bbox = union of subject's
        position in frame_2 and subject's position in frame_3)
      - run diff(frame_3, frame_4) → bbox_b (motion bbox = union of subject's
        position in frame_3 and subject's position in frame_4)
      - cropped frame_2 + subject_bbox_a → crop_a (subject WAS here in frame_2)
      - cropped frame_3 + subject_bbox_b → crop_b (subject IS here in frame_3)
      - run YOLO on the crops and returned the verdict

    This function stitches those outputs into a MotionResult shape so the
    rest of the pipeline (identify_stage, match_telegram, etc.) doesn't
    need to change.

    Trajectory is 4 cells — ALL FOUR computed, not "absent" hardcoded:
      - frame_1: subject_bbox_from_mask(mask_1to2, frame_1) → label, or
                 'absent' if no motion at t=0
      - frame_2: subject_bbox_a (gate-computed AND-bbox on frame_2) → label
      - frame_3: subject_bbox_b (gate-computed AND-bbox on frame_3) → label
      - frame_4: subject_bbox_from_mask(mask_3to4, frame_4) → label, or
                 'absent' if no motion at t=4

    §11.176 (2026-09-06): frames 1 and 4 are no longer hardcoded 'absent'.
    For those frames we recompute the diff mask from the saved PIL frames
    and run subject_bbox_from_mask (single-mask version) to localize the
    subject. Frames 2 and 3 use the gate's already-computed AND-bboxes
    (subject_bbox_a / subject_bbox_b) for tighter localization.

    Args:
        frames: 4 PIL.Image frames in capture order (frames[0]..frames[3]).
            Width/height derived from frames[2] (one of the motion frames).
        crop_a: PIL.Image of bbox_a crop (or None if no motion)
        crop_b: PIL.Image of bbox_b crop (or None if no motion)
        bbox_a: diff(frame_2, frame_3) bbox @ native res, or None if no motion
        bbox_b: diff(frame_3, frame_4) bbox @ native res, or None if no motion
        alert_id: alert UUID (for log lines)
        crop_paths: optional list of 2 disk paths (from verdict.crop_a_path /
            verdict.crop_b_path). Empty list when GATE_KEEP_DISK_ARTIFACTS=false.
        subject_bbox_a: gate's per-frame subject bbox in frame_2
            (AND of mask_1to2 and mask_2to3). Optional; when None we fall
            back to bbox_a (the looser diff bbox) for frame_2.
        subject_bbox_b: gate's per-frame subject bbox in frame_3
            (AND of mask_2to3 and mask_3to4). Optional; when None we fall
            back to bbox_b (the looser diff bbox) for frame_3.

    Returns:
        MotionResult with primary_moving_object.trajectory = 4 cells,
        crop_paths = [crop_a_path, crop_b_path] (or []), and
        no_motion_detected=True iff both bbox_a and bbox_b are None.
    """
    crop_paths = list(crop_paths or [])

    t0 = time.perf_counter()

    result = MotionResult()

    # Derive frame_w/frame_h from frames[2] (one of the motion frames).
    # If frames is missing/short, use (0, 0) → all labels become "absent".
    if len(frames) >= 4:
        frame_w, frame_h = _frame_size(frames[2])
    else:
        frame_w, frame_h = (0, 0)

    # Both bboxes missing → no motion (shouldn't happen if the gate
    # returned a vehicle verdict, but be defensive).
    if bbox_a is None and bbox_b is None:
        result.no_motion_detected = True
        result.elapsed_ms = (time.perf_counter() - t0) * 1000
        log.info(
            f"[{alert_id}] motion_detector: no motion detected "
            f"(both gate bboxes None) [{result.elapsed_ms:.0f}ms]"
        )
        return result

    # Build a 4-frame trajectory from real per-frame subject positions.
    # §11.176 (2026-09-06): frames 1 and 4 are no longer hardcoded "absent".
    # We compute all four positions from data we already have:
    #   - frame_1: subject_bbox_from_mask(mask_1to2, frame_1) — the "old
    #              position" component of the (1→2) diff trail
    #   - frame_2: subject_bbox_a (gate-computed AND-bbox on frame_2)
    #              — falls back to bbox_a if subject_bbox_a is None AND
    #              mask_1to2 has motion (else subject wasn't in frame_2)
    #   - frame_3: subject_bbox_b (gate-computed AND-bbox on frame_3)
    #              — falls back to bbox_b if subject_bbox_b is None
    #   - frame_4: subject_bbox_from_mask(mask_3to4, frame_4) — the "old
    #              position" component of the (3→4) diff trail
    #
    # Why "old position" not "new position": we want the subject's
    # position in frame_1 (where they WERE) not in frame_2 (where they
    # ARE after moving). subject_bbox_from_mask(mask, frame_b) returns
    # the CC that overlaps frame_b's bright pixels = where the subject
    # IS in frame_b. We pass frame_1 so we get the trail's "old position"
    # side, which is where the subject was in frame_1.
    #
    # Frame_2 / frame_3 logic: bbox_a/bbox_b are diff bboxes covering
    # 2-frame windows (2-3 and 3-4). If mask_1to2 is empty (no motion
    # entering frame_2), the subject wasn't in frame_2 yet — frame_2
    # must be 'absent', not bbox_a's loose position.
    frame_1_bbox: tuple[int, int, int, int] | None = None
    frame_4_bbox: tuple[int, int, int, int] | None = None
    mask_1to2_has_motion = False
    if len(frames) >= 4 and frames[0] is not None and frames[3] is not None:
        # Compute mask_1to2 to find where the subject was in frame_1
        try:
            frame_1_gray = np.asarray(frames[0].convert("L"))
            frame_2_gray = np.asarray(frames[1].convert("L"))
            mask_1to2 = pairwise_diff(frame_1_gray, frame_2_gray)
            # subject_bbox_from_mask expects ndarray for frame_b (uses .ndim)
            frame_1_arr = np.asarray(frames[0].convert("RGB"))
            frame_1_bbox = subject_bbox_from_mask(mask_1to2, frame_1_arr)
            mask_1to2_has_motion = bool(mask_1to2.any())
        except Exception as e:  # noqa: BLE001 — defensive: never fail the trajectory
            log.warning(f"[{alert_id}] motion_detector: frame_1 bbox failed: {e}")
        try:
            frame_3_gray = np.asarray(frames[2].convert("L"))
            frame_4_gray = np.asarray(frames[3].convert("L"))
            mask_3to4 = pairwise_diff(frame_3_gray, frame_4_gray)
            frame_4_arr = np.asarray(frames[3].convert("RGB"))
            frame_4_bbox = subject_bbox_from_mask(mask_3to4, frame_4_arr)
        except Exception as e:  # noqa: BLE001 — defensive: never fail the trajectory
            log.warning(f"[{alert_id}] motion_detector: frame_4 bbox failed: {e}")

    # frame_2 fallback: only use bbox_a if mask_1to2 has motion (subject
    # was in frame_2). Otherwise subject appeared later → frame_2 absent.
    frame_2_bbox: tuple[int, int, int, int] | None
    if subject_bbox_a is not None:
        frame_2_bbox = subject_bbox_a
    elif bbox_a is not None and mask_1to2_has_motion:
        frame_2_bbox = bbox_a
    else:
        frame_2_bbox = None  # → "absent"

    # frame_3: similar — subject_bbox_b preferred, bbox_b fallback.
    # mask_2to3 motion is implicit (gate returned a vehicle verdict).
    frame_3_bbox: tuple[int, int, int, int] | None
    if subject_bbox_b is not None:
        frame_3_bbox = subject_bbox_b
    elif bbox_b is not None:
        frame_3_bbox = bbox_b
    else:
        frame_3_bbox = None  # → "absent"

    bbox_per_frame: list[tuple[int, int, int, int]] = [
        frame_1_bbox if frame_1_bbox else (0, 0, 0, 0),  # frame_1
        frame_2_bbox if frame_2_bbox else (0, 0, 0, 0),  # frame_2
        frame_3_bbox if frame_3_bbox else (0, 0, 0, 0),  # frame_3
        frame_4_bbox if frame_4_bbox else (0, 0, 0, 0),  # frame_4
    ]
    centers: list[tuple[int, int]] = []
    areas: list[int] = []
    for bbox in bbox_per_frame:
        if bbox == (0, 0, 0, 0):
            centers.append((0, 0))
            areas.append(0)
        else:
            (cx, cy), area = _bbox_center_area(bbox)
            centers.append((cx, cy))
            areas.append(area)

    trajectory: list[str] = [
        "absent" if c == (0, 0) else _center_to_label(c[0], c[1], frame_w, frame_h)
        for c in centers
    ]

    # Count "present" frames (non-absent, non-zero bbox).
    present_frames = [i for i, a in enumerate(areas) if a > 0]
    avg_area = int(np.mean([areas[i] for i in present_frames])) if present_frames else 0

    primary = MovingObject(
        bbox_per_frame=bbox_per_frame,
        center_per_frame=centers,
        area_per_frame=areas,
        trajectory=trajectory,
        avg_area=avg_area,
        frames_seen=len(present_frames),
        total_motion_pixels=int(sum(areas)),
        position_change_max=0,  # only 2 bboxes, no position-change delta meaningful
        best_crop_path=crop_paths[0] if crop_paths else None,
        crop_paths=list(crop_paths),
    )

    result.moving_objects = [primary]
    result.primary_moving_object = primary
    result.crop_paths = list(crop_paths)
    result.best_crop_path = crop_paths[0] if crop_paths else None
    result.no_motion_detected = False
    result.reference_method = "gate"
    result.total_motion_pixels = int(sum(areas))
    result.elapsed_ms = (time.perf_counter() - t0) * 1000
    log.info(
        f"[{alert_id}] motion_detector: built from gate "
        f"trajectory={trajectory} avg_area={avg_area} "
        f"crops={len(crop_paths)} [{result.elapsed_ms:.0f}ms]"
    )
    return result