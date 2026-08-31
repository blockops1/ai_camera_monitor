"""
motion_detector.py — Pairwise-diff motion detection (Phase.71).

STATUS: legacy (multi-job: motion detect + blob tracking + bbox crop
    generation — see KNOWN VIOLATIONS)
THREAD SAFETY: thread-safe (pure functions, no shared state)

INPUTS:
    - function arg frame_paths: list[str] (required) — JPEG paths from
      one capture batch (typically 6 frames)
    - function arg camera_name: str (required, for log lines only)

OUTPUTS:
    - return value: MotionResult dataclass (per-camera top-N crops)
    - writes file: data/frames/<camera>_<ts>/crop_001.jpg, crop_002.jpg
    - log line per call (debug level)

PUBLIC API:
    detect_motion(frame_paths: list[str], camera_name: str) -> MotionResult
        Run motion detection on a batch of frames. Returns:
        - moving_objects: list of MovingObject (id, bbox, frames_seen, score)
        - top_n_crops: list of (crop_path, bbox, score) — best 3
    MovingObject — dataclass for a tracked moving subject
    MotionResult — dataclass returned by detect_motion()

KNOWN VIOLATIONS (see PLAN.md Part 9):
    - Also produces cropped JPEG files — should split to
      infra/crop_writer.py (~100 lines)
    - Also maintains blob association logic (track_object) — could
      split to infra/blob_tracker.py if it grows

WHY HERE:
    Phase.71 (2026-08-08) replaced Phase.64's median-reference
    algorithm with pairwise-diff. the operator's observation: "you would just
    look at the difference between one frame to the next, not one frame
    to all the previous frames." Pairwise diff detects slow continuous
    motion that the median approach absorbed into the background.

    Pipeline position:
        capture 6 frames -> motion_detector() -> vision(classify) -> alert

CALLED BY:
    - listener.listener: detect_motion() in _process_alert()

CALLS INTO:
    - cv2: image load + absdiff + connected components
    - numpy: array math
    - os, glob: crop file paths

RELATED:
    - data/frames/<camera>_<ts>/ — directory of crop files this module
      writes (lives under infra.paths.FRAMES_DIR)
    - infra.frame_capture — produces the input frame_paths

HISTORY:
    2026-08-30 — Phase.167 §13.5 (Commit 11). Comment scrub:
      removed operator-flavored camera names ("Garage"/"Solar")
      and the operator IP "<CAM_IP_REDACTED>" from the
      MAX_CENTER_DIST_PX rationale and trajectory-grid comment.
      Logic is unchanged; only the inline commentary was made
      operator-agnostic so the constants + their reasoning can
      round-trip into the public repo.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, field

import cv2
import numpy as np

log = logging.getLogger("motion_detector")

# --- Tunables (verified empirically; see PHASE6B64-PRD) ---
RESIZE_W = 1280
RESIZE_H = 960
MOTION_THRESHOLD = 25          # intensity diff to count as motion (0-255)
MIN_FRAMES_SEEN = 3            # blob must appear in >=3 of 6 frames
MIN_AVG_AREA_PX = 500          # blob must average this many pixels
POSITION_CHANGE_MIN = 5        # min center delta (px) between frames
CROP_PAD_PCT = 0.0             # Phase.82: no padding — Qwen sees the
                              # diff zone exactly (same as the green box on
                              # the visualization). the operator 2026-08-16: padding
                              # was pulling in neighbors and causing
                              # misidentifications.
MIN_CROP_DIM = 50              # skip crop if smaller than this (likely noise)
MIN_CROP_SHORT_SIDE = 25       # shorter side may be smaller (distant vehicle in profile)
# Phase.71: switched from IOU-based tracking (MIN_IOU=0.05) to
# center-distance tracking (MAX_CENTER_DIST_PX=300). Under pairwise-diff,
# when an object moves fast enough that its bbox jumps >50% of its own
# width between frames, IOU goes to 0 and the tracker loses the object.
# Center-distance is robust to fast motion. Tested on 4 real alerts:
# no false positives, no dropped trajectories.
# Phase.92 (2026-08-18) — bumped from 300 to 600. The 300 threshold was
# tuned for a stationary-mount camera where vehicles move ~130 px per 2s
# interval, but the longer-throw camera (different mount) sees faster
# cross-frame motion (vehicle drive-by 15:08 EDT: 322 px jump between
# frame 2→3 because pairwise diff produces a trailing-edge bbox whose
# center is the gap left behind, not the vehicle's current position).
# With 300 px, the tracker lost the candidate at frame 3 and reported
# frames_seen=2 (failed MIN_FRAMES_SEEN=3). 600 covers the realistic
# upper bound for fast-moving vehicles on either camera without
# admitting noise (paired bboxes have to actually be near the predicted
# position; far-away blobs still get rejected).
MAX_CENTER_DIST_PX = 600        # max center-to-center distance (px) to associate
MIN_IOU = 0.05                 # legacy — superseded by MAX_CENTER_DIST_PX
TOP_N_CROPS = 3                # Phase.65: save up to 3 crops of moving object
                                # (approach + mid-scene + settled by bbox area ranking)
# Phase.72: switched from 3x3 (9 cells, 426x320 each) to 4x4 (16 cells,
# 320x240 each) per operator's request 2026-08-10. Vertical separation
# matters for the long-throw camera (gate zone vs parking zone vs far-back).
# Rows: T (top), UM (upper-middle), LM (lower-middle), B (bottom).
# Cols: 1, 2, 3, 4. 'absent' stays as the no-detection marker.
TRAJECTORY_LABELS = (
    # Row 0: T (y=0..240)
    "T1", "T2", "T3", "T4",
    # Row 1: UM (y=240..480)
    "UM1", "UM2", "UM3", "UM4",
    # Row 2: LM (y=480..720)
    "LM1", "LM2", "LM3", "LM4",
    # Row 3: B (y=720..960)
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
    crop_paths: list[str] = field(default_factory=list)  # Phase.65: top-N crops

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class MotionResult:
    moving_objects: list[MovingObject] = field(default_factory=list)
    primary_moving_object: MovingObject | None = None
    best_crop_path: str | None = None
    crop_paths: list[str] = field(default_factory=list)  # Phase.65: top-N crops
    no_motion_detected: bool = True
    reference_method: str = "median"
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


def _load_grayscale(path: str) -> np.ndarray | None:
    img = cv2.imread(path, cv2.IMREAD_GRAYSCALE)
    if img is None:
        return None
    if img.shape[1] != RESIZE_W or img.shape[0] != RESIZE_H:
        img = cv2.resize(img, (RESIZE_W, RESIZE_H), interpolation=cv2.INTER_AREA)
    return img


def _center_to_label(cx: int, cy: int) -> str:
    """Map normalized (cx, cy) to one of 16 position labels (Phase.72 4x4 grid)."""
    col = min(3, cx * 4 // RESIZE_W)
    row = min(3, cy * 4 // RESIZE_H)
    return TRAJECTORY_LABELS[row * 4 + col]


def _bbox_iou(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> float:
    """Intersection-over-Union for two (x, y, w, h) bboxes."""
    ax1, ay1, aw, ah = a
    bx1, by1, bw, bh = b
    ax2, ay2 = ax1 + aw, ay1 + ah
    bx2, by2 = bx1 + bw, by1 + bh
    ix1, iy1 = max(ax1, bx1), max(ay1, by1)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    iw, ih = max(0, ix2 - ix1), max(0, iy2 - iy1)
    inter = iw * ih
    union = aw * ah + bw * bh - inter
    return inter / union if union > 0 else 0.0


def _components_per_frame(mask: np.ndarray) -> list[tuple[int, int, int, int]]:
    """Return list of (x, y, w, h) bboxes for connected components in mask."""
    n_comp, _labels, stats, _ = cv2.connectedComponentsWithStats(mask)
    return [
        (int(stats[i][0]), int(stats[i][1]),
         int(stats[i][2]), int(stats[i][3]))
        for i in range(1, n_comp)
        if stats[i][4] >= MIN_AVG_AREA_PX
    ]


def _first_nonempty_frame(
    per_frame_bboxes: list[list[tuple[int, int, int, int]]],
) -> int | None:
    """Return the first frame containing a motion component.

    A vehicle can enter after multiple empty pairwise masks. The old seed
    logic checked only frames 0 and 1, which dropped real late-entry bursts.
    """
    return next((i for i, bboxes in enumerate(per_frame_bboxes) if bboxes), None)


def _track_object(
    per_frame_bboxes: list[list[tuple[int, int, int, int]]],
) -> MovingObject | None:
    """Track one moving object across frames via closest-center-distance.

    Phase.71: switched from IOU-based tracking to center-distance
    matching. Under pairwise-diff, bboxes can jump >50% of their own
    width between consecutive frames (fast-moving objects), making IOU
    collapse to 0 and the tracker loses the object. Center-distance is
    robust to fast motion — only requires the next bbox to be within
    MAX_CENTER_DIST_PX pixels of the previous one's center.

    Seed from frame 0 (or frame 1 if frame 0 is empty, as happens when
    frame 0 is the pairwise baseline with no previous frame to compare
    against). Greedy: in each subsequent frame, find the bbox whose
    center is closest to the previous frame's center. Accept that
    match if distance <= MAX_CENTER_DIST_PX; otherwise mark as absent.

    Returns MovingObject with per-frame bbox/center/area/trajectory.
    """
    # Track from the first frame that actually contains a component. This
    # is usually frame 1 because frame 0 is the pairwise baseline, but real
    # vehicles can enter later in the burst (08:45 alert 8abf1b17 first
    # produced a component in frame 2).
    seed_frame_idx = _first_nonempty_frame(per_frame_bboxes)
    if seed_frame_idx is None:
        return None

    tracked: list[tuple[int, int, int, int]] = [per_frame_bboxes[seed_frame_idx][0]]
    present_in_frame = [True]  # for seed_frame only

    for frame_idx in range(seed_frame_idx + 1, len(per_frame_bboxes)):
        prev_bbox = tracked[-1]
        bboxes_now = per_frame_bboxes[frame_idx]

        if not bboxes_now:
            tracked.append((0, 0, 0, 0))
            present_in_frame.append(False)
            continue

        # Find closest center match.
        prev_cx = prev_bbox[0] + prev_bbox[2] // 2
        prev_cy = prev_bbox[1] + prev_bbox[3] // 2
        best_dist = float("inf")
        best_bbox = None
        for bbox in bboxes_now:
            bcx = bbox[0] + bbox[2] // 2
            bcy = bbox[1] + bbox[3] // 2
            d = ((bcx - prev_cx) ** 2 + (bcy - prev_cy) ** 2) ** 0.5
            if d < best_dist:
                best_dist = d
                best_bbox = bbox

        if best_dist <= MAX_CENTER_DIST_PX and best_bbox is not None:
            tracked.append(best_bbox)
            present_in_frame.append(True)
        else:
            tracked.append((0, 0, 0, 0))
            present_in_frame.append(False)

    # Phase.71: pad present_in_frame and tracked to match per_frame_bboxes
    # length (in case we had an empty frame 0 and seeded from frame 1).
    while len(present_in_frame) < len(per_frame_bboxes):
        present_in_frame.insert(0, False)
    while len(tracked) < len(per_frame_bboxes):
        tracked.insert(0, (0, 0, 0, 0))

    # NOTE: bbox_per_frame[i] describes the diff between frames i-1 and i,
    # not the position on frame_paths[i]. Consumers must apply
    # bbox_per_frame[i] to frame_paths[i-1] (or to the previous image kept
    # in memory). See PLAN.md §11.18.
    mo = MovingObject()
    mo.bbox_per_frame = tracked

    for x, y, w, h in tracked:
        if w == 0 or h == 0:
            mo.center_per_frame.append((0, 0))
            mo.area_per_frame.append(0)
        else:
            mo.center_per_frame.append((x + w // 2, y + h // 2))
            mo.area_per_frame.append(w * h)

    present_frames = [i for i, p in enumerate(present_in_frame) if p]
    mo.frames_seen = len(present_frames)
    if mo.frames_seen == 0:
        return None

    mo.avg_area = int(np.mean([mo.area_per_frame[i] for i in present_frames]))
    mo.trajectory = [
        _center_to_label(cx, cy) if (cx, cy) != (0, 0) else "absent"
        for cx, cy in mo.center_per_frame
    ]

    position_changes = []
    for i in range(1, len(present_frames)):
        prev_idx, curr_idx = present_frames[i - 1], present_frames[i]
        dx = abs(mo.center_per_frame[curr_idx][0] - mo.center_per_frame[prev_idx][0])
        dy = abs(mo.center_per_frame[curr_idx][1] - mo.center_per_frame[prev_idx][1])
        position_changes.append(max(dx, dy))
    mo.position_change_max = int(max(position_changes)) if position_changes else 0

    return mo


def _crop_one(
    obj: MovingObject,
    frames: list[np.ndarray],
    frame_paths: list[str],
    frame_idx: int,
    output_dir: str,
    alert_id: str,
    crop_idx: int,
) -> str | None:
    """Crop the moving object from a single frame, save as JPEG.

    Phase.82: no padding — the bbox itself is the crop region.
    Qwen sees the same diff zone the visualization's green box shows.

    Phase.86 (PLAN.md §11.18): bbox_per_frame[frame_idx] describes the
    diff between frames frame_idx-1 and frame_idx. The "departure" region
    of that diff — the region the tracker typically locks onto for fast-
    moving objects — corresponds to the moving object's position on
    frame_paths[frame_idx - 1]. To apply the bbox to the frame it actually
    describes, we crop frame_paths[frame_idx - 1] (the previous image,
    kept on disk by the listener). Frame 0 has no previous frame; return
    None.

    Used by _crop_top_n. Returns the saved path, or None on failure
    (no previous frame, frame can't be loaded, bbox too small, etc.).
    """
    if frame_idx < 1:
        # No previous frame; the bbox at index 0 is the diff between an
        # imaginary frame -1 and frame 0, which doesn't exist. We can't
        # apply it to any real frame.
        return None
    x, y, w, h = obj.bbox_per_frame[frame_idx]
    # Phase.119: loosened the gate. Previously w >= MIN_CROP_DIM AND h >=
    # MIN_CROP_DIM, which rejected distant vehicles seen in profile (e.g.
    # 70x35 px = ~30m distance on a wide-angle camera). Now: long side must
    # be >= MIN_CROP_DIM (filters noise blobs) AND short side must be >=
    # MIN_CROP_SHORT_SIDE (lets tall+thin real vehicles through). A 30x30
    # noise blob still fails (min=30 < 50 long AND 30 >= 25 short → passes
    # the second check; but the FIRST check (long side >= 50) fails, so it
    # still gets rejected). A 70x35 vehicle passes both.
    if max(w, h) < MIN_CROP_DIM or min(w, h) < MIN_CROP_SHORT_SIDE:
        return None

    # Phase.82: bbox passed through unchanged (no padding). Clamping
    # preserves safety for edge bboxes.
    x0 = max(0, x)
    y0 = max(0, y)
    x1 = min(RESIZE_W, x + w)
    y1 = min(RESIZE_H, y + h)

    # Phase.86: load the PREVIOUS frame (frame_paths[frame_idx - 1])
    # — the image this bbox describes. The bbox was computed from the
    # diff between frames frame_idx-1 and frame_idx; its "departure"
    # region (the tracker-locked component) is the moving object's
    # position on frame_paths[frame_idx - 1].
    src = cv2.imread(frame_paths[frame_idx - 1])
    if src is None:
        return None
    # Scale crop coords back to original frame size.
    sx = src.shape[1] / RESIZE_W
    sy = src.shape[0] / RESIZE_H
    src_x0 = int(x0 * sx)
    src_y0 = int(y0 * sy)
    src_x1 = int(x1 * sx)
    src_y1 = int(y1 * sy)
    crop = src[src_y0:src_y1, src_x0:src_x1]
    if crop.size == 0:
        return None

    os.makedirs(output_dir, exist_ok=True)
    # Phase.65 — write to crops/ subdirectory so multiple crops per
    # alert don't collide with other alert_id outputs.
    crops_dir = os.path.join(output_dir, "crops")
    os.makedirs(crops_dir, exist_ok=True)
    crop_path = os.path.join(crops_dir, f"{alert_id}_crop_{crop_idx}.jpg")
    cv2.imwrite(crop_path, crop, [cv2.IMWRITE_JPEG_QUALITY, 90])
    return crop_path


def _crop_top_n(
    obj: MovingObject,
    frames: list[np.ndarray],
    frame_paths: list[str],
    output_dir: str,
    alert_id: str,
    n: int = TOP_N_CROPS,
) -> list[str]:
    """Save up to N crops of the moving object, sorted by bbox area (largest first).

    Phase.65 — was _crop_best returning a single crop. The
    identifier routine consumes up to 3 crops so vision can see
    different angles (approach / mid / settled).

    Skips frames where the moving object wasn't detected
    (bbox_per_frame[i] is None / zeros).
    """
    if obj.avg_area < MIN_CROP_SHORT_SIDE * MIN_CROP_DIM:
        # Phase.119: relaxed the avg-area floor from MIN_CROP_DIM^2 (2500)
        # to MIN_CROP_DIM * MIN_CROP_SHORT_SIDE (1250). A 70x35 distant
        # vehicle averages ~2555 px², which passes 1250; a 30x30 noise blob
        # averages 900 px², which still fails. The avg-area floor is a
        # second-line filter; the per-frame check above is the primary gate.
        return []

    # Find frames where the object has a usable bbox, ranked by area desc
    candidate_indices: list[tuple[int, int]] = []  # (frame_idx, area)
    for i, bbox in enumerate(obj.bbox_per_frame):
        if bbox is None or len(bbox) != 4:
            continue
        area = obj.area_per_frame[i] if i < len(obj.area_per_frame) else 0
        if area <= 0:
            continue
        candidate_indices.append((i, area))

    candidate_indices.sort(key=lambda x: -x[1])  # largest first

    saved: list[str] = []
    for crop_idx, (frame_idx, _) in enumerate(candidate_indices[:n]):
        path = _crop_one(obj, frames, frame_paths, frame_idx,
                         output_dir, alert_id, crop_idx)
        if path:
            saved.append(path)
    return saved


def _persist_motion_json(result: MotionResult, output_dir: str, alert_id: str) -> None:
    """
    Phase.85 (PLAN.md §11.17): persist the full motion-detection result to
    ``data/frames/<alert_id>/motion.json`` so that bbox / trajectory / area data
    survives past the alert pipeline run. Without this, the per-frame bounding
    boxes that drove vision's crop selection are only available inside the
    MovingObject dataclass for the duration of the pipeline — by the time a
    user asks 'what bbox went to Qwen?' hours later, that data is gone.

    The write is best-effort (log on failure, no raise) because persistent
    forensic data is not part of the alert path's correctness contract — a
    failed write must never abort an alert.

    Contents:
        - moving_objects: list of all detected objects with
          bbox_per_frame / center_per_frame / area_per_frame / trajectory
        - primary_moving_object: the same fields for the primary
        - best_crop_path / crop_paths
        - no_motion_detected / reference_method / total_motion_pixels /
          elapsed_ms
    """
    motion_json_path = os.path.join(output_dir, "motion.json")
    try:
        os.makedirs(output_dir, exist_ok=True)
        tmp = motion_json_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(result.to_dict(), f, indent=2, default=str)
        os.replace(tmp, motion_json_path)
        log.info(
            f"[{alert_id}] motion_detector: persisted motion.json "
            f"({len(result.moving_objects)} object(s))"
        )
    except OSError as e:
        # Best-effort: don't let a forensic write abort the alert pipeline.
        log.warning(f"[{alert_id}] motion_detector: motion.json persist failed: {e}")


def detect_motion(
    frame_paths: list[str],
    output_dir: str,
    alert_id: str,
) -> MotionResult:
    """Run motion detection on 6 captured frames.

    Returns MotionResult with moving_objects[], primary_moving_object, and
    best_crop_path (if a primary was found).
    """
    import time
    t0 = time.perf_counter()

    result = MotionResult()

    if len(frame_paths) < 2:
        log.warning(f"[{alert_id}] motion_detector: need >=2 frames, got {len(frame_paths)}")
        return result

    frames: list[np.ndarray] = []
    for p in frame_paths:
        img = _load_grayscale(p)
        if img is None:
            log.warning(f"[{alert_id}] motion_detector: failed to load {p}")
            return result
        frames.append(img)

    # 1. Pairwise diff: per-frame motion mask = |frame_i - frame_{i-1}|.
    #    Frame 0 has no previous; mask is empty (baseline).
    #    Phase.71: this replaces the 6B.64 pixel-wise median reference,
    #    which absorbed slow-moving objects that occupied the same position
    #    for 3+ frames. See module docstring for full rationale.
    per_frame_masks: list[np.ndarray] = []
    combined_mask = np.zeros((RESIZE_H, RESIZE_W), dtype=np.uint8)
    for i, frame in enumerate(frames):
        if i == 0:
            mask = np.zeros((RESIZE_H, RESIZE_W), dtype=np.uint8)
        else:
            diff = cv2.absdiff(frame, frames[i - 1])
            _, mask = cv2.threshold(diff, MOTION_THRESHOLD, 255, cv2.THRESH_BINARY)  # type: ignore[assignment]
        per_frame_masks.append(mask)
        combined_mask = cv2.bitwise_or(combined_mask, mask)  # type: ignore[assignment]

    result.total_motion_pixels = int(np.count_nonzero(combined_mask))
    result.reference_method = "pairwise"  # Phase.71 — was "median"

    # 4. Per-frame connected components. For each frame, find the bboxes
    #    of moving regions. Then track one object across frames using IOU.
    per_frame_bboxes: list[list[tuple[int, int, int, int]]] = [
        _components_per_frame(mask) for mask in per_frame_masks
    ]

    # 5. Greedy tracker: starts with each bbox in frame 0 as a candidate
    #    seed, follows it across frames via center-distance. For multiple
    #    objects we'd need Hungarian assignment, but for CAM1 (single dominant
    #    moving object) this is sufficient. We keep the candidate with the
    #    best trajectory (most frames seen + largest avg area).
    #
    #    Phase.71: under pairwise-diff, frame 0's mask is empty (no
    #    previous frame to subtract). If frame 0 is empty, seed from
    #    frame 1's bboxes instead. _track_object also handles this case
    #    internally for tracker robustness.
    #
    #    Pairwise diff can produce multiple bboxes per frame (a moving
    #    object leaves its old position "uncovered" and creates a new
    #    one). Seed from frame 1's LARGEST bbox only — the smaller ones
    #    are usually the trailing edge of the same object. This keeps
    #    the candidate count to 1 per detected object.
    candidates: list[MovingObject] = []
    seed_frame_idx = _first_nonempty_frame(per_frame_bboxes)
    if seed_frame_idx is not None:
        seed_bboxes_list = per_frame_bboxes[seed_frame_idx]
        # Pick the largest bbox as the seed (other bboxes in the first
        # non-empty pairwise mask are usually trailing-edge artifacts of
        # the same object).
        largest_seed = max(seed_bboxes_list, key=lambda b: b[2] * b[3])
        seed_bboxes: list[list[tuple[int, int, int, int]]] = [
            [] for _ in per_frame_bboxes
        ]
        seed_bboxes[seed_frame_idx] = [largest_seed]
        for i in range(seed_frame_idx + 1, len(per_frame_bboxes)):
            seed_bboxes[i] = per_frame_bboxes[i]
        tracked = _track_object(seed_bboxes)
        if tracked is not None:
            candidates.append(tracked)

    # 6. Filter candidates.
    filtered = [
        m for m in candidates
        if m.frames_seen >= MIN_FRAMES_SEEN
        and m.avg_area >= MIN_AVG_AREA_PX
        and m.position_change_max >= POSITION_CHANGE_MIN
    ]
    result.moving_objects = filtered

    if not filtered:
        result.no_motion_detected = True
        result.elapsed_ms = (time.perf_counter() - t0) * 1000
        log.info(
            f"[{alert_id}] motion_detector: no motion detected "
            f"(total_motion_px={result.total_motion_pixels}, "
            f"candidates={len(candidates)}) [{result.elapsed_ms:.0f}ms]"
        )
        return result

    # 7. Primary = largest by avg_area.
    primary = max(filtered, key=lambda m: m.avg_area)
    result.primary_moving_object = primary
    result.no_motion_detected = False

    # 8. Crop primary — Phase.65: save up to 3 crops by bbox area.
    crop_paths = _crop_top_n(primary, frames, frame_paths, output_dir, alert_id)
    result.crop_paths = crop_paths
    result.best_crop_path = crop_paths[0] if crop_paths else None  # backward compat
    primary.crop_paths = crop_paths
    primary.best_crop_path = result.best_crop_path

    result.elapsed_ms = (time.perf_counter() - t0) * 1000
    log.info(
        f"[{alert_id}] motion_detector: found {len(filtered)} moving object(s); "
        f"primary avg_area={primary.avg_area}, trajectory={primary.trajectory} "
        f"[{result.elapsed_ms:.0f}ms]"
    )
    # Phase.85: persist full motion-result for forensic debugging.
    # See _persist_motion_json for why this is best-effort.
    _persist_motion_json(result, output_dir, alert_id)
    return result
