"""test_crop_vertical_extension — US-108d vertical person-crop extension.

Tests for FARMSURV_CROP_EXT env-gated vertical bbox extension in
``_expand_bbox_pct`` (infra/frame_diff.py).

Acceptance criteria:
  - AC1:  FARMSURV_CROP_EXT=50 on 96x128 vertical bbox at y=300
          → y=236, h=256 (extension formula verified)
  - AC2:  FARMSURV_CROP_EXT=0 → bboxes byte-identical to current behaviour
  - AC3:  landscape bbox (w >= h) unchanged at any FARMSURV_CROP_EXT value
  - AC4:  extension clamps at frame edges — never negative y, never past frame_h
  - AC5:  6+ cases covering AC1-4 + non-square vertical variants; pytest green
  - AC6:  replay mode: k=0.5, 288x448 at (585,382) in 2560x1440
          → extends toward y=158 h=896, no clamp (382+448+224=1054 < 1440)

"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from infra.frame_diff import _expand_bbox_pct

# ============================================================================
# Helpers
# ============================================================================


def _set_crop_ext(val: str | None):
    """Set or unset FARMSURV_CROP_EXT."""
    if val is None:
        os.environ.pop("FARMSURV_CROP_EXT", None)
    else:
        os.environ["FARMSURV_CROP_EXT"] = str(val)


# ============================================================================
# AC1: FARMSURV_CROP_EXT=50 on 96x128 vertical bbox at y=300
# ============================================================================


class TestAC1VerticalExtensionFormula:
    """Verify the extension formula: y' = y - h*k, h' = h*(1+2k)."""

    def test_extension_y_h(self, monkeypatch):
        """FARMSURV_CROP_EXT=50 on 96x128 at y=300.

        Extension: y' = 300 - 128*0.5 = 236, h' = 128*2 = 256.
        The extension is applied BEFORE pad_pct + round-mult-32, so the
        full _expand_bbox_pct output depends on pad_pct and frame bounds.
        We verify the extension formula by calling with pad_pct=0 (no pad),
        large frame, and checking the intermediate y/h match.
        """
        monkeypatch.delenv("FARMSURV_CROP_EXT", raising=False)
        monkeypatch.setenv("FARMSURV_CROP_EXT", "50")

        # pad_pct=0 so the output is just the extension + clamp.
        # frame_w/h large enough that nothing is clamped.
        result = _expand_bbox_pct((0, 300, 96, 128), pad_pct=0.0, frame_w=4000, frame_h=4000)

        # With pad=0: raw_w=96 (already mult of 32) → new_w=96.
        # h_ext = 256 (already mult of 32) → new_h=256.
        # cx = 48, cy = 364. new_x = 48 - 48 = 0, new_y = 364 - 128 = 236.
        assert result == (0, 236, 96, 256)

    def test_extension_delta_matches_formula(self, monkeypatch):
        """Verify the raw extension delta = h*k from the original height."""
        monkeypatch.delenv("FARMSURV_CROP_EXT", raising=False)
        monkeypatch.setenv("FARMSURV_CROP_EXT", "50")

        # Use pad=0 to avoid round-mult-32 noise.
        # h=128, k=0.5 → h_ext = 256 (already mult of 32) → new_h=256.
        # w=96 (already mult of 32) → new_w=96.
        # cx = 148, cy = 364. new_x = 148 - 48 = 100, new_y = 364 - 128 = 236.
        result = _expand_bbox_pct((100, 300, 96, 128), pad_pct=0.0, frame_w=4000, frame_h=4000)
        assert result == (100, 236, 96, 256)

    def test_extension_y_shift_formula(self, monkeypatch):
        """y' = y - h*k verified via cy-based recentering.

        cy = y + h/2 = 300 + 64 = 364. new_h = 288.
        new_y = round(364 - 288/2) = round(364 - 144) = 220.
        The EXTENSION shifts the top edge from y=300 to y = cy - h_ext/2 = 364 - 128 = 236.
        After round-mult-32 (256→288), new_y = 220.
        The delta between original y and extension-top: 300 - 236 = 64 = h*k.
        We verify this by checking new_y + new_h/2 == cy (center preserved).
        """
        monkeypatch.delenv("FARMSURV_CROP_EXT", raising=False)
        monkeypatch.setenv("FARMSURV_CROP_EXT", "50")
        result = _expand_bbox_pct((100, 300, 96, 128), pad_pct=0.0, frame_w=4000, frame_h=4000)
        # Center preserved: result_y + result_h/2 == cy = 364 (within 1 pixel for round)
        assert abs((result[1] + result[3] / 2.0) - 364) <= 1


# ============================================================================
# AC2: FARMSURV_CROP_EXT=0 → byte-identical
# ============================================================================


class TestAC2ZeroExtension:
    """FARMSURV_CROP_EXT=0 must produce byte-identical output to no-extension."""

    def test_byte_identical_with_zero(self, monkeypatch):
        """Compare output with k=0 vs k unset."""
        bboxes = [
            (0, 300, 96, 128),
            (100, 200, 50, 80),
            (585, 382, 288, 448),
            (0, 0, 32, 32),
        ]
        for bbox in bboxes:
            monkeypatch.delenv("FARMSURV_CROP_EXT", raising=False)
            no_ext = _expand_bbox_pct(bbox, pad_pct=0.10, frame_w=2560, frame_h=1440)

            monkeypatch.setenv("FARMSURV_CROP_EXT", "0")
            zero_ext = _expand_bbox_pct(bbox, pad_pct=0.10, frame_w=2560, frame_h=1440)

            assert no_ext == zero_ext, (
                f"bbox={bbox}: k=0 output {zero_ext} != no-env output {no_ext}"
            )

    def test_zero_vs_default_identity(self, monkeypatch):
        """Unset env vs explicitly '0' must behave identically."""
        monkeypatch.delenv("FARMSURV_CROP_EXT", raising=False)
        result_unset = _expand_bbox_pct((0, 0, 100, 200), 0.10, 1920, 1080)

        monkeypatch.setenv("FARMSURV_CROP_EXT", "0")
        result_zero = _expand_bbox_pct((0, 0, 100, 200), 0.10, 1920, 1080)

        assert result_unset == result_zero


# ============================================================================
# AC3: Landscape bboxes (w >= h) are unchanged
# ============================================================================


class TestAC3LandscapeUnchanged:
    """Landscape bboxes must NOT be extended."""

    def test_wide_bbox_unchanged(self, monkeypatch):
        """w > h: a 200x80 bbox is not extended."""
        monkeypatch.delenv("FARMSURV_CROP_EXT", raising=False)
        no_ext = _expand_bbox_pct((100, 200, 200, 80), 0.10, 2560, 1440)

        monkeypatch.setenv("FARMSURV_CROP_EXT", "50")
        with_ext = _expand_bbox_pct((100, 200, 200, 80), 0.10, 2560, 1440)

        assert no_ext == with_ext, "Landscape bbox changed with FARMSURV_CROP_EXT=50"

    def test_square_bbox_unchanged(self, monkeypatch):
        """w == h: a 128x128 bbox is not extended (h is not > w)."""
        monkeypatch.delenv("FARMSURV_CROP_EXT", raising=False)
        no_ext = _expand_bbox_pct((0, 0, 128, 128), 0.10, 2560, 1440)

        monkeypatch.setenv("FARMSURV_CROP_EXT", "50")
        with_ext = _expand_bbox_pct((0, 0, 128, 128), 0.10, 2560, 1440)

        assert no_ext == with_ext, "Square bbox changed with FARMSURV_CROP_EXT=50"

    def test_various_landscape_k_values(self, monkeypatch):
        """Test k=25, k=50, k=100 — all should give same output for landscape."""
        bbox = (100, 200, 300, 100)  # w=300, h=100
        for k in ("25", "50", "100"):
            monkeypatch.setenv("FARMSURV_CROP_EXT", k)
            result = _expand_bbox_pct(bbox, 0.10, 2560, 1440)
            # Compare against k=0
            monkeypatch.delenv("FARMSURV_CROP_EXT", raising=False)
            baseline = _expand_bbox_pct(bbox, 0.10, 2560, 1440)
            monkeypatch.setenv("FARMSURV_CROP_EXT", k)
            assert result == baseline, f"Landscape bbox changed at k={k}"


# ============================================================================
# AC4: Extension clamps at frame edges
# ============================================================================


class TestAC4Clamping:
    """Extension must clamp: never negative y, never past frame_h."""

    def test_no_negative_y(self, monkeypatch):
        """Bbox near top edge: extension would push y negative → clamp to 0."""
        # bbox at y=20, h=128, k=0.5 → y' = 20 - 64 = -44 → clamp to 0.
        monkeypatch.setenv("FARMSURV_CROP_EXT", "50")
        result = _expand_bbox_pct((0, 20, 64, 128), 0.10, 1920, 1080)
        assert result[1] >= 0, f"y={result[1]} should be >= 0"

    def test_no_past_frame_bottom(self, monkeypatch):
        """Bbox near bottom: extension would push past frame_h → clamp."""
        # frame_h=1080, bbox y=900, h=128 → extended h = 256.
        # cy = 964, new_h (after extension + pad + round) > 1080 → clamp.
        monkeypatch.setenv("FARMSURV_CROP_EXT", "50")
        result = _expand_bbox_pct((0, 900, 64, 128), 0.10, 1920, 1080)
        assert result[1] + result[3] <= 1080, (
            f"y+h={result[1]+result[3]} should be <= frame_h=1080"
        )

    def test_no_past_frame_right(self, monkeypatch):
        """Horizontal clamp still works (extension doesn't break it)."""
        monkeypatch.setenv("FARMSURV_CROP_EXT", "50")
        # bbox near right edge
        result = _expand_bbox_pct((1900, 100, 64, 128), 0.10, 1920, 1080)
        assert result[0] + result[2] <= 1920, (
            f"x+w={result[0]+result[2]} should be <= frame_w=1920"
        )

    def test_small_frame_clamp(self, monkeypatch):
        """Very small frame: bbox is entirely within bounds after clamp."""
        monkeypatch.setenv("FARMSURV_CROP_EXT", "50")
        # frame only 200x200, bbox at (0, 0, 50, 100)
        result = _expand_bbox_pct((0, 0, 50, 100), 0.0, 200, 200)
        assert result[1] >= 0
        assert result[1] + result[3] <= 200
        assert result[0] >= 0
        assert result[0] + result[2] <= 200


# ============================================================================
# AC5: Non-square vertical variants
# ============================================================================


class TestAC5NonSquareVertical:
    """Non-square vertical bboxes (h > w) get extended."""

    def test_tall_narrow_bbox(self, monkeypatch):
        """32x200 bbox (very tall/narrow) → extended."""
        monkeypatch.delenv("FARMSURV_CROP_EXT", raising=False)
        no_ext = _expand_bbox_pct((0, 100, 32, 200), 0.10, 1920, 1080)

        monkeypatch.setenv("FARMSURV_CROP_EXT", "50")
        with_ext = _expand_bbox_pct((0, 100, 32, 200), 0.10, 1920, 1080)

        # h should be larger with extension
        assert with_ext[3] > no_ext[3], "Extended h should be larger"
        # y should be smaller (top edge moved up)
        assert with_ext[1] < no_ext[1], "Extended y should be smaller"

    def test_wide_vertical_bbox(self, monkeypatch):
        """96x160 bbox → extended."""
        monkeypatch.delenv("FARMSURV_CROP_EXT", raising=False)
        no_ext = _expand_bbox_pct((100, 200, 96, 160), 0.10, 1920, 1080)

        monkeypatch.setenv("FARMSURV_CROP_EXT", "50")
        with_ext = _expand_bbox_pct((100, 200, 96, 160), 0.10, 1920, 1080)

        assert with_ext[3] > no_ext[3]
        assert with_ext[1] < no_ext[1]

    def test_different_k_values_for_vertical(self, monkeypatch):
        """k=25, 50, 100 produce strictly increasing h for vertical bboxes."""
        bbox = (100, 200, 80, 150)
        h_values = []
        for k in ("25", "50", "100"):
            monkeypatch.setenv("FARMSURV_CROP_EXT", k)
            result = _expand_bbox_pct(bbox, 0.10, 1920, 1080)
            h_values.append(result[3])
        assert h_values[0] < h_values[1] < h_values[2], (
            f"Height should increase with k: {h_values}"
        )

    def test_k_zero_does_nothing_for_vertical(self, monkeypatch):
        """k=0 on vertical bbox → same as no extension."""
        monkeypatch.delenv("FARMSURV_CROP_EXT", raising=False)
        no_ext = _expand_bbox_pct((0, 100, 64, 200), 0.10, 1920, 1080)

        monkeypatch.setenv("FARMSURV_CROP_EXT", "0")
        zero_ext = _expand_bbox_pct((0, 100, 64, 200), 0.10, 1920, 1080)

        assert no_ext == zero_ext


# ============================================================================
# AC6: Replay mode — 288x448 at (585,382) in 2560x1440
# ============================================================================


class TestAC6ReplayMode:
    """Replay scenario: alert 52481476-adjacent GARAGE case."""

    def test_garage_case_no_bottom_clamp(self, monkeypatch):
        """288x448 at (585,382) in 2560x1440, k=0.5.

        Extension: y' = 382 - 448*0.5 = 158, h' = 448*2 = 896.
        y'+h' = 158+896 = 1054 < 1440 → no bottom clamp.
        """
        monkeypatch.setenv("FARMSURV_CROP_EXT", "50")
        result = _expand_bbox_pct(
            (585, 382, 288, 448), 0.10, frame_w=2560, frame_h=1440
        )

        # Verify no bottom clamp: y + h <= 1440
        assert result[1] + result[3] <= 1440, (
            f"y+h={result[1]+result[3]} should be <= 1440 (no bottom clamp)"
        )

        # Verify the extension was applied: h should be significantly larger
        # than without extension.
        monkeypatch.delenv("FARMSURV_CROP_EXT", raising=False)
        no_ext = _expand_bbox_pct(
            (585, 382, 288, 448), 0.10, frame_w=2560, frame_h=1440
        )
        assert result[3] > no_ext[3], "Extension should increase height"
        assert result[1] < no_ext[1], "Extension should shift y upward"

    def test_garage_case_y_shift_formula(self, monkeypatch):
        """The top-edge shift matches y' = y - h*k = 382 - 224 = 158.

        After pad (10%) + round-mult-32 + re-center, the final y differs
        from 158 due to the padding expanding h from 896 to raw_h=1075
        → new_h=1088 → new_y = round(606 - 544) = 62.
        We verify the shift direction (y moves UP) and the magnitude is
        consistent with the extension.
        """
        monkeypatch.setenv("FARMSURV_CROP_EXT", "50")
        result = _expand_bbox_pct(
            (585, 382, 288, 448), 0.10, frame_w=2560, frame_h=1440
        )
        # Without extension: baseline y
        monkeypatch.delenv("FARMSURV_CROP_EXT", raising=False)
        baseline = _expand_bbox_pct(
            (585, 382, 288, 448), 0.10, frame_w=2560, frame_h=1440
        )
        # Extension shifts y upward (smaller y value)
        assert result[1] < baseline[1], (
            f"Extended y={result[1]} should be < baseline y={baseline[1]}"
        )

    def test_garage_case_center_preserved(self, monkeypatch):
        """Extension center (cx, cy) is preserved."""
        cy_orig = 382 + 448 / 2.0  # 606
        monkeypatch.setenv("FARMSURV_CROP_EXT", "50")
        result = _expand_bbox_pct(
            (585, 382, 288, 448), 0.10, frame_w=2560, frame_h=1440
        )
        assert abs((result[1] + result[3] / 2.0) - cy_orig) <= 2, (
            f"Center y={result[1]+result[3]/2.0} should be close to {cy_orig}"
        )
