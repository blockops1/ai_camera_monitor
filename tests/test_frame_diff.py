"""Tests for frame_diff bbox padding — ACs for US-031a1."""

from __future__ import annotations

import random
import unittest

from infra.frame_diff import (
    DEFAULT_BBOX_PAD_PCT,
    _expand_bbox_pct,
    subject_bbox_from_two_masks,
)


class TestPadPct(unittest.TestCase):
    """AC-6: 10%-per-side pct padding + round-up-to-multiple-of-32."""

    # ------------------------------------------------------------------
    def test_expand_bbox_pct_10_default(self) -> None:
        """AC-6a: bbox=(100,200,50,80) in 2304x1296 -> (93,192,64,96).

        Math:
          raw_w = round(50 * 1.2) = 60  -> ceil to 64
          raw_h = round(80 * 1.2) = 96  -> already mult of 32
          center = (125, 240)
          new_x = round(125 - 64/2) = round(93.0) = 93
          new_y = round(240 - 96/2) = round(192.0) = 192
        """
        result = _expand_bbox_pct((100, 200, 50, 80), DEFAULT_BBOX_PAD_PCT, 2304, 1296)
        self.assertEqual(result, (93, 192, 64, 96))

    # ------------------------------------------------------------------
    def test_expand_bbox_pct_clamps_to_image(self) -> None:
        """AC-6b: bbox near far corner clamps to frame bounds.

        bbox=(2200,1200,100,90) in 2304x1296:
          raw_w = round(100 * 1.2) = 120  -> ceil to 128
          raw_h = round(90 * 1.2)  = 108  -> ceil to 128
          center = (2250, 1245)
          new_x = round(2250 - 64)  = 2186  -> clamp to max(0, 2304-128) = 2176
          new_y = round(1245 - 64)  = 1181  -> clamp to max(0, 1296-128) = 1168
        Result: (2176, 1168, 128, 128) — both dims clamp.
        """
        result = _expand_bbox_pct((2200, 1200, 100, 90), DEFAULT_BBOX_PAD_PCT, 2304, 1296)
        x, y, w, h = result
        # Must be in-frame
        self.assertGreaterEqual(x, 0)
        self.assertGreaterEqual(y, 0)
        self.assertGreaterEqual(w, 0)
        self.assertGreaterEqual(h, 0)
        self.assertGreaterEqual(2304, x + w)
        self.assertGreaterEqual(1296, y + h)
        # Width/height must be multiples of 32
        self.assertEqual(w % 32, 0)
        self.assertEqual(h % 32, 0)

    # ------------------------------------------------------------------
    def test_expand_bbox_pct_is_multiple_of_32(self) -> None:
        """AC-6c: 10 seeded bboxes produce multiples of 32 dimensions.

        Seeded so the test is deterministic across runs.
        """
        rng = random.Random(42)
        frame_w, frame_h = 2304, 1296
        for _ in range(10):
            bx = rng.randint(0, frame_w - 10)
            by = rng.randint(0, frame_h - 10)
            bw = rng.randint(10, frame_w - bx - 10)
            bh = rng.randint(10, frame_h - by - 10)
            result = _expand_bbox_pct((bx, by, bw, bh), DEFAULT_BBOX_PAD_PCT, frame_w, frame_h)
            self.assertEqual(result[2] % 32, 0, f"width {result[2]} not mult of 32 for bbox {(bx,by,bw,bh)}")
            self.assertEqual(result[3] % 32, 0, f"height {result[3]} not mult of 32 for bbox {(bx,by,bw,bh)}")
            # Verify in-frame
            self.assertGreaterEqual(result[0], 0)
            self.assertGreaterEqual(result[1], 0)
            self.assertLessEqual(result[0] + result[2], frame_w)
            self.assertLessEqual(result[1] + result[3], frame_h)

    # ------------------------------------------------------------------
    def test_subject_bbox_uses_default_pct(self) -> None:
        """AC-6d: regression — subject_bbox_from_two_masks returns
        pct-padded-and-rounded bbox (dims are multiples of 32)."""
        # Create two binary masks (320x240) with a 64x48 blob near center.
        mask_a = numpy_mask(320, 240, center=(160, 120), size=(64, 48))
        mask_b = numpy_mask(320, 240, center=(160, 120), size=(64, 48))
        result = subject_bbox_from_two_masks(mask_a, mask_b)
        self.assertIsNotNone(result, "subject_bbox_from_two_masks returned None")
        if result is None:
            self.fail("unreachable")
        _x, _y, w, h = result
        self.assertEqual(w % 32, 0, f"width {w} not mult of 32")
        self.assertEqual(h % 32, 0, f"height {h} not mult of 32")


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------

import numpy as np


def numpy_mask(
    width: int,
    height: int,
    center: tuple[int, int] | None = None,
    size: tuple[int, int] = (100, 80),
) -> np.ndarray:
    """Return a uint8 binary mask (H×W) with a white rectangle in the centre."""
    mask = np.zeros((height, width), dtype=np.uint8)
    cx, cy = center or (width // 2, height // 2)
    sw, sh = size
    x0 = max(0, cx - sw // 2)
    y0 = max(0, cy - sh // 2)
    x1 = min(width, cx + sw // 2)
    y1 = min(height, cy + sh // 2)
    mask[y0:y1, x0:x1] = 255
    return mask


if __name__ == "__main__":
    unittest.main()
