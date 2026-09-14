"""test_motion_visualization.py — Tests for infra/motion_visualization.py.

Verifies the six-layer composite pipeline (median background, cumulative
pairwise diff union, morphology filter, red paint, green bbox outlines,
PNG save) and the render_motion_composite public API.
"""

from __future__ import annotations

import os
import tempfile

import numpy as np
import pytest
from PIL import Image

from infra.motion_visualization import _draw_rectangle, render_motion_composite

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _solid_rgb(w: int, h: int, r: int, g: int, b: int) -> Image.Image:
    """Return a solid-color PIL.Image (RGB) of size (w, h)."""
    arr = np.full((h, w, 3), (r, g, b), dtype=np.uint8)
    return Image.fromarray(arr, mode="RGB")


def _frame_with_scarce_motion(
    w: int, h: int,
    move_x: int = 2, move_y: int = 0,
) -> list[Image.Image]:
    """4-frame burst with motion in frame 2→3 (a bright rectangle moves).

    Frames 1,2: uniform dark background.
    Frame 3: a bright rectangle appears.
    Frame 4: same bright rectangle shifted.

    The rectangle is 30x30 px (900 area) so it survives min_blob_area=500.
    """
    bg = _solid_rgb(w, h, 10, 10, 10)  # dark grey
    bright = _solid_rgb(w, h, 250, 250, 250)  # bright

    f1 = bg.copy()
    f2 = bg.copy()
    # Frame 3: bright rectangle 30x30 at (100, 80)
    rect_area = bright.crop((100, 80, 130, 110))
    f3 = bg.copy()
    f3.paste(rect_area, (100, 80))
    # Frame 4: bright rectangle shifted right
    f4 = bg.copy()
    f4.paste(rect_area, (100 + move_x, 80 + move_y))

    return [f1, f2, f3, f4]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestRenderMotionComposite:
    """Core render_motion_composite integration tests."""

    def test_render_with_two_frames(self):
        """Two frames (wrong count) should raise ValueError."""
        frames = [
            _solid_rgb(320, 240, 50, 100, 150),
            _solid_rgb(320, 240, 50, 100, 150),
        ]
        with pytest.raises(ValueError, match="expects 4 frames"):  # noqa: SIM117
            with tempfile.TemporaryDirectory() as tmpdir:
                render_motion_composite(frames, output_dir=tmpdir)

    def test_render_with_no_diff_returns_median(self):
        """Four identical frames should produce a composite that is the median
        (i.e. the same colour) and write composite.png to disk."""
        frames = [
            _solid_rgb(160, 120, 42, 64, 86),
            _solid_rgb(160, 120, 42, 64, 86),
            _solid_rgb(160, 120, 42, 64, 86),
            _solid_rgb(160, 120, 42, 64, 86),
        ]
        with tempfile.TemporaryDirectory() as tmpdir:
            result = render_motion_composite(frames, output_dir=tmpdir)
            assert result.endswith("composite.png"), f"Expected .png, got {result}"
            assert os.path.isfile(result), f"Output file not found: {result}"

            # Read back and verify the median background colour.
            arr = np.asarray(Image.open(result).convert("RGB"))
            px = arr[60, 80]  # type: ignore[index]
            assert int(px[0]) == 42
            assert int(px[1]) == 64
            assert int(px[2]) == 86

    def test_render_bbox_outline_visible(self):
        """When bbox_a is provided, a green rectangle should be drawn on
        the composite.  We verify by checking that a pixel inside the
        bbox rect on the composite is green-ish (not the background colour)."""
        frames = _frame_with_scarce_motion(200, 150, move_x=2, move_y=0)
        # bbox in (x0, y0, w, h) format — covers the 30x30 moving region
        bbox_a = (100, 78, 30, 32)  # (x0, y0, w, h)
        bbox_b = None

        with tempfile.TemporaryDirectory() as tmpdir:
            result = render_motion_composite(
                frames,
                bbox_a=bbox_a,
                bbox_b=bbox_b,
                output_dir=tmpdir,
                diff_threshold=40,
                min_blob_area=500,
            )
            assert result.endswith("composite.png")
            assert os.path.isfile(result)

            arr = np.asarray(Image.open(result).convert("RGB"))
            # The bbox outline is green.  Check a pixel on the top edge
            # of the bbox rect.  Corners are (100,78) and (130,110).
            # Top edge: (115, 78) should be near green.
            # The red overlay may be present too, so check it's not
            # the same as the background (10,10,10).
            px = arr[78, 115]  # type: ignore[index]
            # Background is (10,10,10); green outline is (0,255,0) in RGB.
            # Even with some red bleed, G should dominate.
            assert int(px[1]) > 100, (
                f"Expected green outline pixel but got {px} at (115,78)"
            )

    def test_render_layer_mask_order(self):
        """Verify the rendering order is: median bg → red overlay → green outlines.

        We construct 4 frames with a moving bright rectangle and verify that:
        (a) the red overlay colour is present where motion was detected
        (b) the green outline colour is present at the bbox perimeter
        (c) the background colour is present in motion-free regions.
        """
        frames = _frame_with_scarce_motion(200, 150, move_x=2, move_y=0)

        # The moving bright rect is 30x30 px, starts at (100, 80).
        # bbox_a covers it: (x0, y0, w, h) = (100, 78, 30, 32) → corners (100,78) to (130,110).
        bbox_a = (100, 78, 30, 32)
        bbox_b = None

        with tempfile.TemporaryDirectory() as tmpdir:
            result = render_motion_composite(
                frames,
                bbox_a=bbox_a,
                bbox_b=bbox_b,
                output_dir=tmpdir,
            )
            assert result.endswith("composite.png")
            assert os.path.isfile(result)

            arr = np.asarray(Image.open(result).convert("RGB"))

            # (c) Background region (top-left corner, far from motion):
            # Should be close to dark grey (10,10,10).
            bg_px = arr[10, 10]  # type: ignore[index]
            assert int(bg_px[0]) < 30 and int(bg_px[1]) < 30 and int(bg_px[2]) < 30, (
                f"Background pixel should be dark but got {bg_px}"
            )

            # (a) Red overlay region — near the motion area.
            # The diff mask will have non-zero pixels where the bright
            # rectangle moved.  Red overlay (~55% opacity red on bg)
            # means R > G and R > B.
            motion_px = arr[95, 115]  # type: ignore[index]
            assert int(motion_px[0]) > int(motion_px[1]), (
                f"Expected red-dominant pixel in motion zone but got {motion_px}"
            )

            # (b) Green outline region — top edge of bbox.
            # Green outline pixel should have G > R and G > B.
            outline_px = arr[78, 115]  # type: ignore[index]
            assert int(outline_px[1]) > int(outline_px[0]) and int(outline_px[1]) > int(outline_px[2]), (
                f"Expected green-dominant outline pixel but got {outline_px}"
            )


class TestRenderMotionCompositeErrors:
    """Edge-case and error-path tests."""

    def test_wrong_frame_count_raises(self):
        """More or fewer than 4 frames should raise ValueError."""
        frames = [_solid_rgb(100, 100, 1, 2, 3)]
        with pytest.raises(ValueError, match="expects 4 frames"):  # noqa: SIM117
            with tempfile.TemporaryDirectory() as tmpdir:
                render_motion_composite(frames, output_dir=tmpdir)

    def test_empty_output_dir_returns_empty(self):
        """An empty or missing output_dir should return '' without writing."""
        frames = [_solid_rgb(100, 100, 1, 2, 3) for _ in range(4)]
        result = render_motion_composite(frames, output_dir="", diff_threshold=40)
        assert result == ""

    def test_output_is_png_at_native_resolution(self):
        """Output image dimensions must match the input frame size."""
        w, h = 640, 480
        frames = [_solid_rgb(w, h, 100, 150, 200) for _ in range(4)]
        with tempfile.TemporaryDirectory() as tmpdir:
            result = render_motion_composite(frames, output_dir=tmpdir)
            img = Image.open(result)
            assert img.size == (w, h), (
                f"Expected {w}x{h}, got {img.size}"
            )
            assert img.format == "PNG", f"Expected PNG format, got {img.format}"


class TestDrawRectangleBoundsClamp:
    """Defense-in-depth bounds-check in _draw_rectangle (US-040b)."""

    def test_draw_rectangle_clamps_to_frame_bounds(self):
        """Out-of-range coords should be clamped to frame dimensions instead
        of raising IndexError.

        Large negative/positive coords must clamp so a rectangle is drawn
        within the valid frame bounds.
        """
        arr = np.zeros((100, 200, 3), dtype=np.uint8)
        # x0=-50 clamps to 0, y0=-50 → 0, x1=9999 → 199, y1=9999 → 99
        # This should draw a green rectangle covering the entire frame.
        _draw_rectangle(arr, x0=-50, y0=-50, x1=9999, y1=9999, thickness=2)
        assert arr.sum() > 0, "Expected green pixels after clamping out-of-range coords"

    def test_draw_rectangle_no_write_on_fully_out_of_range(self):
        """Fully out-of-range coords must produce no writes (sum == 0)."""
        arr = np.zeros((100, 200, 3), dtype=np.uint8)
        # x0=x1=y0=y1=9999 → entirely outside frame → early-exit.
        _draw_rectangle(
            arr, x0=9999, y0=9999, x1=9999, y1=9999, thickness=2
        )
        assert arr.sum() == 0, "Expected no writes when coords are fully out of frame"
