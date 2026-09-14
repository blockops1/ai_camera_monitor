"""
motion_visualization.py — Render the cumulative-pairwise-diff + bbox outline
as a single composite image.

Phase 6B.115 (V2 port): the function accepts in-memory PIL.Image frames
(instead of disk paths). The composite output is written to disk because
Telegram's Bot API requires a file path (or URL/file_id) to attach a photo.

STATUS: stable
THREAD SAFETY: thread-safe (pure function on inputs + PIL I/O;
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

LAYER 6: PNG save — PIL.Image.save to <output_dir>/composite.png
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
    - numpy, PIL.

RELATED:
    - infra.gate.GateVerdict — provides frames, bbox_a, bbox_b.
    - infra.frame_diff.diff_pair_with_bbox — the gate's diff function.
"""

from __future__ import annotations

import os

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
    """Convert PIL.Image (RGB) to numpy BGR array for compositing."""
    rgb = np.asarray(pil_image.convert("RGB"))
    return rgb[..., ::-1]  # RGB -> BGR


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
        # BGR absdiff via numpy
        diff = np.abs(bgr_frames[i].astype(np.int16) - bgr_frames[i - 1].astype(np.int16))
        # Convert to grayscale (mean of BGR channels)
        diff_gray = diff.mean(axis=2).astype(np.uint8)
        # Threshold
        mask = (diff_gray >= threshold).astype(np.uint8) * 255
        # OR with combined
        combined = np.bitwise_or(combined, mask)

    if min_blob_area > 0:
        # Connected components via union-find (pure numpy, matches frame_diff.py).
        n_labels, labels = _connected_components_with_areas(combined)
        kept = np.zeros_like(combined)
        for lbl in range(1, n_labels + 1):
            area = int(_component_area(labels, lbl))
            if area >= min_blob_area:
                kept[labels == lbl] = 255
        combined = kept
    return combined


def _connected_components_with_areas(
    mask: np.ndarray,
) -> tuple[int, np.ndarray]:
    """Label connected components in a binary mask (8-connectivity).

    Uses a union-find (disjoint-set) based approach.  Returns
    ``(num_labels, labels_array)`` where label 0 is background.
    Also returns areas in a dict {label: area}.
    """
    h, w = mask.shape
    labels = np.zeros((h, w), dtype=np.int32)
    max_uid = h * w + 1
    parent = np.arange(max_uid, dtype=np.int32)
    rank = np.zeros(max_uid, dtype=np.int8)

    def _find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def _union(a: int, b: int) -> None:
        ra, rb = _find(a), _find(b)
        if ra == rb:
            return
        if rank[ra] < rank[rb]:
            ra, rb = rb, ra
        parent[rb] = ra
        if rank[ra] == rank[rb]:
            rank[ra] += 1

    uid = 0
    for y in range(h):
        for x in range(w):
            if mask[y, x] == 0:
                continue
            uid += 1
            labels[y, x] = uid
            neighbors: list[int] = []
            if x > 0 and labels[y, x - 1] > 0:
                neighbors.append(int(labels[y, x - 1]))
            if y > 0 and labels[y - 1, x] > 0:
                neighbors.append(int(labels[y - 1, x]))
            if y > 0 and x > 0 and labels[y - 1, x - 1] > 0:
                neighbors.append(int(labels[y - 1, x - 1]))
            if y > 0 and x < w - 1 and labels[y - 1, x + 1] > 0:
                neighbors.append(int(labels[y - 1, x + 1]))
            for nb in neighbors:
                _union(uid, nb)

    # Second pass: relabel to consecutive integers
    root_map: dict[int, int] = {}
    next_label = 1
    for y in range(h):
        for x in range(w):
            if labels[y, x] > 0:
                r = _find(int(labels[y, x]))
                if r not in root_map:
                    root_map[r] = next_label
                    next_label += 1
                labels[y, x] = root_map[r]

    num_labels = next_label - 1
    return num_labels, labels


def _component_area(labels: np.ndarray, label: int) -> int:
    """Count pixels belonging to a given label."""
    return int(np.sum(labels == label))


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
        _draw_rectangle(out, px0, py0, px1, py1, box_thickness)

    # Resolve output path. Telegram Bot API needs a file path.
    if not output_dir:
        return ""
    output_path = os.path.abspath(os.path.join(output_dir, "composite.png"))
    os.makedirs(output_dir, exist_ok=True)

    # Write PNG (lossless; no optimize, no resize, no quality).
    try:
        rgb_out = out[..., ::-1]  # BGR -> RGB for PIL
        Image.fromarray(rgb_out, mode="RGB").save(output_path, format="PNG")
    except OSError:
        return ""

    return output_path


def _draw_rectangle(
    arr: np.ndarray,
    x0: int,
    y0: int,
    x1: int,
    y1: int,
    thickness: int,
) -> None:
    """Draw a filled rectangle outline on a BGR numpy array (in-place).

    Draws the four edges with the given thickness using green color.
    Defensive bounds-clamp: protects against callers that bypass
    _native_bbox_to_corners (e.g. integration tests, direct writes).
    """
    H, W = arr.shape[:2]

    # Early-exit if the unclipped rect is entirely outside the frame.
    if x1 < 0 or x0 >= W or y1 < 0 or y0 >= H:
        return

    # Defensive clamp — mirrors _native_bbox_to_corners logic.
    x0 = max(0, x0)
    x1 = min(W - 1, x1)
    y0 = max(0, y0)
    y1 = min(H - 1, y1)
    thickness = max(0, min(thickness, min(W, H)))

    if x0 > x1 or y0 > y1:
        return  # Clamp collapsed the rect; nothing to draw.

    green = np.array([0, 255, 0], dtype=arr.dtype)  # BGR green
    # Top edge
    for dy in range(thickness):
        arr[y0 + dy, x0:x1 + 1] = green
    # Bottom edge
    for dy in range(thickness):
        arr[y1 - dy, x0:x1 + 1] = green
    # Left edge
    for dx in range(thickness):
        arr[y0:y1 + 1, x0 + dx] = green
    # Right edge
    for dx in range(thickness):
        arr[y0:y1 + 1, x1 - dx] = green
