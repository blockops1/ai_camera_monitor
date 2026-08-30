"""frame_diff — pairwise frame differencing for motion-gate bbox extraction.

STATUS: provisional (Phase.107 §11.37, 2026-08-23)
THREAD SAFETY: single-threaded (caller's responsibility; pure functions internally)
INPUTS: file paths to two image files (JPEG/PNG, any resolution)
OUTPUTS: tuple of (motion_mask: np.ndarray uint8 HxW, bbox: (x, y, w, h) | None,
        changed_pixel_count: int)
PUBLIC API:
  - load_frame(path: str) -> np.ndarray | None    # grayscale, native resolution
  - pairwise_diff(frame_a, frame_b, threshold=25) -> np.ndarray uint8
  - bbox_from_mask(mask, min_area_px=64) -> tuple[int, int, int, int] | None
  - diff_pair_with_bbox(frame_a_path, frame_b_path, threshold=25, min_area_px=64)
      -> tuple[bbox, changed_pixel_count, mask] | (None, 0, empty_mask)
DOES NOT DO:
  - Does NOT classify what's in the bbox (that's quick_classifier's job)
  - Does NOT persist anything to disk (caller saves crops)
  - Does NOT call Qwen, Telegram, or any pipeline code
  - Does NOT resize to motion_detector's 160×120 (we keep native resolution so
    crops have detail for YOLO + human review)
CALLED BY: listener/motion_gate_pipeline.py (Phase.107)
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

# Default bbox padding (pixels on each side) — adds context for YOLO without
# diluting the motion signal.
DEFAULT_BBOX_PADDING_PX = 16


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
    padding_px: int = DEFAULT_BBOX_PADDING_PX,
) -> tuple[int, int, int, int] | None:
    """Find the largest connected region in the mask and return its bbox.

    Returns (x, y, w, h) of the largest connected component that meets
    min_area_px. Pads the bbox by padding_px on each side (clamped to image
    bounds). Returns None if no component meets the area threshold.

    Why "largest component" instead of "union of all components":
      - Option C1 (§11.37) wants one bbox per diff pair. The motion object
        is the largest connected changed region. Smaller blobs are noise
        (sensor glitches, JPEG artifacts).
      - If the diff has multiple motion objects, the LARGEST one is most
        likely the Reolink-detected motion. Smaller blobs can be ignored.

    Padding rationale: YOLO performs better with a little context around the
    object. 16 pixels (~1-2% of a 1920-wide frame) gives the bbox some
    breathing room without diluting the motion signal.
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

    # Pad and clamp to image bounds
    h_img, w_img = mask.shape
    x = max(0, x - padding_px)
    y = max(0, y - padding_px)
    w = min(w_img - x, w + 2 * padding_px)
    h = min(h_img - y, h + 2 * padding_px)

    return (x, y, w, h)


def diff_pair_with_bbox(
    frame_a_path: str,
    frame_b_path: str,
    threshold: int = DEFAULT_DIFF_THRESHOLD,
    min_area_px: int = DEFAULT_MIN_AREA_PX,
    padding_px: int = DEFAULT_BBOX_PADDING_PX,
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
    bbox = bbox_from_mask(mask, min_area_px=min_area_px, padding_px=padding_px)
    return bbox, changed_count, mask


def crop_frame_to_bbox(frame_path: str, bbox: tuple[int, int, int, int]) -> str | None:
    """Crop a frame file to the given bbox. Saves to a sibling _crop suffix.

    Returns the new file path, or None if loading/cropping fails.

    Naming: `<stem>_crop<x>_<y>_<w>x<h>.jpg` next to the source frame.
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
    out_path = src.with_name(f"{src.stem}_crop{x}_{y}_{w}x{h}.jpg")
    ok = cv2.imwrite(str(out_path), crop, [cv2.IMWRITE_JPEG_QUALITY, 90])
    if not ok:
        return None
    return str(out_path)
