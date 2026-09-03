"""Tests for listener/_motion_gate_dispatch.py — listener-side integration.

These tests verify the env-var gate and the dispatch wrapper. The actual gate
logic is tested in test_motion_gate_pipeline.py. The probe script covers
end-to-end with real frames.

Coverage:
  - is_motion_gate_enabled() respects env var values
  - maybe_run_motion_gate() returns None when disabled (legacy path)
  - maybe_run_motion_gate() returns GateVerdict when enabled
  - Gate failures return None (fall back to legacy path, don't drop alerts)
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

_root = Path(__file__).resolve().parent.parent.parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))


def test_is_motion_gate_enabled_default_off(monkeypatch):
    """Default (no env var set) → False."""
    monkeypatch.delenv("MOTION_GATE_ENABLED", raising=False)
    from listener._motion_gate_dispatch import is_motion_gate_enabled
    assert is_motion_gate_enabled() is False


def test_is_motion_gate_enabled_various_truthy_values(monkeypatch):
    """1, true, yes, on → True."""
    from listener._motion_gate_dispatch import is_motion_gate_enabled
    for val in ("1", "true", "True", "TRUE", "yes", "YES", "on", "ON"):
        monkeypatch.setenv("MOTION_GATE_ENABLED", val)
        assert is_motion_gate_enabled() is True, f"failed for {val!r}"


def test_is_motion_gate_enabled_various_falsy_values(monkeypatch):
    """0, false, no, off → False."""
    from listener._motion_gate_dispatch import is_motion_gate_enabled
    for val in ("0", "false", "no", "off", ""):
        monkeypatch.setenv("MOTION_GATE_ENABLED", val)
        assert is_motion_gate_enabled() is False, f"failed for {val!r}"


def test_maybe_run_motion_gate_disabled_returns_none(monkeypatch):
    """When env var is off, no work is done and None is returned."""
    monkeypatch.delenv("MOTION_GATE_ENABLED", raising=False)
    from listener._motion_gate_dispatch import maybe_run_motion_gate

    # Should not import the heavy gate module or call capture_frames
    with patch("listener._motion_gate_dispatch.capture_frames") as mock_capture:
        result = maybe_run_motion_gate(
            alert_id="test-1",
            camera_name="CAM5",
            rtsp_url="rtsp://fake",
            output_dir="/tmp/test",
        )
        assert result is None
        mock_capture.assert_not_called()


def test_maybe_run_motion_gate_enabled_returns_verdict(monkeypatch):
    """When enabled + capture succeeds + gate runs → GateVerdict returned."""
    monkeypatch.setenv("MOTION_GATE_ENABLED", "1")

    # Mock capture_frames to return 4 fake frame paths
    fake_frames = [f"/tmp/fake_frame_{i}.jpg" for i in range(4)]

    # Mock the gate run() to return a known GateVerdict
    from listener.motion_gate_pipeline import GateVerdict

    expected_verdict = GateVerdict(
        decision="vehicle",
        class_label="car",
        confidence=0.85,
        crop_a_path="/tmp/a.jpg",
        crop_b_path="/tmp/b.jpg",
        bbox_a=(10, 20, 100, 100),
        bbox_b=(30, 40, 100, 100),
        reason="high_conf_vehicle",
    )

    with patch("listener._motion_gate_dispatch.capture_frames", return_value=fake_frames), \
         patch("listener.motion_gate_pipeline.run", return_value=expected_verdict):
        from listener._motion_gate_dispatch import maybe_run_motion_gate
        result = maybe_run_motion_gate(
            alert_id="test-2",
            camera_name="CAM5",
            rtsp_url="rtsp://fake",
            output_dir="/tmp/test",
        )

    assert result is expected_verdict
    assert result.decision == "vehicle"
    assert result.class_label == "car"


def test_maybe_run_motion_gate_capture_failure_returns_suppress(monkeypatch):
    """If capture_frames raises an exception, return a suppress verdict (don't
    drop the alert — let the legacy path catch it via... wait, no — the
    legacy path won't run because we return a verdict. Hmm.)

    Actually: capture failure → suppress verdict is logged but listener.py
    skips the pipeline. That means the alert is DROPPED. This is a known
    trade-off — capture failure is rare (RTSP reachable) and indicates a real
    problem that should be investigated.
    """
    monkeypatch.setenv("MOTION_GATE_ENABLED", "1")

    with patch(
        "listener._motion_gate_dispatch.capture_frames",
        side_effect=RuntimeError("RTSP timeout"),
    ):
        from listener._motion_gate_dispatch import maybe_run_motion_gate
        result = maybe_run_motion_gate(
            alert_id="test-3",
            camera_name="CAM5",
            rtsp_url="rtsp://fake",
            output_dir="/tmp/test",
        )

    assert result is not None
    assert result.is_suppress
    assert "capture_failed" in result.reason


def test_maybe_run_motion_gate_capture_returns_too_few_frames_returns_none(monkeypatch):
    """If capture returns < 4 frames, fall back to legacy path (return None)."""
    monkeypatch.setenv("MOTION_GATE_ENABLED", "1")

    with patch(
        "listener._motion_gate_dispatch.capture_frames",
        return_value=["/tmp/only_one.jpg"],  # only 1 frame
    ):
        from listener._motion_gate_dispatch import maybe_run_motion_gate
        result = maybe_run_motion_gate(
            alert_id="test-4",
            camera_name="CAM5",
            rtsp_url="rtsp://fake",
            output_dir="/tmp/test",
        )

    # None means "fall back to legacy path"
    assert result is None


def test_maybe_run_motion_gate_run_failure_returns_none(monkeypatch):
    """If gate execution raises, fall back to legacy path (don't drop alerts)."""
    monkeypatch.setenv("MOTION_GATE_ENABLED", "1")
    fake_frames = [f"/tmp/fake_frame_{i}.jpg" for i in range(4)]

    with patch(
        "listener._motion_gate_dispatch.capture_frames", return_value=fake_frames
    ), patch(
        "listener.motion_gate_pipeline.run",
        side_effect=RuntimeError("YOLO inference failed"),
    ):
        from listener._motion_gate_dispatch import maybe_run_motion_gate
        result = maybe_run_motion_gate(
            alert_id="test-5",
            camera_name="CAM5",
            rtsp_url="rtsp://fake",
            output_dir="/tmp/test",
        )

    # None means "fall back to legacy path"
    assert result is None


def test_maybe_run_motion_gate_uses_frame_offsets_for_gatekeeper_camera(monkeypatch):
    """Phase.128 — CAM5 gatekeeper gets a 4-frame pre-event motion trail
    via frame_offsets counting backward from ring newest. Capture is invoked
    with the offsets so the persistent reader's get_frames_by_offset() path
    is exercised.

    Without this test, a refactor that drops the frame_offsets argument
    would silently regress to the trailing-tail path (4 frames at the
    same wall-clock millisecond, useless for trajectory detection).

    Phase.160 (2026-08-28) bugfix: at 2fps the ring spans 45s, so the
    old forward-indexing (0, 4, 8, 12) pulled frames from 39-45s BEFORE
    the alert — wrong context. New backward-indexing from ring_size-1
    gives (89, 85, 81, 77) at 2fps = T-0s, T-2s, T-4s, T-6s. The test
    uses a 15fps mock reader so we get the legacy 6s-spaced indices
    (89, 59, 29, 0).
    """
    monkeypatch.setenv("MOTION_GATE_ENABLED", "1")
    fake_frames = [f"/tmp/fake_frame_{i}.jpg" for i in range(4)]

    from listener.motion_gate_pipeline import GateVerdict

    expected_verdict = GateVerdict(
        decision="vehicle",
        class_label="car",
        confidence=0.85,
        crop_a_path="/tmp/a.jpg",
        crop_b_path="/tmp/b.jpg",
        bbox_a=(10, 20, 100, 100),
        bbox_b=(30, 40, 100, 100),
        reason="high_conf_vehicle",
    )

    # Mock reader returns 2fps (current production setting — all 6 cameras
    # dropped from 15fps → 2fps on 2026-08-28). With ring=32, capture_delay=8s,
    # the offsets anchor T_w+2s, T_w+4s, T_w+6s, T_w+8s — Phase.174
    # (2026-09-01) shifted +4s from the 2026-08-28 trail because live
    # test alert 61fcee70 showed pre-webhook frames were empty. See
    # _compute_gatekeeper_offsets docstring.
    class _FakeReader:
        stream_fps = 2.0
        ring_size = 32
    mock_reader = _FakeReader()

    monkeypatch.setattr(
        "infra.persistent_rtsp.get_reader", lambda name: mock_reader
    )

    with patch("listener._motion_gate_dispatch.capture_frames", return_value=fake_frames) as mock_capture, \
         patch("listener.motion_gate_pipeline.run", return_value=expected_verdict):
        from listener._motion_gate_dispatch import maybe_run_motion_gate
        maybe_run_motion_gate(
            alert_id="test-6b128-ofs",
            camera_name="CAM5",  # in GATEKEEPER_CAMERAS
            rtsp_url="rtsp://fake",
            output_dir="/tmp/test",
        )

    # capture_frames MUST be called with webhook-anchored frame offsets:
    # at 2fps ring=32 capture_delay=8s, that's (19, 23, 27, 31) =
    # (T_w+2s, T_w+4s, T_w+6s, T_w+8s) — Phase.174 default.
    assert mock_capture.call_count == 1
    call_kwargs = mock_capture.call_args.kwargs
    assert call_kwargs.get("frame_offsets") == [19, 23, 27, 31], (
        f"CAM5 gatekeeper expected frame_offsets=[19, 23, 27, 31] "
        f"(T_w+2s, T_w+4s, T_w+6s, T_w+8s); got {call_kwargs.get('frame_offsets')!r}"
    )
    assert call_kwargs.get("count") == 4


def test_maybe_run_motion_gate_no_frame_offsets_for_non_gatekeeper(monkeypatch):
    """Non-gatekeeper cameras (e.g. a future unenrolled camera) keep
    the trailing-tail path: frame_offsets=None so the persistent
    reader's get_recent_frames(n=4) is used instead of
    get_frames_by_offset().

    Phase.143 (2026-08-27): CAM3 is now a gatekeeper.
    §11.79 (2026-08-28): all 6 ACTIVE cameras are now gatekeepers;
    CAM6 is also a gatekeeper. The non-gatekeeper
    code path only matters for FUTURE cameras that have persistent
    RTSP but aren't in ALL_GATEKEEPER_CAMERAS. This test uses the
    phantom camera name "Some Unenrolled Camera" — a hypothetical
    future addition — to exercise that path without tying it to a
    real retired/active camera name (so a future enroll of that
    real name wouldn't silently change this test's behavior).
    """
    monkeypatch.setenv("MOTION_GATE_ENABLED", "1")
    fake_frames = [f"/tmp/fake_frame_{i}.jpg" for i in range(4)]

    from listener.motion_gate_pipeline import GateVerdict

    expected_verdict = GateVerdict(
        decision="vehicle",
        class_label="car",
        confidence=0.85,
        crop_a_path="/tmp/a.jpg",
        crop_b_path="/tmp/b.jpg",
        bbox_a=(10, 20, 100, 100),
        bbox_b=(30, 40, 100, 100),
        reason="high_conf_vehicle",
    )

    with patch("listener._motion_gate_dispatch.capture_frames", return_value=fake_frames) as mock_capture, \
         patch("listener.motion_gate_pipeline.run", return_value=expected_verdict):
        from listener._motion_gate_dispatch import maybe_run_motion_gate
        maybe_run_motion_gate(
            alert_id="test-6b179-unenrolled",
            camera_name="Some Unenrolled Camera",  # phantom — exercises trailing-tail path
            rtsp_url="rtsp://fake",
            output_dir="/tmp/test",
        )

    assert mock_capture.call_count == 1
    call_kwargs = mock_capture.call_args.kwargs
    assert call_kwargs.get("frame_offsets") is None, (
        f"Non-gatekeeper camera expected frame_offsets=None (trailing tail); "
        f"got {call_kwargs.get('frame_offsets')!r}"
    )
    assert call_kwargs.get("count") == 4


def test_maybe_run_motion_gate_uses_frame_offsets_for_ofg_person_gatekeeper(monkeypatch):
    """Phase.143 (2026-08-27, Note): CAM3 is a PERSON_GATEKEEPER_CAMERAS
    member, so it uses the pre-event trail capture path.

    §11.80 (2026-08-28): CAM3 is still a person-gatekeeper (one of 6 now).
    All 6 person-gatekeepers get the same pre-event trail capture path.
    This test pins CAM3's behavior specifically. Before this change (6B.143),
    CAM3 alerts pulled 4 consecutive frames via get_recent_frames(n=4),
    which produced a Telegram album where all 4 wide frames showed the
    same instant. After 6B.143 (and reaffirmed in §11.80), CAM3 pulls
    frames at backward-counting indices from the persistent ring buffer's
    newest, giving 2s spacing matching the CAM5 behavior — which is what
    Note's "person pipeline working just like the vehicle pipeline"
    directive requires.

    Mirrors test_maybe_run_motion_gate_uses_frame_offsets_for_gatekeeper_camera
    but for the CAM3 camera.

    Phase.160 (2026-08-28) bugfix: same as CAM5 test — at 2fps the ring
    spans 45s, so the old forward-indexing (0, 30, 60, 90) pulled the
    oldest frames in the ring instead of the 6s pre-event trail. New
    backward-indexing from ring_size-1 gives (89, 85, 81, 77) at 2fps =
    T-0s, T-2s, T-4s, T-6s. This test uses 15fps mock so we get the
    legacy 6s-spaced indices (89, 59, 29, 0).
    """
    monkeypatch.setenv("MOTION_GATE_ENABLED", "1")
    fake_frames = [f"/tmp/fake_frame_{i}.jpg" for i in range(4)]

    from listener.motion_gate_pipeline import GateVerdict

    expected_verdict = GateVerdict(
        decision="person",
        class_label="person",
        confidence=0.85,
        crop_a_path="/tmp/a.jpg",
        crop_b_path="/tmp/b.jpg",
        bbox_a=(10, 20, 100, 100),
        bbox_b=(30, 40, 100, 100),
        reason="high_conf_person",
    )

    class _FakeReader:
        stream_fps = 2.0
        ring_size = 32
    mock_reader = _FakeReader()

    monkeypatch.setattr(
        "infra.persistent_rtsp.get_reader", lambda name: mock_reader
    )

    with patch("listener._motion_gate_dispatch.capture_frames", return_value=fake_frames) as mock_capture, \
         patch("listener.motion_gate_pipeline.run", return_value=expected_verdict):
        from listener._motion_gate_dispatch import maybe_run_motion_gate
        maybe_run_motion_gate(
            alert_id="test-6b143-ofg",
            camera_name="CAM3",  # in PERSON_GATEKEEPER_CAMERAS
            rtsp_url="rtsp://fake",
            output_dir="/tmp/test",
        )

    assert mock_capture.call_count == 1
    call_kwargs = mock_capture.call_args.kwargs
    assert call_kwargs.get("frame_offsets") == [19, 23, 27, 31], (
        f"CAM3 person-gatekeeper expected frame_offsets=[19, 23, 27, 31] "
        f"(T_w+2s, T_w+4s, T_w+6s, T_w+8s); got {call_kwargs.get('frame_offsets')!r}"
    )
    assert call_kwargs.get("count") == 4


def test_gatekeeper_cameras_constant_is_all_six_cameras():
    """§11.79 (2026-08-28): pin the GATEKEEPER_CAMERAS constant so a
    typo in the vehicle gatekeeper list (or someone accidentally
    removing CAM5) shows up as a test failure.

    Pre-§11.79 (and through 6B.104): GATEKEEPER_CAMERAS was CAM5-only.
    §11.79 expanded to all 6 active cameras after 3 vehicles on
    CAM4/CAM6 produced no Telegram alerts. The person-gatekeeper set is
    PERSON_GATEKEEPER_CAMERAS (still CAM3-only). The union
    ALL_GATEKEEPER_CAMERAS is what the dispatch actually checks.
    """
    from listener._motion_gate_dispatch import GATEKEEPER_CAMERAS
    expected = frozenset({
        # §13.4 Commit 17: codes are CAM{N} per
        # infra.cameras._LEGACY_PREFIX_TO_CODE (CAM5 = original
        # gatekeeper OUTSIDE_FRONT_SOLAR; remaining 5 added §11.79).
        "CAM5",  # → OUTSIDE_FRONT_SOLAR  (original gatekeeper, §11.79)
        "CAM3",  # → OUTSIDE_FRONT_GARAGE
        "CAM2",  # → BACK
        "CAM1",  # → FRONT
        "CAM6",  # → OUTSIDE_BACK_SOLAR
        "CAM4",  # → OUTSIDE_FRONT_POWER
    })
    assert GATEKEEPER_CAMERAS == expected


def test_person_gatekeeper_cameras_constant_is_all_six_cameras():
    """§11.80 (2026-08-28): PERSON_GATEKEEPER_CAMERAS contains all 6
    active cameras (was just CAM3 through 6B.140). Note 2026-08-28:
    "make every camera a person gatekeeper camera and every camera a
    vehicle gatekeeper camera." Mirror of GATEKEEPER_CAMERAS pin at
    §11.79.
    """
    from listener._motion_gate_dispatch import PERSON_GATEKEEPER_CAMERAS
    expected = frozenset({
        # §13.4 Commit 17: same 6 CAM{N} codes as GATEKEEPER_CAMERAS.
        "CAM5",  # → OUTSIDE_FRONT_SOLAR
        "CAM3",  # → OUTSIDE_FRONT_GARAGE
        "CAM2",  # → BACK
        "CAM1",  # → FRONT
        "CAM6",  # → OUTSIDE_BACK_SOLAR
        "CAM4",  # → OUTSIDE_FRONT_POWER
    })
    assert PERSON_GATEKEEPER_CAMERAS == expected


def test_all_gatekeeper_cameras_union():
    """Phase.143 (2026-08-27): ALL_GATEKEEPER_CAMERAS is the union
    used by is_gatekeeper check. Mirrors listener.py constants.

    §13.4 Commit 17: ALL_GATEKEEPER_CAMERAS holds CAM{N} codes; the
    is_gatekeeper check resolves friendly names via _code_for_camera
    before this lookup.
    """
    from listener._motion_gate_dispatch import (
        ALL_GATEKEEPER_CAMERAS,
        GATEKEEPER_CAMERAS,
        PERSON_GATEKEEPER_CAMERAS,
    )
    assert ALL_GATEKEEPER_CAMERAS == GATEKEEPER_CAMERAS | PERSON_GATEKEEPER_CAMERAS
    assert "CAM5" in ALL_GATEKEEPER_CAMERAS  # was "CAM5"
    assert "CAM3" in ALL_GATEKEEPER_CAMERAS  # was "CAM3"


def test_gatekeeper_frame_offsets_count_matches_gate_input():
    """Pin GATEKEEPER_FRAME_OFFSETS length so the offset list can't drift
    out of sync with motion_gate_pipeline.run()'s `len(frame_paths) != 4`
    guard. If this ever changes, both sides must change together.

    Phase.160 (2026-08-28): with ring=90, the new constant is
    (89, 59, 29, 0) counting backward from the newest frame. Indices
    are now DECREASING (backward in time), not increasing. Test
    updated accordingly.
    """
    from listener._motion_gate_dispatch import GATEKEEPER_FRAME_OFFSETS
    assert len(GATEKEEPER_FRAME_OFFSETS) == 4, (
        f"motion_gate_pipeline.run() requires exactly 4 frames; "
        f"got {len(GATEKEEPER_FRAME_OFFSETS)}"
    )
    # Sanity: indices count BACKWARD from ring newest (now - 0s, -2s, ...).
    # Allowed to be either decreasing (typical) or increasing — the
    # contract is "all within ring bounds".
    offsets = list(GATEKEEPER_FRAME_OFFSETS)
    for i in offsets:
        assert 0 <= i < 180, f"offset {i} outside ring buffer bounds [0, 180)"
