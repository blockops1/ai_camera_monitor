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
    DEFAULT_DIFF_THRESHOLD,
    DEFAULT_MIN_AREA_PCT,
    _dilate_mask,
    _filter_components_and_dilate,
    _get_diff_threshold,
    _get_dilate_px,
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


# ============================================================================
# _get_diff_threshold - env var reader for 4K H.265 codec noise
# ============================================================================


class TestGetDiffThreshold:
    """_get_diff_threshold reads DIFF_THRESHOLD at call time.

    Default 25 worked for 1080p H.264 but on 4K H.265 I-frame block
    artifacts create per-pixel noise up to ~80/255, which floods the
    diff mask. Raise via env to filter codec noise.
    """

    def test_default_when_env_unset(self, monkeypatch):
        monkeypatch.delenv("DIFF_THRESHOLD", raising=False)
        assert _get_diff_threshold() == DEFAULT_DIFF_THRESHOLD

    def test_reads_env_value(self, monkeypatch):
        monkeypatch.setenv("DIFF_THRESHOLD", "80")
        assert _get_diff_threshold() == 80

    def test_clamps_to_254_max(self, monkeypatch):
        monkeypatch.setenv("DIFF_THRESHOLD", "999")
        assert _get_diff_threshold() == 254

    def test_clamps_to_1_min(self, monkeypatch):
        monkeypatch.setenv("DIFF_THRESHOLD", "0")
        assert _get_diff_threshold() == 1

    def test_clamps_negative_to_1(self, monkeypatch):
        monkeypatch.setenv("DIFF_THRESHOLD", "-10")
        assert _get_diff_threshold() == 1

    def test_invalid_string_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("DIFF_THRESHOLD", "not_a_number")
        assert _get_diff_threshold() == DEFAULT_DIFF_THRESHOLD

    def test_strips_whitespace(self, monkeypatch):
        monkeypatch.setenv("DIFF_THRESHOLD", "  120  ")
        assert _get_diff_threshold() == 120


# ============================================================================
# _get_dilate_px - env var reader for FILTER_DILATE_PX
# ============================================================================


class TestGetDilatePx:
    """_get_dilate_px reads FARMSV_FILTER_DILATE_PX at call time.

    Default 12 px (tuned for 1080p; 4K H.265 may need higher).
    """

    def test_default_when_env_unset(self, monkeypatch):
        from infra.frame_diff import FILTER_DILATE_PX

        monkeypatch.delenv("FARMSV_FILTER_DILATE_PX", raising=False)
        assert _get_dilate_px() == FILTER_DILATE_PX

    def test_reads_env_value(self, monkeypatch):
        monkeypatch.setenv("FARMSV_FILTER_DILATE_PX", "24")
        assert _get_dilate_px() == 24

    def test_invalid_falls_back_to_default(self, monkeypatch):
        from infra.frame_diff import FILTER_DILATE_PX

        monkeypatch.setenv("FARMSV_FILTER_DILATE_PX", "not_a_number")
        assert _get_dilate_px() == FILTER_DILATE_PX

    def test_strips_whitespace(self, monkeypatch):
        monkeypatch.setenv("FARMSV_FILTER_DILATE_PX", "  30  ")
        assert _get_dilate_px() == 30


# ============================================================================
# _filter_components_and_dilate - env tunable dilation
# ============================================================================


class TestFilterEnvDilatePx:
    """_filter_components_and_dilate respects FARMSV_FILTER_DILATE_PX."""

    def test_env_override_affects_output_size(self, monkeypatch):
        """FARMSV_FILTER_DILATE_PX=24 produces larger output than default=12."""
        from infra.frame_diff import _filter_components_and_dilate

        mask = np.zeros((1296, 1920), dtype=np.uint8)
        mask[600:650, 900:950] = 255  # 50x50 box

        # Default env (12px)
        monkeypatch.delenv("FARMSV_FILTER_DILATE_PX", raising=False)
        result_default = _filter_components_and_dilate(mask)

        # Override env (24px)
        monkeypatch.setenv("FARMSV_FILTER_DILATE_PX", "24")
        result_24 = _filter_components_and_dilate(mask)

        assert result_24.sum() > result_default.sum()

    def test_env_zero_kills_dilation(self, monkeypatch):
        """FARMSV_FILTER_DILATE_PX=0 skips dilation entirely."""
        from infra.frame_diff import _filter_components_and_dilate

        mask = np.zeros((1296, 1920), dtype=np.uint8)
        mask[600:650, 900:950] = 255

        monkeypatch.setenv("FARMSV_FILTER_DILATE_PX", "0")
        result = _filter_components_and_dilate(mask)

        # Without dilation the box should be exactly 50x50 = 2500 px
        assert result.sum() == 50 * 50 * 255


# ============================================================================
# _get_min_area_pct - per-cam override
# ============================================================================


class TestGetMinAreaPctPerCam:
    """_get_min_area_pct supports per-cam override via FARMSV_{CAM_ID}_MIN_AREA_PCT."""

    def test_per_cam_override(self, monkeypatch):
        """FARMSV_OUTSIDE_FRONT_SOLAR_MIN_AREA_PCT=0.0001 returns 0.0001."""
        monkeypatch.delenv("MIN_AREA_PCT", raising=False)
        monkeypatch.setenv("FARMSV_OUTSIDE_FRONT_SOLAR_MIN_AREA_PCT", "0.0001")
        assert _get_min_area_pct("OUTSIDE_FRONT_SOLAR") == 0.0001

    def test_per_cam_takes_precedence_over_global(self, monkeypatch):
        """Per-cam value overrides global MIN_AREA_PCT."""
        # Remove per-cam overrides from .env that would interfere.
        for cam in ("BACK", "FRONT", "OUTSIDE_FRONT_GARAGE",
                    "OUTSIDE_FRONT_POWER", "OUTSIDE_BACK_SOLAR"):
            monkeypatch.delenv(f"FARMSV_{cam}_MIN_AREA_PCT", raising=False)
        monkeypatch.setenv("MIN_AREA_PCT", "0.001")
        monkeypatch.setenv("FARMSV_OUTSIDE_FRONT_SOLAR_MIN_AREA_PCT", "0.0001")
        assert _get_min_area_pct("OUTSIDE_FRONT_SOLAR") == 0.0001
        # Non-matching cam_id should still see global
        assert _get_min_area_pct("BACK") == 0.001

    def test_per_cam_fallback_to_default_when_unset(self, monkeypatch):
        """When neither per-cam nor global is set, returns DEFAULT."""
        monkeypatch.delenv("MIN_AREA_PCT", raising=False)
        monkeypatch.delenv("FARMSV_OUTSIDE_FRONT_SOLAR_MIN_AREA_PCT", raising=False)
        assert _get_min_area_pct("OUTSIDE_FRONT_SOLAR") == DEFAULT_MIN_AREA_PCT

    def test_per_cam_invalid_falls_back_to_global(self, monkeypatch):
        """Invalid per-cam value falls through to global MIN_AREA_PCT."""
        monkeypatch.setenv("MIN_AREA_PCT", "0.002")
        monkeypatch.setenv("FARMSV_OUTSIDE_FRONT_SOLAR_MIN_AREA_PCT", "not_a_float")
        assert _get_min_area_pct("OUTSIDE_FRONT_SOLAR") == 0.002

    def test_per_cam_invalid_falls_back_to_default_when_no_global(self, monkeypatch):
        """Invalid per-cam + no global -> DEFAULT_MIN_AREA_PCT."""
        monkeypatch.delenv("MIN_AREA_PCT", raising=False)
        monkeypatch.setenv("FARMSV_OUTSIDE_FRONT_SOLAR_MIN_AREA_PCT", "bad")
        assert _get_min_area_pct("OUTSIDE_FRONT_SOLAR") == DEFAULT_MIN_AREA_PCT

    def test_none_cam_id_uses_global(self, monkeypatch):
        """cam_id=None skips per-cam lookup, uses global."""
        monkeypatch.setenv("MIN_AREA_PCT", "0.003")
        assert _get_min_area_pct(None) == 0.003
        assert _get_min_area_pct() == 0.003

    def test_empty_cam_id_uses_global(self, monkeypatch):
        """cam_id='' skips per-cam lookup (truthiness check)."""
        monkeypatch.setenv("MIN_AREA_PCT", "0.004")
        assert _get_min_area_pct("") == 0.004
