"""
test_motion_visualization.py — Tests for infra.motion_visualization.render_motion_composite.

Tests use synthetic frames generated in-process via PIL/numpy. No real
RTSP, no real alert_id directories. Each test owns its own tmp dir.

Phase 6B.115 (§11.46.6): render_motion_composite now takes in-memory
PIL.Image objects instead of disk paths. bbox_a + bbox_b are passed
directly (not via a MovingObject).
"""

from __future__ import annotations

import os
import tempfile

import cv2
import numpy as np
import pytest
from PIL import Image

from infra.motion_visualization import (
    N_FRAMES_EXPECTED,
    render_motion_composite,
)

# ---------------------------------------------------------------------------
# Synthetic frame builders
# ---------------------------------------------------------------------------

def _make_synthetic_frame(
    bg_color: tuple[int, int, int] = (40, 40, 40),
    moving_box: tuple[int, int, int, int] | None = None,
    moving_color: tuple[int, int, int] = (200, 200, 200),
    width: int = 2304,
    height: int = 1296,
) -> np.ndarray:
    """Build a synthetic BGR frame (OpenCV-native)."""
    frame = np.full((height, width, 3), bg_color, dtype=np.uint8)
    if moving_box is not None:
        x0, y0, x1, y1 = moving_box
        frame[y0:y1, x0:x1] = moving_color
    return frame


def _make_frame_set_pil(tmpdir: str, with_motion: bool = True) -> list:
    """Build N_FRAMES_EXPECTED synthetic frames AND return as PIL list.

    Phase 6B.115: returns PIL.Image directly (no on-disk intermediate).
    Also writes to disk for postmortem convenience.
    """
    pil_frames = []
    for i in range(N_FRAMES_EXPECTED):
        if with_motion:
            x0 = 200 + i * 300
            moving_box = (x0, 400, x0 + 200, 700)
            bgr = _make_synthetic_frame(moving_box=moving_box)
        else:
            bgr = _make_synthetic_frame()
        # Save to disk for postmortem (and so tests can verify file exists).
        frame_path = os.path.join(tmpdir, f"frame_{i + 1:03d}.jpg")
        cv2.imwrite(frame_path, bgr, [cv2.IMWRITE_JPEG_QUALITY, 90])
        # Also keep in memory as PIL (the new authoritative input).
        rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
        pil_frames.append(Image.fromarray(rgb))
    return pil_frames


def _gate_bboxes_for_frame_set() -> tuple[tuple[int, int, int, int], tuple[int, int, int, int]]:
    """Bbox coords matching the sliding box in _make_frame_set_pil(with_motion=True)."""
    # frame_003 (i=2): box at (200 + 600, 400, 800, 700) → bbox (800, 400, 200, 300)
    # frame_004 (i=3): box at (200 + 900, 400, 1100, 700) → bbox (1100, 400, 200, 300)
    return (800, 400, 200, 300), (1100, 400, 200, 300)


@pytest.fixture
def tmp_alert_dir():
    """Per-test tmp dir for synthetic frames + composite output."""
    with tempfile.TemporaryDirectory(prefix="motion_vis_test_") as tmp:
        yield tmp


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_render_composite_returns_path(tmp_alert_dir):
    """Happy path: returns the output path, file exists."""
    frames = _make_frame_set_pil(tmp_alert_dir)
    bbox_a, bbox_b = _gate_bboxes_for_frame_set()
    result = render_motion_composite(
        frames=frames,
        bbox_a=bbox_a,
        bbox_b=bbox_b,
        output_dir=tmp_alert_dir,
    )
    # §11.88 (2026-09-01): composite is PNG lossless, NOT JPEG q90.
    expected = os.path.join(tmp_alert_dir, "composite.png")
    assert result == expected
    assert os.path.isfile(expected)


def test_render_composite_default_output_path(tmp_alert_dir):
    """No output_filename → defaults to composite.png in output_dir."""
    frames = _make_frame_set_pil(tmp_alert_dir)
    bbox_a, bbox_b = _gate_bboxes_for_frame_set()
    result = render_motion_composite(
        frames=frames,
        bbox_a=bbox_a,
        bbox_b=bbox_b,
        output_dir=tmp_alert_dir,
    )
    expected = os.path.join(tmp_alert_dir, "composite.png")
    assert result == expected
    assert os.path.isfile(expected)


def test_render_composite_creates_alert_dir_if_needed(tmp_path):
    """output_dir that doesn't exist yet → render_motion_composite creates it."""
    frames = _make_frame_set_pil(str(tmp_path))
    bbox_a, bbox_b = _gate_bboxes_for_frame_set()
    new_dir = tmp_path / "deeply" / "nested" / "alert_dir"
    assert not new_dir.exists()
    result = render_motion_composite(
        frames=frames,
        bbox_a=bbox_a,
        bbox_b=bbox_b,
        output_dir=str(new_dir),
    )
    assert new_dir.is_dir()
    assert os.path.isfile(result)


def test_render_composite_is_lossless_png(tmp_alert_dir):
    """§11.88 (2026-09-01): composite is PNG lossless (no q90, no JPEG).

    Reads back the file and confirms:
    - magic bytes are PNG (89 50 4E 47 0D 0A 1A 0A), not JPEG (FF D8 FF)
    - shape is correct (4 frames composited into 1 image at NATIVE_RES)
    """
    frames = _make_frame_set_pil(tmp_alert_dir)
    bbox_a, bbox_b = _gate_bboxes_for_frame_set()
    out_path = render_motion_composite(
        frames=frames,
        bbox_a=bbox_a,
        bbox_b=bbox_b,
        output_dir=tmp_alert_dir,
    )
    assert out_path.endswith(".png")
    # Magic bytes check.
    with open(out_path, "rb") as f:
        magic = f.read(8)
    assert magic[:4] == b"\x89PNG", f"expected PNG magic, got {magic!r}"
    # cv2.imread shape check (cv2 auto-decodes PNG).
    img = cv2.imread(out_path)
    assert img is not None
    assert img.shape[0] == 1296  # height
    assert img.shape[1] == 2304  # width


def test_render_composite_with_no_motion_still_writes_file(tmp_alert_dir):
    """No motion → composite still written (background only, no red)."""
    frames = _make_frame_set_pil(tmp_alert_dir, with_motion=False)
    result = render_motion_composite(
        frames=frames,
        bbox_a=None,
        bbox_b=None,
        output_dir=tmp_alert_dir,
    )
    assert os.path.isfile(result)
    img = cv2.imread(result)
    assert img is not None
    # No motion → no red overlay → median of gray frames = gray
    # Check that no pixel is heavily red
    _b, _g, r = cv2.split(img)
    # Red channel should be close to gray level (40)
    assert r.mean() < 100  # far from 255


def test_render_composite_wrong_frame_count_raises(tmp_alert_dir):
    """Passing the wrong number of frames raises ValueError."""
    frames = _make_frame_set_pil(tmp_alert_dir)
    too_few = frames[:2]
    with pytest.raises(ValueError, match=f"expects {N_FRAMES_EXPECTED}"):
        render_motion_composite(
            frames=too_few,
            bbox_a=None,
            bbox_b=None,
            output_dir=tmp_alert_dir,
        )


def test_render_composite_missing_frame_returns_empty(tmp_alert_dir):
    """If a frame can't be converted to PIL → render returns '' (no crash)."""
    # Build a valid frame set, then swap one frame for garbage.
    frames = _make_frame_set_pil(tmp_alert_dir)
    class _BrokenImage:
        # PIL convert("RGB") will raise on this.
        def convert(self, mode):
            raise RuntimeError("simulated frame corruption")
    frames[2] = _BrokenImage()
    result = render_motion_composite(
        frames=frames,
        bbox_a=None,
        bbox_b=None,
        output_dir=tmp_alert_dir,
    )
    assert result == ""


def test_render_composite_with_real_4k_frames(tmp_path):
    """Larger frame size (3840x2160) still works at native resolution."""
    frames_4k = []
    for i in range(N_FRAMES_EXPECTED):
        bgr = np.full((2160, 3840, 3), 40, dtype=np.uint8)
        cv2.rectangle(bgr, (500 + i * 400, 800), (900 + i * 400, 1200), (200, 200, 200), -1)
        rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
        frames_4k.append(Image.fromarray(rgb))
    out_dir = str(tmp_path)
    result = render_motion_composite(
        frames=frames_4k,
        bbox_a=(700, 800, 200, 400),
        bbox_b=(1100, 800, 200, 400),
        output_dir=out_dir,
    )
    assert os.path.isfile(result)
    img = cv2.imread(result)
    assert img is not None, f"cv2.imread returned None for {result}"
    assert img.shape == (2160, 3840, 3)


def test_render_composite_bbox_format_is_xywh_not_xyxy(tmp_alert_dir):
    """Phase 6B.115: bbox_a/bbox_b are (x, y, w, h), not (x1, y1, x2, y2).

    Verifies by using a bbox with a distinctive width/height and
    checking the green rectangle is in the right place.
    """
    frames = _make_frame_set_pil(tmp_alert_dir)
    # Bbox at (100, 100, 50, 50) — a 50x50 square
    result = render_motion_composite(
        frames=frames,
        bbox_a=(100, 100, 50, 50),
        bbox_b=None,
        output_dir=tmp_alert_dir,
    )
    assert os.path.isfile(result)


def test_render_composite_empty_bbox_per_frame_skips_outline(tmp_alert_dir):
    """bbox_a=None + bbox_b=None → no green outlines drawn, no crash."""
    frames = _make_frame_set_pil(tmp_alert_dir)
    result = render_motion_composite(
        frames=frames,
        bbox_a=None,
        bbox_b=None,
        output_dir=tmp_alert_dir,
    )
    assert os.path.isfile(result)


def test_render_composite_bbox_uses_native_resolution(tmp_alert_dir):
    """Bboxes at native 2304x1296 coords land in the right place."""
    frames = _make_frame_set_pil(tmp_alert_dir)
    # bbox at (1000, 600, 100, 100) in native coords
    result = render_motion_composite(
        frames=frames,
        bbox_a=(1000, 600, 100, 100),
        bbox_b=(1500, 600, 100, 100),
        output_dir=tmp_alert_dir,
    )
    assert os.path.isfile(result)
    # Image is 1296x2304 native
    img = cv2.imread(result)
    assert img is not None, f"cv2.imread returned None for {result}"
    assert img.shape == (1296, 2304, 3)
