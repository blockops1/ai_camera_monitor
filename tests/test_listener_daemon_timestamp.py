"""
test_listener_daemon_timestamp.py -- Regression test for US-073f.

Verifies that _run_pipeline parses the webhook's ISO 8601 timestamp and
forwards it through _stage_gate to infra.gate.run().  Before this fix,
timestamp was None on every real alert, short-circuiting the
`if timestamp is not None` guard in classify_frame() and keeping
night_suppress_camera_class dormant.

Acceptance criteria (US-073f):
  1. _run_pipeline parses alert["timestamp"] and passes it to _stage_gate.
  2. _stage_gate forwards the timestamp to infra.gate.run(timestamp=ts).
  3. Fallback to datetime.now(UTC) when alert["timestamp"] is missing.
  4. Existing tests still pass; 0 new failures.
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_gate_ok():
    """Return a mock GateVerdict with classification='vehicle'."""
    from infra.gate import GateVerdict

    v = MagicMock(spec=GateVerdict)
    v.classification = "vehicle"
    v.class_label = "car"
    v.confidence = 0.95
    v.reason = "high_conf_vehicle"
    v.crop_a = None
    v.crop_b = None
    v.pairwise_diff_path = None
    v.is_none.return_value = False

    gsum = {
        "classification": "pass",
        "class_label": "car",
        "confidence": 0.95,
        "reason": "high_conf_vehicle",
    }
    return {
        "status": "ok",
        "gate_verdict": v,
        "gsum": gsum,
    }


def _make_artifacts():
    return {
        "crop_a_path": "/tmp/crop_a.png",
        "crop_b_path": "/tmp/crop_b.png",
        "composite_path": "/tmp/composite.png",
    }


def _make_alert(timestamp_str=None):
    alert = {
        "id": "test-ts-001",
        "camera_id": "OUTSIDE_BACK_SOLAR",
        "camera_label": "Outside Back Solar",
        "classification": "md",
        "frames": ["/tmp/frame_001.png", "/tmp/frame_002.png"],
    }
    if timestamp_str is not None:
        alert["timestamp"] = timestamp_str
    return alert


# ---------------------------------------------------------------------------
# AC1: _run_pipeline parses timestamp and passes to _stage_gate
# ---------------------------------------------------------------------------


def test_stage_gate_receives_parsed_datetime():
    """_stage_gate is called with a tz-aware datetime from alert["timestamp"]."""
    from listener.daemon import _run_pipeline

    ts = "2026-09-23T21:19:29Z"
    alert = _make_alert(ts)

    received_ts = None

    def capture_stage_gate(frames, camera_id, alert_id, timestamp):
        nonlocal received_ts
        received_ts = timestamp
        return _make_gate_ok()

    with patch("listener.daemon.stage_load_frames", return_value=alert["frames"]), \
         patch("listener.daemon._stage_gate", side_effect=capture_stage_gate), \
         patch("listener.daemon.stage_cooldown_check", return_value={"status": "ok"}), \
         patch("listener.daemon.stage_prepare_artifacts", return_value=_make_artifacts()), \
         patch("listener.daemon.stage_verify_class_vm1",
               return_value={"class": "vehicle", "subclass": "sedan"}), \
         patch("listener.daemon.stage_build_tg1", return_value={"caption": "tg1"}), \
         patch("listener.daemon.stage_detail_class_vm2",
               return_value={"class_confirmed": "vehicle"}), \
         patch("listener.daemon.stage_build_tg2", return_value={"caption": "tg2"}), \
         patch("listener.daemon.stage_perform_match", return_value={}), \
         patch("listener.daemon.stage_build_tg3", return_value={"caption": "tg3"}), \
         patch("listener.daemon.stage_apply_baseline_override", return_value=alert):

        _run_pipeline(alert)

    assert received_ts is not None
    assert isinstance(received_ts, datetime)
    assert received_ts.tzinfo is not None
    assert received_ts.year == 2026
    assert received_ts.month == 9
    assert received_ts.day == 23


def test_stage_gate_receives_fallback_when_timestamp_missing():
    """When alert has no "timestamp" key, _stage_gate gets datetime.now(UTC)."""
    from listener.daemon import _run_pipeline

    alert = _make_alert(timestamp_str=None)  # no timestamp key
    received_ts = None

    def capture_stage_gate(frames, camera_id, alert_id, timestamp):
        nonlocal received_ts
        received_ts = timestamp
        return _make_gate_ok()

    with patch("listener.daemon.stage_load_frames", return_value=alert["frames"]), \
         patch("listener.daemon._stage_gate", side_effect=capture_stage_gate), \
         patch("listener.daemon.stage_cooldown_check", return_value={"status": "ok"}), \
         patch("listener.daemon.stage_prepare_artifacts", return_value=_make_artifacts()), \
         patch("listener.daemon.stage_verify_class_vm1",
               return_value={"class": "vehicle", "subclass": "sedan"}), \
         patch("listener.daemon.stage_build_tg1", return_value={"caption": "tg1"}), \
         patch("listener.daemon.stage_detail_class_vm2",
               return_value={"class_confirmed": "vehicle"}), \
         patch("listener.daemon.stage_build_tg2", return_value={"caption": "tg2"}), \
         patch("listener.daemon.stage_perform_match", return_value={}), \
         patch("listener.daemon.stage_build_tg3", return_value={"caption": "tg3"}), \
         patch("listener.daemon.stage_apply_baseline_override", return_value=alert):

        _run_pipeline(alert)

    assert received_ts is not None
    assert received_ts.tzinfo is not None


def test_stage_gate_receives_fallback_on_bad_timestamp():
    """When alert["timestamp"] is unparseable, _stage_gate gets datetime.now(UTC)."""
    from listener.daemon import _run_pipeline

    alert = _make_alert(timestamp_str="not-a-timestamp")
    received_ts = None

    def capture_stage_gate(frames, camera_id, alert_id, timestamp):
        nonlocal received_ts
        received_ts = timestamp
        return _make_gate_ok()

    with patch("listener.daemon.stage_load_frames", return_value=alert["frames"]), \
         patch("listener.daemon._stage_gate", side_effect=capture_stage_gate), \
         patch("listener.daemon.stage_cooldown_check", return_value={"status": "ok"}), \
         patch("listener.daemon.stage_prepare_artifacts", return_value=_make_artifacts()), \
         patch("listener.daemon.stage_verify_class_vm1",
               return_value={"class": "vehicle", "subclass": "sedan"}), \
         patch("listener.daemon.stage_build_tg1", return_value={"caption": "tg1"}), \
         patch("listener.daemon.stage_detail_class_vm2",
               return_value={"class_confirmed": "vehicle"}), \
         patch("listener.daemon.stage_build_tg2", return_value={"caption": "tg2"}), \
         patch("listener.daemon.stage_perform_match", return_value={}), \
         patch("listener.daemon.stage_build_tg3", return_value={"caption": "tg3"}), \
         patch("listener.daemon.stage_apply_baseline_override", return_value=alert):

        _run_pipeline(alert)

    assert received_ts is not None
    assert received_ts.tzinfo is not None
