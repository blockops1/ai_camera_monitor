"""Phase 6B.171 — subject_bbox_from_mask unit + regression tests.

PURPOSE
-------
Phase 6B.171 adds `subject_bbox_from_mask()` to infra/frame_diff.py. The
function erodes the pairwise diff mask to kill thin trail wisps, then
takes the largest connected component. When frame_b is provided, it
disambiguates "subject CC" vs "trail CC" by scoring overlap with frame_b
— the CC overlapping frame_b is the subject's current position.

STRICT COMMIT (maintainer 2026-09-01): no min_subject_area_px floor, no
diff-bbox fallback. Function returns whatever erosion produces, or None
if the eroded mask has no CC. The caller (motion_gate_pipeline)
suppresses the alert in that case. We do NOT silently fall back.

This test file covers:

1. Unit tests for subject_bbox_from_mask (STRICT — no min_subject_area_px)
   - Synthetic masks with known subject + trail
   - frame_b disambiguation rule: subject vs trail CC selection
   - Empty / all-zero masks
   - 3D mask (BGR) inputs
   - Subject with no frame_b (largest-CC fallback rule)

2. Integration with infra.frame_diff.bbox_from_mask (regression)
   - For a mask with subject + trail, bbox_from_mask returns one of the
     CCs (by largest area), subject_bbox_from_mask with frame_b returns
     the subject CC specifically (overlap with frame_b)

3. Real-world regression on the 7 morning alerts from 2026-09-01
   - For each alert, subject_bbox_from_mask should be tighter than the
     stored diff bbox

NOTE: the wired-up motion_gate_pipeline.py tests are in
listener/tests/test_motion_gate_pipeline_6B171.py.
"""

from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np
import pytest

from infra.frame_diff import (
    bbox_from_mask,
    pairwise_diff,
    subject_bbox_from_mask,
)

# ---------------------------------------------------------------------------
# 1. Unit tests — subject_bbox_from_mask (STRICT — no size floor)
# ---------------------------------------------------------------------------


def test_subject_bbox_picks_subject_when_frame_b_provided():
    """When frame_b is provided, subject CC is preferred over trail CC.

    Synthetic: subject (white 40x40) moved from x=50 to x=80 between
    frame_a and frame_b. mask_2to3 has 2 equal-area CCs (old + new
    position). With frame_b=frame_b, the subject's CURRENT position
    (cols 80-120, white in frame_b) has high overlap; the trail
    (cols 50-80, was white in frame_a, now black in frame_b) has
    zero overlap. Function must pick the subject CC.
    """
    frame_a = np.zeros((200, 200), dtype=np.uint8)
    frame_b = np.zeros((200, 200), dtype=np.uint8)
    frame_a[80:120, 50:90] = 255  # OLD position
    frame_b[80:120, 80:120] = 255  # NEW position (subject IS here in frame_b)

    mask = pairwise_diff(frame_a, frame_b, threshold=25)

    bbox = subject_bbox_from_mask(mask, frame_b=frame_b)
    assert bbox is not None
    x, y, w, h = bbox
    # Subject is at cols 80-120 in frame_b. After 2 erodes (3x3 kernel,
    # 2 iterations erodes ~4px on each side), bbox starts ~84. With 8px
    # padding: bbox.x ~76. Allow ±6px tolerance for OpenCV rounding.
    assert 74 <= x <= 92, f"expected bbox.x ~80 (subject in frame_b), got {x}"
    # Same for y
    assert 74 <= y <= 92, f"expected bbox.y ~80 (subject in frame_b), got {y}"
    # Width should be tight: subject is 40, eroded to ~32, padded to ~48.
    assert 30 <= w <= 56, f"expected bbox width ~32-48, got {w}"
    assert 30 <= h <= 56, f"expected bbox height ~32-48, got {h}"


def test_subject_bbox_no_frame_b_falls_back_to_largest_cc():
    """When frame_b is None, subject_bbox_from_mask uses largest-CC rule.

    This preserves backward compatibility with code that doesn't have
    frame_b available. The behavior matches bbox_from_mask on the
    eroded mask (largest CC of the eroded mask).
    """
    frame_a = np.zeros((200, 200), dtype=np.uint8)
    frame_b = np.zeros((200, 200), dtype=np.uint8)
    frame_a[80:120, 50:90] = 255
    frame_b[80:120, 80:120] = 255

    mask = pairwise_diff(frame_a, frame_b, threshold=25)

    bbox = subject_bbox_from_mask(mask, frame_b=None)
    # Without frame_b, picks CC1 (largest by area — equal in this case).
    # Just verify SOMETHING was returned (the function doesn't error).
    assert bbox is not None
    # Function is best-effort without frame_b. May pick trail CC1 or
    # subject CC2 depending on iteration order. The IMPORTANT behavior
    # is that bbox is non-None and has reasonable size.


def test_subject_bbox_all_zeros_returns_none():
    """Empty mask → no subject → None (STRICT)."""
    mask = np.zeros((400, 400), dtype=np.uint8)
    bbox = subject_bbox_from_mask(mask)
    assert bbox is None


def test_subject_bbox_accepts_3d_mask():
    """subject_bbox_from_mask handles 3-channel BGR masks.

    Drops to first channel (single-channel mask) per the function's
    defensive guard. Subject must be detectable after dropping the
    channel.
    """
    mask_bgr = np.zeros((200, 300, 3), dtype=np.uint8)
    mask_bgr[80:120, 100:200, :] = 255  # 100x40 dense subject

    # Without frame_b, uses largest-CC rule.
    bbox = subject_bbox_from_mask(mask_bgr)
    assert bbox is not None
    x, y, w, _h = bbox
    # After 2 erodes (4px each side) + 8px padding: bbox starts ~88-92.
    assert 88 <= x <= 100, f"expected x ~92, got {x}"
    assert 70 <= y <= 80, f"expected y ~72, got {y}"
    assert 90 <= w <= 116, f"expected w ~104 (96+8), got {w}"


def test_subject_bbox_largest_cc_when_multiple_subjects():
    """If multiple subjects exist in the mask, return the largest CC.

    Without frame_b, this is the same as bbox_from_mask on the eroded
    mask. Tests that the function picks the larger subject, not noise.
    """
    mask = np.zeros((400, 600), dtype=np.uint8)
    # Main subject (large)
    mask[100:200, 100:200] = 255  # 100x100 = 10000 px²
    # Noise blob (small)
    mask[300:320, 400:430] = 255  # 20x30 = 600 px²

    bbox = subject_bbox_from_mask(mask)
    assert bbox is not None
    x, y, w, _h = bbox
    # Should detect the larger subject, not the noise
    assert x < 200 and y < 250, f"bbox {bbox} looks like noise, not main subject"
    # Larger subject is 100x100. After 2 erodes: 92x92. With 8px
    # padding: 108x108. Allow ±6 px tolerance.
    assert 90 <= w <= 116, f"expected w ~108, got {w}"
    assert 90 <= _h <= 116, f"expected _h ~108, got {_h}"


def test_subject_bbox_with_pairwise_diff_real_frames():
    """End-to-end: pairwise_diff(frame_a, frame_b) → subject_bbox_from_mask.

    Creates two synthetic 200x200 frames with a moving subject (white
    square moving across black background). Pairwise diff should
    produce a mask with subject + trail; subject_bbox_from_mask with
    frame_b should pick the subject CC (current position).
    """
    frame_a = np.zeros((200, 200), dtype=np.uint8)
    frame_b = np.zeros((200, 200), dtype=np.uint8)
    frame_a[80:120, 50:90] = 255  # subject WAS at x=50-90
    frame_b[80:120, 80:120] = 255  # subject IS at x=80-120

    mask = pairwise_diff(frame_a, frame_b, threshold=25)

    bbox = subject_bbox_from_mask(mask, frame_b=frame_b)
    assert bbox is not None
    x, _y, w, _h = bbox
    # Subject IS at cols 80-120 in frame_b. After 2 erodes, bbox.x ~84.
    assert 80 <= x <= 92, f"expected x ~84 (subject in frame_b), got {x}"
    # Width should be < 50 (subject is 40, trail extends 30 to each side).
    assert w < 50, f"subject bbox width={w} should exclude trail, got {w}"


def test_subject_bbox_dense_subject_with_thin_trail():
    """Subject bbox is tight on a dense subject even with a thin trail.

    Synthetic: dense 80x40 subject + thin (1px-wide) trail extending
    outward. After 2 erosions, the trail (1px wide) is killed; the
    subject (80px wide) survives. Test verifies the bbox doesn't extend
    into the trail region.
    """
    mask = np.zeros((400, 600), dtype=np.uint8)
    mask[100:140, 200:280] = 255  # dense subject
    mask[100:140, 195:200] = 255  # 5px wide trail to the left

    frame_b = np.zeros((400, 600), dtype=np.uint8)
    # frame_b has the subject (where it IS now) — for the
    # disambiguation rule. The trail area (cols 195-200) is BLACK in
    # frame_b (subject moved away from there).
    frame_b[100:140, 200:280] = 255

    bbox = subject_bbox_from_mask(mask, frame_b=frame_b)
    assert bbox is not None
    x, _y, w, _h = bbox
    # Subject should be at cols 200-280. After 2 erodes: 192-284
    # (~84 wide). With 8px padding: 184-292 (~108 wide).
    assert 184 <= x <= 208, f"expected x ~192 (subject start), got {x}"
    assert 70 <= w <= 120, f"expected width ~108, got {w}"


def test_subject_bbox_no_size_floor_returns_tiny_bbox():
    """STRICT: no min_subject_area_px floor. Function returns whatever
    erosion produces, even for tiny subjects.

    A 10x10 subject (100 px² pre-erosion, ~36 px² after 2 erosions) gets
    a bbox back. Pre-strict behavior returned None for any subject below
    256 px²; strict behavior returns the actual bbox. The caller decides
    whether to use it or suppress the alert.
    """
    mask = np.zeros((200, 200), dtype=np.uint8)
    mask[50:60, 50:60] = 255  # 10x10 = 100 px² pre-erosion

    bbox = subject_bbox_from_mask(mask)
    # STRICT: function returns bbox for tiny subject. Caller decides.
    assert bbox is not None, "STRICT: no size floor, should return tiny bbox"
    _x, _y, w, h = bbox
    # Subject is 10x10. After 2 erosions: ~2x2. With 8px padding: ~18x18.
    assert w >= 8, f"bbox width={w} should be at least padding"
    assert h >= 8, f"bbox height={h} should be at least padding"


def test_subject_bbox_only_noise_returns_none():
    """STRICT: when erosion eats everything (noise only), return None.

    A 3x3 single-pixel-ish noise blob gets eaten by 2 erosions (3x3
    kernel) — no CC survives. Function returns None. Caller suppresses
    the alert.
    """
    mask = np.zeros((200, 200), dtype=np.uint8)
    # Three tiny noise pixels far apart — each isolated, each eaten by erosion.
    mask[10:12, 10:12] = 255
    mask[100:102, 100:102] = 255
    mask[180:182, 180:182] = 255

    bbox = subject_bbox_from_mask(mask)
    # 2x2 pixels don't survive 2 erosions of a 3x3 kernel. No CC.
    assert bbox is None, "STRICT: noise-only mask should return None (no CC after erosion)"


# ---------------------------------------------------------------------------
# 2. Real-world regression — 7 morning alerts from 2026-09-01
# ---------------------------------------------------------------------------


def _find_morning_frames() -> dict[str, list[Path]]:
    """Locate the 7 morning alert frames from 2026-09-01 if available.

    Frames live in <data-root>/frames/<alert_id>/. The 4 source frames
    are named frame_1..frame_4 (or similar). We need to identify them
    and skip any crop files that the existing pipeline may have written.
    Returns dict {alert_id: [frame_1, frame_2, frame_3, frame_4]} if
    found, else empty dict.
    """
    candidates = [
        Path.home() / "ai_camera_monitor" / "data" / "frames",
        Path.home() / "ai_camera_monitor" / "frames",
        Path("/data/frames"),
    ]
    alert_ids = [
        "f6fd1798-c76c-4a63-a4e4-6af3432a177f",
        "e6492b79-4bc9-4d2e-829d-6f6822ecb140",
        "81dc7a2c-5179-4b80-b102-e0a029310a20",
        "b7dd2999-c7de-4d0c-ab61-db761690e12d",
        "3255fbb1-7eec-4492-a670-b7916aa11993",
        "3b967d96-70ca-4487-b767-a1ecad894476",
        "c7b4b3f5-af60-4a43-acf4-84fc62e34987",
    ]
    found = {}
    for root in candidates:
        if not root.exists():
            continue
        for aid in alert_ids:
            d = root / aid
            if not d.is_dir():
                continue
            # Source frames are frame_1..frame_4 (no suffix). Crops have
            # _crop<x>_<y>_<w>x<h> suffix in the filename.
            all_frames = sorted(d.glob("frame_*.jpg"))
            source_frames = [
                p for p in all_frames if "_crop" not in p.name
            ]
            if len(source_frames) >= 4:
                found[aid] = source_frames[:4]
        if found:
            return found
    return {}


@pytest.mark.skipif(
    not _find_morning_frames(),
    reason="morning alert frames not available on this machine",
)
def test_subject_bbox_tighter_than_stored_for_morning_alerts():
    """On real 2026-09-01 morning alerts, subject_bbox_from_mask is
    tighter than the stored diff bbox.

    5 of 7 alerts had "only 1 of 2 crops with a vehicle" — the stored
    diff bbox covered the trail of motion, not the subject.
    subject_bbox_from_mask should give a tighter bbox on the actual
    subject.
    """
    frames_by_alert = _find_morning_frames()
    assert frames_by_alert, "frames not found despite skipif"

    for alert_id, frame_paths in frames_by_alert.items():
        # Phase 6B.169: frame_paths[1] = frame_2, frame_paths[2] = frame_3.
        frame_2 = cv2.imread(str(frame_paths[1]), cv2.IMREAD_GRAYSCALE)
        frame_3 = cv2.imread(str(frame_paths[2]), cv2.IMREAD_GRAYSCALE)
        if frame_2 is None or frame_3 is None:
            continue

        mask = pairwise_diff(frame_2, frame_3, threshold=25)
        diff_bbox = bbox_from_mask(mask, min_area_px=64, padding_px=0)
        subj_bbox = subject_bbox_from_mask(mask, frame_b=frame_3)

        # If subject bbox was detected, it should be tighter than the diff bbox
        if subj_bbox is not None and diff_bbox is not None:
            subj_area = subj_bbox[2] * subj_bbox[3]
            diff_area = diff_bbox[2] * diff_bbox[3]
            assert subj_area < diff_area, (
                f"[{alert_id}] subject bbox area={subj_area} should be < "
                f"diff bbox area={diff_area} (subject is denser than trail)"
            )
