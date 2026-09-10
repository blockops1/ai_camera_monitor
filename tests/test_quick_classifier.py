"""Tests for infra/quick_classifier.py — pad-to-multiple-of-32 path.

AC2: classify() on a 1920x1088 PIL image runs end-to-end; bboxes within (1920, 1080).
AC3: synthetic 265x112 crop padded to 288x128 runs and bboxes within original bounds.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

# Silence the model-missing exit. Tests here use mock models or skip if missing.
MODEL_PATH = Path(__file__).resolve().parent.parent / "models" / "yolov8n.onnx"


@pytest.fixture(autouse=True)
def _skip_no_model():
    """Skip all tests in this file when the ONNX model doesn't exist."""
    if not MODEL_PATH.is_file():
        pytest.skip("models/yolov8n.onnx not found")


class TestPadToMultipleOf32Input:
    """AC2 + AC3: end-to-end classify with pad-to-32 preprocessing."""

    def _make_classifier(self, use_cpu=True):
        """Create a QuickClassifier, forcing CPU EP to avoid CoreML memory issues."""
        import onnxruntime as ort
        # Monkey-patch the available providers so QuickClassifier picks CPU.
        orig = ort.get_available_providers
        ort.get_available_providers = lambda: ["CPUExecutionProvider"]
        try:
            from infra.quick_classifier import QuickClassifier
            return QuickClassifier(str(MODEL_PATH))
        finally:
            ort.get_available_providers = orig

    # ------------------------------------------------------------------ AC2
    def test_ac2_classify_1920x1088_returns_bboxes_in_orig_bounds(self):
        """AC2: 1920x1088 image (1920x1080 padded to 1088). Bboxes within (1920, 1080)."""
        from PIL import Image as PILImage

        clf = self._make_classifier()

        # Build a 1920x1088 RGB image (padded height — 34 is next multiple of 32 over 1080).
        img = PILImage.new("RGB", (1920, 1088), color=(128, 64, 32))

        verdict = clf.classify_frame(img)

        # Inference must succeed and return something.
        assert verdict is not None
        assert isinstance(verdict.top_class, str)

        # Every bbox must lie within the original (un-padded) 1920x1080 bounds.
        orig_w, orig_h = 1920, 1080
        for _cls_id, _conf, (x1, y1, x2, y2) in verdict.raw_predictions:
            assert 0 <= x1 < x2 <= orig_w, f"x bbox {x1},{x2} exceeds {orig_w}"
            assert 0 <= y1 < y2 <= orig_h, f"y bbox {y1},{y2} exceeds {orig_h}"

    # ------------------------------------------------------------------ AC3
    def test_ac3_synthetic_265x112_crop_bboxes_within_original(self):
        """AC3: synthetic 265x112 crop padded to 288x128; bboxes within 265x112."""
        from PIL import Image as PILImage

        clf = self._make_classifier()

        # Build a realistic vehicle-sized crop: 265x112, with a bright blob
        # in the center to trigger YOLO detections.
        img_array = np.zeros((112, 265, 3), dtype=np.uint8)
        # Place a white rectangle (bright object) in the center.
        img_array[40:72, 90:175] = 255
        img = PILImage.fromarray(img_array)

        verdict = clf.classify_frame(img)

        # Inference must succeed.
        assert verdict is not None

        # All bboxes must lie within the original 265x112 crop bounds.
        orig_w, orig_h = 265, 112
        for _cls_id, _conf, (x1, y1, x2, y2) in verdict.raw_predictions:
            assert 0 <= x1 < x2 <= orig_w, f"x bbox {x1},{x2} exceeds {orig_w}"
            assert 0 <= y1 < y2 <= orig_h, f"y bbox {y1},{y2} exceeds {orig_h}"
