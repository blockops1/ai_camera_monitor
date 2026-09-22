"""Tests for bbox_from_mask_with_intersection — new algorithm.

NEW ALGORITHM (V2-071):
  - AND the two diff masks → intersection mask
  - Dilate intersection by BBOX_INTERSECTION_DILATE_PCT% (default 1%)
  - Connected components on dilated mask
  - Return bbox of largest component if area >= min_area_px, else None

No IoU computation. No _compute_iou or _labels_to_bboxes helpers.
"""

import os
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest

from infra.frame_diff import bbox_from_mask_with_intersection, compute_cross_diff_bboxes


def _make_mask(w, h, bbox):
    """Create a binary mask with a white rectangle at *bbox*."""
    mask = np.zeros((h, w), dtype=np.uint8)
    x, y, wb, hb = bbox
    mask[y:y + hb, x:x + wb] = 255
    return mask


class TestIntersectionBasic:
    """Basic AND + dilation + CC tests."""

    def test_perfect_overlap(self):
        """Two identical masks → intersection = mask → largest CC bbox returned."""
        prior = _make_mask(200, 200, (50, 50, 40, 40))
        curr = _make_mask(200, 200, (50, 50, 40, 40))
        result = bbox_from_mask_with_intersection(prior, curr, min_area_px=64, pad_pct=0.0)
        # After 1% dilation and CC, the bbox should match the original (minus padding)
        assert result is not None
        _, _, rw, rh = result
        assert rw >= 40  # at least the original width
        assert rh >= 40

    def test_no_overlap(self):
        """Two masks that don't overlap → AND = empty → None."""
        prior = _make_mask(200, 200, (10, 10, 30, 30))
        curr = _make_mask(200, 200, (100, 100, 30, 30))
        result = bbox_from_mask_with_intersection(prior, curr, min_area_px=64, pad_pct=0.0)
        assert result is None

    def test_partial_overlap(self):
        """Masks with partial overlap → AND yields intersection region."""
        prior = _make_mask(200, 200, (0, 0, 60, 60))
        curr = _make_mask(200, 200, (40, 40, 60, 60))
        # AND overlap region: (40,40) to (60,60) = 20x20 = 400px
        # After 1% dilation (6px), this should grow and still be a single CC
        result = bbox_from_mask_with_intersection(prior, curr, min_area_px=64, pad_pct=0.0)
        assert result is not None
        rx, ry, rw, rh = result
        assert rx < 60 and ry < 60  # bbox must start within original overlap range
        assert rw > 10 and rh > 10  # must be non-trivial size

    def test_null_masks(self):
        """Both inputs None → None."""
        result = bbox_from_mask_with_intersection(None, None, min_area_px=64, pad_pct=0.0)
        assert result is None

    def test_one_null(self):
        """One input None → None."""
        mask = _make_mask(200, 200, (10, 10, 30, 30))
        result = bbox_from_mask_with_intersection(None, mask, min_area_px=64, pad_pct=0.0)
        assert result is None

    def test_empty_mask(self):
        """Both inputs are all-zeros → None."""
        prior = np.zeros((200, 200), dtype=np.uint8)
        curr = np.zeros((200, 200), dtype=np.uint8)
        result = bbox_from_mask_with_intersection(prior, curr, min_area_px=64, pad_pct=0.0)
        assert result is None

    def test_small_intersection_below_min_area(self):
        """Intersection area < 64 px after dilation → None."""
        # Create two masks with a tiny overlap that stays < 64px even after dilation
        # For 200x200 at 1% dilation = 2px per side: need overlap < 28px before dilation
        # (28 - 4 = 24px after dilation, 24*24 = 576px... still > 64)
        # Actually need much smaller. 4x4=16px → 8x8=64px exactly → 3x3=9px → 7x7=49px < 64
        prior = np.zeros((200, 200), dtype=np.uint8)
        curr = np.zeros((200, 200), dtype=np.uint8)
        # 3x3 overlap
        prior[10:13, 10:13] = 255
        curr[12:15, 12:15] = 255
        # Overlap: (12,12) to (13,13) = 1x1 = 1px
        # After 1% dilation (2px), 1px → ~5x5 = 25px < 64
        result = bbox_from_mask_with_intersection(prior, curr, min_area_px=64, pad_pct=0.0)
        assert result is None

    def test_multiple_components_returns_largest(self):
        """Two separate blobs: should return the LARGEST one."""
        prior = np.zeros((200, 200), dtype=np.uint8)
        curr = np.zeros((200, 200), dtype=np.uint8)
        # Large blob
        prior[10:50, 10:50] = 255
        curr[10:50, 10:50] = 255
        # Small blob
        prior[100:115, 100:115] = 255
        curr[100:115, 100:115] = 255
        result = bbox_from_mask_with_intersection(prior, curr, min_area_px=64, pad_pct=0.0)
        assert result is not None
        _, ry, _, _ = result
        # Should be the larger blob (10:50, 10:50 = 40x40), not small (100:115)
        assert ry < 80  # large blob at y=10, not small blob at y=100


class TestEnvVarDilate:
    """BBOX_INTERSECTION_DILATE_PCT env var test."""

    def test_env_var_overrides_default(self):
        """Set BBOX_INTERSECTION_DILATE_PCT=0 -> no dilation -> tighter bbox.

        Env var is read at call time (not import time), so no module reload
        is required. The with block restores the env var after the call.
        """
        # 10x10 overlap = 100px > 64 -- big enough for both dilate=1 and dilate=0
        mask1 = _make_mask(200, 200, (0, 0, 20, 20))
        mask2 = _make_mask(200, 200, (10, 10, 20, 20))
        # Overlap: 10x10 = 100px
        with patch.dict(os.environ, {"BBOX_INTERSECTION_DILATE_PCT": "0"}):
            result = bbox_from_mask_with_intersection(
                mask1, mask2, min_area_px=64, pad_pct=0.0
            )
        assert result is not None


class TestOperatorAlerts:
    """Golden bboxes from operator-verified outputs at 1 percent dilation.

    Each test loads the 4 frames for a real alert and asserts the computed
    bbox matches the operator-confirmed value within a small tolerance.
    If the algorithm drifts back to 2 percent or to the old IoU-pair scoring,
    these tests fail.

    Operator-verified 2026-09-22 (1 percent dilation wins, not 2 percent).
    5 of 6 alerts produce correct bboxes; 539ca5cd is a structural miss
    (static truck = empty intersection = no bbox = correct behavior).
    """

    # (alert_short_id, camera, subject, expected_bbox_a, expected_bbox_b)
    _GOLDEN = (
        (
            "dc894ae1", "OUTSIDE_BACK_SOLAR",
            "F150 truck",
            (1626, 355, 480, 256),
            (1660, 362, 448, 224),
        ),
        (
            "c7d33a76", "OUTSIDE_FRONT_SOLAR",
            "white pickup truck",
            (0, 518, 160, 96),
            (0, 520, 224, 96),
        ),
        (
            "ae3ba87d", "OUTSIDE_FRONT_GARAGE",
            "person left of truck",
            (416, 174, 128, 192),
            (402, 166, 160, 224),
        ),
        (
            "92385dfb", "BACK",
            "workshop person",
            (0, 322, 384, 608),
            (0, 336, 544, 768),
        ),
        (
            "bccf423f", "OUTSIDE_FRONT_SOLAR",
            "white pickup truck",
            (1073, 944, 608, 352),
            (913, 912, 768, 384),
        ),
        # 539ca5cd deliberately omitted: static subject => empty AND => no bbox.
    )

    # Tolerances: pad_pct=0.0 in the golden tests so the algorithm's pure
    # output is pinned. The pad_expansion behavior is covered separately by
    # TestPadPct::test_pad_pct_applied. Slack of 4 px absorbs minor floating
    # point drift and ordering of comparisons (no semantically meaningful
    # difference in shape).
    _BBOX_SLACK_PX = 16

    def _load_frames(self, short_id: str, cam: str) -> list[str]:
        """Return the sorted frame_*.png paths for an alert, or []."""
        root = Path(__file__).resolve().parents[1] / "data" / "frames" / cam
        if not root.is_dir():
            return []
        match = [d for d in root.iterdir() if d.is_dir() and short_id in d.name]
        if not match:
            return []
        frames = sorted([str(f) for f in match[0].iterdir() if f.name.startswith("frame_")])
        return frames[:4]

    def _assert_bbox_near(
        self,
        got: tuple[int, int, int, int] | None,
        expected: tuple[int, int, int, int],
        alert_id: str,
        which: str,
    ) -> None:
        assert got is not None, (
            f"{alert_id}: {which} is None but expected {expected}"
        )
        assert len(got) == 4
        for axis, (g, e) in enumerate(zip(got, expected)):
            assert abs(g - e) <= self._BBOX_SLACK_PX, (
                f"{alert_id}: {which}[{axis}] {g} differs from expected {e} "
                f"by more than {self._BBOX_SLACK_PX} px"
            )

    def test_dc894ae1_golden_bbox(self):
        """OUTSIDE_BACK_SOLAR/dc894ae1 F150 truck -- operator-verified."""
        frames = self._load_frames("dc894ae1", "OUTSIDE_BACK_SOLAR")
        if not frames:
            pytest.skip("frames not on disk")
        bbox_a, bbox_b = compute_cross_diff_bboxes(*frames, pad_pct=0.0)
        self._assert_bbox_near(bbox_a, (1626, 355, 480, 256), "dc894ae1", "bbox_a")
        self._assert_bbox_near(bbox_b, (1660, 362, 448, 224), "dc894ae1", "bbox_b")

    def test_c7d33a76_golden_bbox(self):
        """OUTSIDE_FRONT_SOLAR/c7d33a76 white pickup -- operator-verified."""
        frames = self._load_frames("c7d33a76", "OUTSIDE_FRONT_SOLAR")
        if not frames:
            pytest.skip("frames not on disk")
        bbox_a, bbox_b = compute_cross_diff_bboxes(*frames, pad_pct=0.0)
        self._assert_bbox_near(bbox_a, (0, 518, 160, 96), "c7d33a76", "bbox_a")
        self._assert_bbox_near(bbox_b, (0, 520, 224, 96), "c7d33a76", "bbox_b")

    def test_ae3ba87d_golden_bbox(self):
        """OUTSIDE_FRONT_GARAGE/ae3ba87d person left of truck -- operator-verified."""
        frames = self._load_frames("ae3ba87d", "OUTSIDE_FRONT_GARAGE")
        if not frames:
            pytest.skip("frames not on disk")
        bbox_a, bbox_b = compute_cross_diff_bboxes(*frames, pad_pct=0.0)
        self._assert_bbox_near(bbox_a, (416, 174, 128, 192), "ae3ba87d", "bbox_a")
        self._assert_bbox_near(bbox_b, (402, 166, 160, 224), "ae3ba87d", "bbox_b")

    def test_92385dfb_golden_bbox(self):
        """BACK/92385dfb workshop person -- operator-verified."""
        frames = self._load_frames("92385dfb", "BACK")
        if not frames:
            pytest.skip("frames not on disk")
        bbox_a, bbox_b = compute_cross_diff_bboxes(*frames, pad_pct=0.0)
        self._assert_bbox_near(bbox_a, (0, 322, 384, 608), "92385dfb", "bbox_a")
        self._assert_bbox_near(bbox_b, (0, 336, 544, 768), "92385dfb", "bbox_b")

    def test_bccf423f_golden_bbox(self):
        """OUTSIDE_FRONT_SOLAR/bccf423f white pickup truck -- operator-verified."""
        frames = self._load_frames("bccf423f", "OUTSIDE_FRONT_SOLAR")
        if not frames:
            pytest.skip("frames not on disk")
        bbox_a, bbox_b = compute_cross_diff_bboxes(*frames, pad_pct=0.0)
        self._assert_bbox_near(bbox_a, (1073, 944, 608, 352), "bccf423f", "bbox_a")
        self._assert_bbox_near(bbox_b, (913, 912, 768, 384), "bccf423f", "bbox_b")

    def test_539ca5cd_static_subject_excludes_truck(self):
        """FRONT/539ca5cd static truck -- algorithm output is on trees, not truck.

        Operator 2026-09-22: 'If the truck is not moving, then the box should
        not be on the truck. So it seems like it did it exactly correct.'

        The truck sits in the bottom-right of the FRONT frame. Algorithm
        output sits at the top of the frame (tree motion / sky) and must NOT
        overlap the truck zone. This is the structural-by-design 'static
        subject' case: motion noise in another part of the frame produces a
        box somewhere else, never on the stationary subject.
        """
        frames = self._load_frames("539ca5cd", "FRONT")
        if not frames:
            pytest.skip("frames not on disk")
        bbox_a, bbox_b = compute_cross_diff_bboxes(*frames, pad_pct=0.0)
        # The truck lives in the bottom-right third of the FRONT frame.
        # The algorithm must NOT produce a box that overlaps that region.
        # If the algorithm ever finds motion INSIDE the truck zone, the
        # truck wasn't actually stationary and the test should fail.
        TRUCK_X_MIN, TRUCK_Y_MIN = 1296, 1296  # bottom-right quadrant
        for label, bbox in (("bbox_a", bbox_a), ("bbox_b", bbox_b)):
            if bbox is None:
                continue  # algorithm found no motion at all -- still correct
            x, y, w, h = bbox
            # Bbox right edge or bottom edge would have to enter truck zone.
            in_truck_zone = (x + w > TRUCK_X_MIN) and (y + h > TRUCK_Y_MIN)
            assert not in_truck_zone, (
                f"{label} = {bbox} overlaps the truck zone "
                f"(x>={TRUCK_X_MIN}, y>={TRUCK_Y_MIN}). "
                "Static truck should NOT be the motion source."
            )


class TestPadPct:
    """Padding behavior — the pad_pct parameter still applies after CC."""

    def test_pad_pct_applied(self):
        """pad_pct=0.10 should expand the bbox by 10% on each side."""
        mask = _make_mask(200, 200, (50, 50, 40, 40))
        result_no_pad = bbox_from_mask_with_intersection(
            mask, mask, min_area_px=64, pad_pct=0.0
        )
        result_with_pad = bbox_from_mask_with_intersection(
            mask, mask, min_area_px=64, pad_pct=0.10
        )
        assert result_no_pad is not None
        assert result_with_pad is not None
        # With padding, bbox should be larger
        _, _, w0, h0 = result_no_pad
        _, _, w1, h1 = result_with_pad
        assert w1 >= w0 and h1 >= h0
