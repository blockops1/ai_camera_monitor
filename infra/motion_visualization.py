"""
motion_visualization.py — Render the cumulative-pairwise-diff + bbox outline as a single composite image.

Phase.115 (§11.46.6, 2026-08-25): the function now takes in-memory
PIL.Image objects instead of disk paths. The composite output is STILL
written to disk because Telegram's Bot API requires a file path (or
URL/file_id) to attach a photo.

STATUS: stable
THREAD SAFETY: thread-safe (pure function on inputs + OpenCV/PIL I/O;
    no shared mutable state, no caches, no lock acquired).

INPUTS:
    - frames: list of 4 PIL.Image.Image at NATIVE resolution.
      Phase.115: previously frame_paths (4 disk paths from the gate).
      Now PIL images held in memory by the gate's verdict — no fs read
      on the hot path.
    - bbox_a: tuple | None — diff(frame_2, frame_3) bbox from the gate,
      in NATIVE frame coordinates. Used as the first green outline.
    - bbox_b: tuple | None — diff(frame_3, frame_4) bbox from the gate,
      in NATIVE frame coordinates. Used as the second green outline.
    - output_dir: directory to write composite.png into (defaults to
       the alert's output_dir passed via ctx).
    - output_filename: filename for the composite (default "composite.png").
    - diff_threshold: pixel intensity delta above which a pixel is
      "changed". Default 40.
    - min_blob_area: minimum connected-component area in NATIVE coords
      to keep. Default 500.

OUTPUTS:
    - Writes one PNG to disk at <output_dir>/<output_filename> (§11.88 lossless).
    - Returns the absolute path to the rendered PNG, or '' on failure.

PUBLIC API:
    def render_motion_composite(
        frames: list[PIL.Image.Image],
        bbox_a: tuple[int, int, int, int] | None = None,
        bbox_b: tuple[int, int, int, int] | None = None,
        output_dir: str | None = None,
        # §11.88 (2026-09-01) — PNG lossless, NOT JPEG. See render_motion_composite.
        output_filename: str = "composite.png",
        diff_threshold: int = 40,
        min_blob_area: int = 500,
    ) -> str:
        # Returns the path to the rendered PNG, or '' on any failure.

DOES NOT DO:
    - Does NOT classify vehicles (that's vehicle_identifier.identify_from_crops).
    - Does NOT compute motion detection — the gate already produced bbox_a + bbox_b.
    - Does NOT save crops — the gate already saved crop_a + crop_b.
    - Does NOT send Telegram (that's telegram_formatter.composite_telegram).
    - Does NOT draw Qwen / vision bboxes. Only the gate's diff bboxes are drawn.
    - Does NOT read frames from disk. All frames are in-memory PIL.Image.

CALLED BY:
    - listener.vehicle_event_pipeline._send_vehicle_messages (Phase.115)
    - telegram_formatter.composite_telegram.send_composite_alert (Phase.115)

CALLS INTO:
    - numpy, opencv-python (cv2), PIL.

RELATED:
    - listener.motion_gate_pipeline.GateVerdict — provides frames, bbox_a, bbox_b.
    - infra.frame_diff.diff_pair_with_bbox — the gate's diff function.

WHY THIS MODULE EXISTS (Phase.79, Note 2026-08-14)
  Note asked for "one image that shows the differences between the
  six images together with the static background image and the
  boxes that are generated, sent as a separate Telegram." The
  reference image he showed was a gatekeeper-camera alert —
  rendered as the cumulative pairwise differential union painted
  in red, on the
  median-of-burst background, with the per-frame bboxes drawn as
  green outlines on top. This module produces that visualization.

  Phase.115 (2026-08-25, Note):
    - Frame count changed 6 → 4 (gate captures 4 frames, not 6).
    - Coordinate system changed: was 1280x960 resized (legacy
      detect_motion()'s internal canvas); now NATIVE frame coords
      (the gate works at native resolution per §11.37).
    - bbox source changed: was read from MovingObject.bbox_per_frame
      (legacy 6-frame bboxes); now passed as bbox_a + bbox_b args
      (the gate's 2 diff bboxes).
    - Frame input changed: was 4 disk paths; now 4 in-memory PIL.Image.
      The composite is STILL written to disk for Telegram.
"""

from __future__ import annotations

import os

import cv2
import numpy as np
from PIL import Image

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Phase.115: number of frames the motion gate produces per vehicle alert.
N_FRAMES_EXPECTED = 4

# Default diff threshold for the visualization (stricter than the
# gate's MOTION_THRESHOLD=25 to filter foliage/lighting jitter from
# the visualization while keeping vehicle-scale motion).
DEFAULT_DIFF_THRESHOLD = 40

# Default minimum connected-component area (native-coord px) to keep.
# Drops small scene-noise (leaves, grass, camera-burned timestamp
# digits). Keeps vehicle-scale blobs. Phrased at native resolution
# (Phase.115 — was RESIZE_W/RESIZE_H coords in the legacy flow).
DEFAULT_MIN_BLOB_AREA = 500

# JPEG quality for output.
JPEG_QUALITY = 90

# Red overlay alpha (0..100). 55 ≈ 55% opacity red.
RED_ALPHA_PERCENT = 55

# Green bbox outline thickness (scales with original frame height).
BBOX_THICKNESS_DIVISOR = 600


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _pil_to_bgr(pil_image: Image.Image) -> np.ndarray:
    """Convert PIL.Image (RGB) to numpy BGR array for cv2."""
    rgb = np.asarray(pil_image.convert("RGB"))
    return cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)


def _project_bbox(
    bbox: tuple[int, int, int, int],
    W: int,
    H: int,
) -> tuple[int, int, int, int] | None:
    """Phase.115: legacy projection helper kept as identity (native-res bboxes).

    Pre-§11.46.6 bboxes lived in 1280x960 resized coords; this helper
    re-projected them back to native frame coords. Post-§11.46.6 the
    gate's bboxes are already at native resolution, so projection is
    identity. Kept as a back-compat alias for vehicle_alert.annotate_frame_bboxes.
    Returns None if the bbox is empty / inverse.
    """
    return _native_bbox_to_corners(bbox, W, H)


def _native_bbox_to_corners(
    bbox: tuple[int, int, int, int],
    W: int,
    H: int,
) -> tuple[int, int, int, int] | None:
    """Convert (x0, y0, w, h) bbox in native frame coords to clipped (x0, y0, x1, y1).

    Phase.115: the gate's diff bboxes are already in native coords,
    so no projection/scaling is needed. We just clip to image bounds.

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


def _median_background_from_frames(bgr_frames: list[np.ndarray]) -> np.ndarray | None:
    """Return the median-of-burst background at NATIVE resolution.

    Phase.115: no resize. The 4 gate frames are loaded at their
    native resolution (e.g., 2304x1296 for the gatekeeper-camera
    capture), and the per-pixel median is computed directly. A
    moving object occupies <half the
    frames at any given pixel, so its value gets pushed out by the
    static-scene pixels that dominate.

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

    Phase.115: for i in 1..(N-1): absdiff(frame[i], frame[i-1])
    → grayscale → threshold @ `threshold` → OR. Then drop connected
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
    frames: list,
    bbox_a: tuple[int, int, int, int] | None = None,
    bbox_b: tuple[int, int, int, int] | None = None,
    output_dir: str | None = None,
    # §11.88 (2026-09-01) — PNG lossless, NOT JPEG. Composite is 4-frame
    # median + diff overlay; lossless preserves bbox outlines precisely.
    output_filename: str = "composite.png",
    diff_threshold: int = DEFAULT_DIFF_THRESHOLD,
    min_blob_area: int = DEFAULT_MIN_BLOB_AREA,
) -> str:
    """Render the cumulative-pairwise-diff + bbox-outline composite.

    Layers (composited in order):
      1. Background — median of the 4 gate frames at NATIVE resolution.
      2. Red overlay — cumulative pairwise diff (3 diffs at native res)
         painted as a translucent red layer (~55% opacity) on the background.
      3. Green bbox outlines — gate's bbox_a + bbox_b in native coords,
         drawn as thin rectangles. Two outlines only (one per gate bbox).

    Phase.115 (2026-08-25):
      - Frame count: 4 (was 6)
      - Resolution: native (was 1280x960 resized)
      - Bbox source: bbox_a + bbox_b params (was moving_object.bbox_per_frame)
      - Frame input: list[PIL.Image.Image] (was 4 disk paths)

    The composite is ALWAYS written to disk — Telegram's Bot API
    requires a file path (or URL/file_id) to attach a photo. Disk
    write goes to output_dir/output_filename.

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

    # Convert PIL → BGR numpy once.
    try:
        bgr_frames = [_pil_to_bgr(f) for f in frames]
    except Exception:
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

    # Green bbox outlines (gate's diff bboxes, not invented).
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
    if output_dir is None:
        return ""  # caller must provide an output_dir for the composite
    output_path = os.path.abspath(os.path.join(output_dir, output_filename))
    os.makedirs(output_dir, exist_ok=True)

    # Write PNG (lossless; §11.88).
    # cv2.imwrite infers format from extension (.png).
    if not cv2.imwrite(output_path, out):
        return ""

    return output_path