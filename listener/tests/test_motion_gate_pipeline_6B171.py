"""Phase 6B.175 tests — two independent per-frame subject bboxes.

maintainer 2026-09-01 (fe3f88c6 walk-test): "the AND of 1→2 and 2→3 should
be applied to frame_2; the AND of 2→3 and 3→4 should be applied to
frame_3." Each crop frame gets its own subject bbox computed from the
diff pair that brackets it.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

# Add repo root to sys.path so the listener/ + infra/ packages import.
_root = Path(__file__).resolve().parent.parent.parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

import cv2

from listener.motion_gate_pipeline import run
from listener.tests.test_motion_gate_pipeline import FakeClassifier, _verdict

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_synthetic_frames(out_dir: Path, motion: bool = True) -> list[str]:
    """4 frames, dark background (50), bright vehicle (220).

    Vehicle is 50x50 moving 30px/frame across 4 frames.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for i in range(4):
        frame = np.full((200, 200, 3), 50, dtype=np.uint8)
        if motion:
            x_offset = 30 + i * 30
            frame[80:130, x_offset : x_offset + 50] = (220, 220, 220)
        path = out_dir / f"frame_{i+1:03d}.jpg"
        cv2.imwrite(str(path), frame)
        paths.append(str(path))
    return paths


def _make_moving_vehicle_frames(out_dir: Path) -> list[str]:
    """4 frames, 50x50 white vehicle moving 30px/frame on dark background."""
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for i in range(4):
        frame = np.full((200, 200, 3), 50, dtype=np.uint8)
        x_offset = 30 + i * 30
        frame[80:130, x_offset : x_offset + 50] = (220, 220, 220)
        path = out_dir / f"frame_{i+1:03d}.jpg"
        cv2.imwrite(str(path), frame)
        paths.append(str(path))
    return paths


# ---------------------------------------------------------------------------
# 1. GateVerdict exposes subject_bbox_a + subject_bbox_b (Phase 6B.175)
# ---------------------------------------------------------------------------


def test_gateverdict_has_two_subject_bbox_fields(tmp_path, monkeypatch):
    """Phase 6B.175: GateVerdict exposes TWO subject bboxes —
    subject_bbox_a (for frame_2) and subject_bbox_b (for frame_3).
    Replaces 6B.173's single subject_bbox.
    """
    monkeypatch.setenv("GATE_KEEP_DISK_ARTIFACTS", "true")
    paths = _make_synthetic_frames(tmp_path / "frames", motion=True)
    classifier = FakeClassifier([
        _verdict("car", 0.85, "pass_with_hint"),
        _verdict("car", 0.78, "pass_with_hint"),
    ])
    verdict = run(
        frame_paths=paths,
        camera_name="CAM5",
        alert_id="test-6B175-fields",
        output_dir=str(tmp_path),
        classifier=classifier,
    )

    assert verdict.bbox_a is not None
    assert verdict.bbox_b is not None
    # New fields exist (Phase 6B.175)
    assert hasattr(verdict, "subject_bbox_a")
    assert hasattr(verdict, "subject_bbox_b")
    # Both should be non-None when motion is detected and the AND-regions
    # have real CCs
    assert verdict.subject_bbox_a is not None
    assert verdict.subject_bbox_b is not None
    # crop_bbox_a uses subject_bbox_a; crop_bbox_b uses subject_bbox_b
    assert verdict.crop_bbox_a == verdict.subject_bbox_a
    assert verdict.crop_bbox_b == verdict.subject_bbox_b


# ---------------------------------------------------------------------------
# 2. Subject bboxes come from independent ANDs of motion mask pairs
# ---------------------------------------------------------------------------


def test_subject_bbox_a_uses_diff_1to2_and_diff_2to3(tmp_path, monkeypatch):
    """subject_bbox_a = AND(diff(1,2), diff(2,3)) — applies to frame_2.

    With constant-speed motion across all 4 frames, the AND of
    diff(1,2) and diff(2,3) is the LEADING EDGE of the vehicle's
    position in frame_2.
    """
    monkeypatch.setenv("GATE_KEEP_DISK_ARTIFACTS", "true")
    paths = _make_moving_vehicle_frames(tmp_path / "frames")
    classifier = FakeClassifier([
        _verdict("car", 0.85, "pass_with_hint"),
        _verdict("car", 0.78, "pass_with_hint"),
    ])
    verdict = run(
        frame_paths=paths,
        camera_name="CAM5",
        alert_id="test-6B175-bbox-a",
        output_dir=str(tmp_path),
        classifier=classifier,
    )

    assert verdict.subject_bbox_a is not None, (
        "subject_bbox_a should be detected when diff(1,2) and "
        "diff(2,3) both have motion and they overlap"
    )
    assert verdict.bbox_a is not None
    assert verdict.bbox_b is not None
    _, _, aw, ah = verdict.subject_bbox_a
    a_area = aw * ah
    # AND-region should be a real bbox (positive area)
    assert a_area > 0
    # AND-region should fit inside the larger of the two diff bboxes
    da_area = verdict.bbox_a[2] * verdict.bbox_a[3]
    db_area = verdict.bbox_b[2] * verdict.bbox_b[3]
    assert a_area <= max(da_area, db_area), (
        f"subject_bbox_a (AND) area={a_area} should fit inside the larger "
        f"diff bbox area ({da_area}, {db_area})"
    )


def test_subject_bbox_b_uses_diff_2to3_and_diff_3to4(tmp_path, monkeypatch):
    """subject_bbox_b = AND(diff(2,3), diff(3,4)) — applies to frame_3.

    With constant-speed motion across all 4 frames, the AND of
    diff(2,3) and diff(3,4) is the LEADING EDGE of the vehicle's
    position in frame_3.
    """
    monkeypatch.setenv("GATE_KEEP_DISK_ARTIFACTS", "true")
    paths = _make_moving_vehicle_frames(tmp_path / "frames")
    classifier = FakeClassifier([
        _verdict("car", 0.85, "pass_with_hint"),
        _verdict("car", 0.78, "pass_with_hint"),
    ])
    verdict = run(
        frame_paths=paths,
        camera_name="CAM5",
        alert_id="test-6B175-bbox-b",
        output_dir=str(tmp_path),
        classifier=classifier,
    )

    assert verdict.subject_bbox_b is not None, (
        "subject_bbox_b should be detected when diff(2,3) and "
        "diff(3,4) both have motion and they overlap"
    )
    assert verdict.bbox_a is not None
    assert verdict.bbox_b is not None
    _, _, bw, bh = verdict.subject_bbox_b
    b_area = bw * bh
    assert b_area > 0
    da_area = verdict.bbox_a[2] * verdict.bbox_a[3]
    db_area = verdict.bbox_b[2] * verdict.bbox_b[3]
    assert b_area <= max(da_area, db_area), (
        f"subject_bbox_b (AND) area={b_area} should fit inside the larger "
        f"diff bbox area ({da_area}, {db_area})"
    )


def test_subject_bboxes_track_different_positions(tmp_path, monkeypatch):
    """With constant-speed motion, subject_bbox_a and subject_bbox_b
    should be at DIFFERENT x positions (frame_2 vs frame_3).

    This is the bug 6B.173 had: a single bbox applied to both crops
    meant crop_a showed the subject at frame_3's position, not
    frame_2's position.
    """
    monkeypatch.setenv("GATE_KEEP_DISK_ARTIFACTS", "true")
    paths = _make_moving_vehicle_frames(tmp_path / "frames")
    classifier = FakeClassifier([
        _verdict("car", 0.85, "pass_with_hint"),
        _verdict("car", 0.78, "pass_with_hint"),
    ])
    verdict = run(
        frame_paths=paths,
        camera_name="CAM5",
        alert_id="test-6B175-distinct",
        output_dir=str(tmp_path),
        classifier=classifier,
    )

    assert verdict.subject_bbox_a is not None
    assert verdict.subject_bbox_b is not None
    ax, _, _, _ = verdict.subject_bbox_a
    bx, _, _, _ = verdict.subject_bbox_b
    # Vehicle moves 30px/frame, so frame_2 position should be ~30px
    # LEFT of frame_3 position
    assert ax < bx, (
        f"subject_bbox_a x={ax} should be < subject_bbox_b x={bx} "
        f"(vehicle moved left-to-right between frame_2 and frame_3)"
    )


def test_crop_uses_per_frame_subject_bbox(tmp_path, monkeypatch):
    """Each crop uses its own AND-bbox (crop_a's coords != crop_b's
    coords when the subject moved between frames)."""
    monkeypatch.setenv("GATE_KEEP_DISK_ARTIFACTS", "true")
    paths = _make_moving_vehicle_frames(tmp_path / "frames")
    classifier = FakeClassifier([
        _verdict("car", 0.85, "pass_with_hint"),
        _verdict("car", 0.78, "pass_with_hint"),
    ])
    verdict = run(
        frame_paths=paths,
        camera_name="CAM5",
        alert_id="test-6B175-crops",
        output_dir=str(tmp_path),
        classifier=classifier,
    )

    assert verdict.crop_a_path is not None
    assert verdict.crop_b_path is not None
    assert verdict.crop_bbox_a is not None
    assert verdict.crop_bbox_b is not None
    crop_bbox_a = verdict.crop_bbox_a
    crop_bbox_b = verdict.crop_bbox_b
    # crop_a and crop_b should be at DIFFERENT positions (one per frame)
    assert crop_bbox_a != crop_bbox_b, (
        f"crop_a {crop_bbox_a} and crop_b {crop_bbox_b} should differ — "
        f"each is computed from its own AND of motion masks"
    )
    # crop_a filename pattern matches crop_bbox_a
    expected_a = f"crop{crop_bbox_a[0]}_{crop_bbox_a[1]}_"
    assert expected_a in Path(verdict.crop_a_path).name
    # crop_b filename pattern matches crop_bbox_b
    expected_b = f"crop{crop_bbox_b[0]}_{crop_bbox_b[1]}_"
    assert expected_b in Path(verdict.crop_b_path).name


# ---------------------------------------------------------------------------
# 3. No-subject detection suppresses alert (STRICT, no fallback)
# ---------------------------------------------------------------------------


def test_no_subject_detection_suppresses_alert(tmp_path, monkeypatch):
    """STRICT: when EITHER AND (subject_bbox_a or subject_bbox_b) has
    no CC above 500 px, the alert suppresses with
    reason="no_subject_detected".

    Construct frames where the motion happens only between
    frame_2→frame_3, then the vehicle is STATIONARY from frame_3→frame_4.
    diff(1,2) is empty, diff(2,3) has motion, diff(3,4) is empty.
    AND(diff(1,2), diff(2,3)) = empty → subject_bbox_a is None →
    suppress.
    """
    monkeypatch.setenv("GATE_KEEP_DISK_ARTIFACTS", "true")
    out_dir = tmp_path / "frames"
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    # Vehicle at x=0 in frame_1 (no prior motion), x=10 in frame_2,
    # x=30 in frame_3 (motion), then stationary at x=30 in frame_4.
    # diff(1,2) = motion 0→10 = small motion
    # diff(2,3) = motion 10→30 = 20px motion
    # diff(3,4) = no motion 30→30
    for i, x_offset in enumerate([0, 10, 30, 30]):
        frame = np.full((200, 400, 3), 50, dtype=np.uint8)
        frame[80:120, x_offset : x_offset + 30] = (220, 220, 220)
        path = out_dir / f"frame_{i+1:03d}.jpg"
        cv2.imwrite(str(path), frame)
        paths.append(str(path))

    classifier = FakeClassifier([
        _verdict("car", 0.85, "pass_with_hint"),
        _verdict("car", 0.78, "pass_with_hint"),
    ])
    verdict = run(
        frame_paths=paths,
        camera_name="CAM5",
        alert_id="test-6B175-suppress",
        output_dir=str(tmp_path),
        classifier=classifier,
    )

    # STRICT: at least one subject bbox is None → alert suppresses.
    # (Which one depends on whether AND(diff(1,2), diff(2,3)) or
    # AND(diff(2,3), diff(3,4)) is empty. diff(3,4) is empty here, so
    # subject_bbox_b is the empty one.)
    if verdict.bbox_a is not None:
        assert verdict.subject_bbox_b is None, (
            "AND of diff(2,3) and empty diff(3,4) should be empty — "
            "subject_bbox_b is None"
        )
        # STRICT: alert suppressed
        assert verdict.decision == "suppress"
        assert verdict.reason == "no_subject_detected"


# ---------------------------------------------------------------------------
# 4. Real-world regression — 7 morning alerts (sanity check)
# ---------------------------------------------------------------------------


def _find_morning_frames() -> dict[str, list[Path]]:
    """Locate the 7 morning alert frames from 2026-09-01."""
    candidates = [
        Path.home() / "ai_camera_monitor" / "data" / "frames",
        Path.home() / "ai_camera_monitor" / "frames",
        Path("/data/frames"),
    ]
    alert_ids = [
        "f6fd1798-c76c-4a63-a4e4-6af3432a177f",
        "e6492b79-4bc9-4d2e-829d-6f6822ecb140",
        "81dc7a2c-5179-4b80-b102-e0a029310a20",
        "b7dd2999-c7de-4d0c-ab61-db761690e12d",
        "3255fbb1-7eec-4492-a670-b7916aa11993",
        "3b967d96-70ca-4487-b767-a1ecad894476",
        "c7b4b3f5-af60-4a43-acf4-84fc62e34987",
    ]
    found = {}
    for root in candidates:
        if not root.exists():
            continue
        for aid in alert_ids:
            d = root / aid
            if not d.is_dir():
                continue
            all_frames = sorted(d.glob("frame_*.jpg"))
            source_frames = [p for p in all_frames if "_crop" not in p.name]
            if len(source_frames) >= 4:
                found[aid] = source_frames[:4]
        if found:
            return found
    return {}


@pytest.mark.skipif(
    not _find_morning_frames(),
    reason="morning alert frames not available on this machine",
)
def test_real_world_two_bboxes(tmp_path, monkeypatch):
    """Real 2026-09-01 morning alerts: two subject bboxes, one per crop.

    Each subject bbox should be tighter than (or equal to) the larger
    of its two input diff bboxes. The two bboxes may differ in
    position when the subject moved between frame_2 and frame_3.
    """
    monkeypatch.setenv("GATE_KEEP_DISK_ARTIFACTS", "true")
    frames_by_alert = _find_morning_frames()

    for alert_id, frame_paths in frames_by_alert.items():
        classifier = FakeClassifier([
            _verdict("car", 0.85, "pass_with_hint"),
            _verdict("car", 0.85, "pass_with_hint"),
        ])
        verdict = run(
            frame_paths=[str(p) for p in frame_paths],
            camera_name="CAM5",
            alert_id=alert_id,
            output_dir=str(tmp_path / alert_id),
            classifier=classifier,
        )
        # When both AND-regions have real CCs, each subject bbox should
        # be tighter than the larger of its two diff inputs.
        if (
            verdict.subject_bbox_a is not None
            and verdict.subject_bbox_b is not None
            and verdict.bbox_a is not None
            and verdict.bbox_b is not None
        ):
            sa = verdict.subject_bbox_a[2] * verdict.subject_bbox_a[3]
            sb = verdict.subject_bbox_b[2] * verdict.subject_bbox_b[3]
            da = verdict.bbox_a[2] * verdict.bbox_a[3]
            db = verdict.bbox_b[2] * verdict.bbox_b[3]
            assert sa <= max(da, db), (
                f"[{alert_id}] subject_bbox_a area={sa} should be <= "
                f"max(diff_a_area={da}, diff_b_area={db})"
            )
            assert sb <= max(da, db), (
                f"[{alert_id}] subject_bbox_b area={sb} should be <= "
                f"max(diff_a_area={da}, diff_b_area={db})"
            )