"""
motion_visualization.py — Render the cumulative-pairwise-diff + bbox outline
as a single composite image.

Phase 6B.115 (V2 port): the function accepts in-memory PIL.Image frames
(instead of disk paths). The composite output is written to disk because
Telegram's Bot API requires a file path (or URL/file_id) to attach a photo.

STATUS: stable
THREAD SAFETY: thread-safe (pure function on inputs + OpenCV/PIL I/O;
    no shared mutable state, no caches, no lock acquired).

LAYER 1: median background — per-pixel median of the 4 gate frames at
    NATIVE resolution. A moving object occupies <half the frames at any
    pixel, so its value gets pushed out by the static-scene pixels.

LAYER 2: cumulative pairwise diff union — for i in 1..(N-1):
    absdiff(frame[i], frame[i-1]) → grayscale → threshold → OR.
    Produces a binary mask at native frame resolution.

LAYER 3: morphology + min_blob_area filter — connected-components
    analysis on the diff mask; drops components whose area is below
    min_blob_area (default 500 px at native resolution).

LAYER 4: red paint — the filtered diff mask is composited as a
    translucent red (~55% opacity) overlay on the median background.

LAYER 5: green bbox outlines — gate's bbox_a + bbox_b in native coords,
    drawn as thin green rectangles on top.

LAYER 6: PNG save — cv2.imwrite to <output_dir>/composite.png
    (lossless; no optimize=True, no resize, no quality param).

INPUTS:
    - frames: list[PIL.Image.Image] — 4 frames at NATIVE resolution.
    - bbox_a: tuple[int, int, int, int] | None — diff(frame_2, frame_3)
      bbox from the gate, in NATIVE frame coordinates. First green outline.
    - bbox_b: tuple[int, int, int, int] | None — diff(frame_3, frame_4)
      bbox from the gate, in NATIVE frame coordinates. Second green outline.
    - output_dir: str — directory to write composite.png into.
    - diff_threshold: int — pixel intensity delta above which a pixel is
      "changed". Default 40.
    - min_blob_area: int — minimum connected-component area in NATIVE coords
      to keep. Default 500.

OUTPUTS:
    - Writes one PNG to disk at <output_dir>/composite.png.
    - Returns the absolute path to the rendered PNG, or '' on failure.

PUBLIC API:
    def render_motion_composite(
        frames: list[PIL.Image.Image],
        bbox_a: tuple[int, int, int, int] | None = None,
        bbox_b: tuple[int, int, int, int] | None = None,
        output_dir: str = "",
        diff_threshold: int = 40,
        min_blob_area: int = 500,
    ) -> str:
        # Returns the path to the rendered PNG, or '' on any failure.
        # Example:
        #   from PIL import Image
        #   frames = [Image.new("RGB", (1920, 1080)) for _ in range(4)]
        #   path = render_motion_composite(frames, (100,100,50,50), (110,110,50,50), "data/alerts")
        #   # path == "data/alerts/composite.png"

DOES NOT DO:
    - Does NOT classify vehicles (that's quick_classifier).
    - Does NOT compute motion detection — the gate already produced bbox_a + bbox_b.
    - Does NOT save crops — the gate already saved crop_a + crop_b.
    - Does NOT send Telegram.
    - Does NOT draw Qwen / vision bboxes. Only the gate's diff bboxes are drawn.
    - Does NOT read frames from disk. All frames are in-memory PIL.Image.

CALLED BY:
    - infra.alert_artifacts.prepare_alert_artifacts (stage 2 of pipeline)

CALLS INTO:
    - numpy, opencv-python (cv2), PIL.

RELATED:
    - infra.gate.GateVerdict — provides frames, bbox_a, bbox_b.
    - infra.frame_diff.diff_pair_with_bbox — the gate's diff function.
"""

from __future__ import annotations

import os

import cv2
import numpy as np
from PIL import Image

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

N_FRAMES_EXPECTED = 4
DEFAULT_DIFF_THRESHOLD = 40
DEFAULT_MIN_BLOB_AREA = 500
RED_ALPHA_PERCENT = 55
BBOX_THICKNESS_DIVISOR = 600


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _pil_to_bgr(pil_image: Image.Image) -> np.ndarray:
    """Convert PIL.Image (RGB) to numpy BGR array for cv2."""
    rgb = np.asarray(pil_image.convert("RGB"))
    return cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)


def _native_bbox_to_corners(
    bbox: tuple[int, int, int, int],
    W: int,
    H: int,
) -> tuple[int, int, int, int] | None:
    """Convert (x0, y0, w, h) bbox in native frame coords to clipped (x0, y0, x1, y1).

    Returns None if the bbox is empty / inverse (zero or negative w/h).
    """
    x0, y0, w, h = bbox
    if w <= 0 or h <= 0:
        return None
    x1 = x0 + w
    y1 = y0 + h
    x0 = max(0, min(W - 1, x0))
    y0 = max(0, min(H - 1, y0))
    x1 = max(0, min(W - 1, x1))
    y1 = max(0, min(H - 1, y1))
    return (x0, y0, x1, y1)


def _median_background_from_frames(
    bgr_frames: list[np.ndarray],
) -> np.ndarray | None:
    """Return the median-of-burst background at NATIVE resolution.

    Returns None if frames have inconsistent shapes.
    """
    first_shape = bgr_frames[0].shape
    for f in bgr_frames[1:]:
        if f.shape != first_shape:
            return None
    median_frame: np.ndarray = np.median(
        np.stack(bgr_frames, axis=0), axis=0
    ).astype(np.uint8)
    return median_frame


def _cumulative_diff_mask_from_frames(
    bgr_frames: list[np.ndarray],
    threshold: int,
    min_blob_area: int,
) -> np.ndarray | None:
    """Cumulative pairwise diff at NATIVE resolution.

    For i in 1..(N-1): absdiff(frame[i], frame[i-1])
    -> grayscale -> threshold @ `threshold` -> OR. Then drop connected
    components whose area is below `min_blob_area`.

    Returns uint8 mask (0/255) at native frame resolution, or None
    if frames have inconsistent shapes.
    """
    first_shape = bgr_frames[0].shape
    H, W = first_shape[:2]
    for f in bgr_frames[1:]:
        if f.shape != first_shape:
            return None

    combined = np.zeros((H, W), dtype=np.uint8)
    for i in range(1, len(bgr_frames)):
        diff = cv2.absdiff(bgr_frames[i], bgr_frames[i - 1])
        diff_gray = cv2.cvtColor(diff, cv2.COLOR_BGR2GRAY)
        _, mask = cv2.threshold(diff_gray, threshold, 255, cv2.THRESH_BINARY)
        combined = cv2.bitwise_or(combined, mask)  # type: ignore[assignment]
    if min_blob_area > 0:
        n_labels, labels, stats, _ = cv2.connectedComponentsWithStats(
            combined, connectivity=8,
        )
        kept = np.zeros_like(combined)
        for lbl in range(1, n_labels):
            if int(stats[lbl, cv2.CC_STAT_AREA]) >= min_blob_area:
                kept[labels == lbl] = 255
        combined = kept
    return combined


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def render_motion_composite(
    frames: list[Image.Image],
    bbox_a: tuple[int, int, int, int] | None = None,
    bbox_b: tuple[int, int, int, int] | None = None,
    output_dir: str = "",
    diff_threshold: int = DEFAULT_DIFF_THRESHOLD,
    min_blob_area: int = DEFAULT_MIN_BLOB_AREA,
) -> str:
    """Render the cumulative-pairwise-diff + bbox-outline composite.

    Layers (composited in order):
      1. Background — median of the 4 gate frames at NATIVE resolution.
      2. Red overlay — cumulative pairwise diff painted as a translucent
         red layer (~55% opacity) on the background.
      3. Green bbox outlines — gate's bbox_a + bbox_b in native coords,
         drawn as thin rectangles. Two outlines only.

    The composite is ALWAYS written to disk — Telegram's Bot API
    requires a file path (or URL/file_id) to attach a photo.

    Failure modes (returns '' — caller skips Telegram):
      - Wrong number of frames (not 4).
      - Any frame unreadable.
      - Frames with inconsistent shapes.
      - Any write error.

    Returns the absolute path to the rendered PNG on success.
    """
    if len(frames) != N_FRAMES_EXPECTED:
        raise ValueError(
            f"render_motion_composite expects {N_FRAMES_EXPECTED} frames, got {len(frames)}"
        )

    # Convert PIL -> BGR numpy once.
    try:
        bgr_frames = [_pil_to_bgr(f) for f in frames]
    except ValueError:
        return ""

    # Compute background + diff at native resolution.
    background_full = _median_background_from_frames(bgr_frames)
    if background_full is None:
        return ""
    diff_mask_full = _cumulative_diff_mask_from_frames(
        bgr_frames, threshold=diff_threshold, min_blob_area=min_blob_area,
    )
    if diff_mask_full is None:
        return ""

    H_orig, W_orig = background_full.shape[:2]
    motion_mask = diff_mask_full > 0

    # Composite: red overlay on background.
    out = background_full.copy()
    red_layer = np.zeros_like(out)
    red_layer[:, :, 0] = 0
    red_layer[:, :, 1] = 0
    red_layer[:, :, 2] = 255  # BGR red
    alpha = RED_ALPHA_PERCENT / 100.0
    out[motion_mask] = (
        alpha * red_layer[motion_mask]
        + (1.0 - alpha) * out[motion_mask]
    ).astype(np.uint8)

    # Green bbox outlines (gate's diff bboxes).
    box_thickness = max(2, H_orig // BBOX_THICKNESS_DIVISOR)
    for bbox in (bbox_a, bbox_b):
        if bbox is None:
            continue
        corners = _native_bbox_to_corners(bbox, W_orig, H_orig)
        if corners is None:
            continue
        px0, py0, px1, py1 = corners
        cv2.rectangle(
            out,
            (px0, py0), (px1, py1),
            (0, 255, 0),  # BGR green
            box_thickness,
        )

    # Resolve output path. Telegram Bot API needs a file path.
    if not output_dir:
        return ""
    output_path = os.path.abspath(os.path.join(output_dir, "composite.png"))
    os.makedirs(output_dir, exist_ok=True)

    # Write PNG (lossless; no optimize, no resize, no quality).
    if not cv2.imwrite(output_path, out):
        return ""

    return output_path
