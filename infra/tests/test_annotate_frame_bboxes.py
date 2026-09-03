"""
testannotate_frame_bboxes.py — Tests for listener.listener.annotate_frame_bboxes.

This helper draws the detector's per-frame bboxes onto the 6 frames that
accompany the OFS lead motion Telegram, so Note can see at a glance which
pixel cluster triggered the alert. It is a sibling helper to
`infra.motion_visualization.render_motion_composite`, but it operates on
the raw captured frames (not the burst diff) and writes annotated copies
to `<frame_dir>/annotated_<basename>.jpg`.

Phase.81 (PLAN.md §11.14): per-frame bbox annotation for the lead motion
Telegram's 6-frame media group.

Tests use synthetic frames generated in-process via numpy + cv2 — no real
RTSP, no real alert_id directories. Each test owns its own tmp dir.
"""
from __future__ import annotations

import os

import cv2
import numpy as np

from infra.motion_detector import MovingObject
from telegram_formatter.vehicle_alert import annotate_frame_bboxes

# ---------------------------------------------------------------------------
# Synthetic frame builders
# ---------------------------------------------------------------------------

def _make_frame(width: int = 2560, height: int = 1920, color_bgr: tuple = (40, 40, 40)) -> np.ndarray:
    """Return a solid-color BGR frame as numpy uint8 array."""
    return np.full((height, width, 3), color_bgr, dtype=np.uint8)


def _write_jpeg(path: str, frame: np.ndarray, quality: int = 90) -> None:
    """Encode frame as JPEG at the given quality and write to disk."""
    cv2.imwrite(path, frame, [cv2.IMWRITE_JPEG_QUALITY, quality])


def _green_pixel_count(path: str) -> int:
    """Count the number of pixels that are pure green (0, 255, 0) in BGR.

    Used to assert that the bbox outline was drawn — green outlines produce
    a small but non-zero number of pure-green pixels. We use 'pure green'
    rather than 'green-ish' so background noise doesn't trigger false
    positives.
    """
    img = cv2.imread(path)
    if img is None:
        return 0
    # BGR == (0, 255, 0) means blue=0, green=255, red=0.
    mask = (img[:, :, 0] == 0) & (img[:, :, 1] == 255) & (img[:, :, 2] == 0)
    return int(mask.sum())


# ---------------------------------------------------------------------------
# Vertical tracer bullet: core behavior
# ---------------------------------------------------------------------------

def test_annotate_draws_green_bbox_on_frame(tmp_path):
    """Core behavior: given one frame with a non-empty bbox for index 1,
    the annotated output JPEG must contain green pixels (the bbox outline).

    Phase.86 (PLAN.md §11.18): annotate_frame_bboxes now draws
    bbox_per_frame[i+1] on frame_paths[i] (the bbox at index i+1 describes
    frame_paths[i]). So a 2-frame fixture with the bbox at index 1
    produces an annotated frame_paths[0].
    """
    # Arrange: two JPEGs on disk, a MovingObject with two bboxes (the
    # first is empty/absent for frame 0, the second describes frame 0).
    frame_path_0 = str(tmp_path / "frame0.jpg")
    frame_path_1 = str(tmp_path / "frame1.jpg")
    _write_jpeg(frame_path_0, _make_frame())
    _write_jpeg(frame_path_1, _make_frame())

    obj = MovingObject(bbox_per_frame=[(0, 0, 0, 0), (400, 300, 200, 200)])

    # Act
    result = annotate_frame_bboxes([frame_path_0, frame_path_1], obj)

    # Assert: frame_paths[0] has its annotated version (bbox at index 1).
    assert len(result) == 2
    annotated_path = result[0]
    assert annotated_path != frame_path_0, "annotated path should differ from original"
    assert os.path.exists(annotated_path), f"annotated file missing: {annotated_path}"
    assert os.path.basename(annotated_path).startswith("annotated_")
    green_count = _green_pixel_count(annotated_path)
    assert green_count > 0, (
        f"expected green bbox pixels in {annotated_path}, got {green_count}"
    )


def test_annotate_handles_six_frames_with_bboxes(tmp_path):
    """6-frame media group case: given 6 frames each with a bbox, the
    helper returns 6 annotated paths, one per frame, each containing
    green pixels.

    This is the realistic OFS case (Phase.62+ sends a 6-frame trail).
    Each frame is in a different part of the 1280x960 grid so the bboxes
    are distinguishable across frames.

    Phase.86 (PLAN.md §11.18): bbox_per_frame[i+1] is drawn on
    frame_paths[i]. To produce annotated outputs for all 6 frames, we
    need 7 bboxes (index 0 is absent for the imaginary prior frame,
    indices 1..6 each describe one of the 6 real frames). Frame 5's
    annotation uses bbox_per_frame[6].
    """
    # Arrange: 6 frames on disk, 7 bboxes (index 0 empty for the absent
    # pre-frame, indices 1..6 each describing one of the 6 real frames).
    frame_paths = []
    for i in range(6):
        fp = str(tmp_path / f"frame{i}.jpg")
        _write_jpeg(fp, _make_frame())
        frame_paths.append(fp)

    # bboxes in 1280x960 coords: spread across the 4x4 grid. Each one is
    # 100x100 pixels in resized space → scales to 200x200 in 2560x1920
    # original space. Distinct positions so any per-frame mixup would
    # produce a wrong green-pixel location.
    bboxes = [
        (0, 0, 0, 0),              # index 0: absent (no previous frame)
        (100, 100, 100, 100),      # index 1 → describes frame_paths[0]
        (300, 100, 100, 100),      # index 2 → describes frame_paths[1]
        (500, 100, 100, 100),      # index 3 → describes frame_paths[2]
        (700, 100, 100, 100),      # index 4 → describes frame_paths[3]
        (100, 400, 100, 100),      # index 5 → describes frame_paths[4]
        (700, 400, 100, 100),      # index 6 → describes frame_paths[5]
    ]
    obj = MovingObject(bbox_per_frame=list(bboxes))

    # Act
    result = annotate_frame_bboxes(frame_paths, obj)

    # Assert: 6 paths back, all different from originals, all contain
    # green pixels.
    assert len(result) == 6
    for i, annotated in enumerate(result):
        assert annotated != frame_paths[i], (
            f"frame {i}: annotated path should differ from original"
        )
        assert os.path.exists(annotated), (
            f"frame {i}: annotated file missing: {annotated}"
        )
        assert os.path.basename(annotated).startswith("annotated_"), (
            f"frame {i}: filename pattern wrong: {os.path.basename(annotated)}"
        )
        green_count = _green_pixel_count(annotated)
        assert green_count > 0, (
            f"frame {i}: expected green bbox pixels in {annotated}, got {green_count}"
        )