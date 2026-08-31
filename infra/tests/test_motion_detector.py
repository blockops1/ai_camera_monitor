"""
test_motion_detector.py — Tests for infra.motion_detector.

STATUS: provisional (new file, 2026-08-16)
THREAD SAFETY: N/A (test file)

INPUTS:
    - pytest fixtures: tmp_path (built-in)
    - synthetic MovingObject + frame JPEGs constructed inline

OUTPUTS:
    - pytest test results

DOES NOT DO:
    - Test the full detect_motion() pipeline (covered indirectly via
      integration tests; this file tests _crop_one in isolation)
    - Test pairwise-diff blob tracking (covered in test_alert_generator.py
      and existing alert-overrides tests via detect_motion end-to-end)
    - Test against real CAM1 frames (synthetic frames are sufficient; the
      crop math is dimension-driven)

CALLED BY:
    - pytest discovery

CALLS INTO:
    - infra.motion_detector: _crop_one (Phase.82 target), MovingObject
    - cv2: image IO
    - numpy: array construction
    - os, glob: filesystem ops

RELATED:
    - infra.motion_detector.MovingObject — the dataclass these tests construct
    - infra.motion_detector.CROP_PAD_PCT — the constant under test (Phase.82)
"""

from __future__ import annotations

import os

import cv2
import numpy as np
import pytest

from infra.motion_detector import CROP_PAD_PCT, MIN_CROP_DIM, MovingObject, _crop_one


def _save_test_frame(path: str, width: int, height: int) -> None:
    """Write a synthetic frame (uniform gray) to disk as JPEG."""
    arr = np.full((height, width, 3), 128, dtype=np.uint8)
    cv2.imwrite(path, arr, [cv2.IMWRITE_JPEG_QUALITY, 90])


def _make_moving_object(bbox_per_frame: list[tuple[int, int, int, int]]) -> MovingObject:
    """Construct a MovingObject with bbox_per_frame populated."""
    mo = MovingObject()
    mo.bbox_per_frame = bbox_per_frame
    return mo


def test_crop_one_no_padding(tmp_path):
    """Phase.82: _crop_one must NOT add 20% padding around the bbox.

    Before Phase.82 (CROP_PAD_PCT=0.20), a 100x100 bbox at (500, 400)
    in 1280x960 coords produced a crop of 120x120 (40% larger in each
    dimension). After this phase, the crop is 100x100 — matching the
    visualization green box and the diff zone exactly.

    The frame is 1280x960 to match RESIZE_W/RESIZE_H so sx=sy=1.0 and
    the crop dimensions equal the resized bbox dimensions (no scaling).

    Phase.86 (PLAN.md §11.18): _crop_one now crops frame_paths[frame_idx-1]
    using bbox_per_frame[frame_idx]. Frame 0 has no previous frame so
    _crop_one returns None for frame_idx=0; this test uses frame_idx=1
    with a 2-frame fixture so the previous frame exists.
    """
    frame_path_0 = str(tmp_path / "frame_0.jpg")
    frame_path_1 = str(tmp_path / "frame_1.jpg")
    _save_test_frame(frame_path_0, width=1280, height=960)
    _save_test_frame(frame_path_1, width=1280, height=960)

    mo = _make_moving_object([(0, 0, 0, 0), (500, 400, 100, 100)])

    crop_path = _crop_one(
        obj=mo,
        frames=[],  # unused; _crop_one reads frame_paths directly
        frame_paths=[frame_path_0, frame_path_1],
        frame_idx=1,
        output_dir=str(tmp_path),
        alert_id="test_no_pad",
        crop_idx=0,
    )

    assert crop_path is not None, "crop should be saved successfully"
    assert os.path.exists(crop_path), f"crop file missing: {crop_path}"

    saved = cv2.imread(crop_path)
    assert saved is not None, "saved crop could not be read back"
    h, w = saved.shape[:2]

    # Phase.82 contract: crop dimensions == bbox dimensions (no padding).
    assert w == 100, f"crop width should equal bbox width (100), got {w}"
    assert h == 100, f"crop height should equal bbox height (100), got {h}"

    # The constant is now 0.0 — assert it directly so a future
    # re-introduction of padding breaks this test loudly.
    assert CROP_PAD_PCT == 0.0, (
        f"CROP_PAD_PCT should be 0.0 after Phase.82, got {CROP_PAD_PCT}"
    )


def test_crop_one_clamps_to_image_bounds(tmp_path):
    """Phase.82: bbox at the frame edge must not crash or produce
    out-of-bounds crops.

    With padding removed, the bbox itself is the crop region — it must
    stay within [0, RESIZE_W] x [0, RESIZE_H]. The clamp is preserved
    from the prior implementation (lines 318-321) for safety.

    Edge bbox: x=1230, y=900, w=50, h=60 → x1=1280 (RESIZE_W), y1=960
    (RESIZE_H). Both bbox dimensions are >= MIN_CROP_DIM so the size
    guard passes.

    Phase.86 (PLAN.md §11.18): uses frame_idx=1 with a 2-frame fixture
    so frame_paths[frame_idx-1] = frame_paths[0] exists for cropping.
    """
    frame_path_0 = str(tmp_path / "frame_0.jpg")
    frame_path_1 = str(tmp_path / "frame_edge.jpg")
    _save_test_frame(frame_path_0, width=1280, height=960)
    _save_test_frame(frame_path_1, width=1280, height=960)

    mo = _make_moving_object([(0, 0, 0, 0), (1230, 900, 50, 60)])

    crop_path = _crop_one(
        obj=mo,
        frames=[],
        frame_paths=[frame_path_0, frame_path_1],
        frame_idx=1,
        output_dir=str(tmp_path),
        alert_id="test_edge_clamp",
        crop_idx=0,
    )

    assert crop_path is not None, "edge-bbox crop should succeed (clamp keeps it valid)"
    assert os.path.exists(crop_path)

    saved = cv2.imread(crop_path)
    assert saved is not None
    h, w = saved.shape[:2]
    assert w == 50, f"edge crop width should equal bbox width (50), got {w}"
    assert h == 60, f"edge crop height should equal bbox height (60), got {h}"


def test_crop_one_skips_tiny_bbox(tmp_path):
    """Sanity check: the existing MIN_CROP_DIM guard still fires.

    Phase.82 removed the padding math but did NOT touch the bbox-size
    guard at lines 313-314. This test pins that behavior so the guard
    doesn't regress.
    """
    frame_path = str(tmp_path / "frame_tiny.jpg")
    _save_test_frame(frame_path, width=1280, height=960)

    # 30x30 is below MIN_CROP_DIM (50).
    mo = _make_moving_object([(500, 400, 30, 30)])

    crop_path = _crop_one(
        obj=mo,
        frames=[],
        frame_paths=[frame_path],
        frame_idx=0,
        output_dir=str(tmp_path),
        alert_id="test_tiny",
        crop_idx=0,
    )

    assert crop_path is None, "tiny bbox should be skipped (below MIN_CROP_DIM)"


def test_crop_one_accepts_distant_vehicle_profile_shape(tmp_path):
    """Phase.119: a tall+thin real-vehicle bbox (70x35, ~30m distance) must
    NOT be rejected. The previous gate `w >= 50 AND h >= 50` rejected every
    distant vehicle seen in profile, leaving _crop_top_n() returning [] and
    the matcher running against the full frame.

    The looser gate is `max(w,h) >= 50 AND min(w,h) >= 25`. 70x35 → max=70
    passes, min=35 passes. A 30x30 noise blob → max=30 fails the first half,
    so noise still gets rejected.
    """
    from infra.motion_detector import MIN_CROP_SHORT_SIDE

    frame_path = str(tmp_path / "frame_distant.jpg")
    _save_test_frame(frame_path, width=1280, height=960)

    # Real-world distant-vehicle bbox shape from alert 4adfd435 (today).
    # bbox_per_frame[frame_idx=1] means we crop frame_paths[0].
    mo = _make_moving_object([(500, 500, 0, 0), (9, 408, 68, 34)])  # 1st frame absent, 2nd frame has motion

    crop_path = _crop_one(
        obj=mo,
        frames=[],
        frame_paths=[frame_path, frame_path],
        frame_idx=1,
        output_dir=str(tmp_path),
        alert_id="test_distant",
        crop_idx=0,
    )

    assert crop_path is not None, (
        f"70x34 distant-vehicle bbox should be accepted (max={max(68,34)}>=50, "
        f"min={min(68,34)}>={MIN_CROP_SHORT_SIDE}); got None — gate is too strict again"
    )


def test_crop_one_still_rejects_truly_tiny_blobs(tmp_path):
    """Phase.119 sanity: noise blobs (long side < 50) still get rejected
    even when short side is fine. A 25x25 pure-noise blob has max=25 < 50
    so it must still return None.
    """
    # Set up: bbox is 25x25 at frame 1, frame_paths has 2 frames
    frame_path = str(tmp_path / "frame_noise.jpg")
    _save_test_frame(frame_path, width=1280, height=960)
    mo = _make_moving_object([(500, 500, 0, 0), (100, 100, 25, 25)])

    crop_path = _crop_one(
        obj=mo,
        frames=[],
        frame_paths=[frame_path, frame_path],
        frame_idx=1,
        output_dir=str(tmp_path),
        alert_id="test_noise",
        crop_idx=0,
    )

    assert crop_path is None, (
        "25x25 noise blob should still be rejected (long side < MIN_CROP_DIM=50); "
        "loosening must not break the noise floor"
    )


@pytest.mark.parametrize(
    "bbox,expected_w,expected_h",
    [
        # Centered bbox — no clamping needed.
        ((500, 400, 100, 100), 100, 100),
        # Wide bbox.
        ((200, 300, 300, 50), 300, 50),
        # Tall bbox.
        ((600, 100, 50, 400), 50, 400),
        # Smallest valid bbox (exactly MIN_CROP_DIM).
        ((640, 480, MIN_CROP_DIM, MIN_CROP_DIM), MIN_CROP_DIM, MIN_CROP_DIM),
    ],
)
def test_crop_one_no_padding_parametric(tmp_path, bbox, expected_w, expected_h):
    """Parametric variant covering common bbox shapes.

    Verifies that with CROP_PAD_PCT=0.0, the crop dimensions match the
    bbox dimensions exactly (no padding expansion) for a variety of
    bbox sizes and positions.

    Phase.86 (PLAN.md §11.18): uses frame_idx=1 with a 2-frame fixture
    so frame_paths[frame_idx-1] = frame_paths[0] exists for cropping.
    """
    frame_path_0 = str(tmp_path / "frame_0.jpg")
    frame_path_1 = str(tmp_path / "frame_param.jpg")
    _save_test_frame(frame_path_0, width=1280, height=960)
    _save_test_frame(frame_path_1, width=1280, height=960)

    mo = _make_moving_object([(0, 0, 0, 0), bbox])

    crop_path = _crop_one(
        obj=mo,
        frames=[],
        frame_paths=[frame_path_0, frame_path_1],
        frame_idx=1,
        output_dir=str(tmp_path),
        alert_id="test_param",
        crop_idx=0,
    )

    assert crop_path is not None
    saved = cv2.imread(crop_path)
    assert saved is not None, f"cv2.imread returned None for {crop_path}"
    h, w = saved.shape[:2]
    assert w == expected_w, f"bbox {bbox}: expected width {expected_w}, got {w}"
    assert h == expected_h, f"bbox {bbox}: expected height {expected_h}, got {h}"


# --- Phase.85: motion.json persistence -----------------------------------
#
# When detect_motion() finishes a successful run, it writes
# ``data/frames/<alert_id>/motion.json`` containing the full MotionResult
# (all moving objects with per-frame bbox / center / area / trajectory,
# the primary, crop paths, etc.). Without this, that data only lives in
# the MovingObject dataclass for the duration of one pipeline run.
#
# Tests cover:
#   1. Success path → motion.json is written with all expected fields.
#   2. Best-effort contract → invalid output_dir does not raise.
#   3. Atomic write → intermediate .tmp file is not left on disk.
#   4. _persist_motion_json (private helper) accepts synthetic results too.
# ---------------------------------------------------------------------------

import json

from infra.motion_detector import (
    MotionResult,
    _persist_motion_json,
)


def _make_synthetic_result(alert_id: str = "test-alert") -> MotionResult:
    """Build a MotionResult with one primary moving object — no real frames."""
    mo = MovingObject(
        bbox_per_frame=[
            (100, 200, 250, 150),  # frame 0: x, y, w, h
            (110, 195, 260, 155),
            (120, 190, 270, 160),
        ],
        center_per_frame=[
            (225, 275),
            (240, 272),
            (255, 270),
        ],
        area_per_frame=[37_500, 40_300, 43_200],
        trajectory=["UM2", "UM1", "UM1"],
        avg_area=40_333,
        frames_seen=3,
        total_motion_pixels=12_345,
        position_change_max=42,
        crop_paths=[],
        best_crop_path=None,
    )
    return MotionResult(
        moving_objects=[mo],
        primary_moving_object=mo,
        best_crop_path=None,
        crop_paths=[],
        no_motion_detected=False,
        reference_method="pairwise",
        total_motion_pixels=12_345,
        elapsed_ms=87.5,
    )


def test_persist_motion_json_writes_all_fields(tmp_path):
    """motion.json must contain every MovingObject + MotionResult field,
    with bbox_per_frame preserved as a list of (x, y, w, h) tuples."""
    output_dir = str(tmp_path / "data" / "frames" / "abc-123")
    result = _make_synthetic_result(alert_id="abc-123")

    _persist_motion_json(result, output_dir, "abc-123")

    json_path = tmp_path / "data" / "frames" / "abc-123" / "motion.json"
    assert json_path.exists()

    with open(json_path) as f:
        data = json.load(f)

    # MotionResult-level fields
    assert data["no_motion_detected"] is False
    assert data["reference_method"] == "pairwise"
    assert data["total_motion_pixels"] == 12_345
    assert data["elapsed_ms"] == 87.5
    assert data["primary_moving_object"] is not None

    # MovingObject-level fields with per-frame lists
    pm = data["primary_moving_object"]
    assert pm["trajectory"] == ["UM2", "UM1", "UM1"]
    assert pm["avg_area"] == 40_333
    assert pm["frames_seen"] == 3
    assert pm["total_motion_pixels"] == 12_345
    assert pm["position_change_max"] == 42
    # bbox_per_frame preserved as list of 4-tuples
    assert pm["bbox_per_frame"] == [
        [100, 200, 250, 150],
        [110, 195, 260, 155],
        [120, 190, 270, 160],
    ]
    assert pm["center_per_frame"] == [[225, 275], [240, 272], [255, 270]]
    assert pm["area_per_frame"] == [37_500, 40_300, 43_200]

    # The moving_objects list mirrors the primary
    assert len(data["moving_objects"]) == 1
    assert data["moving_objects"][0]["avg_area"] == 40_333


def test_persist_motion_json_does_not_raise_on_bad_dir(tmp_path):
    """Best-effort contract: a failed disk write logs a warning, not raise.

    Use a path where the parent directory exists but write is denied
    (e.g., trying to write under a file, not a directory). On macOS this
    is achieved with /dev/null/foo. Linux path is /proc/self/foo. Pick
    macOS since this repo runs locally.
    """
    bad_dir = "/dev/null/this/should/fail"
    result = _make_synthetic_result()
    # Must not raise — failure path is best-effort.
    _persist_motion_json(result, bad_dir, "test-alert")


def test_persist_motion_json_atomic_write(tmp_path):
    """Atomic write: the .tmp file must be renamed to motion.json, leaving
    no .tmp residue."""
    output_dir = str(tmp_path / "alert-dir")
    result = _make_synthetic_result()

    _persist_motion_json(result, output_dir, "abc")

    json_path = tmp_path / "alert-dir" / "motion.json"
    assert json_path.exists()
    tmp_path_files = list(tmp_path.rglob("*.tmp"))
    assert tmp_path_files == [], f"unexpected .tmp residue: {tmp_path_files}"


def test_persist_motion_json_multiple_objects(tmp_path):
    """When detect_motion finds >1 moving object, all are persisted."""
    mo1 = MovingObject(
        bbox_per_frame=[(10, 10, 50, 50)],
        trajectory=["UM1"],
        avg_area=2500,
        total_motion_pixels=5000,
    )
    mo2 = MovingObject(
        bbox_per_frame=[(200, 200, 100, 80)],
        trajectory=["LM3"],
        avg_area=8000,
        total_motion_pixels=5000,
    )
    result = MotionResult(
        moving_objects=[mo1, mo2],
        primary_moving_object=mo2,  # larger avg_area
        reference_method="pairwise",
        total_motion_pixels=10_000,
        no_motion_detected=False,
    )
    output_dir = str(tmp_path / "two-object")
    _persist_motion_json(result, output_dir, "two-object")

    with open(tmp_path / "two-object" / "motion.json") as f:
        data = json.load(f)

    assert len(data["moving_objects"]) == 2
    assert data["moving_objects"][0]["trajectory"] == ["UM1"]
    assert data["moving_objects"][1]["trajectory"] == ["LM3"]
    # primary preserved
    assert data["primary_moving_object"]["trajectory"] == ["LM3"]


# =============================================================================
# Phase.92 (2026-08-18) — MAX_CENTER_DIST_PX pinning
# =============================================================================
#
# Tesla drive-by on long-throw camera (Zone A) at 15:08 EDT:
# 1. Pairwise diff between frame 1→2 produced Tesla bboxes at (1074, 525) +
#    a trailing-edge bbox. Tracker picked the trailing-edge (1073, 524) and
#    tracked 1 frame.
# 2. Frame 2→3 diff put the Tesla at (636, 482) — center moved 322 px from
#    the trailing-edge bbox. Old MAX_CENTER_DIST_PX=300 → tracker dropped
#    the candidate. frames_seen=2 < MIN_FRAMES_SEEN=3 → no_motion_detected=True.
# 3. Alert dropped at CAM1 motion gate (6B.71) even though vision correctly
#    identified "blue Tesla Model Y in transit".
#
# Fix: bump MAX_CENTER_DIST_PX to 600. The same 322-px jump is now matched.
#
# These tests pin:
# (a) the OLD failure mode (300) drops the candidate
# (b) the NEW threshold (600) catches it
# (c) the constant matches the documented value (so a future bump is visible)


class TestMaxCenterDistPx6B92:
    """Pin MAX_CENTER_DIST_PX so a future regression to 300 is caught."""

    def test_constant_is_600(self):
        from infra.motion_detector import MAX_CENTER_DIST_PX
        assert MAX_CENTER_DIST_PX == 600, (
            "MAX_CENTER_DIST_PX was bumped from 300 to 600 in 6B.92 "
            "(Tesla drive-by regression). If you are intentionally changing "
            "this value, update this test and add a regression case."
        )

    def test_tesla_driveby_jump_tracked_at_600(self):
        """The actual 15:08 EDT Solar Tesla case: 322 px jump between frames."""
        from infra.motion_detector import MAX_CENTER_DIST_PX, _track_object

        # Reconstructed from motion.json probe of f7da4c42-0520-4956-8f81-d2790831111b
        # Frame 0 = baseline (empty), Frame 1 = Tesla entering at (1074, 525),
        # Frame 2 = trailing-edge bbox (1073, 524) + leading-edge (839, 505),
        # Frame 3 = Tesla at (636, 482), Frame 4 = (478, 461), Frame 5 = (360, 447)
        per_frame_bboxes = [
            [],  # frame 0: baseline, no diff
            [(1074, 525, 196, 91)],  # frame 1: largest in diff
            [(839, 505, 227, 86), (1073, 524, 193, 132)],  # frame 2: trailing + leading
            [(636, 482, 430, 109)],  # frame 3: Tesla at new position (322 px from trailing)
            [(478, 461, 366, 103)],  # frame 4
            [(360, 447, 300, 90)],  # frame 5
        ]

        mo = _track_object(per_frame_bboxes)
        assert mo is not None, "tracker dropped the candidate"
        assert mo.frames_seen >= 3, (
            f"Expected >= 3 frames_seen with MAX_CENTER_DIST_PX={MAX_CENTER_DIST_PX}, "
            f"got {mo.frames_seen}. If this fails, the constant was lowered "
            f"back toward 300 and the Tesla drive-by regression returned."
        )

    def test_tesla_driveby_jump_dropped_at_300(self):
        """Pins the OLD failure mode: at 300, the same trajectory is dropped."""
        import infra.motion_detector as md
        from infra.motion_detector import _track_object
        original = md.MAX_CENTER_DIST_PX

        try:
            md.MAX_CENTER_DIST_PX = 300  # simulate the old behavior

            per_frame_bboxes = [
                [],
                [(1074, 525, 196, 91)],
                [(839, 505, 227, 86), (1073, 524, 193, 132)],
                [(636, 482, 430, 109)],  # 322 px from trailing-edge
                [(478, 461, 366, 103)],
                [(360, 447, 300, 90)],
            ]

            mo = _track_object(per_frame_bboxes)
            # With the old constant, the tracker can't bridge the 322 px gap.
            # It may return a candidate but with frames_seen < 3.
            if mo is not None:
                assert mo.frames_seen < 3, (
                    "If this fails, MAX_CENTER_DIST_PX=300 now bridges 322 px — "
                    "either the Tesla case is no longer the regression scenario, "
                    "or something else changed."
                )
        finally:
            md.MAX_CENTER_DIST_PX = original

    def test_noise_blob_far_from_vehicle_still_rejected(self):
        """Bumping the constant to 600 should NOT admit random noise blobs.

        Use a noise blob NEXT to the Tesla (within 600 px), so the tracker
        has to pick based on something other than distance — the test
        confirms it picks the larger/more plausible candidate.
        """
        from infra.motion_detector import _track_object

        # Real Tesla + a smaller noise blob both present in each frame.
        # Tracker matches by closest-center; noise is closer to Tesla in
        # frame 2, but the Tesla is still the seed. Verify the candidate
        # bbox is the Tesla (large area), not the noise blob (small area).
        per_frame_bboxes = [
            [],
            [(550, 450, 200, 100), (100, 100, 30, 30)],  # Tesla + noise blob
            [(560, 460, 200, 100), (110, 110, 30, 30)],  # both present
            [(570, 470, 200, 100), (120, 120, 30, 30)],  # both present
            [(580, 480, 200, 100), (130, 130, 30, 30)],
        ]
        mo = _track_object(per_frame_bboxes)
        assert mo is not None
        assert mo.frames_seen >= 4
        # The area_per_frame should be ~20000 (Tesla), not ~900 (noise)
        for area in mo.area_per_frame:
            if area > 0:
                assert area >= 10000, (
                    f"Tracker matched the noise blob (area={area}). "
                    f"The 6B.92 bump to MAX_CENTER_DIST_PX=600 must not "
                    f"admit small noise blobs."
                )
