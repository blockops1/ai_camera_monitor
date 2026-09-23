"""frame_diff — pairwise frame differencing for motion-gate bbox extraction.

STATUS: provisional (Phase 6B.107 §11.37, 2026-08-23; 6B.171 subject bbox 2026-09-01,
        STRICT commit — no size floor, no diff-bbox fallback; 6B.173 two-mask
        intersection anchor 2026-09-01; V2-071 pixel-wise AND + dilate + CC 2026-09-22)
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
  - compute_diff_bbox(frame_a_path, frame_b_path, threshold=25, min_area=64)
      -> (x, y, w, h) — convenience wrapper, never returns None
  - crop_frame_to_bbox(frame_path, bbox) -> str | None
  - bbox_from_mask_with_intersection(mask_prior, mask_curr, ...)
      -> tuple[int,int,int,int] | None  # cross-diff intersection anchor
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
CALLS INTO: nothing in this repo (pure numpy + PIL)
RELATED: infra/motion_detector.py does its OWN pairwise diff at 160×120 for
  its 6-frame trajectory tracking — that module is unchanged. frame_diff is a
  focused helper for the gate's 2-frame diff (Option C1 in §11.37).

Implementation notes:
  - Native-resolution grayscale (not resized). Reason: the bbox we return is
    used to crop from the ORIGINAL frames at their original size, then we
    letterbox for YOLO at 640×640 inside quick_classifier. Keeping native
    resolution in frame_diff means the bbox coordinates map directly to the
    frame's pixels — no scale math.
  - np.abs diff is fast (~10ms on 1920×1080 grayscale on Apple Silicon CPU).
  - Threshold 25 is empirically reasonable for daytime (headlight flare at
    night may need lower; future tuning per-camera).
  - Connected components via pure-numpy union-find (8-connectivity) — gets
    labels + areas; bboxes derived from label mask.
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

import os
from pathlib import Path

import numpy as np
from PIL import Image

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

# Default dilation percentage for pixel-wise intersection of two consecutive
# diff masks.  Applied BEFORE connected-components on the AND result.
# 1% = 0.01 of the smaller frame dimension (e.g. ~13 px on 1296-height
# frames).  Tunable via env var BBOX_INTERSECTION_DILATE_PCT.
DEFAULT_INTERSECTION_DILATE_PCT = 0.01


def _get_intersection_dilate_pct() -> float:
    """Read BBOX_INTERSECTION_DILATE_PCT at call time.

    Reading at call time (not import time) means operator changes to the
    env var take effect on the next test run / next listener start without
    needing a Python ``importlib.reload()``. Cached module-level constants
    on env vars also leak state across pytest runs when a test mutates
    the env var and reloads the module.
    """
    return float(
        os.environ.get("BBOX_INTERSECTION_DILATE_PCT", DEFAULT_INTERSECTION_DILATE_PCT)
    )


def _connected_components(
    mask: np.ndarray,
) -> tuple[int, np.ndarray]:
    """Label connected components in a binary mask (8-connectivity).

    Uses a union-find (disjoint-set) based approach.  Returns
    ``(num_labels, labels_array)`` where label 0 is background.
    """
    h, w = mask.shape
    labels = np.zeros((h, w), dtype=np.int32)
    # Union-find with path compression + union-by-rank
    # We need indices 0..max_uid where max_uid <= h*w.
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
            # Look at already-labeled neighbors (left, top, top-left, top-right)
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


def _dilate_mask(
    mask: np.ndarray,
    px: int,
) -> np.ndarray:
    """Dilate a binary uint8 mask by *px* pixels in all directions.

    Uses a rolling-sum approach: for each pixel in the structuring element
    (square of size ``2*px+1``), roll the mask and take the element-wise
    maximum.  This is equivalent to a morphological dilation with a
    square structuring element.

    Returns a uint8 ndarray (0 or 255).
    """
    if px <= 0:
        return mask.copy()
    dilated = np.zeros_like(mask)
    for ky in range(-px, px + 1):
        for kx in range(-px, px + 1):
            shifted = np.roll(mask, (ky, kx), axis=(0, 1))
            dilated = np.maximum(dilated, shifted)
    return dilated


def load_frame(path: str) -> np.ndarray | None:
    """Load an image file as grayscale at native resolution.

    Returns None if the file can't be read.

    Native resolution (not resized) because the bbox we extract from this
    frame needs to map directly to the frame's pixel coordinates for cropping.
    """
    try:
        pil_img = Image.open(path).convert("L")
    except OSError:
        return None
    return np.array(pil_img, dtype=np.uint8)


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
    diff = np.abs(frame_a.astype(np.int16) - frame_b.astype(np.int16))
    mask = (diff >= threshold).astype(np.uint8) * 255
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
    # Pure-numpy connected-components (8-connectivity).
    # Returns (num_labels, label_array) where label 0 = background.
    num_labels, labels = _connected_components(mask)
    if num_labels < 1:
        # No components at all. No motion.
        return None

    # Find the largest component (excluding background at index 0)
    largest_idx = 1
    largest_area = 0
    for i in range(1, num_labels + 1):
        area = int(np.sum(labels == i))
        if area > largest_area:
            largest_area = area
            largest_idx = i

    if largest_area < min_area_px:
        return None

    # Compute bbox from the label mask
    rows = np.where(labels == largest_idx)[0]
    cols = np.where(labels == largest_idx)[1]
    y, x = int(rows.min()), int(cols.min())
    h, w = int(rows.max() - rows.min() + 1), int(cols.max() - cols.min() + 1)

    # Expand by percentage, round UP to mult of 32, clamp to image bounds.
    h_img, w_img = mask.shape
    return _expand_bbox_pct((x, y, w, h), pad_pct, w_img, h_img)


def _compute_iou(
    a: tuple[int, int, int, int],
    b: tuple[int, int, int, int],
) -> float:
    """Standard intersection-over-union for two (x, y, w, h) bboxes.

    Returns 0.0 for non-overlapping bboxes.
    """
    x1, y1, w1, h1 = a
    x2, y2, w2, h2 = b

    # Intersection box
    ix1 = max(x1, x2)
    iy1 = max(y1, y2)
    ix2 = min(x1 + w1, x2 + w2)
    iy2 = min(y1 + h1, y2 + h2)

    inter_w = max(0, ix2 - ix1)
    inter_h = max(0, iy2 - iy1)
    intersection = inter_w * inter_h

    area1 = w1 * h1
    area2 = w2 * h2
    union = area1 + area2 - intersection

    if union == 0:
        return 0.0
    return intersection / union


def _labels_to_bboxes(
    num_labels: int,
    labels: np.ndarray,
    min_area_px: int,
) -> list[tuple[int, int, int, int]]:
    """Extract bboxes for all connected components meeting min_area_px.

    Returns list of (x, y, w, h) bboxes in label order (excluding background).
    """
    bboxes = []
    for i in range(1, num_labels + 1):
        area = int(np.sum(labels == i))
        if area < min_area_px:
            continue
        rows = np.where(labels == i)[0]
        cols = np.where(labels == i)[1]
        x = int(cols.min())
        y = int(rows.min())
        w = int(cols.max() - cols.min() + 1)
        h = int(rows.max() - rows.min() + 1)
        bboxes.append((x, y, w, h))
    return bboxes


def bbox_from_mask_with_intersection(
    mask_prior: np.ndarray | None,
    mask_curr: np.ndarray | None,
    min_area_px: int = DEFAULT_MIN_AREA_PX,
    pad_pct: float = DEFAULT_BBOX_PAD_PCT,
    dilate_pct: float | None = None,
) -> tuple[int, int, int, int] | None:
    """Find the bbox anchored to the current frame by pixel-wise AND + dilate.

    Computes the pixel-wise AND of two consecutive diff masks, dilates the
    result by 1% of the smaller frame dimension (configurable via
    ``dilate_pct`` or env var ``BBOX_INTERSECTION_DILATE_PCT``), then takes
    the largest connected component and bboxes it.

    The caller is expected to pass ``diff(1,2)`` as ``mask_prior`` and
    ``diff(2,3)`` as ``mask_curr``.  The AND result captures regions that
    changed in BOTH consecutive time windows — i.e. the subject's persistent
    motion footprint.

    Uses ``_expand_bbox_pct`` for padding and round-mult-32.

    Returns (x, y, w, h) of the largest connected component's expanded bbox,
    or ``None`` if no component meets the area threshold.
    """
    if mask_prior is None or mask_prior.size == 0 or mask_curr is None or mask_curr.size == 0:
        return None

    # Dilate percentage: caller override, then env var (read at call time), then default.
    if dilate_pct is None:
        dilate_pct = _get_intersection_dilate_pct()

    # Step 1: pixel-wise AND
    intersection = ((mask_prior > 0) & (mask_curr > 0)).astype(np.uint8) * 255

    # Step 2: dilate
    h, w = mask_curr.shape
    min_dim = min(h, w)
    dilate_px = round(dilate_pct * min_dim)
    intersection = _dilate_mask(intersection, dilate_px)

    # Step 3: connected components, rank by intensity sum (not area).
    #
    # A vehicle that is driving onto the property and stopping produces a
    # compact region of HIGH pixel-difference values (bright, concentrated
    # motion) across both consecutive diff pairs.  A diffuse foliage / tree
    # canopy region produces many more changed pixels but each one is only
    # weakly different (low per-pixel intensity).  Ranking by area on the
    # AND mask therefore rewards the diffuse noise and hides the vehicle —
    # see US-073a regression analysis (truck bbox (403,497) 598x287 had
    # 30.6 M intensity vs canopy's 13.8 M at 138k area).  Summing the raw
    # uint8 intensity (np.sum of the mask values, not np.count_nonzero)
    # across the AND mask gives us the total "brightness" of the
    # persistent-motion region and correctly surfaces bright, compact
    # vehicles above diffuse foliage noise.
    num_labels, labels = _connected_components(intersection)
    if num_labels < 1:
        return None

    best_intensity = 0
    best_idx = 1
    for i in range(1, num_labels + 1):
        comp_mask = (labels == i)
        intensity = int(np.sum(mask_prior[comp_mask]))
        if intensity > best_intensity:
            best_intensity = intensity
            best_idx = i

    best_area = int(np.sum(labels == best_idx))
    if best_area < min_area_px:
        return None

    rows = np.where(labels == best_idx)[0]
    cols = np.where(labels == best_idx)[1]
    y_cc = int(rows.min())
    x_cc = int(cols.min())
    h_cc = int(rows.max() - rows.min() + 1)
    w_cc = int(cols.max() - cols.min() + 1)

    # Step 4: expand and clamp
    h_img, w_img = mask_curr.shape
    return _expand_bbox_pct((x_cc, y_cc, w_cc, h_cc), pad_pct, w_img, h_img)


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


def compute_diff_bbox(
    frame_a_path: str,
    frame_b_path: str,
    threshold: int = DEFAULT_DIFF_THRESHOLD,
    min_area: int = DEFAULT_MIN_AREA_PX,
) -> tuple[int, int, int, int]:
    """Convenience wrapper: load 2 frames, diff, return raw bbox (x, y, w, h).

    Always returns a tuple (never None).  When no motion is detected the
    bbox defaults to ``(0, 0, 0, 0)``.

    Returns the unexpanded bbox of the largest connected component
    (no padding applied).
    """
    a = load_frame(frame_a_path)
    b = load_frame(frame_b_path)
    if a is None or b is None:
        return (0, 0, 0, 0)
    if a.shape != b.shape:
        return (0, 0, 0, 0)
    mask = pairwise_diff(a, b, threshold=threshold)
    changed_count = int(np.count_nonzero(mask))
    if changed_count < min_area:
        return (0, 0, 0, 0)

    num_labels, labels = _connected_components(mask)
    if num_labels < 1:
        return (0, 0, 0, 0)

    largest_idx = 1
    largest_area = 0
    for i in range(1, num_labels + 1):
        area = int(np.sum(labels == i))
        if area > largest_area:
            largest_area = area
            largest_idx = i

    if largest_area < min_area:
        return (0, 0, 0, 0)

    rows = np.where(labels == largest_idx)[0]
    cols = np.where(labels == largest_idx)[1]
    return (int(cols.min()), int(rows.min()), int(cols.max() - cols.min() + 1), int(rows.max() - rows.min() + 1))


def crop_frame_to_bbox(frame_path: str, bbox: tuple[int, int, int, int]) -> str | None:
    """Crop a frame file to the given bbox. Saves to a sibling _crop suffix.

    Returns the new file path, or None if loading/cropping fails.

    Naming: `<stem>_crop<x>_<y>_<w>x<h>.png` next to the source frame.
    Saves to disk because YOLO loads from file paths.
    """
    src = Path(frame_path)
    if not src.is_file():
        return None
    try:
        pil_img = Image.open(str(src)).convert("RGB")
    except OSError:
        return None
    x, y, w, h = bbox
    # Clamp to image bounds
    w_img, h_img = pil_img.size
    x = max(0, min(x, w_img - 1))
    y = max(0, min(y, h_img - 1))
    w = max(1, min(w, w_img - x))
    h = max(1, min(h, h_img - y))
    crop = pil_img.crop((x, y, x + w, y + h))
    if crop.size == 0:
        return None
    out_path = src.with_name(f"{src.stem}_crop{x}_{y}_{w}x{h}.png")
    # §11.88 (2026-09-01) — PNG lossless, NOT JPEG q90.
    try:
        crop.save(str(out_path), format="PNG", optimize=False)
    except OSError:
        return None
    return str(out_path)


def compute_cross_diff_bboxes(
    frame_1_path: str,
    frame_2_path: str,
    frame_3_path: str,
    frame_4_path: str,
    threshold: int = DEFAULT_DIFF_THRESHOLD,
    min_area_px: int = DEFAULT_MIN_AREA_PX,
    pad_pct: float = DEFAULT_BBOX_PAD_PCT,
) -> tuple[tuple[int, int, int, int] | None, tuple[int, int, int, int] | None]:
    """Compute cross-diff bboxes for the motion gate pipeline.

    Loads 4 frames, computes diff(1,2), diff(2,3), diff(3,4) masks, then:
      - bbox_a = bbox_from_mask_with_intersection(mask_12, mask_23, ...)
        (anchored to frame_2 — the subject WAS here)
      - bbox_b = bbox_from_mask_with_intersection(mask_23, mask_34, ...)
        (anchored to frame_3 — the subject IS here)

    Either may be None if no component meets the area threshold.

    Returns (bbox_a, bbox_b).
    """
    f1 = load_frame(frame_1_path)
    f2 = load_frame(frame_2_path)
    f3 = load_frame(frame_3_path)
    f4 = load_frame(frame_4_path)

    # If any frame fails to load, return (None, None).
    if f1 is None or f2 is None or f3 is None or f4 is None:
        return None, None

    mask_12 = pairwise_diff(f1, f2, threshold=threshold)
    mask_23 = pairwise_diff(f2, f3, threshold=threshold)
    mask_34 = pairwise_diff(f3, f4, threshold=threshold)

    bbox_a = bbox_from_mask_with_intersection(
        mask_12, mask_23,
        min_area_px=min_area_px,
        pad_pct=pad_pct,
    )
    bbox_b = bbox_from_mask_with_intersection(
        mask_23, mask_34,
        min_area_px=min_area_px,
        pad_pct=pad_pct,
    )

    return bbox_a, bbox_b
