"""Tests for listener/_gate_aware_capture.py — bridge between gate + vehicle pipeline.

Phase.115 (2026-08-25): the legacy fallback path is removed. The
gate is the sole producer of frames + crops + diff bboxes. If the gate
didn't produce frames, SkipEvent is raised (no legacy fallback).

Tests cover:
  - gate_aware_vehicle_capture: gate_verdict present + frames on disk
    → sets ctx.frame_paths to 4 gate frames
  - gate_aware_vehicle_capture: gate_verdict missing → SkipEvent
  - gate_aware_vehicle_capture: gate_verdict frames not on disk → SkipEvent
  - ctx.capture_source observability field ("gate" or "missing")
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_root = Path(__file__).resolve().parent.parent.parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))


# --- Helpers ---------------------------------------------------------------


@pytest.fixture
def gate_verdict_with_frames(tmp_path):
    """Build a GateVerdict with 4 in-memory PIL frames.

    Phase.115 (§11.46.6): the verdict carries frames as PIL.Image,
    not as disk paths. We still set crop_*_path for any downstream
    code that wants to log/reference the on-disk artifacts.
    """
    from PIL import Image as _PILImage

    from listener.motion_gate_pipeline import GateVerdict

    out_dir = tmp_path / "alert"
    out_dir.mkdir()
    # Write 4 dummy frame files (kept on disk for postmortem tools).
    pil_frames = []
    for i in (1, 2, 3, 4):
        path = out_dir / f"frame_{i:03d}.jpg"
        img = _PILImage.new("RGB", (640, 480), color=(128, 128, 128))
        img.save(str(path), "JPEG")
        pil_frames.append(img)
    crop_a_path = str(out_dir / "frame_003.jpg")
    crop_b_path = str(out_dir / "frame_004.jpg")
    crop_a_pil = pil_frames[2].copy()
    crop_b_pil = pil_frames[3].copy()

    return GateVerdict(
        decision="vehicle",
        class_label="truck",
        confidence=0.76,
        crop_a_path=crop_a_path,
        crop_b_path=crop_b_path,
        bbox_a=(100, 200, 150, 100),
        bbox_b=(120, 220, 160, 110),
        frames=pil_frames,
        crop_a=crop_a_pil,
        crop_b=crop_b_pil,
        frame_paths=[str(out_dir / f"frame_{i:03d}.jpg") for i in (1, 2, 3, 4)],
        raw_verdicts=[],
        reason="high_conf_vehicle",
    )


@pytest.fixture
def gate_verdict_with_person_frames(tmp_path):
    """GateVerdict for the person path (Phase.139, §11.60).

    Mirrors `gate_verdict_with_frames` but with a person-class verdict
    and 4 distinct PIL frames (each is a different solid color so
    tests can assert which frame was selected).

    Frames indexed 0..3:
        frames[0] = pre-event-1  (solid red)
        frames[1] = pre-event-2  (solid green) ← person path picks this
        frames[2] = event        (solid blue)  ← person path picks this
        frames[3] = post-event   (solid yellow)
    """
    from PIL import Image as _PILImage

    from listener.motion_gate_pipeline import GateVerdict

    out_dir = tmp_path / "alert"
    out_dir.mkdir()
    colors = [(255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 0)]
    pil_frames = []
    for i, color in enumerate(colors, start=1):
        path = out_dir / f"frame_{i:03d}.jpg"
        img = _PILImage.new("RGB", (640, 480), color=color)
        img.save(str(path), "JPEG")
        pil_frames.append(img)
    crop_a_pil = pil_frames[2].copy()  # event frame's crop
    crop_b_pil = pil_frames[3].copy()  # post-event frame's crop

    return GateVerdict(
        decision="person",
        class_label="person",
        confidence=0.82,
        crop_a_path=str(out_dir / "frame_003.jpg"),
        crop_b_path=str(out_dir / "frame_004.jpg"),
        bbox_a=(180, 200, 320, 480),
        bbox_b=(200, 220, 300, 460),
        frames=pil_frames,
        crop_a=crop_a_pil,
        crop_b=crop_b_pil,
        frame_paths=[str(out_dir / f"frame_{i:03d}.jpg") for i in (1, 2, 3, 4)],
        raw_verdicts=[],
        reason="high_conf_person",
    )


@pytest.fixture
def gate_verdict_with_person_frames_no_disk(tmp_path):
    """GateVerdict for the person path WITH frame_paths=[] (verifies 6B.139 disk-write fallback).

    Mirrors `gate_verdict_with_person_frames` but sets frame_paths to
    an empty list — simulating GATE_KEEP_DISK_ARTIFACTS=false.
    gate_aware_person_capture must write the 2 selected PIL frames to
    disk under ctx.output_dir.
    """
    from PIL import Image as _PILImage

    from listener.motion_gate_pipeline import GateVerdict

    out_dir = tmp_path / "alert"
    out_dir.mkdir()
    pil_frames = []
    for i in range(1, 5):
        img = _PILImage.new("RGB", (640, 480), color=(50, 50, 50))
        pil_frames.append(img)
    crop_a_pil = pil_frames[2].copy()
    crop_b_pil = pil_frames[3].copy()

    return GateVerdict(
        decision="person",
        class_label="person",
        confidence=0.82,
        crop_a_path="",
        crop_b_path="",
        bbox_a=(180, 200, 320, 480),
        bbox_b=(200, 220, 300, 460),
        frames=pil_frames,
        crop_a=crop_a_pil,
        crop_b=crop_b_pil,
        frame_paths=[],  # GATE_KEEP_DISK_ARTIFACTS=false
        raw_verdicts=[],
        reason="high_conf_person",
    )


@pytest.fixture
def gate_verdict_no_frames():
    """GateVerdict with empty frames list (simulating gate failure)."""
    from listener.motion_gate_pipeline import GateVerdict

    return GateVerdict(
        decision="vehicle",
        class_label="truck",
        confidence=0.76,
        crop_a_path="/nonexistent/frame_003.jpg",
        crop_b_path="/nonexistent/frame_004.jpg",
        bbox_a=None,
        bbox_b=None,
        frames=[],
        crop_a=None,
        crop_b=None,
        frame_paths=[],
        raw_verdicts=[],
        reason="high_conf_vehicle",
    )


@pytest.fixture
def alert_ctx(tmp_path):
    """Minimal AlertContext for testing."""
    from listener.vehicle_event_pipeline import AlertContext

    return AlertContext(
        alert_id="test-alert-115",
        camera_name="CAM5",
        timestamp="2026-08-25T10:00:00-04:00",
        event_type="vehicle",
        rtsp_url="rtsp://test/oftest",
        output_dir=str(tmp_path / "alert"),
        is_vehicle_event=True,
        known_vehicles=[],
        bot_token="",
        chat_id="",
        api_url="http://test/v1/chat/completions",
        gatekeeper_cameras=frozenset({"CAM5"}),
    )


# --- Vehicle capture, gate verdict + frames on disk (FAST PATH) -----------


def test_vehicle_capture_fast_path_uses_gate_frames(
    monkeypatch, alert_ctx, gate_verdict_with_frames
):
    """Verdict with PIL frames → ctx.frames populated, ctx.frame_paths populated.

    Phase.115 (§11.46.6): the hot path reads PIL.Image objects from
    ctx.frames. ctx.frame_paths is also populated for code that still
    needs a path (logging, debugging).
    """
    from PIL import Image as _PILModule

    from listener._gate_aware_capture import gate_aware_vehicle_capture
    _PILImage = _PILModule.Image

    gate_verdict = gate_verdict_with_frames
    # Set output_dir to the dir the verdict's frames point at.
    alert_ctx.output_dir = str(gate_verdict.frame_paths[0]).rsplit("/", 1)[0]
    alert_ctx.gate_verdict = gate_verdict

    gate_aware_vehicle_capture(alert_ctx)

    # In-memory frames (authoritative for the pipeline hot path)
    assert len(alert_ctx.frames) == 4
    # All 4 are PIL.Image
    for img in alert_ctx.frames:
        assert isinstance(img, _PILImage), f"got {type(img)}"
    # Disk paths populated too (for logging/debugging)
    assert len(alert_ctx.frame_paths) == 4
    for p in alert_ctx.frame_paths:
        assert Path(p).is_file(), f"{p} not on disk"
    # Pre-cropped crops too
    assert isinstance(alert_ctx.crop_a, _PILImage)
    assert isinstance(alert_ctx.crop_b, _PILImage)
    assert alert_ctx.capture_source == "gate"


# --- Vehicle capture, frames missing → SKIP --------------------------------


def test_vehicle_capture_missing_frames_raises(
    monkeypatch, alert_ctx, gate_verdict_no_frames
):
    """Env on + verdict but crop files not on disk → raise SkipEvent."""
    from listener._gate_aware_capture import SkipEvent, gate_aware_vehicle_capture

    alert_ctx.gate_verdict = gate_verdict_no_frames

    with pytest.raises(SkipEvent):
        gate_aware_vehicle_capture(alert_ctx)


# --- Vehicle capture, no verdict → SKIP ------------------------------------


def test_vehicle_capture_no_verdict_raises(monkeypatch, alert_ctx):
    """Env on + gate_verdict None → raise SkipEvent (no legacy fallback)."""
    from listener._gate_aware_capture import SkipEvent, gate_aware_vehicle_capture

    alert_ctx.gate_verdict = None

    with pytest.raises(SkipEvent):
        gate_aware_vehicle_capture(alert_ctx)


# --- Vehicle ctx fields default to legacy values ---------------------------


def test_vehicle_ctx_defaults_when_no_gate_aware_call(alert_ctx):
    """AlertContext default: capture_source='rtsp', gate_verdict=None.

    Note: legacy_capture_avoided was removed in 6B.115 (no legacy
    path to avoid — that's now structural).
    """
    # No gate_aware call → fields stay at defaults
    assert alert_ctx.capture_source == "rtsp"
    assert alert_ctx.gate_verdict is None

# =============================================================================
# Phase.139 (§11.60) — person path gate-aware capture tests
# =============================================================================
#
# The person pipeline previously had a stub that delegated to a 6-second-late
# fresh RTSP pull. 6B.139 mirrors the vehicle path: read PIL frames + crops
# from the gate verdict, write 2 selected frames to disk for Qwen.
#
# These tests pin the new contract.


@pytest.fixture
def person_ctx(tmp_path):
    """Minimal PersonContext for testing (6B.139, §11.60).

    Phase.140 (2026-08-27): camera_name updated to CAM3.
    """
    from listener.person_event_pipeline import PersonContext

    return PersonContext(
        alert_id="test-alert-139",
        camera_name="CAM3",  # Phase.140
        timestamp="2026-08-27T10:00:00-04:00",
        event_type="person",
        rtsp_url="rtsp://test/outside-front-garage",
        output_dir=str(tmp_path / "alert"),
        bot_token="",
        chat_id="",
        api_url="",
    )


# --- Person capture fast path: verdict has frames + paths ---


def test_person_capture_fast_path_uses_gate_frames(person_ctx, gate_verdict_with_person_frames):
    """Verdict with 4 PIL + 4 paths → ctx.frames has 4, ctx.frame_paths has 2.

    6B.139 §11.60 contract:
      - ctx.frames = 4 PIL (full copy from verdict)
      - ctx.crop_a, ctx.crop_b = gate's crops
      - ctx.frame_paths = 2 paths (verdict.frame_paths[1], verdict.frame_paths[2])
      - ctx.selected_frames = 2 PIL (verdict.frames[1], verdict.frames[2])
      - ctx.capture_source = "gate"
    """
    from listener._gate_aware_capture import gate_aware_person_capture

    person_ctx.gate_verdict = gate_verdict_with_person_frames

    gate_aware_person_capture(person_ctx)

    assert len(person_ctx.frames) == 4
    # frame_paths: middle two of the gate's 4
    assert len(person_ctx.frame_paths) == 2
    assert person_ctx.frame_paths == [
        gate_verdict_with_person_frames.frame_paths[1],
        gate_verdict_with_person_frames.frame_paths[2],
    ]
    # selected_frames: middle two PIL
    assert len(person_ctx.selected_frames) == 2
    assert person_ctx.selected_frames[0] is gate_verdict_with_person_frames.frames[1]
    assert person_ctx.selected_frames[1] is gate_verdict_with_person_frames.frames[2]
    # crops
    assert person_ctx.crop_a is not None
    assert person_ctx.crop_b is not None
    # capture_source
    assert person_ctx.capture_source == "gate"


def test_person_capture_selected_frames_are_middle_two(person_ctx, gate_verdict_with_person_frames):
    """Verify frames[1] and frames[2] are specifically selected (bracketing frames)."""
    from listener._gate_aware_capture import gate_aware_person_capture

    person_ctx.gate_verdict = gate_verdict_with_person_frames

    gate_aware_person_capture(person_ctx)

    green = person_ctx.selected_frames[0]
    blue = person_ctx.selected_frames[1]
    # Verify by sampling a center pixel
    assert green.getpixel((320, 240)) == (0, 255, 0)  # green
    assert blue.getpixel((320, 240)) == (0, 0, 255)    # blue


def test_person_capture_no_verdict_raises(person_ctx):
    """No gate_verdict → SkipEvent (no legacy fallback, matches 6B.115 contract)."""
    from listener._gate_aware_capture import SkipEvent, gate_aware_person_capture

    person_ctx.gate_verdict = None

    with pytest.raises(SkipEvent):
        gate_aware_person_capture(person_ctx)
    assert person_ctx.capture_source == "missing"


def test_person_capture_missing_frames_raises(person_ctx, gate_verdict_no_frames):
    """Verdict present but frames empty → SkipEvent."""
    from listener._gate_aware_capture import SkipEvent, gate_aware_person_capture

    person_ctx.gate_verdict = gate_verdict_no_frames

    with pytest.raises(SkipEvent):
        gate_aware_person_capture(person_ctx)
    assert person_ctx.capture_source == "missing"


def test_person_capture_no_crops_still_proceeds(person_ctx, tmp_path):
    """Verdict has frames but crop_a/crop_b are None → no SkipEvent.

    Person events without YOLO-detected crops still get the wide-angle
    selected frames; crops are an enhancement, not a requirement.
    """
    from PIL import Image as _PILImage

    from listener._gate_aware_capture import gate_aware_person_capture
    from listener.motion_gate_pipeline import GateVerdict

    out_dir = tmp_path / "alert"
    out_dir.mkdir()
    pil_frames = [
        _PILImage.new("RGB", (640, 480), color=(i * 50, i * 50, i * 50))
        for i in range(4)
    ]

    verdict = GateVerdict(
        decision="person",
        class_label="person",
        confidence=0.7,
        crop_a_path="",
        crop_b_path="",
        bbox_a=None,
        bbox_b=None,
        frames=pil_frames,
        crop_a=None,
        crop_b=None,
        frame_paths=[str(out_dir / f"frame_{i:03d}.jpg") for i in (1, 2, 3, 4)],
        raw_verdicts=[],
        reason="high_conf_person",
    )
    person_ctx.gate_verdict = verdict

    gate_aware_person_capture(person_ctx)  # does NOT raise

    assert len(person_ctx.frames) == 4
    assert len(person_ctx.frame_paths) == 2
    assert person_ctx.crop_a is None
    assert person_ctx.crop_b is None
    assert person_ctx.capture_source == "gate"


def test_person_capture_writes_pil_when_verdict_paths_empty(
    person_ctx, gate_verdict_with_person_frames_no_disk
):
    """Verdict frame_paths=[] (GATE_KEEP_DISK_ARTIFACTS=false) → 6B.139 disk-write fallback.

    gate_aware_person_capture must write the 2 selected PIL frames to
    ctx.output_dir as frame_gate_001.jpg and frame_gate_002.jpg.
    """
    from pathlib import Path as _Path

    from listener._gate_aware_capture import gate_aware_person_capture

    person_ctx.gate_verdict = gate_verdict_with_person_frames_no_disk

    gate_aware_person_capture(person_ctx)

    assert len(person_ctx.frame_paths) == 2
    assert _Path(person_ctx.frame_paths[0]).name == "frame_gate_001.jpg"
    assert _Path(person_ctx.frame_paths[1]).name == "frame_gate_002.jpg"
    # Files exist on disk
    assert _Path(person_ctx.frame_paths[0]).is_file()
    assert _Path(person_ctx.frame_paths[1]).is_file()
    assert person_ctx.capture_source == "gate"


def test_person_capture_short_frames_raises(person_ctx):
    """Verdict with 2 frames (not 4) → SkipEvent."""
    from PIL import Image as _PILImage

    from listener._gate_aware_capture import SkipEvent, gate_aware_person_capture
    from listener.motion_gate_pipeline import GateVerdict

    verdict = GateVerdict(
        decision="person",
        class_label="person",
        confidence=0.7,
        crop_a_path="",
        crop_b_path="",
        bbox_a=None,
        bbox_b=None,
        frames=[_PILImage.new("RGB", (640, 480)), _PILImage.new("RGB", (640, 480))],
        crop_a=None,
        crop_b=None,
        frame_paths=[],
        raw_verdicts=[],
        reason="high_conf_person",
    )
    person_ctx.gate_verdict = verdict

    with pytest.raises(SkipEvent):
        gate_aware_person_capture(person_ctx)
    assert person_ctx.capture_source == "missing"


def test_person_capture_never_calls_capture_frames(person_ctx, gate_verdict_with_person_frames):
    """Regression: 6B.139 removes the second RTSP pull entirely."""
    from unittest.mock import patch

    from listener._gate_aware_capture import gate_aware_person_capture

    person_ctx.gate_verdict = gate_verdict_with_person_frames

    with patch("infra.frame_capture.capture_frames") as mock_capture:
        gate_aware_person_capture(person_ctx)
        mock_capture.assert_not_called()
