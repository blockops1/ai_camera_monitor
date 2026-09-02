"""Unit tests for the gate-driven motion result builder.

Phase 6B.115 (2026-08-25): the legacy 6-frame pairwise-diff detector
was REMOVED. The motion gate is now the sole producer of frames +
crops + diff bboxes. The refactor vocabulary wrapper exposes
`build_motion_result_from_gate()` which stitches the gate's outputs
into a PositionResult with a 4-cell trajectory.

Tests here:
  1. Pure data classes (BoundingBox, MovingObject, PositionResult) work.
  2. The refactor's vocab dataclasses are aliases of the impl's.
  3. build_motion_result_from_gate returns a refactor PositionResult.
  4. End-to-end: 4 frames + 2 bboxes produce a 4-cell trajectory.
  5. Missing bboxes produce no_motion_detected=True.
"""

from __future__ import annotations

import sys
from pathlib import Path

_root = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_root))

import pytest

from vehicle_position.motion_detector import (
    BoundingBox,
    MotionDetectorConfig,
    MovingObject,
    PositionResult,
    build_motion_result_from_gate,
)

# --- Pure data classes ------------------------------------------------------


def test_bounding_box_area():
    bb = BoundingBox(x=10, y=20, width=100, height=50)
    assert bb.area == 5000


def test_bounding_box_is_frozen():
    bb = BoundingBox(x=10, y=20, width=100, height=50)
    with pytest.raises(Exception):
        bb.x = 99  # type: ignore[misc]


def test_position_result_defaults():
    r = PositionResult()
    assert r.no_motion_detected is True
    assert r.moving_objects == []
    assert r.primary_moving_object is None
    assert r.best_crop_path is None
    assert r.crop_paths == []
    assert r.total_motion_pixels == 0
    assert r.reference_method == "gate"  # Phase 6B.115: was "pairwise"


def test_moving_object_to_dict():
    mo = MovingObject(
        bbox_per_frame=[(10, 20, 30, 40)],
        trajectory=["B2"],
        avg_area=1200,
        frames_seen=3,
    )
    d = mo.to_dict()
    assert d["bbox_per_frame"] == [(10, 20, 30, 40)]
    assert d["trajectory"] == ["B2"]
    assert d["avg_area"] == 1200


def test_position_result_to_dict_round_trip():
    r = PositionResult(
        no_motion_detected=False,
        moving_objects=[MovingObject(avg_area=500)],
        best_crop_path="/tmp/crop.jpg",
        total_motion_pixels=1234,
        elapsed_ms=42.0,
    )
    d = r.to_dict()
    assert d["no_motion_detected"] is False
    assert d["best_crop_path"] == "/tmp/crop.jpg"
    assert d["total_motion_pixels"] == 1234


def test_motion_detector_config_is_frozen():
    c = MotionDetectorConfig()
    with pytest.raises(Exception):
        c.foo = 1  # type: ignore[attr-defined]


# --- build_motion_result_from_gate: synthetic frames -----------------------


def _try_import_opencv():
    try:
        import cv2
        import numpy as np
        return np, cv2
    except ImportError:
        return None, None


def _pil_crop(image, bbox):
    """PIL.Image.crop expects (left, top, right, bottom); convert from (x, y, w, h)."""
    x, y, w, h = bbox
    return image.crop((x, y, x + w, y + h))


def _load_synthetic_frames_as_pil(tmp_path, n_frames=4):
    """Write 4 native-resolution frames to disk AND return as PIL.Image list.

    Phase 6B.115 (§11.46.6): build_motion_result_from_gate now takes
    in-memory PIL.Image objects, not disk paths. We still write to disk
    so postmortem tooling can find them, then load them back as PIL.
    """
    np, cv2 = _try_import_opencv()
    if np is None:
        pytest.skip("numpy/cv2 not available")
    try:
        from PIL import Image as _PILImage
    except ImportError:
        pytest.skip("PIL not available")
    # Native resolution (matches CAM1: 2304x1296).
    height, width = 1296, 2304
    pil_frames: list = []
    for i in range(n_frames):
        # 3-channel color frame (cv2.imwrite writes 3-channel JPEG by default).
        img = np.zeros((height, width, 3), dtype=np.uint8)
        x0 = 100 + i * 60
        cv2.rectangle(img, (x0, 500), (x0 + 200, 700), (255, 255, 255), -1)
        path = tmp_path / f"frame_{i+1:03d}.jpg"
        cv2.imwrite(str(path), img)
        pil_frames.append(_PILImage.open(str(path)).convert("RGB"))
    return pil_frames, [str(p) for p in tmp_path.glob("frame_*.jpg")]


def _gate_bboxes_for_synthetic_frames():
    """Bboxes matching the moving rectangle in _load_synthetic_frames_as_pil."""
    # frame_003 (i=2) has x0=220, rect (220, 500, 200, 200) → bbox (220, 500, 200, 200)
    # frame_004 (i=3) has x0=280, rect (280, 500, 200, 200) → bbox (280, 500, 200, 200)
    return (220, 500, 200, 200), (280, 500, 200, 200)


def test_build_motion_result_basic_shape(tmp_path):
    """End-to-end: 4 frames + 2 bboxes + 2 crops → PositionResult."""
    pil_frames, _frame_paths = _load_synthetic_frames_as_pil(tmp_path)
    out = tmp_path / "out"
    crops = [str(out / "crop_0.jpg"), str(out / "crop_1.jpg")]
    bbox_a, bbox_b = _gate_bboxes_for_synthetic_frames()
    result = build_motion_result_from_gate(
        frames=pil_frames,
        crop_a=_pil_crop(pil_frames[2], bbox_a),
        crop_b=_pil_crop(pil_frames[3], bbox_b),
        bbox_a=bbox_a,
        bbox_b=bbox_b,
        alert_id="basic",
        crop_paths=crops,
    )
    assert isinstance(result, PositionResult)
    assert result.no_motion_detected is False
    assert len(result.moving_objects) == 1
    primary = result.primary_moving_object
    assert primary is not None
    assert primary.avg_area > 0
    assert primary.frames_seen == 2  # only bbox_a + bbox_b
    # Verify crops.
    assert result.crop_paths == crops
    assert result.best_crop_path == crops[0]
    assert result.elapsed_ms > 0
    # Verify 4-cell trajectory.
    assert len(primary.trajectory) == 4
    assert primary.trajectory[0] == "absent"
    assert primary.trajectory[1] == "absent"
    # frame_3 + frame_4 should NOT be 'absent'.
    assert primary.trajectory[2] != "absent"
    assert primary.trajectory[3] != "absent"


def test_build_motion_result_no_motion_when_both_bboxes_none(tmp_path):
    """Both bbox_a and bbox_b None → no_motion_detected=True."""
    pil_frames, _ = _load_synthetic_frames_as_pil(tmp_path)
    result = build_motion_result_from_gate(
        frames=pil_frames,
        crop_a=None,
        crop_b=None,
        bbox_a=None,
        bbox_b=None,
        alert_id="no_motion",
        crop_paths=[],
    )
    assert isinstance(result, PositionResult)
    assert result.no_motion_detected is True
    assert result.moving_objects == []


def test_build_motion_result_only_bbox_a(tmp_path):
    """One bbox present → 1 frames_seen, 4-cell trajectory, frame_4 absent."""
    pil_frames, _ = _load_synthetic_frames_as_pil(tmp_path)
    out = tmp_path / "out"
    bbox_a, _ = _gate_bboxes_for_synthetic_frames()
    result = build_motion_result_from_gate(
        frames=pil_frames,
        crop_a=_pil_crop(pil_frames[2], bbox_a),
        crop_b=None,
        bbox_a=bbox_a,
        bbox_b=None,
        alert_id="only_a",
        crop_paths=[str(out / "crop_a.jpg")],
    )
    assert result.no_motion_detected is False
    primary = result.primary_moving_object
    assert primary is not None
    assert primary.frames_seen == 1
    traj = primary.trajectory
    assert traj[0] == "absent"
    assert traj[1] == "absent"
    assert traj[2] != "absent"
    assert traj[3] == "absent"  # frame_4 has no bbox


def test_build_motion_result_reference_method_is_gate(tmp_path):
    """Phase 6B.115: reference_method is 'gate', not 'pairwise'."""
    pil_frames, _ = _load_synthetic_frames_as_pil(tmp_path)
    bbox_a, bbox_b = _gate_bboxes_for_synthetic_frames()
    result = build_motion_result_from_gate(
        frames=pil_frames,
        crop_a=_pil_crop(pil_frames[2], bbox_a),
        crop_b=_pil_crop(pil_frames[3], bbox_b),
        bbox_a=bbox_a,
        bbox_b=bbox_b,
        alert_id="ref_method",
        crop_paths=[],
    )
    assert result.reference_method == "gate"


def test_build_motion_result_to_dict_field_completeness(tmp_path):
    """Every field of PositionResult survives to_dict."""
    pil_frames, _ = _load_synthetic_frames_as_pil(tmp_path)
    bbox_a, bbox_b = _gate_bboxes_for_synthetic_frames()
    result = build_motion_result_from_gate(
        frames=pil_frames,
        crop_a=_pil_crop(pil_frames[2], bbox_a),
        crop_b=_pil_crop(pil_frames[3], bbox_b),
        bbox_a=bbox_a,
        bbox_b=bbox_b,
        alert_id="dict_check",
        crop_paths=[],
    )
    d = result.to_dict()
    assert "moving_objects" in d
    assert "primary_moving_object" in d
    assert "best_crop_path" in d
    assert "crop_paths" in d
    assert "no_motion_detected" in d
    assert "reference_method" in d
    assert "total_motion_pixels" in d
    assert "elapsed_ms" in d
    assert d["reference_method"] == "gate"


def test_build_motion_result_path_objects_accepted(tmp_path):
    """Phase 6B.115: the public API accepts PIL.Image (no Path objects)."""
    pil_frames, _ = _load_synthetic_frames_as_pil(tmp_path)
    bbox_a, bbox_b = _gate_bboxes_for_synthetic_frames()
    # PIL.Image already (not Path objects — that path is gone in §11.46.6).
    result = build_motion_result_from_gate(
        frames=pil_frames,
        crop_a=_pil_crop(pil_frames[2], bbox_a),
        crop_b=_pil_crop(pil_frames[3], bbox_b),
        bbox_a=bbox_a,
        bbox_b=bbox_b,
        alert_id="path_test",
        crop_paths=[],
    )
    assert isinstance(result, PositionResult)
    assert result.no_motion_detected is False