"""
Tests for infra/image_prep.py — frame image processing (crop; downscale REMOVED §11.88).

§11.88 (2026-09-01): downscale_for_qwen is now a PASS-THROUGH returning
frame_path unchanged. Tests for the old downscale+JPEG behavior are deleted.
New tests cover the pass-through contract. crop_face_region_from_4k tests
remain but default small_size changed from (1280, 720) to NATIVE_RES, and
output format changed from JPEG to PNG.

Covers:
    - downscale_for_qwen: §11.88 pass-through (returns frame_path unchanged,
      ignores output_dir, doesn't write files)
    - crop_face_region_from_4k: bbox center conversion, edge clamping,
      square crop enforcement, raises on invalid bbox, edge shift logic
      (PNG output, NATIVE_RES default)
    - Constants: NATIVE_RES / INSIGHTFACE_CROP_SIZE values

All tests create real PNGs via PIL (we test PIL integration directly — no mocking).
"""

import os
import tempfile

import pytest
from PIL import Image

from infra.image_prep import (
    INSIGHTFACE_CROP_SIZE,
    NATIVE_RES,
    crop_face_region_from_4k,
    downscale_for_qwen,
)

# ---------------------------------------------------------------------------
# Fixtures: create real PNGs of known dimensions (3840×2160 = "4K-ish" source,
# 2304×1296 = Reolink native which the new default operates on).
# ---------------------------------------------------------------------------


@pytest.fixture
def high_res_frame():
    """Create a large PNG and yield its path; clean up after."""
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        path = f.name
    img = Image.new("RGB", (3840, 2160), color=(128, 128, 128))
    img.save(path, format="PNG")
    try:
        yield path
    finally:
        os.unlink(path)


@pytest.fixture
def native_res_frame():
    """Create a 2304x1296 PNG (Reolink native) and yield its path; clean up after."""
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        path = f.name
    img = Image.new("RGB", NATIVE_RES, color=(64, 64, 64))
    img.save(path, format="PNG")
    try:
        yield path
    finally:
        os.unlink(path)


@pytest.fixture
def output_dir():
    """Yield a temp output dir; clean up after."""
    with tempfile.TemporaryDirectory() as d:
        yield d


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


class TestConstants:
    """Verify the documented module-level constants."""

    def test_native_res(self):
        # Reolink REOLINK_MODEL native — §11.88 sends this to Qwen directly.
        assert NATIVE_RES == (2304, 1296)

    def test_insightface_crop_size(self):
        # InsightFace resizes internally to 640x640.
        assert INSIGHTFACE_CROP_SIZE == (640, 640)


# ---------------------------------------------------------------------------
# downscale_for_qwen (§11.88 PASS-THROUGH)
# ---------------------------------------------------------------------------


class TestDownscaleForQwen:
    """§11.88 — downscale_for_qwen is a pass-through. Qwen now sees native res."""

    def test_passthrough_returns_input_path(self, native_res_frame):
        """When called with a valid frame_path, returns it unchanged."""
        result = downscale_for_qwen(native_res_frame)
        assert result == native_res_frame

    def test_passthrough_ignores_output_dir(self, native_res_frame, output_dir):
        """The output_dir kwarg is accepted but ignored — no file is written there."""
        result = downscale_for_qwen(native_res_frame, output_dir=output_dir)
        assert result == native_res_frame
        # No files should have been written to output_dir.
        assert os.listdir(output_dir) == []

    def test_passthrough_does_not_resize(self, native_res_frame):
        """Pass-through does NOT modify the frame on disk — bytes unchanged."""
        before = open(native_res_frame, "rb").read()
        _ = downscale_for_qwen(native_res_frame)
        after = open(native_res_frame, "rb").read()
        assert before == after


# ---------------------------------------------------------------------------
# crop_face_region_from_4k
# ---------------------------------------------------------------------------


class TestCropFaceRegionFrom4k:
    """Verify bbox → crop coordinate conversion + edge clamping (PNG output, NATIVE_RES default)."""

    def test_basic_crop_at_center(self, high_res_frame, output_dir):
        # bbox centered in the 2304×1296 space → relative center (0.5, 0.5)
        # → crop centered in 3840×2160 → (1920, 1080) center → 640×640 crop.
        # Use bbox centered in 3840x2160 (small_size default == NATIVE_RES).
        # Crop coords in NATIVE_RES space: center (1152, 648), 200x40 bbox.
        bbox = [1052, 628, 1252, 668]
        result = crop_face_region_from_4k(
            high_res_frame,
            bbox_small=bbox,
            output_dir=output_dir,
        )
        assert os.path.exists(result)
        with Image.open(result) as img:
            assert img.size == (640, 640)
            assert img.format == "PNG"

    def test_crop_offset_top_left(self, high_res_frame, output_dir):
        # bbox at (0, 0) in NATIVE_RES → relative center (0, 0)
        # → crop should hit top-left edge and shift back to (0, 0).
        bbox = [0, 0, 100, 100]
        result = crop_face_region_from_4k(
            high_res_frame,
            bbox_small=bbox,
            output_dir=output_dir,
        )
        assert os.path.exists(result)
        with Image.open(result) as img:
            # Crop at (0, 0) should produce a full 640x640 region from
            # the top-left of the source image.
            assert img.size == (640, 640)

    def test_crop_offset_bottom_right(self, high_res_frame, output_dir):
        # bbox at NATIVE_RES in NATIVE_RES → relative center (1, 1)
        # → crop should hit bottom-right edge and shift back.
        bbox = [2204, 1196, 2304, 1296]
        result = crop_face_region_from_4k(
            high_res_frame,
            bbox_small=bbox,
            output_dir=output_dir,
        )
        assert os.path.exists(result)
        with Image.open(result) as img:
            assert img.size == (640, 640)

    def test_relative_center_at_thirds(self, high_res_frame, output_dir):
        # bbox centered at 1/3 width, 2/3 height of NATIVE_RES (2304x1296)
        # → relative (0.333, 0.667)
        # → 4K center (1280, 1440) → crop box (960, 1120, 1600, 1760)
        bbox = [768, 862, 769, 863]  # tiny bbox at (768.5, 862.5)
        result = crop_face_region_from_4k(
            high_res_frame,
            bbox_small=bbox,
            output_dir=output_dir,
        )
        with Image.open(result) as img:
            assert img.size == (640, 640)

    def test_invalid_bbox_length_raises(self, high_res_frame, output_dir):
        # bbox must be 4 numbers.
        with pytest.raises(ValueError, match="bbox_small must be"):
            crop_face_region_from_4k(high_res_frame, [1, 2, 3], output_dir=output_dir)
        with pytest.raises(ValueError, match="bbox_small must be"):
            crop_face_region_from_4k(high_res_frame, [1, 2, 3, 4, 5], output_dir=output_dir)

    def test_non_square_crop_size_raises(self, high_res_frame, output_dir):
        # crop_size must be square.
        bbox = [640, 360, 740, 460]
        with pytest.raises(ValueError, match="crop_size must be square"):
            crop_face_region_from_4k(
                high_res_frame,
                bbox_small=bbox,
                crop_size=(640, 480),  # not square
                output_dir=output_dir,
            )

    def test_custom_crop_size(self, high_res_frame, output_dir):
        # crop_size override works.
        bbox = [640, 360, 740, 460]
        result = crop_face_region_from_4k(
            high_res_frame,
            bbox_small=bbox,
            crop_size=(320, 320),
            output_dir=output_dir,
        )
        with Image.open(result) as img:
            assert img.size == (320, 320)

    def test_output_filename_has_face_marker(self, high_res_frame, output_dir):
        bbox = [640, 360, 740, 460]
        result = crop_face_region_from_4k(
            high_res_frame, bbox, output_dir=output_dir
        )
        basename = os.path.basename(result)
        # §11.88 — output is now PNG: e.g. crop_<basename>_face.png.
        # Must include "face" marker in the basename.
        assert "face" in basename
        # §11.88 — extension is .png not .jpg.
        assert basename.endswith(".png")

    def test_output_is_png_lossless(self, high_res_frame, output_dir):
        # §11.88 — crops are written as PNG (lossless), NOT JPEG q95.
        bbox = [640, 360, 740, 460]
        result = crop_face_region_from_4k(high_res_frame, bbox, output_dir=output_dir)
        with Image.open(result) as img:
            assert img.format == "PNG"
            assert img.size == (640, 640)

    def test_auto_tempdir_when_output_dir_is_none(self, high_res_frame):
        # When output_dir=None, a /tmp subdir is created and used.
        bbox = [640, 360, 740, 460]
        result = crop_face_region_from_4k(high_res_frame, bbox)
        assert os.path.exists(result)
        try:
            os.unlink(result)
            os.rmdir(os.path.dirname(result))
        except OSError:
            pass
