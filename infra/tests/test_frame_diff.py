"""Tests for infra/frame_diff.py — pairwise frame differencing for motion-gate.

Tests cover:
  - load_frame: returns None for missing file, returns grayscale ndarray for valid file
  - pairwise_diff: returns zero mask for identical frames, returns non-zero for changed
  - pairwise_diff: raises ValueError on shape mismatch
  - pairwise_diff: respects threshold parameter
  - bbox_from_mask: returns None for empty mask, None for mask below min_area
  - bbox_from_mask: returns correct bbox for synthetic mask with a known region
  - bbox_from_mask: padding is applied and clamped to image bounds
  - diff_pair_with_bbox: end-to-end on two synthetic frames with a moving square
  - diff_pair_with_bbox: returns (None, 0, ...) when frame_a is missing
  - crop_frame_to_bbox: saves a cropped file, returns its path, file exists on disk

These are unit tests with synthetic frames — no real Reolink data needed.
The motion_gate_pipeline end-to-end probe covers real-frame behavior.
"""

from __future__ import annotations

import sys
from pathlib import Path

import cv2
import numpy as np
import pytest

_root = Path(__file__).resolve().parent.parent.parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

from infra.frame_diff import (  # noqa: E402
    DEFAULT_BBOX_PADDING_PX,
    bbox_from_mask,
    crop_frame_to_bbox,
    diff_pair_with_bbox,
    load_frame,
    pairwise_diff,
)


def _save_synthetic_frame(path: Path, gray_array: np.ndarray) -> None:
    """Helper: save a uint8 grayscale ndarray as a JPEG."""
    assert gray_array.dtype == np.uint8
    assert gray_array.ndim == 2
    path.parent.mkdir(parents=True, exist_ok=True)
    ok = cv2.imwrite(str(path), gray_array, [cv2.IMWRITE_JPEG_QUALITY, 90])
    assert ok, f"cv2.imwrite failed for {path}"


def test_load_frame_missing_file_returns_none(tmp_path):
    result = load_frame(str(tmp_path / "does_not_exist.jpg"))
    assert result is None


def test_load_frame_returns_grayscale_ndarray(tmp_path):
    # 100x80 grayscale frame
    gray = np.full((80, 100), 128, dtype=np.uint8)
    path = tmp_path / "frame.jpg"
    _save_synthetic_frame(path, gray)

    result = load_frame(str(path))
    assert result is not None
    assert result.dtype == np.uint8
    assert result.ndim == 2
    assert result.shape == (80, 100)


def test_pairwise_diff_identical_frames_returns_zero_mask():
    gray = np.full((100, 100), 128, dtype=np.uint8)
    mask = pairwise_diff(gray, gray)
    assert mask.shape == gray.shape
    assert mask.dtype == np.uint8
    assert np.count_nonzero(mask) == 0


def test_pairwise_diff_detects_change():
    a = np.full((100, 100), 100, dtype=np.uint8)
    b = a.copy()
    # Change a 30x30 region in the middle from 100 to 200
    b[35:65, 35:65] = 200
    mask = pairwise_diff(a, b)
    # All pixels in the changed region should exceed default threshold (25)
    changed_in_region = np.count_nonzero(mask[35:65, 35:65])
    assert changed_in_region == 30 * 30
    # No change outside the region
    assert np.count_nonzero(mask[0:35, :]) == 0
    assert np.count_nonzero(mask[65:, :]) == 0


def test_pairwise_diff_respects_threshold():
    a = np.full((100, 100), 100, dtype=np.uint8)
    b = a.copy()
    # Small change: 100 -> 110 (delta=10, below default threshold 25)
    b[40:60, 40:60] = 110
    # Default threshold (25) should ignore this
    mask_default = pairwise_diff(a, b, threshold=25)
    assert np.count_nonzero(mask_default[40:60, 40:60]) == 0
    # Lower threshold (5) should pick it up
    mask_low = pairwise_diff(a, b, threshold=5)
    assert np.count_nonzero(mask_low[40:60, 40:60]) == 20 * 20


def test_pairwise_diff_shape_mismatch_raises():
    a = np.zeros((100, 100), dtype=np.uint8)
    b = np.zeros((100, 200), dtype=np.uint8)
    with pytest.raises(ValueError, match="shapes must match"):
        pairwise_diff(a, b)


def test_pairwise_diff_dtype_mismatch_raises():
    a = np.zeros((100, 100), dtype=np.uint8)
    b = np.zeros((100, 100), dtype=np.float32)
    with pytest.raises(ValueError, match="must be uint8"):
        pairwise_diff(a, b)


def test_bbox_from_mask_empty_mask_returns_none():
    mask = np.zeros((100, 100), dtype=np.uint8)
    assert bbox_from_mask(mask) is None


def test_bbox_from_mask_below_min_area_returns_none():
    mask = np.zeros((100, 100), dtype=np.uint8)
    # Tiny 5x5 blob — below default min_area of 64
    mask[40:45, 40:45] = 255
    assert bbox_from_mask(mask, min_area_px=64) is None


def test_bbox_from_mask_large_blob_returns_correct_bbox():
    mask = np.zeros((200, 200), dtype=np.uint8)
    # 50x40 blob at (30, 50)
    mask[50:90, 30:80] = 255
    bbox = bbox_from_mask(mask, min_area_px=64, padding_px=0)
    assert bbox is not None
    x, y, w, h = bbox
    assert x == 30
    assert y == 50
    assert w == 50
    assert h == 40


def test_bbox_from_mask_padding_applied_and_clamped():
    mask = np.zeros((100, 100), dtype=np.uint8)
    # 40x40 blob at (10, 10) — close to top-left corner
    mask[10:50, 10:50] = 255
    bbox = bbox_from_mask(mask, min_area_px=64, padding_px=16)
    assert bbox is not None, "test fixture has no bbox"
    x, y, w, h = bbox
    # Padded by 16 on each side: x=10-16=-6 → clamped to 0
    assert x == 0
    assert y == 0
    # Width grows by 32 (16 on each side): 40 + 32 = 72
    # Original right edge was x+w = 10+40 = 50; padded right = 50+16 = 66
    # But because x was clamped to 0, the bbox spans x=0 to x=0+72=72
    assert w == 72
    assert h == 72
    # Sanity: bbox stays within image bounds
    assert x + w <= mask.shape[1]
    assert y + h <= mask.shape[0]


def test_bbox_from_mask_largest_component_wins():
    mask = np.zeros((200, 200), dtype=np.uint8)
    # Small blob at top
    mask[10:20, 10:30] = 255  # 10x20 = 200
    # Large blob at center
    mask[80:140, 80:160] = 255  # 60x80 = 4800
    bbox = bbox_from_mask(mask, min_area_px=64, padding_px=0)
    assert bbox == (80, 80, 80, 60)


def test_diff_pair_with_bbox_end_to_end(tmp_path):
    """Two synthetic frames with a moving square should produce a bbox."""
    a = np.full((200, 200), 100, dtype=np.uint8)
    b = a.copy()
    # Move a 40x40 white square from (30, 30) to (80, 30)
    a[30:70, 30:70] = 200
    b[30:70, 80:120] = 200
    path_a = tmp_path / "frame_a.jpg"
    path_b = tmp_path / "frame_b.jpg"
    _save_synthetic_frame(path_a, a)
    _save_synthetic_frame(path_b, b)

    bbox, count, mask = diff_pair_with_bbox(str(path_a), str(path_b))
    assert bbox is not None
    # The diff union is (30-70, 30-70) ∪ (30-70, 80-120) = a wider region
    _x, y, _w, h = bbox
    assert y == 30 - DEFAULT_BBOX_PADDING_PX  # padded
    assert h == 40 + 2 * DEFAULT_BBOX_PADDING_PX
    assert count > 0
    assert mask.shape == a.shape


def test_diff_pair_with_bbox_missing_frame_returns_none(tmp_path):
    a = np.full((100, 100), 128, dtype=np.uint8)
    path_a = tmp_path / "frame_a.jpg"
    _save_synthetic_frame(path_a, a)
    path_b = tmp_path / "missing.jpg"  # doesn't exist

    bbox, count, _mask = diff_pair_with_bbox(str(path_a), str(path_b))
    assert bbox is None
    assert count == 0


def test_diff_pair_with_bbox_identical_frames_returns_none_bbox(tmp_path):
    gray = np.full((100, 100), 128, dtype=np.uint8)
    path_a = tmp_path / "frame_a.jpg"
    path_b = tmp_path / "frame_b.jpg"
    _save_synthetic_frame(path_a, gray)
    _save_synthetic_frame(path_b, gray)

    bbox, count, _mask = diff_pair_with_bbox(str(path_a), str(path_b))
    assert bbox is None
    assert count == 0


def test_crop_frame_to_bbox_saves_file(tmp_path):
    # 200x200 colored frame
    color = np.zeros((200, 200, 3), dtype=np.uint8)
    color[50:150, 50:150] = (0, 200, 0)  # green square in middle
    src = tmp_path / "frame.jpg"
    cv2.imwrite(str(src), color)

    cropped_path = crop_frame_to_bbox(str(src), (50, 50, 100, 100))
    assert cropped_path is not None
    assert Path(cropped_path).is_file()
    # Verify the crop file is loadable and has the expected dimensions
    loaded = cv2.imread(cropped_path)
    assert loaded is not None
    assert loaded.shape == (100, 100, 3)


def test_crop_frame_to_bbox_missing_source_returns_none(tmp_path):
    result = crop_frame_to_bbox(str(tmp_path / "missing.jpg"), (10, 10, 50, 50))
    assert result is None


def test_crop_frame_to_bbox_clamps_to_image_bounds(tmp_path):
    # 100x100 frame
    gray = np.zeros((100, 100, 3), dtype=np.uint8)
    src = tmp_path / "frame.jpg"
    cv2.imwrite(str(src), gray)
    # Bbox that exceeds bounds: (90, 90, 50, 50) should be clamped
    cropped = crop_frame_to_bbox(str(src), (90, 90, 50, 50))
    assert cropped is not None
    loaded = cv2.imread(cropped)
    assert loaded is not None
    # Bbox gets clamped to (90, 90, 10, 10)
    assert loaded.shape == (10, 10, 3)
