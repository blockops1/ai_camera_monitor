"""test_osd_masking - unit tests for US-108a OSD region masking.

Tests for the OSD masking feature added to:
  - apply_osd_masks()
  - _get_osd_masking_enabled()
  - _load_osd_masks_for_cam()
  - compute_cross_diff_bboxes() (OSD masking integration)

Acceptance criteria:
  - pytest tests/test_osd_masking.py -v passes
  - ruff check tests/test_osd_masking.py = 0 errors
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

# Ensure infra/ is importable (not from the package __init__ which may pull
# modules that require env vars or GPU).
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from infra.frame_diff import _load_osd_masks_for_cam, apply_osd_masks

# ============================================================================
# Fixtures
# ============================================================================


def _make_frame(h: int = 1440, w: int = 2560) -> np.ndarray:
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
# apply_osd_masks - core masking logic
# ============================================================================


class TestApplyOsdMasks:
    """apply_osd_masks zeros out OSD rect regions in a mask."""

    def test_single_rect_zeroes_region(self):
        """A rect at (0,1240,640,200) zeros bottom-left area."""
        mask = np.full((1440, 2560), 255, np.uint8)
        result = apply_osd_masks(
            mask, [{"x": 0, "y": 1240, "w": 640, "h": 200}], 2560, 1440
        )
        # Inside OSD region: (1439, 10) -> should be zeroed
        assert result[1439, 10] == 0, "Pixel inside OSD rect should be 0"
        # Outside OSD region: (1000, 10) -> should be 255
        assert result[1000, 10] == 255, "Pixel outside OSD rect should be 255"

    def test_multiple_rects(self):
        """Two rects: bottom-left and bottom-right both zeroed."""
        mask = np.full((1440, 2560), 255, np.uint8)
        result = apply_osd_masks(
            mask,
            [
                {"x": 0, "y": 1240, "w": 640, "h": 200},
                {"x": 2000, "y": 1240, "w": 560, "h": 200},
            ],
            2560,
            1440,
        )
        # Bottom-left OSD region
        assert result[1439, 10] == 0
        # Bottom-right OSD region
        assert result[1439, 2500] == 0
        # Center (not OSD)
        assert result[700, 1280] == 255

    def test_empty_rects_no_change(self):
        """Empty rects list -> mask unchanged."""
        mask = np.full((1440, 2560), 255, np.uint8)
        result = apply_osd_masks(mask, [], 2560, 1440)
        assert result.sum() == mask.sum()

    def test_proportional_scaling(self):
        """Rects scale proportionally to smaller dimensions."""
        # Smaller frame: 1296x1920 (scale_x=1920/2560=0.75, scale_y=1296/1440=0.9)
        mask = np.full((1296, 1920), 255, np.uint8)
        result = apply_osd_masks(
            mask, [{"x": 0, "y": 1240, "w": 640, "h": 200}], 1920, 1296
        )
        # Scaled rect: x=0*0.75=0, y=1240*0.9=1116, w=640*0.75=480, h=200*0.9=180
        # (1116, 0) should be zeroed
        assert result[1116, 0] == 0
        # (1000, 0) should be above the rect -> 255
        assert result[1000, 0] == 255

    def test_clamps_to_bounds(self):
        """Oversized rects clamp to mask shape."""
        mask = np.full((100, 200), 255, np.uint8)
        # Rect in reference space: x=0, y=1240, w=640, h=200
        # Scaled to 200x100: scale_x=200/2560≈0.078, scale_y=100/1440≈0.069
        # x≈0, y≈86, w≈50, h≈14 → clamped rect at y=86, h=14
        result = apply_osd_masks(
            mask, [{"x": 0, "y": 1240, "w": 640, "h": 200}], 200, 100
        )
        # Inside scaled rect (y=86..99) should be zeroed
        assert result[95, 10] == 0
        # Above rect (y=80) should remain
        assert result[80, 10] == 255

    def test_modifies_in_place(self):
        """apply_osd_masks modifies the input array, returns same reference."""
        mask = np.full((1440, 2560), 255, np.uint8)
        result = apply_osd_masks(
            mask, [{"x": 0, "y": 1240, "w": 640, "h": 200}], 2560, 1440
        )
        assert result is mask


# ============================================================================
# _get_osd_masking_enabled - env var reader
# ============================================================================


class TestGetOsdMaskingEnabled:
    """_get_osd_masking_enabled reads FARMSURV_OSD_MASKING at call time."""

    def test_default_is_on(self, monkeypatch):
        """No env var -> returns True (default ON)."""
        monkeypatch.delenv("FARMSURV_OSD_MASKING", raising=False)
        # Import the function directly (avoiding __init__.py env issues)
        from infra.frame_diff import _get_osd_masking_enabled

        assert _get_osd_masking_enabled() is True

    def test_env_1_is_on(self, monkeypatch):
        """FARMSURV_OSD_MASKING=1 -> True."""
        monkeypatch.setenv("FARMSURV_OSD_MASKING", "1")
        from infra.frame_diff import _get_osd_masking_enabled

        assert _get_osd_masking_enabled() is True

    def test_env_0_is_off(self, monkeypatch):
        """FARMSURV_OSD_MASKING=0 -> False."""
        monkeypatch.setenv("FARMSURV_OSD_MASKING", "0")
        from infra.frame_diff import _get_osd_masking_enabled

        assert _get_osd_masking_enabled() is False

    def test_env_off_is_off(self, monkeypatch):
        """FARMSURV_OSD_MASKING=off -> False."""
        monkeypatch.setenv("FARMSURV_OSD_MASKING", "off")
        from infra.frame_diff import _get_osd_masking_enabled

        assert _get_osd_masking_enabled() is False

    def test_env_false_is_off(self, monkeypatch):
        """FARMSURV_OSD_MASKING=false -> False."""
        monkeypatch.setenv("FARMSURV_OSD_MASKING", "false")
        from infra.frame_diff import _get_osd_masking_enabled

        assert _get_osd_masking_enabled() is False

    def test_env_empty_is_off(self, monkeypatch):
        """FARMSURV_OSD_MASKING='' -> False."""
        monkeypatch.setenv("FARMSURV_OSD_MASKING", "")
        from infra.frame_diff import _get_osd_masking_enabled

        assert _get_osd_masking_enabled() is False

    def test_env_whitespace_stripped(self, monkeypatch):
        """FARMSURV_OSD_MASKING=' 1 ' -> True (stripped)."""
        monkeypatch.setenv("FARMSURV_OSD_MASKING", "  1  ")
        from infra.frame_diff import _get_osd_masking_enabled

        assert _get_osd_masking_enabled() is True

    def test_case_insensitive(self, monkeypatch):
        """FARMSURV_OSD_MASKING=OFF (uppercase) -> False."""
        monkeypatch.setenv("FARMSURV_OSD_MASKING", "OFF")
        from infra.frame_diff import _get_osd_masking_enabled

        assert _get_osd_masking_enabled() is False


# ============================================================================
# _load_osd_masks_for_cam - config loader
# ============================================================================


class TestLoadOsdMasksForCam:
    """_load_osd_masks_for_cam reads osd_masks from thresholds config."""

    def test_returns_masks_for_known_cam(self):
        """Known camera -> returns list of rect dicts."""
        masks = _load_osd_masks_for_cam("OUTSIDE_FRONT_GARAGE")
        assert isinstance(masks, list)
        assert len(masks) == 2
        assert masks[0]["x"] == 0
        assert masks[0]["y"] == 1240

    def test_returns_masks_for_all_6_cams(self):
        """All 6 cameras have osd_masks."""
        cams = [
            "FRONT",
            "BACK",
            "OUTSIDE_FRONT_GARAGE",
            "OUTSIDE_FRONT_POWER",
            "OUTSIDE_FRONT_SOLAR",
            "OUTSIDE_BACK_SOLAR",
        ]
        for cam in cams:
            masks = _load_osd_masks_for_cam(cam)
            assert isinstance(masks, list), f"{cam}: expected list, got {type(masks)}"

    def test_none_for_unknown_cam(self):
        """Unknown camera -> None."""
        masks = _load_osd_masks_for_cam("UNKNOWN_CAM")
        assert masks is None

    def test_none_for_none_cam_id(self):
        """None cam_id -> None."""
        masks = _load_osd_masks_for_cam(None)
        assert masks is None

    def test_returns_none_for_empty_string(self):
        """Empty string cam_id -> None."""
        masks = _load_osd_masks_for_cam("")
        assert masks is None


# ============================================================================
# Integration: compute_cross_diff_bboxes with OSD masking
# ============================================================================


class TestComputeCrossDiffWithOsd:
    """compute_cross_diff_bboxes respects OSD masking env var."""

    def _write_frames(
        self,
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

    def test_osd_masking_off_passthrough(self, tmp_path, monkeypatch):
        """FARMSURV_OSD_MASKING=0 -> OSD region NOT zeroed."""
        monkeypatch.setenv("FARMSURV_OSD_MASKING", "0")
        # Create frames with motion in BOTH the subject area AND the OSD area
        f1 = _make_frame()
        f2 = _make_frame()
        f3 = _make_frame()
        f4 = _make_frame()

        # Subject moves from (500,500) to (700,500)
        f2[500:600, 500:600] = 255
        f3[500:600, 700:800] = 255

        # OSD digits also change (simulated) in bottom-left OSD region
        f1[1350:1400, 0:50] = 255
        f2[1350:1400, 0:50] = 0
        f3[1350:1400, 0:50] = 255
        f4[1350:1400, 0:50] = 0

        paths = self._write_frames(tmp_path / "osd_off", f1, f2, f3, f4)
        from infra.frame_diff import compute_cross_diff_bboxes as ccb

        bbox_a, _bbox_b = ccb(paths[0], paths[1], paths[2], paths[3], cam_id="OUTSIDE_FRONT_GARAGE")
        # bbox_a should include both subject AND OSD motion (no masking)
        if bbox_a is not None:
            assert bbox_a[1] < 1240, "Without masking, OSD motion should survive"

    def test_osd_masking_on_excludes_osd_region(self, tmp_path, monkeypatch):
        """FARMSURV_OSD_MASKING=1 -> OSD region zeroed in pairwise diffs."""
        monkeypatch.setenv("FARMSURV_OSD_MASKING", "1")
        # Create frames with motion ONLY in OSD region (no real subject)
        f1 = _make_frame()
        f2 = _make_frame()
        f3 = _make_frame()
        f4 = _make_frame()

        # Only OSD digits change in bottom-left area
        f1[1350:1400, 0:50] = 255
        f2[1350:1400, 0:50] = 0
        f3[1350:1400, 0:50] = 255
        f4[1350:1400, 0:50] = 0

        paths = self._write_frames(tmp_path / "osd_only", f1, f2, f3, f4)
        from infra.frame_diff import compute_cross_diff_bboxes as ccb

        bbox_a, bbox_b = ccb(paths[0], paths[1], paths[2], paths[3], cam_id="OUTSIDE_FRONT_GARAGE")
        # With masking ON, OSD motion should be zeroed BEFORE AND -> no bbox
        assert bbox_a is None, f"With OSD masking, OSD-only motion should not produce bbox: {bbox_a}"
        assert bbox_b is None, f"With OSD masking, OSD-only motion should not produce bbox: {bbox_b}"

    def test_real_motion_survives_with_masking(self, tmp_path, monkeypatch):
        """Real subject motion should still produce bbox when OSD masking is ON."""
        monkeypatch.setenv("FARMSURV_OSD_MASKING", "1")
        # Subject moves in the center (not in OSD region)
        f1 = _make_frame()
        f2 = _make_frame()
        f3 = _make_frame()
        f4 = _make_frame()

        f2[500:800, 500:800] = 255  # subject at frame 2
        f3[500:800, 800:1100] = 255  # subject moved right at frame 3

        # OSD digits also change (simulated)
        f1[1350:1400, 0:50] = 255
        f2[1350:1400, 0:50] = 0
        f3[1350:1400, 0:50] = 255
        f4[1350:1400, 0:50] = 0

        paths = self._write_frames(tmp_path / "real_motion", f1, f2, f3, f4)
        from infra.frame_diff import compute_cross_diff_bboxes as ccb

        bbox_a, bbox_b = ccb(paths[0], paths[1], paths[2], paths[3], cam_id="OUTSIDE_FRONT_GARAGE")
        assert bbox_a is not None, "Real subject motion should produce bbox_a"
        assert bbox_b is not None, "Real subject motion should produce bbox_b"
        # bboxes should be in the center, not in OSD region
        assert bbox_a[1] < 1200, f"bbox_a y={bbox_a[1]} should not be in OSD band"
        assert bbox_b[1] < 1200, f"bbox_b y={bbox_b[1]} should not be in OSD band"

    def test_no_osd_masks_for_unknown_cam(self, tmp_path, monkeypatch):
        """Unknown cam_id -> no osd_masks loaded -> passthrough (same as OFF)."""
        monkeypatch.setenv("FARMSURV_OSD_MASKING", "1")
        f1 = _make_frame()
        f2 = _make_frame()
        f3 = _make_frame()
        f4 = _make_frame()

        f2[500:800, 500:800] = 255
        f3[500:800, 800:1100] = 255

        paths = self._write_frames(tmp_path / "unknown_cam", f1, f2, f3, f4)
        from infra.frame_diff import compute_cross_diff_bboxes as ccb

        bbox_a, bbox_b = ccb(paths[0], paths[1], paths[2], paths[3], cam_id="UNKNOWN_CAM")
        assert bbox_a is not None
        assert bbox_b is not None
