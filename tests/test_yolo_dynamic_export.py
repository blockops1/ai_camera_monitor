"""test_yolo_dynamic_export — verify dynamic-axes YOLOv8n ONNX accepts variable inputs.

AC3: loads models/yolov8n.onnx via onnxruntime and runs inference on three
dummy tensors at multiples-of-32 sizes (the model's constraint), asserting
anchor counts match.

CONSTRAINT: YOLOv8's ONNX export requires input (h, w) with h%32==0 and
w%32==0 due to an upsample-nearest -> Concat off-by-one at non-aligned sizes.
The model is dynamic (accepts any (h, w) where h%32==0 and w%32==0); the
caller is responsible for padding to multiples of 32 before inference.
"""

import numpy as np
import onnxruntime as ort
import pytest
from pathlib import Path

MODEL_PATH = Path(__file__).resolve().parent.parent / "models" / "yolov8n.onnx"

# (resolution, expected_anchor_count) — all sizes are multiples of 32
TEST_CASES = [
    ((640, 640), 8400),
    ((1280, 736), 19320),    # 1280x720 padded 16px on width to multiple of 32
    ((1920, 1088), 42840),   # 1920x1080 padded 8px on height to multiple of 32
]

# Sizes that MUST fail — documents the constraint
NON_MULTIPLES_OF_32 = [
    (1920, 1080),
    (1280, 720),
]


def test_dynamic_onnx_anchor_counts():
    """Run inference on three dummy tensors and verify anchor counts."""
    assert MODEL_PATH.is_file(), f"Model not found at {MODEL_PATH}"

    sess = ort.InferenceSession(str(MODEL_PATH))
    input_name = sess.get_inputs()[0].name

    for (h, w), expected_anchors in TEST_CASES:
        dummy = np.random.rand(1, 3, h, w).astype(np.float32)
        result = sess.run(None, {input_name: dummy})
        arr = np.asarray(result[0])  # type: ignore[arg-type]
        assert arr.shape[-1] == expected_anchors, (
            f"anchor count mismatch at {h}x{w}: got {arr.shape[-1]}, "
            f"expected {expected_anchors}"
        )


def test_dynamic_onnx_input_is_dynamic():
    """Verify the ONNX input shape has dynamic axes (channels=3, h and w dynamic)."""
    sess = ort.InferenceSession(str(MODEL_PATH))
    input_shape = sess.get_inputs()[0].shape
    # channels should be 3, h and w should be string names (dynamic in ultralytics export)
    # ultralytics names the dynamic dims 'batch' and 'height'/'width' rather than -1
    assert input_shape[1] == 3, f"channels should be 3, got {input_shape[1]}"
    # The export uses symbolic dims ('height', 'width') for spatial axes — verify they're
    # NOT fixed integers.
    assert not isinstance(input_shape[2], int) or input_shape[2] == -1, (
        f"height should be dynamic, got {input_shape[2]}"
    )
    assert not isinstance(input_shape[3], int) or input_shape[3] == -1, (
        f"width should be dynamic, got {input_shape[3]}"
    )


def test_dynamic_onnx_constraint_at_non_multiples_of_32():
    """Verify that non-multiples-of-32 sizes FAIL — documents the constraint.

    YOLOv8's upsample-nearest -> Concat chain has an off-by-one dimension
    mismatch when input h or w is not a multiple of 32. This test pins that
    behavior so future export attempts don't claim a fix that doesn't exist.
    """
    sess = ort.InferenceSession(str(MODEL_PATH))
    input_name = sess.get_inputs()[0].name

    for h, w in NON_MULTIPLES_OF_32:
        dummy = np.random.rand(1, 3, h, w).astype(np.float32)
        with pytest.raises(Exception) as exc_info:
            sess.run(None, {input_name: dummy})
        # Should fail with a Concat dimension mismatch
        assert "Concat" in str(exc_info.value) or "mismatch" in str(exc_info.value).lower(), (
            f"Expected Concat-related error at {h}x{w}, got: {exc_info.value}"
        )


def test_dynamic_onnx_padded_crop_runs():
    """Verify a typical-sized crop (e.g. 265x128 vehicle) padded to multiples
    of 32 runs successfully.

    This is the realistic call pattern: a motion-diff bbox gives a crop at,
    say, 265x128, the caller pads to 288x128, and inference runs.
    """
    sess = ort.InferenceSession(str(MODEL_PATH))
    input_name = sess.get_inputs()[0].name

    # Real observed: c59e3a72 car 265x112 + 8px pad = 281x128, pad to 288x128
    crop_h, crop_w = 265, 112
    pad_h = (32 - crop_h % 32) % 32  # = 23
    pad_w = (32 - crop_w % 32) % 32  # = 16 (since 112 is multiple of 32, pad is 0)
    # Actually 112 % 32 = 16, so pad_w = 16
    padded_h = crop_h + pad_h  # 288
    padded_w = crop_w + pad_w  # 128

    dummy = np.random.rand(1, 3, padded_h, padded_w).astype(np.float32)
    result = sess.run(None, {input_name: dummy})
    arr = np.asarray(result[0])  # type: ignore[arg-type]
    assert arr.shape == (1, 84, arr.shape[-1]), (
        f"Expected (1, 84, N) output, got {arr.shape}"
    )
    assert arr.shape[-1] > 0, "Should produce non-zero anchors"
