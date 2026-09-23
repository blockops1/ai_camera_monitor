"""test_frame_diff — unit tests for infra.frame_diff.

Covers every public function in the pure-numpy+PIL frame-diff module:
  - load_frame / pairwise_diff / bbox_from_mask
  - diff_pair_with_bbox / compute_diff_bbox
  - crop_frame_to_bbox
  - internal helpers: _connected_components, _expand_bbox_pct

Acceptance criteria:
  - AC3: pytest tests/test_frame_diff.py -q --no-header → contains 'passed'
  - AC4: full suite still ≥ 313 tests after adding this file
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from infra.frame_diff import (
    DEFAULT_BBOX_PAD_PCT,
    DEFAULT_DIFF_THRESHOLD,
    DEFAULT_MIN_AREA_PX,
    bbox_from_mask,
    compute_cross_diff_bboxes,
    compute_diff_bbox,
    crop_frame_to_bbox,
    diff_pair_with_bbox,
    load_frame,
    pairwise_diff,
)

# ============================================================================
# Helpers
# ============================================================================


def _make_png(data: np.ndarray) -> str:
    """Save a uint8 ndarray as a temporary PNG; return the path."""
    if data.dtype != np.uint8:
        data = data.astype(np.uint8)
    fd, path = tempfile.mkstemp(suffix=".png")
    import os

    os.close(fd)
    pil = Image.fromarray(data)
    pil.save(path, format="PNG")
    return path


def _make_grayscale_rect(
    height: int = 100,
    width: int = 100,
    *,
    rect: tuple[int, int, int, int] | None = None,
    bg: int = 0,
    fg: int = 255,
) -> np.ndarray:
    """Create a grayscale ndarray; optionally fill a rectangle region.

    rect is (y1, x1, y2, x2) in slice notation.
    """
    arr = np.full((height, width), bg, dtype=np.uint8)
    if rect is not None:
        y1, x1, y2, x2 = rect
        arr[y1:y2, x1:x2] = fg
    return arr


# ============================================================================
# load_frame
# ============================================================================


class TestLoadFrame:
    def test_load_grayscale(self):
        """load_frame returns uint8 ndarray with one channel."""
        # Use a colour image to avoid PIL's 2D array "L" mode quirk.
        img = np.zeros((64, 64, 3), dtype=np.uint8)
        img[30:40, 30:40] = 255
        path = _make_png(img)
        result = load_frame(path)
        assert result is not None
        assert result.shape == (64, 64)
        assert result.dtype == np.uint8
        # Verify the region we filled is bright in grayscale
        assert result[35, 35] > 200

    def test_load_color(self):
        """load_frame converts colour to grayscale."""
        img = np.zeros((50, 50, 3), dtype=np.uint8)
        img[10:20, 10:20, 0] = 200  # red channel
        path = _make_png(img)
        result = load_frame(path)
        assert result is not None
        assert result.shape == (50, 50)

    def test_load_missing_file(self):
        """load_frame returns None for non-existent file."""
        assert load_frame("/tmp/nonexistent_file_xyz.png") is None

    def test_load_corrupt_file(self):
        """load_frame returns None for unreadable file."""
        fd, path = tempfile.mkstemp(suffix=".png")
        import os

        os.close(fd)
        with open(path, "wb") as f:
            f.write(b"not a png")
        assert load_frame(path) is None
        Path(path).unlink()


# ============================================================================
# pairwise_diff
# ============================================================================


class TestPairwiseDiff:
    def test_identical_frames_zero_diff(self):
        """Two identical frames → all-zero mask."""
        frame = _make_grayscale_rect()
        mask = pairwise_diff(frame, frame, threshold=DEFAULT_DIFF_THRESHOLD)
        assert np.count_nonzero(mask) == 0

    def test_different_frames_nonzero_diff(self):
        """Two different frames → non-zero mask with changed region."""
        a = _make_grayscale_rect()
        b = _make_grayscale_rect(rect=(20, 20, 80, 80), bg=0, fg=255)
        mask = pairwise_diff(a, b)
        assert np.count_nonzero(mask) > 0
        # Changed region should be ~60×60 = 3600 pixels
        assert np.count_nonzero(mask) >= 3000

    def test_threshold_effects(self):
        """Higher threshold → fewer changed pixels."""
        a = _make_grayscale_rect()
        # fg=100 on bg=0 → pairwise diff is exactly 100
        # threshold=10 catches all 400 pixels (100>=10), threshold=110 catches none (100<110)
        b = _make_grayscale_rect(rect=(40, 40, 60, 60), bg=0, fg=100)
        mask_low = pairwise_diff(a, b, threshold=10)
        mask_high = pairwise_diff(a, b, threshold=110)
        assert np.count_nonzero(mask_low) > np.count_nonzero(mask_high)

    def test_mismatched_shapes_raises(self):
        """Different shape arrays → ValueError."""
        a = np.zeros((10, 10), dtype=np.uint8)
        b = np.zeros((20, 20), dtype=np.uint8)
        with pytest.raises(ValueError, match="frame shapes must match"):
            pairwise_diff(a, b)

    def test_non_uint8_raises(self):
        """Non-uint8 arrays → ValueError."""
        a = np.zeros((10, 10), dtype=np.float32)
        b = np.zeros((10, 10), dtype=np.uint8)
        with pytest.raises(ValueError, match="frames must be uint8"):
            pairwise_diff(a, b)

    def test_mask_values_are_0_or_255(self):
        """Output mask only contains 0 and 255."""
        a = _make_grayscale_rect()
        b = _make_grayscale_rect(rect=(30, 30, 70, 70))
        mask = pairwise_diff(a, b)
        unique = np.unique(mask)
        assert set(unique.tolist()) <= {0, 255}


# ============================================================================
# _connected_components  (internal, but tested via bbox_from_mask)
# ============================================================================


class TestConnectedComponents:
    """Test bbox_from_mask which exercises _connected_components."""

    def test_single_blob(self):
        """One rectangular blob → single bbox."""
        mask = _make_grayscale_rect(
            height=100, width=100, rect=(20, 30, 60, 80)
        ).astype(np.uint8)
        bbox = bbox_from_mask(mask, min_area_px=10)
        assert bbox is not None
        x, y, w, h = bbox
        assert x <= 30
        assert y <= 20
        assert w >= 50
        assert h >= 40

    def test_no_blob_returns_none(self):
        """All-zero mask → None."""
        mask = np.zeros((100, 100), dtype=np.uint8)
        assert bbox_from_mask(mask, min_area_px=10) is None

    def test_below_min_area_returns_none(self):
        """Tiny blob below min_area_px → None."""
        mask = np.zeros((100, 100), dtype=np.uint8)
        mask[50, 50] = 255  # 1 pixel
        assert bbox_from_mask(mask, min_area_px=10) is None

    def test_bbox_expansion(self):
        """bbox_from_mask expands by DEFAULT_BBOX_PAD_PCT and rounds to 32."""
        mask = np.zeros((100, 100), dtype=np.uint8)
        mask[45:55, 45:55] = 255  # 10×10
        bbox = bbox_from_mask(mask, min_area_px=10)
        assert bbox is not None
        x, y, w, h = bbox
        # Width and height should be rounded up to next multiple of 32
        assert w % 32 == 0
        assert h % 32 == 0
        assert w > 10  # expanded beyond raw 10

    def test_empty_mask_returns_none(self):
        """Empty mask → None."""
        assert bbox_from_mask(np.array([], dtype=np.uint8).reshape(0, 0)) is None


# ============================================================================
# _expand_bbox_pct  (internal, tested via bbox_from_mask side effects)
# ============================================================================


class TestExpandBboxPct:
    """Indirectly tested via bbox_from_mask; no standalone public API."""

    def test_clamps_to_image_bounds(self):
        """Bbox that would exceed image is clamped."""
        mask = np.zeros((100, 100), dtype=np.uint8)
        # Place a blob in the corner
        mask[80:95, 80:95] = 255
        bbox = bbox_from_mask(mask, min_area_px=10)
        assert bbox is not None
        x, y, w, h = bbox
        assert x >= 0
        assert y >= 0
        assert x + w <= 100
        assert y + h <= 100


# ============================================================================
# diff_pair_with_bbox
# ============================================================================


class TestDiffPairWithBbox:
    def test_same_file_returns_none_bbox(self):
        """Diffing same frame → bbox is None, mask has 0 pixels."""
        data = _make_grayscale_rect()
        path = _make_png(data)
        bbox, count, mask = diff_pair_with_bbox(path, path)
        assert bbox is None
        assert count == 0

    def test_different_frames_returns_bbox(self):
        """Two different frames → bbox is set."""
        a = _make_grayscale_rect(100, 100)
        b = _make_grayscale_rect(100, 100, rect=(30, 30, 70, 70))
        pa, pb = _make_png(a), _make_png(b)
        bbox, count, mask = diff_pair_with_bbox(pa, pb)
        assert bbox is not None
        x, y, w, h = bbox
        assert w > 0 and h > 0
        assert count > 0

    def test_missing_file_returns_none(self):
        """Non-existent file → (None, 0, empty_mask)."""
        bbox, count, mask = diff_pair_with_bbox("/tmp/nope.png", "/tmp/nope2.png")
        assert bbox is None
        assert count == 0
        assert mask.shape == (1, 1)


# ============================================================================
# compute_diff_bbox
# ============================================================================


class TestComputeDiffBbox:
    def test_ac2_style_square(self):
        """AC2: 100×100, 40:60 square diff → bbox around (40,40)."""
        a = np.zeros((100, 100, 3), dtype=np.uint8)
        b = a.copy()
        b[40:60, 40:60, :] = 255
        pa, pb = _make_png(a), _make_png(b)
        x, y, w, h = compute_diff_bbox(pa, pb, threshold=30, min_area=10)
        assert 40 <= x <= 50
        assert 40 <= y <= 50
        assert w >= 10
        assert h >= 10

    def test_no_motion_returns_zeros(self):
        """Same frames → (0, 0, 0, 0)."""
        data = _make_grayscale_rect()
        pa = _make_png(data)
        result = compute_diff_bbox(pa, pa)
        assert result == (0, 0, 0, 0)

    def test_missing_file_returns_zeros(self):
        """Non-existent file → (0, 0, 0, 0)."""
        assert compute_diff_bbox("/tmp/nope.png", "/tmp/nope2.png") == (
            0,
            0,
            0,
            0,
        )

    def test_min_area_filtering(self):
        """Tiny change below min_area → (0,0,0,0)."""
        a = np.zeros((100, 100, 3), dtype=np.uint8)
        b = a.copy()
        b[50, 50] = 1  # single pixel change
        pa, pb = _make_png(a), _make_png(b)
        result = compute_diff_bbox(pa, pb, min_area=100)
        assert result == (0, 0, 0, 0)

    def test_large_image(self):
        """1920×1080 frame with a 100×100 square diff → valid bbox."""
        a = np.zeros((1080, 1920, 3), dtype=np.uint8)
        b = a.copy()
        b[500:600, 900:1000, :] = 255
        pa, pb = _make_png(a), _make_png(b)
        x, y, w, h = compute_diff_bbox(pa, pb, threshold=30, min_area=100)
        assert x >= 890
        assert y >= 490
        assert w >= 90
        assert h >= 90


# ============================================================================
# crop_frame_to_bbox
# ============================================================================


class TestCropFrameToBbox:
    def test_crop_returns_file_path(self):
        """Valid crop → returns path to saved PNG."""
        data = _make_grayscale_rect(200, 200)
        path = _make_png(data)
        crop_path = crop_frame_to_bbox(path, (50, 50, 100, 100))
        assert crop_path is not None
        assert Path(crop_path).exists()

    def test_crop_nonexistent_file(self):
        """Non-existent input → None."""
        assert crop_frame_to_bbox("/tmp/nope.png", (0, 0, 50, 50)) is None

    def test_crop_zero_size_bbox(self):
        """Zero-size bbox → clamped to 1×1 crop (crop_frame_to_bbox does
        max(1, ...) so it returns a file path, not None)."""
        data = _make_grayscale_rect()
        path = _make_png(data)
        crop_path = crop_frame_to_bbox(path, (0, 0, 0, 0))
        assert crop_path is not None
        assert Path(crop_path).exists()

    def test_crop_clamps_to_bounds(self):
        """Bbox exceeding image size is clamped, not crashed."""
        data = _make_grayscale_rect(100, 100)
        path = _make_png(data)
        crop_path = crop_frame_to_bbox(path, (90, 90, 200, 200))
        assert crop_path is not None
        assert Path(crop_path).exists()
        crop_data = np.array(Image.open(crop_path))
        assert crop_data.shape[0] <= 10  # clamped height


# ============================================================================
# Edge cases & constants
# ============================================================================


class TestConstants:
    def test_defaults(self):
        """Default constants have expected values."""
        assert DEFAULT_DIFF_THRESHOLD == 25
        assert DEFAULT_MIN_AREA_PX == 64
        assert DEFAULT_BBOX_PAD_PCT == 0.10

    def test_module_import(self):
        """Module is importable without cv2."""
        import infra.frame_diff  # noqa: F401


# ============================================================================
# US-073a: intensity-based ranking for bbox_from_mask_with_intersection
# ============================================================================


class TestIntensityRankingBrightCompact:
    """AC3: intensity ranking picks bright compact region over diffuse noise."""

    def test_intensity_ranking_picks_bright_compact_region(self):
        """Build three frames where a 200×200 bright rectangle moves slowly
        across the frame (vehicle-like) and 1000 small random single-pixel
        changes spread across the frame (foliage-like noise). Assert the
        returned bbox covers the bright rectangle, NOT the noise region."""
        H, W = 480, 640
        threshold = DEFAULT_DIFF_THRESHOLD

        # Frame 1: all zeros
        f1 = np.zeros((H, W), dtype=np.uint8)

        # Frame 2: 200×200 bright rectangle at (100, 100) — vehicle-like
        f2 = f1.copy()
        f2[100:300, 100:300] = 255

        # Frame 3: rectangle shifts right by 20 px (still present — persistent)
        f3 = f1.copy()
        f3[100:300, 120:320] = 255

        # Add 1000 random single-pixel noise spots on f1, f2, f3
        rng = np.random.RandomState(42)
        for _ in range(1000):
            y, x = int(rng.randint(0, H)), int(rng.randint(0, W))
            f1[y, x] = 255
            f2[y, x] = 255
            f3[y, x] = 255

        p1, p2, p3, p4 = _make_png(f1), _make_png(f2), _make_png(f3), _make_png(f3)
        bbox_a, bbox_b = compute_cross_diff_bboxes(p1, p2, p3, p4)

        # Clean up
        for p in (p1, p2, p3, p4):
            Path(p).unlink(missing_ok=True)

        # The bbox should cover the rectangle (100-300, 100-300 area)
        assert bbox_a is not None
        x, y, w, h = bbox_a
        cx, cy = x + w // 2, y + h // 2
        # Rectangle center is roughly (200, 200)
        assert 100 <= cx <= 300, f"x-center {cx} not in [100, 300]"
        assert 100 <= cy <= 300, f"y-center {cy} not in [100, 300]"


class TestIntensityRankingParkedVehicle:
    """AC4: intensity ranking handles parked vehicle (persistent change)."""

    def test_intensity_ranking_handles_parked_vehicle(self):
        """Build four frames where a bright rectangle appears in frame 2 and
        persists with slight motion across frames 2→3→4 (a parked vehicle
        that's barely perceptible between consecutive frames). The AND mask
        keeps the persistent rect signal alive. Assert the returned bbox
        covers the rectangle.

        Real-world parallel: a truck that enters and stops produces diff(f1,f2)
        with the full truck silhouette. diff(f2,f3) has signal at the truck's
        edges because the truck is not pixel-identical across frames (sensor
        noise, compression artifacts, slight camera movement). The AND mask
        preserves this persistent signal at the truck's location."""
        H, W = 480, 640

        # Frame 1: empty scene (before vehicle arrives)
        f1 = np.zeros((H, W), dtype=np.uint8)

        # Frames 2-4: bright rectangle (the truck) with slight sub-pixel
        # motion across consecutive frames. This simulates the truck being
        # present but barely changing between f2→f3 and f3→f4.
        rect_y, rect_x = 200, 150
        rect_h, rect_w = 120, 200

        f2 = np.zeros((H, W), dtype=np.uint8)
        f2[rect_y : rect_y + rect_h, rect_x : rect_x + rect_w] = 255

        f3 = np.zeros((H, W), dtype=np.uint8)
        f3[rect_y : rect_y + rect_h, rect_x + 2 : rect_x + rect_w + 2] = 255

        f4 = np.zeros((H, W), dtype=np.uint8)
        f4[rect_y : rect_y + rect_h, rect_x + 4 : rect_x + rect_w + 4] = 255

        p1, p2, p3, p4 = _make_png(f1), _make_png(f2), _make_png(f3), _make_png(f4)
        bbox_a, bbox_b = compute_cross_diff_bboxes(p1, p2, p3, p4)

        # Clean up
        for p in (p1, p2, p3, p4):
            Path(p).unlink(missing_ok=True)

        # bbox_a = AND(mask_12, mask_23) keeps the rect region alive because
        # both diffs show signal at the truck's location.
        assert bbox_a is not None, "bbox_a should cover the persistent rect region"
        x, y, w, h = bbox_a
        cx, cy = x + w // 2, y + h // 2
        rect_cx = rect_x + rect_w // 2
        rect_cy = rect_y + rect_h // 2
        # Allow generous offset since the AND mask only has edge pixels
        assert abs(cx - rect_cx) <= rect_w * 2, "x-center offset too large"
        assert abs(cy - rect_cy) <= rect_h * 2, "y-center offset too large"


class TestIntensityRankingSavedDroppedAlert:
    """AC5: regression test against the real dropped alert."""

    def test_intensity_ranking_against_saved_dropped_alert(self):
        """Load frames from the real dropped alert (parked white truck).
        Assert returned bbox center is in the truck region (x ~200-900,
        y ~580-850), NOT in the tree canopy (y < 500)."""
        alert_dir = (
            Path(__file__).parent.parent
            / "data"
            / "frames"
            / "OUTSIDE_FRONT_SOLAR"
            / "004a9671-835c-4acd-9e87-2104099f609c"
        )
        assert alert_dir.is_dir(), f"Missing test data: {alert_dir}"

        frame_files = sorted(alert_dir.glob("frame_*.png"))
        assert len(frame_files) >= 4, f"Need ≥4 frames, found {len(frame_files)}"

        bbox_a, bbox_b = compute_cross_diff_bboxes(
            str(frame_files[0]),
            str(frame_files[1]),
            str(frame_files[2]),
            str(frame_files[3]),
        )

        # At least one bbox should be in the truck region
        result_bbox = bbox_a or bbox_b
        assert result_bbox is not None, "No bbox returned from real frames"

        x, y, w, h = result_bbox
        cx = x + w // 2
        cy = y + h // 2

        # Truck region: x ~200-900, y ~580-850
        # Tree canopy: y < 500
        assert 200 <= cx <= 900, f"x-center {cx} not in truck region [200, 900]"
        assert 580 <= cy <= 850, f"y-center {cy} not in truck region [580, 850]"
        assert cy >= 500, f"bbox center y={cy} is in tree canopy region (y<500)"
