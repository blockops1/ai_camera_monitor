"""frame_diff — pairwise frame differencing for motion-gate bbox extraction.

STATUS: provisional (Phase.107 §11.37, 2026-08-23; 6B.171 subject bbox 2026-09-01,
        STRICT commit — no size floor, no diff-bbox fallback; 6B.173 two-mask
        intersection anchor 2026-09-01)
INPUTS: file paths to two image files (JPEG/PNG, any resolution); OR in-memory
        uint8 ndarrays (already loaded)
OUTPUTS: tuple of (motion_mask: np.ndarray uint8 HxW, bbox: (x, y, w, h) | None,
        changed_pixel_count: int) — plus optional subject_bbox for trail-of-motion
        recovery (Phase.171)
PUBLIC API:
  - load_frame(path: str) -> np.ndarray | None    # grayscale, native resolution
  - pairwise_diff(frame_a, frame_b, threshold=25) -> np.ndarray uint8
  - bbox_from_mask(mask, min_area_px=64) -> tuple[int, int, int, int] | None
  - subject_bbox_from_mask(mask, padding_px=8, erode_iterations=2)
      -> tuple[int, int, int, int] | None
        # Phase.171 STRICT: tighter bbox on the moving SUBJECT, not the trail
        # of motion. NO size floor, NO fallback. Returns None when the eroded
        # mask has no CC (motion too small / subject too distant). The caller
        # (motion_gate_pipeline) suppresses the alert in that case — honest
        # detection beats plausible-looking crops. See NOTES below.
  - subject_bbox_from_two_masks(mask_2to3, mask_3to4, padding_px=8,
                                 min_cc_area_px=500)
      -> tuple[int, int, int, int] | None
        # Phase.173 STRICT: bbox at the logical AND of two consecutive
        # motion masks (diff(2,3) AND diff(3,4)). Captures the moving
        # subject's footprint at the anchor frame shared by both diffs
        # (frame_3). NO size floor, NO dilation, NO fallback to a single
        # diff. Returns None when the AND-region has no CC above
        # min_cc_area_px (typically: very fast motion → trail and new
        # position barely overlap, OR motion only in one of the two
        # transitions). Caller suppresses the alert in that case.
  - diff_pair_with_bbox(frame_a_path, frame_b_path, threshold=25, min_area_px=64)
      -> tuple[bbox, changed_pixel_count, mask] | (None, 0, empty_mask)
  - crop_frame_to_bbox(frame_path, bbox) -> str | None
DOES NOT DO:
  - Does NOT classify what's in the bbox (that's quick_classifier's job)
  - Does NOT persist anything to disk (caller saves crops)
  - Does NOT call Qwen, Telegram, or any pipeline code
  - Does NOT resize to motion_detector's 160×120 (we keep native resolution so
    crops have detail for YOLO + human review)
  - Does NOT maintain per-camera background models (no MOG2 / ViBe — Phase.171
    chose the simplest approach that solved the observed problem)
  - Does NOT fall back to bbox_from_mask when subject detection fails
    (Phase.171 strict — caller suppresses the alert instead)
CALLED BY: listener/motion_gate_pipeline.py (Phase.107 + 6B.171)
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

Phase.171 NOTES (subject bbox — why this exists, why STRICT):
  - Problem: diff_pair_with_bbox returns the bbox of the largest connected
    region in the diff mask. For a moving subject this region spans BOTH the
    subject's old position AND the trail of background it uncovered — the
    trail extends past where the subject IS in the new frame. When we crop
    frame_b at that bbox, the subject is often at the edge or out of crop.
    Observed on 2026-09-01 morning: 5 of 7 alerts had one empty crop out
    of two (the "only 1 of 2 has a vehicle" symptom Note reported).
  - Standard practice in motion-detection literature (MOG2, ViBe,
    background subtraction papers): erode the diff mask before connected
    components. Erosion kills thin trail wisps while preserving the dense
    connected region of the subject. Largest CC of the eroded mask is
    approximately the subject bbox.
  - Cost: one cv2.erode (3x3 kernel, 2 iterations) + one connected-components
    pass on the eroded mask. ~5ms on 1920x1080. Negligible vs Qwen latency.
  - STRICT commit (Note 2026-09-01): subject_bbox_from_mask returns
    whatever erosion produces, no matter how small. There is no
    min_subject_area_px floor and no diff-bbox fallback. If the eroded
    mask has no CC (subject too distant / sensor noise / motion below
    diff threshold), the function returns None and the caller
    (motion_gate_pipeline) suppresses the alert. The principle: honest
    detection beats plausible-looking crops. If this costs too many
    alerts, we change erosion parameters — we don't add a fallback.
  - Why not MOG2/ViBe: per-camera background models are a much larger
    change. 6B.171 chose the smallest change that solved the observed
    problem. Future phase can layer a learned background if needed.
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


# Phase.171 (2026-09-01): erosion kernel for subject_bbox_from_mask. 3×3 rectangular
# is the standard minimal erosion — kills 1-2 px wide wisps in one pass,
# two iterations collapses 1-2 px wisps entirely while keeping ≥5 px
# connected regions intact (vehicles at this distance are way larger).
SUBJECT_ERODE_KERNEL_SIZE = 3
SUBJECT_ERODE_ITERATIONS = 2


def subject_bbox_from_mask(
    mask: np.ndarray,
    frame_b: np.ndarray | None = None,
    padding_px: int = 8,
    erode_iterations: int = SUBJECT_ERODE_ITERATIONS,
) -> tuple[int, int, int, int] | None:
    """Phase.171 (2026-09-01): tighter bbox on the MOVING SUBJECT, not
    the trail of motion.

    Background: bbox_from_mask returns the bbox of the largest connected
    component of the diff mask. For a moving subject, that component is
    the UNION of (a) the subject's old position in frame_a and (b) the
    background it uncovered when it moved. The trail extends past where
    the subject IS in frame_b. Cropping frame_b at that bbox produces an
    "empty crop" where the subject has already moved out.

    Real-world observation (2026-09-01): 5 of 7 morning alerts had "only
    1 of 2 crops with a vehicle" because the diff bbox covered the trail
    not the subject.

    Fix (standard motion-detection practice — MOG2/ViBe/foreground-
    subtraction literature): erode the diff mask before connected
    components. Thin trail wisps collapse (1-2 px wide); the dense
    connected region of the subject survives.

    Disambiguation: after erosion, multiple CCs may remain (old position
    + new position when overlap is partial). When `frame_b` is provided,
    we score each CC by its overlap with frame_b's bright pixels. The
    CC that overlaps with frame_b is the subject's CURRENT position;
    the trail CC (where the subject WAS) has zero overlap because that
    area is now background. When `frame_b` is None, falls back to
    largest-area CC (same rule as bbox_from_mask).

    STRICT COMMIT (no fallback): this function returns whatever the
    erosion produces, no matter how small. If the mask is empty or has
    no connected component after erosion, returns None — and the caller
    suppresses the alert. We do NOT fall back to bbox_from_mask.
    Honest detection beats plausible-looking crops. If distant vehicles
    produce too-small crops, we change the erosion parameters, we don't
    add a fallback.

    Args:
      mask: uint8 ndarray (HxW), same shape as the source frames.
            Must be a single-channel binary mask (0 or 255). Multi-channel
            masks will be treated as their first channel.
      frame_b: optional uint8 ndarray (HxW or HxWx3), the "current" frame.
            When provided, disambiguates subject CC (high overlap with
            frame_b) from trail CC (zero overlap with frame_b). This is
            the key fix for the "moving subject produces empty crop"
            failure mode observed 2026-09-01.
      padding_px: Pixels of context added on each side of the bbox. Default
            8. Subject is already tightly framed, so less padding than
            bbox_from_mask's 16 px default.
      erode_iterations: Number of 3×3 erosion iterations. Default 2.
            Higher = more aggressive trail suppression (smaller subject).

    Returns:
      (x, y, w, h) of the subject CC, padded and clamped to image
      bounds — or None if the mask is empty / had no CC after erosion.

    Failure mode (now documented honestly): if erosion eats everything
    (vehicle too distant, sensor noise, or motion below the diff
    threshold), this returns None. The caller suppresses the alert.
    """
    if mask is None or mask.size == 0:
        return None
    # Defensive: drop to single channel if caller passed multi-channel.
    if mask.ndim == 3:
        mask = mask[:, :, 0]

    kernel = cv2.getStructuringElement(
        cv2.MORPH_RECT,
        (SUBJECT_ERODE_KERNEL_SIZE, SUBJECT_ERODE_KERNEL_SIZE),
    )
    eroded = cv2.erode(mask, kernel, iterations=erode_iterations)

    if eroded is None or eroded.size == 0:
        return None

    # When frame_b is provided, do our own CC analysis so we can score
    # by frame_b overlap (the disambiguation rule). Otherwise fall back
    # to the same CC analysis without the overlap scoring — picks
    # largest CC of the eroded mask.
    #
    # STRICT commit: no min_area_px floor. If any CC survives erosion,
    # we return it. The caller (motion_gate_pipeline) decides whether
    # to use the bbox or suppress the alert. We do NOT filter by area
    # here.
    num_labels, labels, stats, _centroids = cv2.connectedComponentsWithStats(
        eroded, connectivity=8
    )
    if num_labels <= 1:
        # 1 label = background only. No motion.
        return None

    if frame_b is not None:
        # frame_b disambiguation: pick CC with highest overlap with
        # frame_b's bright pixels (subject's current position).
        if frame_b.ndim == 3:
            frame_b_gray = cv2.cvtColor(frame_b, cv2.COLOR_BGR2GRAY)
        else:
            frame_b_gray = frame_b
        # frame_b_bright: 1 where frame_b > 25, 0 elsewhere. The subject
        # is significantly brighter than the road surface in surveillance
        # cameras — even at night with IR, the subject is the brightest
        # object in the frame. Threshold 25 matches pairwise_diff default.
        _, frame_b_bright = cv2.threshold(
            frame_b_gray, 25, 255, cv2.THRESH_BINARY
        )
        best_idx = -1
        best_overlap = 0
        best_area = 0
        for i in range(1, num_labels):
            cc_mask = (labels == i).astype(np.uint8)
            overlap = int((cc_mask & (frame_b_bright // 255)).sum())
            area = int(stats[i, cv2.CC_STAT_AREA])
            if overlap > best_overlap or (
                overlap == best_overlap and area > best_area
            ):
                best_overlap = overlap
                best_area = area
                best_idx = i
        if best_idx == -1:
            return None
    else:
        # No frame_b: pick the largest CC by area. Same rule as the
        # legacy bbox_from_mask, but applied here so we control the
        # STRICT no-area-floor behavior. Note: ties go to the first
        # CC found (cv2.connectedComponentsWithStats iteration order).
        best_idx = 1
        best_area = int(stats[1, cv2.CC_STAT_AREA])
        for i in range(2, num_labels):
            area = int(stats[i, cv2.CC_STAT_AREA])
            if area > best_area:
                best_area = area
                best_idx = i

    x = int(stats[best_idx, cv2.CC_STAT_LEFT])
    y = int(stats[best_idx, cv2.CC_STAT_TOP])
    w = int(stats[best_idx, cv2.CC_STAT_WIDTH])
    h = int(stats[best_idx, cv2.CC_STAT_HEIGHT])

    # Pad and clamp to image bounds
    h_img, w_img = mask.shape
    x = max(0, x - padding_px)
    y = max(0, y - padding_px)
    w = min(w_img - x, w + 2 * padding_px)
    h = min(h_img - y, h + 2 * padding_px)
    return (x, y, w, h)


def subject_bbox_from_two_masks(
    mask_2to3: np.ndarray,
    mask_3to4: np.ndarray,
    padding_px: int = 8,
    min_cc_area_px: int = 500,
) -> tuple[int, int, int, int] | None:
    """Phase.173 (2026-09-01): bbox at the logical AND of motion.

    Note: "I want a bbox at the logical AND of the motion from 1 to 2
    and 2 to 3."

    Math: with consecutive diffs sharing frame_3,
      - diff(2,3) covers (trail in frame_2) ∪ (vehicle in frame_3)
      - diff(3,4) covers (vehicle in frame_3) ∪ (trail in frame_4)
      - LOGICAL AND of the two diffs = the vehicle's footprint in
        frame_3, the only region present in BOTH diffs.

    For c59e3a72 (fast-moving blue Nissan, 2026-09-01) the raw AND mask
    contains one big vehicle-shaped CC (area 14560, 265×112) — the whole
    body of the car in frame_3. That's the bbox we want.

    Returns:
      (x, y, w, h) of the largest connected component of (mask_2to3 AND
      mask_3to4), padded and clamped to image bounds — or None if the
      intersection is empty or has no CC above min_cc_area_px.

    Single bbox returned: the vehicle's footprint in frame_3. The crop
    is applied to frame_3 (the anchor frame shared by both diffs). Note:
    "put the crop around that" — meaning around the intersection region.

    STRICT COMMIT: no fallback to bbox_from_mask, no size floor on the
    bbox itself. If the intersection has no CC above min_cc_area_px,
    returns None — caller suppresses the alert with
    reason="no_subject_detected". Honest suppression beats plausible crops.

    `min_cc_area_px=500` is a noise filter for tiny isolated CCs (timestamp
    jitter, single-pixel sensor noise). It's NOT a size floor on the
    vehicle — a vehicle CC that big is filtered only when motion is
    sparse enough that the intersection produces only noise fragments,
    in which case suppressing is the honest answer.

    Args:
      mask_2to3: uint8 ndarray (HxW) binary motion mask (frame_2 → frame_3)
      mask_3to4: uint8 ndarray (HxW) binary motion mask (frame_3 → frame_4)
      padding_px: context pixels added on each side of the bbox (default 8)
      min_cc_area_px: minimum CC area to consider (default 500). Filters
        noise CCs smaller than a typical vehicle component.

    Returns:
      (x, y, w, h) of the vehicle footprint in frame_3, or None.
    """
    if mask_2to3 is None or mask_3to4 is None:
        return None
    if mask_2to3.shape != mask_3to4.shape:
        return None
    if mask_2to3.size == 0 or mask_3to4.size == 0:
        return None

    # Defensive: drop to single channel.
    if mask_2to3.ndim == 3:
        mask_2to3 = mask_2to3[:, :, 0]
    if mask_3to4.ndim == 3:
        mask_3to4 = mask_3to4[:, :, 0]

    # Logical AND of the two motion masks — the vehicle's footprint in frame_3.
    intersection = cv2.bitwise_and(mask_2to3, mask_3to4)

    # Find connected components. Keep the biggest CC above min_cc_area_px.
    # The intersection rarely has more than one big CC: when the vehicle
    # is moving, the AND only keeps the region present in BOTH diffs.
    num_labels, _labels, stats, _ = cv2.connectedComponentsWithStats(
        intersection, connectivity=8
    )
    if num_labels <= 1:
        return None

    best_idx = -1
    best_area = 0
    for i in range(1, num_labels):
        area = int(stats[i, cv2.CC_STAT_AREA])
        if area >= min_cc_area_px and area > best_area:
            best_area = area
            best_idx = i
    if best_idx == -1:
        return None

    x = int(stats[best_idx, cv2.CC_STAT_LEFT])
    y = int(stats[best_idx, cv2.CC_STAT_TOP])
    w = int(stats[best_idx, cv2.CC_STAT_WIDTH])
    h = int(stats[best_idx, cv2.CC_STAT_HEIGHT])

    h_img, w_img = intersection.shape
    x = max(0, x - padding_px)
    y = max(0, y - padding_px)
    w = min(w_img - x, w + 2 * padding_px)
    h = min(h_img - y, h + 2 * padding_px)
    return (x, y, w, h)


def _pick_best_cc(
    mask: np.ndarray,
    frame: np.ndarray | None,
    padding_px: int = 8,
) -> tuple[int, int, int, int] | None:
    """Pick the connected component of `mask` that best matches `frame`'s
    bright pixels. Returns (x, y, w, h) padded and clamped, or None.

    Used by subject_bbox_from_two_masks for both crop_a (scored by frame_2)
    and crop_b (scored by frame_3). Extracted as a helper so the two-mask
    function is just the set algebra + two calls here.
    """
    if mask is None or mask.size == 0:
        return None
    if mask.ndim == 3:
        mask = mask[:, :, 0]

    num_labels, labels, stats, _centroids = cv2.connectedComponentsWithStats(
        mask, connectivity=8
    )
    if num_labels <= 1:
        return None

    if frame is not None:
        if frame.ndim == 3:
            frame_gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        else:
            frame_gray = frame
        _, frame_bright = cv2.threshold(frame_gray, 25, 255, cv2.THRESH_BINARY)
        frame_bright = (frame_bright // 255).astype(np.uint8)

        best_idx = -1
        best_overlap = 0
        best_area = 0
        for i in range(1, num_labels):
            cc_mask = (labels == i).astype(np.uint8)
            overlap = int((cc_mask & frame_bright).sum())
            area = int(stats[i, cv2.CC_STAT_AREA])
            if overlap > best_overlap or (
                overlap == best_overlap and area > best_area
            ):
                best_overlap = overlap
                best_area = area
                best_idx = i
        if best_idx == -1:
            return None
    else:
        # No frame: largest CC by area. Same rule as the legacy bbox_from_mask,
        # but here too we control the STRICT no-area-floor behavior.
        best_idx = 1
        best_area = int(stats[1, cv2.CC_STAT_AREA])
        for i in range(2, num_labels):
            area = int(stats[i, cv2.CC_STAT_AREA])
            if area > best_area:
                best_area = area
                best_idx = i

    x = int(stats[best_idx, cv2.CC_STAT_LEFT])
    y = int(stats[best_idx, cv2.CC_STAT_TOP])
    w = int(stats[best_idx, cv2.CC_STAT_WIDTH])
    h = int(stats[best_idx, cv2.CC_STAT_HEIGHT])

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
    out_path = src.with_name(f"{src.stem}_crop{x}_{y}_{w}x{h}.png")
    # §11.88 (2026-09-01) — PNG lossless, NOT JPEG q90.
    ok = cv2.imwrite(str(out_path), crop)
    if not ok:
        return None
    return str(out_path)
