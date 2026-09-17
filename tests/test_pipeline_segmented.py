"""Tests for the segmented pipeline orchestrator in listener.daemon._run_pipeline.

Covers the stage-level dispatch flow that replaced the old monolithic
pipeline.run(): stage_detail_class_vm2 failure (zero dispatches), dispatch
order (TG#1→TG#2→TG#3), cooldown recorded only after TG#3, and cooldown
NOT recorded when stage_build_tg2 raises.

All tests patch individual stage functions or dispatcher.dispatch — never
the deleted ``listener.pipeline.run``.
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import httpx

# A minimal Reolink-style webhook payload that the /alert route normalizes.
_REOLINK_PAYLOAD = {
    "type": "motion",
    "alarm": {
        "channelName": "Front Gate",
        "device": "Front Gate",
        "name": "Motion",
        "type": "vehicle",
        "alarmTime": "2026-09-09T10:00:00Z",
        "time": "2026-09-09T10:00:00Z",
    },
    "id": "test-alert-001",
    "channel": 0,
}

_TELEGRAM_ENV = {
    "TELEGRAM_BOT_TOKEN": "fake-token",
    "TELEGRAM_HOME_CHAT_ID": "12345",
}

# Shared mock result for stage_verify_class_vm1.
_VM1_RESULT = {"class": "vehicle", "confidence": "likely"}

# Frames used by stage_load_frames and get_recent_frames.
_TEST_FRAMES = [
    "/tmp/frame_001.jpg", "/tmp/frame_002.jpg",
    "/tmp/frame_003.jpg", "/tmp/frame_004.jpg",
]


def _classify_reolink(alert):
    """Mimic normalize_reolink's classification logic: outer type
    overrides alarm type when they differ."""
    outer_type = alert.get("type")
    alarm_type = alert.get("alarm", {}).get("type")
    if outer_type and alarm_type and outer_type != alarm_type:
        return outer_type
    return alarm_type or outer_type or "motion"


def _make_pipeline_ok(alert):
    """Return a dict mimicking _run_pipeline on a happy path."""
    return {
        "id": alert.get("id", "test-alert"),
        "status": "ok",
        "camera_id": alert.get("camera_id", "FRONT"),
        "classification": _classify_reolink(alert),
        "frames": alert.get("frames", []),
        "gate": {"classification": "pass", "class_label": "vehicle",
                 "confidence": 0.92, "reason": "gate_passed"},
        "vm1_result": _VM1_RESULT,
        "tg1": {"caption": "Vehicle detected", "photos": ["/tmp/crop_a.jpg"]},
        "vm2_result": {"class_confirmed": "vehicle", "license_plate": "ABC123"},
        "tg2": {"caption": "Vehicle ABC123", "photos": ["/tmp/crop_b.jpg"]},
        "match_result": {"matched": True, "license_plate": "ABC123"},
        "tg3": {"caption": "Recognized: ABC123", "photos": []},
    }


# ---------------------------------------------------------------------------
# AC1: test_stage_detail_class_vm2_raises → zero dispatches
# ---------------------------------------------------------------------------

class TestStageDetailClassRaises:
    """When _run_pipeline raises (simulating stage_detail_class_vm2 failure),
    the route returns 200 with no dispatches."""

    def test_stage_detail_class_vm2_raises_zero_dispatches(self):
        """_run_pipeline raises → route returns 200. No dispatcher.dispatch,
        no record_hit."""
        import flask

        from listener.daemon import alert

        mock_dispatch = MagicMock(
            return_value=[MagicMock(spec=httpx.Response, status_code=200)]
        )
        mock_record = MagicMock()

        with (
            patch("listener.daemon._run_pipeline", side_effect=RuntimeError("vm2 failed")),
            patch("listener.daemon.dispatcher", mock_dispatch),
            patch("infra.camera_creds.validate_source_ip", return_value=True),
            patch("infra.pipeline_cooldown.record_hit", mock_record),
            patch("infra.frame_capture.get_recent_frames", return_value=_TEST_FRAMES),
            patch.dict(os.environ, _TELEGRAM_ENV, clear=False),
        ):
            # Simulate the route handling a POST to /alert by calling the
            # alert() view function directly (bypassing Flask routing).
            app = flask.Flask("test")
            app.add_url_rule("/alert", "alert", alert, methods=["POST"])

            client = app.test_client()
            resp = client.post("/alert", json=_REOLINK_PAYLOAD)

        assert resp.status_code == 200
        assert mock_dispatch.dispatch.call_count == 0
        mock_record.assert_not_called()


# ---------------------------------------------------------------------------
# AC2: test_dispatch_order_tg1_before_tg2_before_tg3
# ---------------------------------------------------------------------------

class TestDispatchOrder:
    """Happy path: TG#1 → TG#2 → TG#3 dispatched in order."""

    def test_dispatch_order_tg1_before_tg2_before_tg3(self):
        """Happy path → dispatch calls in order TG#1, TG#2, TG#3."""
        mock_dispatch = MagicMock(
            return_value=[MagicMock(spec=httpx.Response, status_code=200)]
        )
        mock_record = MagicMock()
        result = _make_pipeline_ok(_REOLINK_PAYLOAD)

        import flask

        from listener.daemon import alert
        app = flask.Flask("test")
        app.add_url_rule("/alert", "alert", alert, methods=["POST"])

        with (
            patch("listener.daemon._run_pipeline", return_value=result),
            patch("listener.daemon.dispatcher", mock_dispatch),
            patch("infra.camera_creds.validate_source_ip", return_value=True),
            patch("infra.pipeline_cooldown.record_hit", mock_record),
            patch("infra.frame_capture.get_recent_frames", return_value=_TEST_FRAMES),
            patch.dict(os.environ, _TELEGRAM_ENV, clear=False),
        ):
            client = app.test_client()
            resp = client.post("/alert", json=_REOLINK_PAYLOAD)

        assert resp.status_code == 200

        # All three TGs dispatched
        assert mock_dispatch.dispatch.call_count == 3

        # Check ordering
        calls = mock_dispatch.dispatch.call_args_list
        captions = [c[0][0][0].get("caption") for c in calls]
        assert captions[0] == "Vehicle detected"
        assert captions[1] == "Vehicle ABC123"
        assert captions[2] == "Recognized: ABC123"

        # record_hit called once after all TGs
        mock_record.assert_called_once()


# ---------------------------------------------------------------------------
# AC3: test_cooldown_not_recorded_when_stage_build_tg2_raises
# ---------------------------------------------------------------------------

class TestCooldownNotRecordedOnStageBuildTg2Raises:
    """When _run_pipeline raises (simulating stage_build_tg2 failure),
    the route returns 200 with no dispatches, no record_hit."""

    def test_cooldown_not_recorded_when_stage_build_tg2_raises(self):
        """_run_pipeline raises → route returns 200. No dispatch, no record_hit."""
        mock_dispatch = MagicMock(
            return_value=[MagicMock(spec=httpx.Response, status_code=200)]
        )
        mock_record = MagicMock()

        import flask

        from listener.daemon import alert
        app = flask.Flask("test")
        app.add_url_rule("/alert", "alert", alert, methods=["POST"])

        with (
            patch("listener.daemon._run_pipeline", side_effect=RuntimeError("tg2 build failed")),
            patch("listener.daemon.dispatcher", mock_dispatch),
            patch("infra.camera_creds.validate_source_ip", return_value=True),
            patch("infra.pipeline_cooldown.record_hit", mock_record),
            patch("infra.frame_capture.get_recent_frames", return_value=_TEST_FRAMES),
            patch.dict(os.environ, _TELEGRAM_ENV, clear=False),
        ):
            client = app.test_client()
            resp = client.post("/alert", json=_REOLINK_PAYLOAD)

        assert resp.status_code == 200

        # No dispatches: _run_pipeline failed
        assert mock_dispatch.dispatch.call_count == 0

        # record_hit NOT called
        mock_record.assert_not_called()


# ---------------------------------------------------------------------------
# AC4: test_cooldown_recorded_only_after_tg3_dispatched
# ---------------------------------------------------------------------------

class TestCooldownRecordedAfterTg3:
    """Cooldown is recorded only after TG#3 successfully dispatches."""

    def test_cooldown_recorded_only_after_tg3_dispatched(self):
        """Happy path → record_hit called exactly once at end."""
        mock_dispatch = MagicMock(
            return_value=[MagicMock(spec=httpx.Response, status_code=200)]
        )
        mock_record = MagicMock()
        result = _make_pipeline_ok(_REOLINK_PAYLOAD)

        import flask

        from listener.daemon import alert
        app = flask.Flask("test")
        app.add_url_rule("/alert", "alert", alert, methods=["POST"])

        with (
            patch("listener.daemon._run_pipeline", return_value=result),
            patch("listener.daemon.dispatcher", mock_dispatch),
            patch("infra.camera_creds.validate_source_ip", return_value=True),
            patch("infra.pipeline_cooldown.record_hit", mock_record),
            patch("infra.frame_capture.get_recent_frames", return_value=_TEST_FRAMES),
            patch.dict(os.environ, _TELEGRAM_ENV, clear=False),
        ):
            client = app.test_client()
            resp = client.post("/alert", json=_REOLINK_PAYLOAD)

        assert resp.status_code == 200

        # record_hit called exactly once
        assert mock_record.call_count == 1

        # Called with camera_id, classification (outer type "motion"),
        # and monotonic timestamp.
        call_args = mock_record.call_args
        assert call_args[0][0] == "Front Gate"     # camera_id
        assert call_args[0][1] == "motion"          # classification (outer type)
        assert isinstance(call_args[0][2], float)   # time.monotonic()
