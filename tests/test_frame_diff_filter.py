"""test_frame_diff_filter - unit tests for US-096c noise-filter pipeline.

Tests for the new filter + dilate pipeline added to:
  - _filter_components_and_dilate()
  - compute_cross_diff_bboxes() (uses the filter pipeline)
  - _get_min_area_pct() (env var reader)

Acceptance criteria:
  - pytest tests/test_frame_diff_filter.py -v passes
  - ruff check tests/test_frame_diff_filter.py = 0 errors
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

# Ensure infra/ is importable (not from the package __init__ which may pull
# modules that require env vars or GPU).
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from infra.frame_diff import (
    DEFAULT_MIN_AREA_PCT,
    _dilate_mask,
    _filter_components_and_dilate,
    _get_min_area_pct,
)

# ============================================================================
# Fixtures
# ============================================================================


def _make_frame(h: int = 1296, w: int = 1920) -> np.ndarray:
    """Return a uniform-gray uint8 frame."""
    return np.full((h, w), 128, dtype=np.uint8)


def _make_motion_box(
    frame: np.ndarray,
    x: int,
    y: int,
    bw: int,
    bh: int,
    value: int = 255,
) -> np.ndarray:
    """Return a copy of frame with a white rectangle at (x,y,w,h)."""
    copy = frame.copy()
    copy[y : y + bh, x : x + bw] = value
    return copy


# ============================================================================
# _get_min_area_pct - env var reading
# ============================================================================


class TestGetMinAreaPct:
    """_get_min_area_pct reads env at call time, defaults to DEFAULT_MIN_AREA_PCT."""

    def test_default_value(self, monkeypatch):
        """No env var set -> returns DEFAULT_MIN_AREA_PCT."""
        monkeypatch.delenv("MIN_AREA_PCT", raising=False)
        assert _get_min_area_pct() == DEFAULT_MIN_AREA_PCT

    def test_env_override(self, monkeypatch):
        """Env var set to 0.001 -> returns 0.001."""
        monkeypatch.setenv("MIN_AREA_PCT", "0.001")
        assert _get_min_area_pct() == 0.001

    def test_env_invalid_falls_back(self, monkeypatch):
        """Non-numeric env var -> falls back to default."""
        monkeypatch.setenv("MIN_AREA_PCT", "abc")
        assert _get_min_area_pct() == DEFAULT_MIN_AREA_PCT

    def test_env_empty_falls_back(self, monkeypatch):
        """Empty env var -> falls back to default."""
        monkeypatch.setenv("MIN_AREA_PCT", "")
        assert _get_min_area_pct() == DEFAULT_MIN_AREA_PCT


# ============================================================================
# _filter_components_and_dilate - noise filtering
# ============================================================================


class TestFilterComponentsAndDilate:
    """_filter_components_and_dilate filters small CCs, then dilates."""

    def test_empty_mask(self):
        """Empty mask returns all-zeros."""
        mask = np.zeros((100, 100), dtype=np.uint8)
        result = _filter_components_and_dilate(mask)
        assert (result == 0).all()

    def test_all_noise_filtered(self):
        """Many small components below floor are dropped, result is empty."""
        h, w = 1296, 1920
        mask = np.zeros((h, w), dtype=np.uint8)
        # Place 10 single-pixel components spaced apart
        for i in range(10):
            mask[10 + i * 100, 100 + i * 50] = 255

        result = _filter_components_and_dilate(mask)
        # All single pixels are below MIN_AREA_PX_FLOOR (50) -> filtered out
        assert (result == 0).all()

    def test_large_component_survives(self):
        """A 200x200 white box at 0.05% of frame area survives filtering."""
        h, w = 1296, 1920
        mask = np.zeros((h, w), dtype=np.uint8)
        mask[100:300, 100:300] = 255  # 200x200 = 40,000 px

        result = _filter_components_and_dilate(mask)
        assert result.sum() > 0, "Large component should survive"

    def test_dilation_increases_size(self):
        """After filter+dilate, output is larger than input (by ~12px on each side)."""
        h, w = 1296, 1920
        mask = np.zeros((h, w), dtype=np.uint8)
        mask[600:650, 900:950] = 255  # 50x50 box

        result = _filter_components_and_dilate(mask)
        # Result should be larger than the original box due to dilation
        assert result.sum() > 50 * 50, "Dilation should expand the box"

    def test_custom_dilate_px(self):
        """Custom dilate_px overrides FILTER_DILATE_PX."""
        h, w = 1296, 1920
        mask = np.zeros((h, w), dtype=np.uint8)
        mask[600:650, 900:950] = 255

        result_0 = _filter_components_and_dilate(mask, dilate_px=0)
        result_24 = _filter_components_and_dilate(mask, dilate_px=24)

        # 24px dilation should produce larger output than 0px
        assert result_24.sum() > result_0.sum()

    def test_no_pre_dilation(self):
        """Filter operates on RAW mask - no pre-dilation before CC."""
        # Create two small blobs separated by a gap smaller than 12px
        h, w = 1296, 1920
        mask = np.zeros((h, w), dtype=np.uint8)
        mask[100, 100] = 255
        mask[100, 105] = 255  # 5px gap
        # If pre-dilated, these would merge before filtering.
        # Since they're both below the 50px floor, both should be filtered out.
        result = _filter_components_and_dilate(mask)
        assert (result == 0).all()


# ============================================================================
# compute_cross_diff_bboxes - US-096c filter integration
# ============================================================================


class TestComputeCrossDiffBboxes:
    """compute_cross_diff_bboxes uses filter + dilate pipeline."""

    def _write_frames(
        self,
        base_dir: Path,
        f1: np.ndarray,
        f2: np.ndarray,
        f3: np.ndarray,
        f4: np.ndarray,
    ) -> list[str]:
        """Write 4 frame PNGs and return their paths sorted."""
        base_dir.mkdir(parents=True, exist_ok=True)
        from PIL import Image

        paths = []
        for i, frame in enumerate([f1, f2, f3, f4], start=1):
            p = base_dir / f"frame_{i:03d}.png"
            Image.fromarray(frame, mode="L").save(str(p))
            paths.append(str(p))
        return sorted(paths)

    def test_no_motion_returns_none(self, tmp_path):
        """Identical frames -> all diffs zero -> bboxes are None."""
        frame = _make_frame()
        paths = self._write_frames(tmp_path / "no_motion", frame, frame, frame, frame)
        from infra.frame_diff import compute_cross_diff_bboxes as ccb

        bbox_a, bbox_b = ccb(paths[0], paths[1], paths[2], paths[3])
        assert bbox_a is None
        assert bbox_b is None

    def test_small_noise_suppressed(self, tmp_path):
        """Small AND-overlap from shifted blobs -> filtered -> None."""
        f1 = _make_frame()
        f4 = _make_frame()
        # 4x4=16px blob, shifted 3px between f2 and f3.
        # AND overlap is only ~9px, below the 50px floor.
        f2 = _make_frame()
        f3 = _make_frame()
        f2[100:104, 100:104] = 255  # 4x4 = 16px
        f3[103:107, 103:107] = 255  # shifted 3px
        paths = self._write_frames(tmp_path / "noise", f1, f2, f3, f4)
        from infra.frame_diff import compute_cross_diff_bboxes as ccb

        bbox_a, bbox_b = ccb(paths[0], paths[1], paths[2], paths[3])
        assert bbox_a is None, f"bbox_a={bbox_a} should be None (filtered noise)"
        assert bbox_b is None, f"bbox_b={bbox_b} should be None (filtered noise)"

    def test_real_motion_survives_filter(self, tmp_path):
        """A moving vehicle-sized object (100x300px) survives filter+dilate."""
        f1 = _make_frame()
        f2 = _make_frame()
        f3 = _make_frame()
        f4 = _make_frame()

        # Object moves from left to right across frames
        # Frame 2: object at x=100, y=500
        f2[500:800, 100:400] = 255
        # Frame 3: object at x=500, y=500
        f3[500:800, 500:800] = 255

        paths = self._write_frames(tmp_path / "motion", f1, f2, f3, f4)
        from infra.frame_diff import compute_cross_diff_bboxes as ccb

        bbox_a, bbox_b = ccb(paths[0], paths[1], paths[2], paths[3])
        assert bbox_a is not None
        assert bbox_b is not None
        # bbox_a should be near the old position (x~100)
        # bbox_b should be near the new position (x~500)
        assert bbox_a[0] < bbox_b[0], "Object moved right"

    def test_none_frames_return_none(self, tmp_path):
        """Missing frame files -> (None, None)."""
        p1 = str(tmp_path / "nonexistent_1.png")
        p2 = str(tmp_path / "nonexistent_2.png")
        p3 = str(tmp_path / "nonexistent_3.png")
        p4 = str(tmp_path / "nonexistent_4.png")
        from infra.frame_diff import compute_cross_diff_bboxes as ccb

        bbox_a, bbox_b = ccb(p1, p2, p3, p4)
        assert bbox_a is None
        assert bbox_b is None


# ============================================================================
# _dilate_mask - edge cases
# ============================================================================


class TestDilateMask:
    """_dilate_mask expands a binary mask by px pixels."""

    def test_zero_dilate(self):
        """px=0 returns a copy (unchanged)."""
        mask = np.array([[0, 0, 0], [0, 1, 0], [0, 0, 0]], dtype=np.uint8) * 255
        result = _dilate_mask(mask, 0)
        np.testing.assert_array_equal(result, mask)

    def test_px1_dilate(self):
        """px=1 dilates a single pixel to a 3x3 block."""
        h, w = 10, 10
        mask = np.zeros((h, w), dtype=np.uint8)
        mask[5, 5] = 255
        result = _dilate_mask(mask, 1)
        # A single pixel at (5,5) dilated by 1px should cover
        # rows 4-6, cols 4-6 = 9 pixels
        assert result[4:7, 4:7].sum() == 9 * 255

    def test_dilate_preserves_bounds(self):
        """Dilation does not exceed mask shape."""
        mask = np.zeros((10, 10), dtype=np.uint8)
        mask[0, 0] = 255
        result = _dilate_mask(mask, 100)  # huge dilation
        assert result.shape == (10, 10)
