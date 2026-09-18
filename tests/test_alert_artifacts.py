"""
test_alert_artifacts.py — Tests for infra/alert_artifacts.py.

Acceptance criteria for US-024b:
    - test_prepare_writes_three_files: verify 3 files written when crops +
      composite present.
    - test_prepare_returns_None_for_missing_bbox: verify None when either
      PIL crop is None (no subject bbox).
    - test_prepare_with_no_motion_returns_no_crops: verify no_crops case.
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image

from infra.alert_artifacts import AlertArtifacts, prepare_alert_artifacts

# ---------------------------------------------------------------------------
# Fake GateVerdict for tests
# ---------------------------------------------------------------------------

@dataclass
class FakeGateVerdict:
    """Minimal GateVerdict-like object for tests."""

    classification: str = "vehicle"
    class_label: str | None = "car"
    confidence: float = 0.85
    frames: list = field(default_factory=list)  # list[PIL.Image.Image]
    crop_a: object = None  # PIL.Image | None
    crop_b: object = None  # PIL.Image | None
    bbox_a: tuple | None = None
    bbox_b: tuple | None = None
    crop_bbox_a: tuple | None = None
    crop_bbox_b: tuple | None = None
    frame_paths: list = field(default_factory=list)
    crop_a_path: str | None = None
    crop_b_path: str | None = None
    pairwise_diff_path: str | None = None
    raw_verdicts: list = field(default_factory=list)
    reason: str = "high_conf_vehicle"


def _make_frame(w=320, h=240, color=(100, 150, 200)):
    return Image.new("RGB", (w, h), color)


def _make_cropped_frame(w=64, h=48, color=(200, 100, 100)):
    return Image.new("RGB", (w, h), color)


# ---------------------------------------------------------------------------
# Test: test_prepare_writes_three_files
# ---------------------------------------------------------------------------

def test_prepare_writes_three_files():
    """AC7a: prepare_alert_artifacts writes crop_a.png, crop_b.png,
    and composite.png when both crops and motion are present."""
    with tempfile.TemporaryDirectory() as tmpdir:
        # Build a gate_verdict with valid crops and frames.
        f1 = _make_frame(160, 120)
        f2 = _make_frame(160, 120)
        f3 = _make_frame(160, 120)
        f4 = _make_frame(160, 120)

        ca = _make_cropped_frame(64, 48)
        cb = _make_cropped_frame(64, 48)

        verdict = FakeGateVerdict(
            classification="vehicle",
            class_label="car",
            confidence=0.85,
            frames=[f1, f2, f3, f4],
            crop_a=ca,
            crop_b=cb,
            bbox_a=(10, 10, 50, 40),
            bbox_b=(20, 20, 40, 30),
            reason="high_conf_vehicle",
            frame_paths=["/fake/frame_1.jpg", "/fake/frame_2.jpg",
                         "/fake/frame_3.jpg", "/fake/frame_4.jpg"],
        )

        artifacts = prepare_alert_artifacts(verdict, tmpdir)

        # Verify structure.
        assert isinstance(artifacts, AlertArtifacts)
        assert artifacts.crop_a_path is not None
        assert artifacts.crop_b_path is not None
        assert artifacts.composite_path is not None

        # Verify files exist on disk.
        assert Path(artifacts.crop_a_path).is_file()
        assert Path(artifacts.crop_b_path).is_file()
        assert Path(artifacts.composite_path).is_file()

        # Verify PNG format (magic bytes).
        for path in [artifacts.crop_a_path, artifacts.crop_b_path,
                     artifacts.composite_path]:
            with open(path, "rb") as f:
                magic = f.read(4)
            assert magic == b"\x89PNG", f"{path} is not a PNG (magic={magic!r})"


# ---------------------------------------------------------------------------
# Test: test_prepare_returns_None_for_missing_bbox
# ---------------------------------------------------------------------------

def test_prepare_returns_None_for_missing_bbox():
    """AC7b: When a crop is None (no subject bbox), the corresponding path
    is None."""
    with tempfile.TemporaryDirectory() as tmpdir:
        f1 = _make_frame(160, 120)
        f2 = _make_frame(160, 120)
        f3 = _make_frame(160, 120)
        f4 = _make_frame(160, 120)

        verdict = FakeGateVerdict(
            classification="vehicle",
            class_label="car",
            confidence=0.85,
            frames=[f1, f2, f3, f4],
            crop_a=_make_cropped_frame(64, 48),  # crop_a present
            crop_b=None,  # crop_b missing — diff(3,4) had no motion
            bbox_a=(10, 10, 50, 40),
            bbox_b=None,  # no bbox for crop_b
            reason="high_conf_vehicle",
            frame_paths=["/fake/frame_1.jpg", "/fake/frame_2.jpg",
                         "/fake/frame_3.jpg", "/fake/frame_4.jpg"],
        )

        artifacts = prepare_alert_artifacts(verdict, tmpdir)

        assert artifacts.crop_a_path is not None
        assert artifacts.crop_b_path is None
        assert Path(artifacts.crop_a_path).is_file()
        # composite should still render (bbox_b=None is OK for render_motion_composite)
        assert artifacts.composite_path is not None


# ---------------------------------------------------------------------------
# Test: test_prepare_with_no_motion_returns_no_crops
# ---------------------------------------------------------------------------

def test_prepare_with_no_motion_returns_no_crops():
    """AC7c: When both crops are None (no motion detected by either diff),
    both paths are None."""
    with tempfile.TemporaryDirectory() as tmpdir:
        f1 = _make_frame(160, 120)
        f2 = _make_frame(160, 120)
        f3 = _make_frame(160, 120)
        f4 = _make_frame(160, 120)

        verdict = FakeGateVerdict(
            classification="vehicle",
            class_label="car",
            confidence=0.85,
            frames=[f1, f2, f3, f4],
            crop_a=None,  # no diff motion at all
            crop_b=None,  # no diff motion at all
            bbox_a=None,
            bbox_b=None,
            reason="high_conf_vehicle",
            frame_paths=["/fake/frame_1.jpg", "/fake/frame_2.jpg",
                         "/fake/frame_3.jpg", "/fake/frame_4.jpg"],
        )

        artifacts = prepare_alert_artifacts(verdict, tmpdir)

        assert artifacts.crop_a_path is None
        assert artifacts.crop_b_path is None
        # composite still renders with no bboxes (gate provides frames)
        assert artifacts.composite_path is not None
        assert Path(artifacts.composite_path).is_file()
