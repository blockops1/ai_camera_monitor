"""test_roi_exclusion_zones - unit tests for US-112a ROI exclusion zones.

Tests for the ROI masking feature added to:
  - apply_roi_exclusions()
  - _get_roi_masking_enabled()
  - _load_roi_zones_for_cam()
  - compute_cross_diff_bboxes() (ROI masking integration)

Acceptance criteria:
  - pytest tests/test_roi_exclusion_zones.py -v passes
  - ruff check tests/test_roi_exclusion_zones.py = 0 errors
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

# Ensure infra/ is importable (not from the package __init__ which may pull
# modules that require env vars or GPU).
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from infra.frame_diff import (
    _load_roi_zones_for_cam,
    apply_roi_exclusions,
    compute_cross_diff_bboxes,
)

# ============================================================================
# Fixtures
# ============================================================================


def _make_frame(h: int = 1440, w: int = 2560) -> np.ndarray:
    """Return a uniform-gray uint8 frame."""
    return np.full((h, w), 128, dtype=np.uint8)


def _write_frames(
    base_dir: Path,
    f1: np.ndarray,
    f2: np.ndarray,
    f3: np.ndarray,
    f4: np.ndarray,
) -> list[str]:
    """Write 4 frame PNGs and return their paths."""
    base_dir.mkdir(parents=True, exist_ok=True)
    from PIL import Image

    paths = []
    for i, frame in enumerate([f1, f2, f3, f4], start=1):
        p = base_dir / f"frame_{i:03d}.png"
        Image.fromarray(frame, mode="L").save(str(p))
        paths.append(str(p))
    return sorted(paths)


# ============================================================================
# apply_roi_exclusions - core masking logic
# ============================================================================


class TestApplyRoiExclusions:
    """apply_roi_exclusions zeros out ROI rect regions in a mask."""

    def test_single_rect_zeroes_region(self):
        """A rect at (1920,0,640,1440) zeros right-side area."""
        mask = np.full((1440, 2560), 255, np.uint8)
        result = apply_roi_exclusions(
            mask, [{"x": 1920, "y": 0, "w": 640, "h": 1440}], 2560, 1440
        )
        # Inside ROI region: (700, 2200) -> should be zeroed
        assert result[700, 2200] == 0, "Pixel inside ROI rect should be 0"
        # Outside ROI region: (700, 100) -> should be 255
        assert result[700, 100] == 255, "Pixel outside ROI rect should be 255"

    def test_empty_rects_no_change(self):
        """Empty rects list -> mask unchanged."""
        mask = np.full((1440, 2560), 255, np.uint8)
        result = apply_roi_exclusions(mask, [], 2560, 1440)
        assert result.sum() == mask.sum()

    def test_proportional_scaling(self):
        """Rects scale proportionally to smaller dimensions."""
        # Smaller frame: 1280x720 (scale_x=1280/2560=0.5, scale_y=720/1440=0.5)
        mask = np.full((720, 1280), 255, np.uint8)
        result = apply_roi_exclusions(
            mask, [{"x": 1920, "y": 0, "w": 640, "h": 1440}], 1280, 720
        )
        # Scaled rect: x=1920*0.5=960, y=0*0.5=0, w=640*0.5=320, h=1440*0.5=720
        # (100, 1000) -> should be zeroed
        assert result[100, 1000] == 0, "Pixel inside scaled ROI rect should be 0"
        # (100, 500) -> outside ROI -> should be 255
        assert result[100, 500] == 255, "Pixel outside scaled ROI rect should be 255"

    def test_modifies_in_place(self):
        """apply_roi_exclusions modifies the input array, returns same reference."""
        mask = np.full((1440, 2560), 255, np.uint8)
        result = apply_roi_exclusions(
            mask, [{"x": 1920, "y": 0, "w": 640, "h": 1440}], 2560, 1440
        )
        assert result is mask


# ============================================================================
# _get_roi_masking_enabled - env var reader
# ============================================================================


class TestGetRoiMaskingEnabled:
    """_get_roi_masking_enabled reads FARMSURV_ROI_MASKING at call time."""

    def test_default_is_on(self, monkeypatch):
        """No env var -> returns True (default ON)."""
        monkeypatch.delenv("FARMSURV_ROI_MASKING", raising=False)
        from infra.frame_diff import _get_roi_masking_enabled as get_roi

        assert get_roi() is True

    def test_env_1_is_on(self, monkeypatch):
        """FARMSURV_ROI_MASKING=1 -> True."""
        monkeypatch.setenv("FARMSURV_ROI_MASKING", "1")
        from infra.frame_diff import _get_roi_masking_enabled as get_roi

        assert get_roi() is True

    def test_env_0_is_off(self, monkeypatch):
        """FARMSURV_ROI_MASKING=0 -> False."""
        monkeypatch.setenv("FARMSURV_ROI_MASKING", "0")
        from infra.frame_diff import _get_roi_masking_enabled as get_roi

        assert get_roi() is False

    def test_env_off_is_off(self, monkeypatch):
        """FARMSURV_ROI_MASKING=off -> False."""
        monkeypatch.setenv("FARMSURV_ROI_MASKING", "off")
        from infra.frame_diff import _get_roi_masking_enabled as get_roi

        assert get_roi() is False

    def test_env_false_is_off(self, monkeypatch):
        """FARMSURV_ROI_MASKING=false -> False."""
        monkeypatch.setenv("FARMSURV_ROI_MASKING", "false")
        from infra.frame_diff import _get_roi_masking_enabled as get_roi

        assert get_roi() is False

    def test_env_empty_is_off(self, monkeypatch):
        """FARMSURV_ROI_MASKING='' -> False."""
        monkeypatch.setenv("FARMSURV_ROI_MASKING", "")
        from infra.frame_diff import _get_roi_masking_enabled as get_roi

        assert get_roi() is False


# ============================================================================
# _load_roi_zones_for_cam - config loader
# ============================================================================


class TestLoadRoiZonesForCam:
    """_load_roi_zones_for_cam reads roi_exclusion_zones from thresholds config."""

    def test_returns_zones_for_ofp(self):
        """OUTSIDE_FRONT_POWER -> returns zone list."""
        zones = _load_roi_zones_for_cam("OUTSIDE_FRONT_POWER")
        assert isinstance(zones, list)
        assert len(zones) == 1
        assert zones[0]["x"] == 1920
        assert zones[0]["y"] == 0
        assert zones[0]["w"] == 640
        assert zones[0]["h"] == 1440

    def test_none_for_cam_without_zones(self):
        """Camera without roi_exclusion_zones -> None."""
        zones = _load_roi_zones_for_cam("OUTSIDE_FRONT_GARAGE")
        assert zones is None

    def test_none_for_unknown_cam(self):
        """Unknown camera -> None."""
        zones = _load_roi_zones_for_cam("UNKNOWN_CAM")
        assert zones is None

    def test_none_for_none_cam_id(self):
        """None cam_id -> None."""
        zones = _load_roi_zones_for_cam(None)
        assert zones is None

    def test_none_for_empty_string(self):
        """Empty string cam_id -> None."""
        zones = _load_roi_zones_for_cam("")
        assert zones is None


# ============================================================================
# Integration: compute_cross_diff_bboxes with ROI masking
# ============================================================================


class TestComputeCrossDiffWithRoi:
    """compute_cross_diff_bboxes respects ROI exclusion zones."""

    def test_flare_in_zone_masked(self, tmp_path, monkeypatch):
        """Flare blob entirely inside OFP exclusion zone -> (None, None)."""
        monkeypatch.setenv("FARMSURV_ROI_MASKING", "1")
        f1 = _make_frame()
        f2 = _make_frame()
        f3 = _make_frame()
        f4 = _make_frame()

        # Bright flare blob at right edge (inside x>=1920 zone)
        # Frame 2: blob appears
        f2[500:700, 2000:2400] = 255
        # Frame 3: same blob (persistent — survives AND)
        f3[500:700, 2000:2400] = 255

        paths = _write_frames(tmp_path / "flare_in_zone", f1, f2, f3, f4)
        bbox_a, bbox_b = compute_cross_diff_bboxes(
            paths[0], paths[1], paths[2], paths[3], cam_id="OUTSIDE_FRONT_POWER"
        )
        assert bbox_a is None, f"Flare in exclusion zone should not produce bbox_a: {bbox_a}"
        assert bbox_b is None, f"Flare in exclusion zone should not produce bbox_b: {bbox_b}"

    def test_motion_outside_zone_produces_bbox(self, tmp_path, monkeypatch):
        """Same blob at x<1920 -> bbox still produced (zone does not leak)."""
        monkeypatch.setenv("FARMSURV_ROI_MASKING", "1")
        f1 = _make_frame()
        f2 = _make_frame()
        f3 = _make_frame()
        f4 = _make_frame()

        # Motion in the LEFT half (x < 1920) — should NOT be masked.
        # Pattern from OSD test: f2 has blob at (500-800, 500-800),
        # f3 has blob at (500-800, 800-1100). Overlap at (500-800, 500-800)
        # survives AND -> bbox produced.
        f2[500:800, 500:800] = 255
        f3[500:800, 800:1100] = 255

        paths = _write_frames(tmp_path / "motion_left", f1, f2, f3, f4)
        bbox_a, bbox_b = compute_cross_diff_bboxes(
            paths[0], paths[1], paths[2], paths[3], cam_id="OUTSIDE_FRONT_POWER"
        )
        assert bbox_a is not None, "Motion outside zone should produce bbox_a"
        assert bbox_b is not None, "Motion outside zone should produce bbox_b"
        # bbox_a centered around (650, 650) -> entirely < 1920
        assert bbox_a[0] + bbox_a[2] < 1920, f"bbox_a x+w={bbox_a[0]+bbox_a[2]} should be < 1920"

    def test_roi_masking_off_passthrough(self, tmp_path, monkeypatch):
        """FARMSURV_ROI_MASKING=0 -> blob in zone still produces bbox."""
        monkeypatch.setenv("FARMSURV_ROI_MASKING", "0")
        f1 = _make_frame()
        f2 = _make_frame()
        f3 = _make_frame()
        f4 = _make_frame()

        # Blob inside OFP zone coords with motion
        # f2 has blob at (500-800, 2000-2200), f3 has blob at (500-800, 2200-2400).
        # Overlap at (500-800, 2000-2200) survives AND -> bbox produced.
        f2[500:800, 2000:2200] = 255
        f3[500:800, 2200:2400] = 255

        paths = _write_frames(tmp_path / "masking_off", f1, f2, f3, f4)
        bbox_a, bbox_b = compute_cross_diff_bboxes(
            paths[0], paths[1], paths[2], paths[3], cam_id="OUTSIDE_FRONT_POWER"
        )
        assert bbox_a is not None, "ROI masking off should produce bbox_a"
        assert bbox_b is not None, "ROI masking off should produce bbox_b"

    def test_cam_without_zones_produces_bbox(self, tmp_path, monkeypatch):
        """Cam without zones key (e.g. OFG) -> blob in OFP-zone coords still produces bbox."""
        monkeypatch.setenv("FARMSURV_ROI_MASKING", "1")
        f1 = _make_frame()
        f2 = _make_frame()
        f3 = _make_frame()
        f4 = _make_frame()

        # Blob at OFP zone coords with motion
        f2[500:800, 2000:2200] = 255
        f3[500:800, 2200:2400] = 255

        paths = _write_frames(tmp_path / "no_zones_cam", f1, f2, f3, f4)
        bbox_a, bbox_b = compute_cross_diff_bboxes(
            paths[0], paths[1], paths[2], paths[3], cam_id="OUTSIDE_FRONT_GARAGE"
        )
        assert bbox_a is not None, "Cam without zones should produce bbox_a"
        assert bbox_b is not None, "Cam without zones should produce bbox_b"

    def test_rect_scaling_applied(self, tmp_path, monkeypatch):
        """Zone defined in 2560x1440 ref applied to 1280x720 mask zeroes scaled region."""
        monkeypatch.setenv("FARMSURV_ROI_MASKING", "1")
        h, w = 720, 1280
        f1 = np.full((h, w), 128, dtype=np.uint8)
        f2 = np.full((h, w), 128, dtype=np.uint8)
        f3 = np.full((h, w), 128, dtype=np.uint8)
        f4 = np.full((h, w), 128, dtype=np.uint8)

        # Blob in right half of 720p (which maps to ROI zone in 2560x1440)
        # Scaled zone: x=1920*0.5=960, w=640*0.5=320
        # Blob at (1000:1200, 100:300) should be zeroed
        f2[100:300, 1000:1200] = 255
        f3[100:300, 1000:1200] = 255

        paths = _write_frames(tmp_path / "scaling_test", f1, f2, f3, f4)
        bbox_a, bbox_b = compute_cross_diff_bboxes(
            paths[0], paths[1], paths[2], paths[3], cam_id="OUTSIDE_FRONT_POWER"
        )
        assert bbox_a is None, f"Scaling test: blob in ROI zone should not produce bbox_a: {bbox_a}"
        assert bbox_b is None, f"Scaling test: blob in ROI zone should not produce bbox_b: {bbox_b}"
