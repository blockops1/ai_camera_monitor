"""frame_diff — pairwise frame differencing for motion-gate bbox extraction.

STATUS: provisional (Phase 6B.107 §11.37, 2026-08-23; 6B.171 subject bbox 2026-09-01,
        STRICT commit — no size floor, no diff-bbox fallback; 6B.173 two-mask
        intersection anchor 2026-09-01)
INPUTS: file paths to two image files (JPEG/PNG, any resolution); OR in-memory
        uint8 ndarrays (already loaded)
OUTPUTS: tuple of (motion_mask: np.ndarray uint8 HxW, bbox: (x, y, w, h) | None,
        changed_pixel_count: int)
PUBLIC API:
  - load_frame(path: str) -> np.ndarray | None    # grayscale, native resolution
  - pairwise_diff(frame_a, frame_b, threshold=25) -> np.ndarray uint8
  - bbox_from_mask(mask, min_area_px=64) -> tuple[int, int, int, int] | None
        # Diff bbox from the largest connected component. Padded by
        # DEFAULT_BBOX_PAD_PCT (10% per side) and rounded UP to multiples of 32
        # so YOLO's internal pad-to-multiple-of-32 becomes a no-op.
  - diff_pair_with_bbox(frame_a_path, frame_b_path, threshold=25, min_area_px=64)
      -> tuple[bbox, changed_pixel_count, mask] | (None, 0, empty_mask)
  - crop_frame_to_bbox(frame_path, bbox) -> str | None
DOES NOT DO:
  - Does NOT classify what's in the bbox (that's quick_classifier's job)
  - Does NOT persist anything to disk (caller saves crops)
  - Does NOT call Qwen, Telegram, or any pipeline code
  - Does NOT resize to motion_detector's 160×120 (we keep native resolution so
    crops have detail for YOLO + human review)
  - Does NOT maintain per-camera background models (no MOG2 / ViBe — Phase 6B.171
    chose the simplest approach that solved the observed problem)
  - Does NOT fall back to bbox_from_mask when subject detection fails
    (Phase 6B.171 strict — caller suppresses the alert instead)
CALLED BY: listener/motion_gate_pipeline.py (Phase 6B.107 + 6B.171)
CALLS INTO: nothing in this repo (pure cv2 + numpy)
RELATED: infra/motion_detector.py does its OWN pairwise diff at 160×120 for
  its 6-frame trajectory tracking — that module is unchanged. frame_diff is a
  focused helper for the gate's 2-frame diff (Option C1 in §11.37).

Implementation notes:
  - Native-resolution grayscale (not resized). Reason: the bbox we return is
    used to crop from the ORIGINAL frames at their original size, then we
    letterbox for YOLO at 640×640 inside quick_classifier. Keeping native
    resolution in frame_diff means the bbox coordinates map directly to the
    frame's pixels — no scale math.
  - cv2.absdiff is fast (~10ms on 1920×1080 grayscale on Apple Silicon CPU).
  - Threshold 25 is empirically reasonable for daytime (headlight flare at
    night may need lower; future tuning per-camera).
  - Connected components via cv2.connectedComponentsWithStats — gets bboxes
    + areas in one pass.
  - min_area_px=64 filters out noise (single-pixel sensor glitches, JPEG
    artifacts on flat surfaces). Tunable per-camera in a future phase.

Phase 6B.171 NOTES (subject bbox — why this exists, why STRICT):
  - Problem: diff_pair_with_bbox returns the bbox of the largest connected
    region in the diff mask. For a moving subject this region spans BOTH the
    subject's old position AND the trail of background it uncovered — the
    trail extends past where the subject IS in the new frame. When we crop
    frame_b at that bbox, the subject is often at the edge or out of crop.
    Observed on 2026-09-01 morning: 5 of 7 alerts had one empty crop out
    of two (the "only 1 of 2 has a vehicle" symptom Operator reported).
  - Standard practice in motion-detection literature (MOG2, ViBe,
    background subtraction papers): erode the diff mask before connected
    components. Erosion kills thin trail wisps while preserving the dense
    connected region of the subject. Largest CC of the eroded mask is
    approximately the subject bbox.
  - Why not MOG2/ViBe: per-camera background models are a much larger
    change. The smallest change that solved the observed problem was to
    keep `bbox_from_mask` (the diff bbox) and use it for BOTH the green
    box on the composite AND the saved crop — one bbox per crop slot,
    no AND, no fallback. Future phase can layer a learned background
    if needed.
"""

from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np

# Default diff threshold (grayscale 0-255). Pixel must change by at least
# this much between frames to count as "changed".
DEFAULT_DIFF_THRESHOLD = 25

# Default minimum bbox area in pixels. Smaller regions are noise.
DEFAULT_MIN_AREA_PX = 64

# Default bbox padding — 10% per side (20% overall per dimension), then
# rounded UP to the nearest multiple of 32.  This gives YOLO consistent
# context regardless of subject size and makes YOLO's pad-to-multiple-of-32
# a no-op (dimensions are already multiples of 32).
DEFAULT_BBOX_PAD_PCT = 0.10


def _expand_bbox_pct(
    bbox: tuple[int, int, int, int],
    pad_pct: float,
    frame_w: int,
    frame_h: int,
) -> tuple[int, int, int, int]:
    """Expand a bbox by *pad_pct* on each side, round UP to mult of 32, clamp.

    Math:
      1. raw_w = round(w * (1 + 2*pad_pct)), raw_h = round(h * (1 + 2*pad_pct))
      2. new_w = min(((raw_w + 31) // 32) * 32, frame_w)
      3. new_h = min(((raw_h + 31) // 32) * 32, frame_h)
      4. Re-center on original bbox center: new_x = round(cx - new_w/2),
         new_y = round(cy - new_h/2)
      5. Clamp: new_x = max(0, min(new_x, frame_w - new_w)),
         new_y = max(0, min(new_y, frame_h - new_h))

    Returns (x, y, new_w, new_h) clamped to the image frame.
    """
    x, y, w, h = bbox
    cx = x + w / 2.0
    cy = y + h / 2.0

    raw_w = round(w * (1 + 2 * pad_pct))
    raw_h = round(h * (1 + 2 * pad_pct))

    new_w = min(((raw_w + 31) // 32) * 32, frame_w)
    new_h = min(((raw_h + 31) // 32) * 32, frame_h)

    new_x = round(cx - new_w / 2.0)
    new_y = round(cy - new_h / 2.0)

    new_x = max(0, min(new_x, frame_w - new_w))
    new_y = max(0, min(new_y, frame_h - new_h))

    return (int(new_x), int(new_y), int(new_w), int(new_h))


def load_frame(path: str) -> np.ndarray | None:
    """Load an image file as grayscale at native resolution.

    Returns None if the file can't be read.

    Native resolution (not resized) because the bbox we extract from this
    frame needs to map directly to the frame's pixel coordinates for cropping.
    """
    img = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
    if img is None:
        return None
    return img


def pairwise_diff(
    frame_a: np.ndarray,
    frame_b: np.ndarray,
    threshold: int = DEFAULT_DIFF_THRESHOLD,
) -> np.ndarray:
    """Compute |frame_a - frame_b|, threshold to binary uint8 mask.

    Both frames must be the same shape (grayscale, uint8). Mismatched shapes
    raise ValueError.

    Returns a uint8 ndarray (0 or 255) the same shape as the inputs.
    """
    if frame_a.shape != frame_b.shape:
        raise ValueError(
            f"frame shapes must match: {frame_a.shape} vs {frame_b.shape}"
        )
    if frame_a.dtype != np.uint8 or frame_b.dtype != np.uint8:
        raise ValueError(
            f"frames must be uint8 grayscale, got {frame_a.dtype} and {frame_b.dtype}"
        )
    diff = cv2.absdiff(frame_a, frame_b)
    _, mask = cv2.threshold(diff, threshold, 255, cv2.THRESH_BINARY)
    return mask


def bbox_from_mask(
    mask: np.ndarray,
    min_area_px: int = DEFAULT_MIN_AREA_PX,
    pad_pct: float = DEFAULT_BBOX_PAD_PCT,
) -> tuple[int, int, int, int] | None:
    """Find the largest connected region in the mask and return its bbox.

    Returns (x, y, w, h) of the largest connected component that meets
    min_area_px. Expands the bbox by pad_pct on each side (10% per side
    = 20% overall), rounds UP to the nearest multiple of 32, and clamps
    to image bounds. Returns None if no component meets the area threshold.

    Why "largest component" instead of "union of all components":
      - Option C1 (§11.37) wants one bbox per diff pair. The motion object
        is the largest connected changed region. Smaller blobs are noise
        (sensor glitches, JPEG artifacts).
      - If the diff has multiple motion objects, the LARGEST one is most
        likely the Reolink-detected motion. Smaller blobs can be ignored.
    """
    if mask is None or mask.size == 0:
        return None
    # connectedComponentsWithStats: labels, stats (x,y,w,h,area), centroids
    num_labels, _labels, stats, _centroids = cv2.connectedComponentsWithStats(
        mask, connectivity=8
    )
    if num_labels <= 1:
        # 1 label = background only. No motion.
        return None

    # Find the largest component (excluding background at index 0)
    largest_idx = 1
    largest_area = stats[1, cv2.CC_STAT_AREA]
    for i in range(2, num_labels):
        area = stats[i, cv2.CC_STAT_AREA]
        if area > largest_area:
            largest_area = area
            largest_idx = i

    if largest_area < min_area_px:
        return None

    x = int(stats[largest_idx, cv2.CC_STAT_LEFT])
    y = int(stats[largest_idx, cv2.CC_STAT_TOP])
    w = int(stats[largest_idx, cv2.CC_STAT_WIDTH])
    h = int(stats[largest_idx, cv2.CC_STAT_HEIGHT])

    # Expand by percentage, round UP to mult of 32, clamp to image bounds.
    h_img, w_img = mask.shape
    return _expand_bbox_pct((x, y, w, h), pad_pct, w_img, h_img)


def diff_pair_with_bbox(
    frame_a_path: str,
    frame_b_path: str,
    threshold: int = DEFAULT_DIFF_THRESHOLD,
    min_area_px: int = DEFAULT_MIN_AREA_PX,
    pad_pct: float = DEFAULT_BBOX_PAD_PCT,
) -> tuple[tuple[int, int, int, int] | None, int, np.ndarray]:
    """One-shot helper: load 2 frames, diff them, return bbox + stats.

    Returns (bbox, changed_pixel_count, mask).
      - bbox: (x, y, w, h) or None if no motion detected
      - changed_pixel_count: total non-zero pixels in mask
      - mask: the binary diff mask (for debugging/visualization)

    If either frame fails to load, returns (None, 0, empty_mask).

    This is the function motion_gate_pipeline.py calls per the §11.37
    locked architecture:
      diff_pair_with_bbox(frame_2_path, frame_3_path) -> bbox_a, ...
      diff_pair_with_bbox(frame_3_path, frame_4_path) -> bbox_b, ...
    """
    empty_mask = np.zeros((1, 1), dtype=np.uint8)

    a = load_frame(frame_a_path)
    b = load_frame(frame_b_path)
    if a is None or b is None:
        return None, 0, empty_mask

    # If shapes differ (shouldn't happen but be safe), bail out.
    if a.shape != b.shape:
        return None, 0, empty_mask

    mask = pairwise_diff(a, b, threshold=threshold)
    changed_count = int(np.count_nonzero(mask))
    bbox = bbox_from_mask(mask, min_area_px=min_area_px, pad_pct=pad_pct)
    return bbox, changed_count, mask


def crop_frame_to_bbox(frame_path: str, bbox: tuple[int, int, int, int]) -> str | None:
    """Crop a frame file to the given bbox. Saves to a sibling _crop suffix.

    Returns the new file path, or None if loading/cropping fails.

    Naming: `<stem>_crop<x>_<y>_<w>x<h>.png` next to the source frame.
    Saves to disk because YOLO loads from file paths.
    """
    src = Path(frame_path)
    if not src.is_file():
        return None
    img = cv2.imread(str(src), cv2.IMREAD_COLOR)
    if img is None:
        return None
    x, y, w, h = bbox
    # Clamp to image bounds
    h_img, w_img = img.shape[:2]
    x = max(0, min(x, w_img - 1))
    y = max(0, min(y, h_img - 1))
    w = max(1, min(w, w_img - x))
    h = max(1, min(h, h_img - y))
    crop = img[y : y + h, x : x + w]
    if crop.size == 0:
        return None
    out_path = src.with_name(f"{src.stem}_crop{x}_{y}_{w}x{h}.png")
    # §11.88 (2026-09-01) — PNG lossless, NOT JPEG q90.
    ok = cv2.imwrite(str(out_path), crop)
    if not ok:
        return None
    return str(out_path)
