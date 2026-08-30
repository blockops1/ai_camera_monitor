"""
motion_detector_impl — Build MotionResult from motion-gate outputs (vehicle path).

Phase.115 (§11.46, §11.46.6): the motion gate is the sole producer
of frames + crops + diff bboxes on the vehicle path. This module no
longer reads from disk or runs any frame analysis. It just stitches
the gate's outputs into a MotionResult shape so the rest of the
pipeline (identify_stage, match_telegram, etc.) doesn't need to change.

Phase.115 (§11.46.6): the gate now hands in-memory PIL.Image objects
via `frames` + `crop_a` + `crop_b`. No filesystem reads on the hot path.

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
    - Resize frames → removed Phase.115 (gate already works at native)
    - Run pairwise diff on the frames → gate already produced bbox_a + bbox_b
    - Save crops from the frames → gate already saved crop_a + crop_b (when env var on)
    - Run YOLO → that lives in infra/quick_classifier (called by the gate)
    - Read from disk on the hot path → all inputs are in-memory PIL.Image
    - Compose multiple crops into a single image → infra/motion_visualization

CALLED BY:
    - listener.vehicle_event_pipeline.identify_stage (Phase.115)

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
from dataclasses import asdict, dataclass, field

import numpy as np

log = logging.getLogger("motion_detector")


# Phase.72: 4x4 grid (16 cells) for trajectory labels.
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


@dataclass
class MovingObject:
    bbox_per_frame: list[tuple[int, int, int, int]] = field(default_factory=list)
    center_per_frame: list[tuple[int, int]] = field(default_factory=list)
    area_per_frame: list[int] = field(default_factory=list)
    trajectory: list[str] = field(default_factory=list)
    avg_area: int = 0
    frames_seen: int = 0
    total_motion_pixels: int = 0
    position_change_max: int = 0
    best_crop_path: str | None = None
    crop_paths: list[str] = field(default_factory=list)  # disk paths for postmortem

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class MotionResult:
    moving_objects: list[MovingObject] = field(default_factory=list)
    primary_moving_object: MovingObject | None = None
    best_crop_path: str | None = None
    crop_paths: list[str] = field(default_factory=list)
    no_motion_detected: bool = True
    reference_method: str = "gate"  # always "gate"
    total_motion_pixels: int = 0
    elapsed_ms: float = 0.0

    def to_dict(self) -> dict:
        return {
            "moving_objects": [m.to_dict() for m in self.moving_objects],
            "primary_moving_object":
                self.primary_moving_object.to_dict()
                if self.primary_moving_object else None,
            "best_crop_path": self.best_crop_path,
            "crop_paths": self.crop_paths,
            "no_motion_detected": self.no_motion_detected,
            "reference_method": self.reference_method,
            "total_motion_pixels": self.total_motion_pixels,
            "elapsed_ms": self.elapsed_ms,
        }


def _center_to_label(cx: int, cy: int, frame_w: int, frame_h: int) -> str:
    """Map a center coordinate to one of 16 grid labels (4x4).

    Phase.115: caller passes native frame dims instead of relying on
    a hardcoded RESIZE_W=1280/RESIZE_H=960. The gate's bboxes are at
    native resolution (e.g., 2304x1296 for OFS) so the labels need to
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
) -> MotionResult:
    """Build a MotionResult from the motion-gate's in-memory outputs.

    Phase.115 (§11.46.6): all inputs are in-memory PIL.Image objects.
    No filesystem reads on the hot path. crop_paths is optional — only
    present when GATE_KEEP_DISK_ARTIFACTS=true (postmortem convenience).

    The gate has already:
      - captured 4 frames @ native resolution
      - run diff(frame_2, frame_3) → bbox_a
      - run diff(frame_3, frame_4) → bbox_b
      - cropped frame_3 + bbox_a → crop_a (PIL.Image)
      - cropped frame_4 + bbox_b → crop_b (PIL.Image)
      - run YOLO on the crops and returned the verdict

    This function just stitches those outputs into a MotionResult shape
    so the rest of the pipeline (identify_stage, match_telegram, etc.)
    doesn't need to change.

    Trajectory is 4 cells:
      - frame_1: 'absent' (no motion in frame_1)
      - frame_2: 'absent' (no motion in frame_2; gate's diff starts at 2-3)
      - frame_3: label from bbox_a center (or 'absent' if bbox_a is None)
      - frame_4: label from bbox_b center (or 'absent' if bbox_b is None)

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

    # Build a 4-frame trajectory from the 2 gate bboxes.
    # frame_1 + frame_2 have no motion (gate's diff starts at frame_2 vs frame_3).
    # frame_3 gets label from bbox_a center.
    # frame_4 gets label from bbox_b center.
    bbox_per_frame: list[tuple[int, int, int, int]] = [
        (0, 0, 0, 0),  # frame_1: no motion
        (0, 0, 0, 0),  # frame_2: no motion
        bbox_a if bbox_a else (0, 0, 0, 0),  # frame_3
        bbox_b if bbox_b else (0, 0, 0, 0),  # frame_4
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