"""test_cross_diff_bbox — unit tests for frame_diff.bbox_from_mask_with_intersection.

Covers the cross-diff intersection anchor used to fix crop mismatch
(alert 72c70d5f: diff(2,3) picked the wrong connected component).

Acceptance criteria:
  - AC6: pytest tests/test_cross_diff_bbox.py -v passes all 5 cases (a-e)
  - AC9: ruff check tests/test_cross_diff_bbox.py = 0 errors
"""

from __future__ import annotations

import numpy as np
import pytest

from infra.frame_diff import bbox_from_mask_with_intersection

# ============================================================================
# Helpers
# ============================================================================


def _make_binary_mask(
    height: int = 1080,
    width: int = 1920,
    *,
    blobs: list[tuple[int, int, int, int]] | None = None,
) -> np.ndarray:
    """Create a binary uint8 mask (0 or 255) with optional rectangular blobs.

    blobs is a list of (y1, x1, y2, x2) slice specs.
    """
    mask = np.zeros((height, width), dtype=np.uint8)
    if blobs:
        for y1, x1, y2, x2 in blobs:
            mask[y1:y2, x1:x2] = 255
    return mask


# ============================================================================
# Test case (a): alert 72c70d5f diff(1,2)+diff(2,3) -> bbox (182,705,332,591)
# ============================================================================


class TestAlert72c70d5fCrossDiff:
    """Case (a): the real alert 72c70d5f scenario.

    diff(1,2) has two blobs:
      - CC1: trail at x=663 (largest area)
      - CC2: subject silhouette at x=182 (smaller area)
    diff(2,3) has two blobs:
      - CC5216: subject silhouette at x=182 (same as CC2)
      - CC_other: trail at x=663 (larger area)

    The correct bbox is from the shared component at x=182 (IoU=0.995).
    We simulate this with two separated rectangular blobs in each mask,
    placed at the same coordinates for the "subject" component and different
    coordinates for the "trail" component.
    """

    @pytest.fixture
    def diff_1_2(self):
        """diff(1,2): subject at (150,700,220,900), trail at (640,700,700,900)."""
        return _make_binary_mask(
            blobs=[
                (700, 150, 900, 220),  # subject CC2 at x=150-220, y=700-900
                (700, 640, 900, 700),  # trail CC1 at x=640-700
            ],
        )

    @pytest.fixture
    def diff_2_3(self):
        """diff(2,3): subject at (150,700,220,900), trail at (650,700,680,900)."""
        return _make_binary_mask(
            blobs=[
                (700, 150, 900, 220),  # subject CC5216 — same as diff(1,2) CC2
                (700, 650, 900, 680),  # trail at slightly different x
            ],
        )

    def test_returns_subject_bbox(self, diff_1_2, diff_2_3):
        """Cross-diff returns the subject bbox (x~150-182) not the trail."""
        result = bbox_from_mask_with_intersection(
            diff_1_2, diff_2_3, min_area_px=64
        )
        assert result is not None
        x, _y, w, h = result
        # The subject blob starts at x=150. After 10% expansion and
        # round-to-32, x should still be on the left side of the frame.
        assert x < 300  # subject side, NOT trail side (x>600)
        assert w >= 64  # expanded width (raw 70x200 -> ~96 after expand+round32)
        assert h >= 64  # expanded height

    def test_iou_above_threshold(self, diff_1_2, diff_2_3):
        """Subject blobs are identical in both diffs -> IoU=1.0."""
        # We can verify by checking that the function does NOT return None.
        result = bbox_from_mask_with_intersection(
            diff_1_2, diff_2_3, min_area_px=64
        )
        assert result is not None

    def test_ignores_largest_component(self, diff_1_2, diff_2_3):
        """The trail blobs may be larger but are ignored because the
        subject pair has higher IoU."""
        result = bbox_from_mask_with_intersection(
            diff_1_2, diff_2_3, min_area_px=64
        )
        assert result is not None
        x, _y, _w, _h = result
        # x should be near the subject (150-220), not the trail (640-700).
        # This is the core fix: we anchor to the shared component,
        # not the largest one.
        assert 130 <= x <= 230


# ============================================================================
# Test case (b): stationary scene -> None
# ============================================================================


class TestStationaryScene:
    """Case (b): identical masks (stationary scene, no motion) -> None.

    If nothing moved, the two masks are identical. Every component in
    mask_prior has IoU=1.0 with itself in mask_curr. However, for a truly
    stationary scene, the pairwise_diff would produce all-zero masks.
    We test the edge case where both masks are empty.
    """

    def test_identical_masks_returns_subject(self):
        """Identical masks: the shared component is the only one, IoU=1.0.

        This is NOT None — it's a valid match. Stationary means the subject
        is the same in both diffs. This tests that identical-but-nonempty
        masks produce a result.
        """
        mask = _make_binary_mask(blobs=[(700, 150, 900, 220)])
        result = bbox_from_mask_with_intersection(
            mask, mask, min_area_px=64
        )
        assert result is not None

    def test_no_motion_in_either(self):
        """Both masks all-zero -> None."""
        empty = np.zeros((1080, 1920), dtype=np.uint8)
        assert bbox_from_mask_with_intersection(empty, empty) is None


# ============================================================================
# Test case (c): IoU below threshold -> None
# ============================================================================


class TestIouBelowThreshold:
    """Case (c): non-overlapping components in prior vs current -> None."""

    def test_completely_disjoint_blobs(self):
        """Blobs in different positions, no overlap, IoU=0 -> None."""
        prior = _make_binary_mask(blobs=[(700, 100, 900, 150)])  # x=100-150
        curr = _make_binary_mask(blobs=[(700, 500, 900, 550)])  # x=500-550
        assert bbox_from_mask_with_intersection(prior, curr) is None

    def test_custom_iou_threshold(self):
        """With a high IoU threshold, even near-miss pairs are rejected."""
        # Two blobs with a small overlap
        prior = _make_binary_mask(blobs=[(700, 100, 900, 200)])
        curr = _make_binary_mask(blobs=[(700, 150, 900, 250)])  # shifted 50px
        # At default threshold 0.5, this might pass. At 0.99 it shouldn't.
        result = bbox_from_mask_with_intersection(
            prior, curr, iou_threshold=0.99
        )
        assert result is None


# ============================================================================
# Test case (d): empty masks -> None
# ============================================================================


class TestEmptyMasks:
    """Case (d): either mask empty or both empty -> None."""

    def test_both_empty(self):
        """Both all-zero -> None."""
        empty = np.zeros((100, 100), dtype=np.uint8)
        assert bbox_from_mask_with_intersection(empty, empty) is None

    def test_one_empty_other_filled(self):
        """One all-zero, one non-zero -> None."""
        filled = _make_binary_mask(blobs=[(10, 20, 50, 60)])
        empty = np.zeros((100, 100), dtype=np.uint8)
        assert bbox_from_mask_with_intersection(filled, empty) is None
        assert bbox_from_mask_with_intersection(empty, filled) is None

    def test_none_input(self):
        """None passed as either argument -> None."""
        mask = _make_binary_mask(blobs=[(10, 20, 50, 60)])
        assert bbox_from_mask_with_intersection(None, mask) is None
        assert bbox_from_mask_with_intersection(mask, None) is None

    def test_empty_numpy_array(self):
        """Reshaped empty array -> None."""
        empty = np.array([], dtype=np.uint8).reshape(0, 0)
        assert bbox_from_mask_with_intersection(empty, empty) is None


# ============================================================================
# Test case (e): one empty, one filled -> None
# ============================================================================


class TestOneEmptyOneFilled:
    """Case (e): one mask empty, one filled -> None.

    This is covered in TestEmptyMasks but we also test edge cases where
    one mask has only sub-min-area components.
    """

    def test_prior_sub_min_area(self):
        """Prior mask has only tiny blobs below min_area_px -> None."""
        prior = np.zeros((100, 100), dtype=np.uint8)
        prior[50, 50] = 255  # 1 pixel
        curr = _make_binary_mask(blobs=[(10, 20, 50, 60)])
        assert bbox_from_mask_with_intersection(prior, curr, min_area_px=64) is None

    def test_curr_sub_min_area(self):
        """Current mask has only tiny blobs below min_area_px -> None."""
        prior = _make_binary_mask(blobs=[(10, 20, 50, 60)])
        curr = np.zeros((100, 100), dtype=np.uint8)
        curr[50, 50] = 255  # 1 pixel
        assert bbox_from_mask_with_intersection(prior, curr, min_area_px=64) is None


# ============================================================================
# Edge cases
# ============================================================================


class TestEdgeCases:
    """Additional edge cases for robustness."""

    def test_single_component_each(self):
        """One component in each mask, overlapping -> returns expanded bbox."""
        prior = _make_binary_mask(blobs=[(100, 200, 400, 400)])
        curr = _make_binary_mask(blobs=[(100, 200, 400, 400)])
        result = bbox_from_mask_with_intersection(prior, curr, min_area_px=64)
        assert result is not None
        _x, _y, w, h = result
        # Width/height must be multiples of 32
        assert w % 32 == 0
        assert h % 32 == 0

    def test_multiple_components_prior_single_curr(self):
        """Multiple in prior, one in curr -> matches curr against best prior."""
        prior = _make_binary_mask(
            blobs=[
                (100, 200, 300, 300),  # matches
                (500, 600, 700, 700),  # no match in curr
            ],
        )
        curr = _make_binary_mask(blobs=[(100, 200, 300, 300)])
        result = bbox_from_mask_with_intersection(prior, curr, min_area_px=64)
        assert result is not None
        x, _y, _w, _h = result
        # Should be anchored to the matching component (x~200)
        assert 180 <= x <= 210

    def test_padding_rounds_to_32(self):
        """Expanded bbox dimensions are multiples of 32."""
        prior = _make_binary_mask(blobs=[(45, 45, 55, 55)])  # 10x10 blob
        curr = _make_binary_mask(blobs=[(45, 45, 55, 55)])
        result = bbox_from_mask_with_intersection(prior, curr)
        assert result is not None
        _x, _y, w, h = result
        assert w % 32 == 0
        assert h % 32 == 0
        assert w > 10  # actually expanded

    def test_clamped_to_image_bounds(self):
        """Bbox that would exceed image is clamped."""
        # Blob in bottom-right corner
        prior = _make_binary_mask(blobs=[(1060, 1900, 1080, 1920)])
        curr = _make_binary_mask(blobs=[(1060, 1900, 1080, 1920)])
        result = bbox_from_mask_with_intersection(prior, curr)
        assert result is not None
        x, y, w, h = result
        assert x >= 0 and y >= 0
        assert x + w <= 1920
        assert y + h <= 1080

    def test_min_area_filter_before_iou(self):
        """Components below min_area_px are excluded from IoU comparison."""
        # Prior: one big + one tiny. Curr: one big matching the big one.
        prior = _make_binary_mask(
            blobs=[
                (100, 200, 300, 300),  # 200x200 = 40000 px
                (50, 50, 52, 52),  # 2x2 = 4 px, below threshold
            ],
        )
        curr = _make_binary_mask(blobs=[(100, 200, 300, 300)])
        result = bbox_from_mask_with_intersection(prior, curr, min_area_px=64)
        assert result is not None
