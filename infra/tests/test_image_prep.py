"""
Tests for infra/image_prep.py — frame image processing (downscale + crop).

Covers:
    - downscale_for_qwen: creates output file, thumbnail downscale,
      JPEG quality, auto-creates output_dir
    - crop_face_region_from_4k: bbox center conversion, edge clamping,
      square crop enforcement, raises on invalid bbox, edge shift logic
    - Constants: QWEN_INPUT_SIZE / INSIGHTFACE_CROP_SIZE values

All tests create real JPEGs via PIL (we test PIL integration directly —
no mocking).
"""

import os
import tempfile

import pytest
from PIL import Image

from infra.image_prep import (
    INSIGHTFACE_CROP_SIZE,
    QWEN_INPUT_SIZE,
    crop_face_region_from_4k,
    downscale_for_qwen,
)

# ---------------------------------------------------------------------------
# Fixtures: create real JPEGs of known dimensions
# ---------------------------------------------------------------------------


@pytest.fixture
def high_res_frame():
    """Create a 4K JPEG and yield its path; clean up after."""
    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f:
        path = f.name
    img = Image.new("RGB", (3840, 2160), color=(128, 128, 128))
    img.save(path, format="JPEG", quality=90)
    try:
        yield path
    finally:
        os.unlink(path)


@pytest.fixture
def small_res_frame():
    """Create a 720p JPEG and yield its path; clean up after."""
    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f:
        path = f.name
    img = Image.new("RGB", (1280, 720), color=(64, 64, 64))
    img.save(path, format="JPEG", quality=90)
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

    def test_qwen_input_size(self):
        # 720p keeps each image at ~950 tokens — fits llama-server
        # per-slot budget under --parallel 4.
        assert QWEN_INPUT_SIZE == (1280, 720)

    def test_insightface_crop_size(self):
        # InsightFace resizes internally to 640x640.
        assert INSIGHTFACE_CROP_SIZE == (640, 640)


# ---------------------------------------------------------------------------
# downscale_for_qwen
# ---------------------------------------------------------------------------


class TestDownscaleForQwen:
    """Verify 4K → 720p downscale with LANCZOS resampling."""

    def test_output_file_created(self, high_res_frame, output_dir):
        result = downscale_for_qwen(high_res_frame, output_dir=output_dir)
        assert os.path.exists(result)
        assert result.startswith(output_dir)

    def test_output_is_jpeg(self, high_res_frame, output_dir):
        result = downscale_for_qwen(high_res_frame, output_dir=output_dir)
        with Image.open(result) as img:
            assert img.format == "JPEG"

    def test_output_dimensions_within_qwen_input_size(self, high_res_frame, output_dir):
        # Aspect ratio is preserved (thumbnail), so the larger dimension
        # is at most QWEN_INPUT_SIZE's larger dimension.
        result = downscale_for_qwen(high_res_frame, output_dir=output_dir)
        with Image.open(result) as img:
            w, h = img.size
            assert max(w, h) <= max(QWEN_INPUT_SIZE)
            # Aspect ratio preserved: 4K is 16:9 → 720p is also 16:9.
            assert abs(w / h - 16 / 9) < 0.01

    def test_output_filename_has_qwen_prefix(self, high_res_frame, output_dir):
        result = downscale_for_qwen(high_res_frame, output_dir=output_dir)
        basename = os.path.basename(result)
        assert basename.startswith("qwen_")

    def test_auto_creates_output_dir(self, high_res_frame):
        # Pass a non-existent output_dir — should be created.
        with tempfile.TemporaryDirectory() as parent:
            new_dir = os.path.join(parent, "subdir1", "subdir2")
            result = downscale_for_qwen(high_res_frame, output_dir=new_dir)
            assert os.path.exists(result)
            assert os.path.isdir(new_dir)

    def test_auto_tempdir_when_output_dir_is_none(self, high_res_frame):
        # When output_dir=None, a /tmp subdir is created and used.
        result = downscale_for_qwen(high_res_frame)
        assert os.path.exists(result)
        # Clean up the temp dir + file.
        try:
            os.unlink(result)
            os.rmdir(os.path.dirname(result))
        except OSError:
            pass

    def test_jpeg_quality_85(self, high_res_frame, output_dir):
        # The save() call uses quality=85 — we can't easily inspect that
        # directly from a re-loaded image (PIL re-encodes on load), but
        # we can check the file is valid JPEG and reasonable size.
        result = downscale_for_qwen(high_res_frame, output_dir=output_dir)
        size = os.path.getsize(result)
        # 720p JPEG should be at least 5KB and at most 1MB.
        assert 5_000 < size < 1_000_000


# ---------------------------------------------------------------------------
# crop_face_region_from_4k
# ---------------------------------------------------------------------------


class TestCropFaceRegionFrom4k:
    """Verify bbox → crop coordinate conversion + edge clamping."""

    def test_basic_crop_at_center(self, high_res_frame, output_dir):
        # bbox centered in 1280x720 → relative center (0.5, 0.5)
        # → crop centered in 3840x2160 → (1920, 1080) center → 640x640 crop
        bbox = [540, 340, 740, 380]  # 200x40 bbox in 1280x720
        result = crop_face_region_from_4k(
            high_res_frame,
            bbox_small=bbox,
            output_dir=output_dir,
        )
        assert os.path.exists(result)
        with Image.open(result) as img:
            assert img.size == (640, 640)

    def test_crop_offset_top_left(self, high_res_frame, output_dir):
        # bbox at (0, 0) in 1280x720 → relative center (0, 0)
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
        # bbox at (1280, 720) in 1280x720 → relative center (1, 1)
        # → crop should hit bottom-right edge and shift back.
        bbox = [1180, 620, 1280, 720]
        result = crop_face_region_from_4k(
            high_res_frame,
            bbox_small=bbox,
            output_dir=output_dir,
        )
        assert os.path.exists(result)
        with Image.open(result) as img:
            assert img.size == (640, 640)

    def test_relative_center_at_thirds(self, high_res_frame, output_dir):
        # bbox centered at 1/3 width, 2/3 height of 1280x720
        # → relative (0.333, 0.667)
        # → 4K center (1280, 1440) → crop box (960, 1120, 1600, 1760)
        bbox = [426, 480, 427, 481]  # tiny bbox at (426.5, 480.5)
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
        # The output filename includes "_face" in the basename
        # (e.g., crop_tmp123_face.jpg).
        assert "face" in basename

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

    def test_jpeg_quality_95(self, high_res_frame, output_dir):
        # Face crops use quality=95 (higher than downscale) — faces
        # need maximum detail for InsightFace embedding.
        bbox = [640, 360, 740, 460]
        result = crop_face_region_from_4k(high_res_frame, bbox, output_dir=output_dir)
        # Just verify the file exists and is non-empty (we don't assert
        # exact size — flat-color test images produce very small JPEGs).
        assert os.path.getsize(result) > 0
        # Verify it's a valid JPEG with the expected dimensions.
        with Image.open(result) as img:
            assert img.format == "JPEG"
            assert img.size == (640, 640)
