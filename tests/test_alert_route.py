"""
test_alert_route.py — Tests for listener/daemon.py /alert route.

Tests: flat payload normalization, Reolink nested shape, invalid JSON,
bad source IP (spoof), unknown payload shape, learned camera map,
frame pull, pipeline integration, end-to-end full pipeline.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from listener.daemon import app, normalize_flat, normalize_reolink


class TestNormalizeFlat:
    """Tests for normalize_flat() helper."""

    def test_flat_all_keys(self):
        """Flat payload with all required keys produces valid alert dict."""
        payload = {
            "camera": "FRONT",
            "ip": "192.168.1.39",
            "event": "motion",
            "timestamp": "2026-09-07T12:00:00Z",
        }
        result = normalize_flat(payload)
        assert result is not None
        assert result["camera_id"] == "FRONT"
        assert result["camera_label"] == "FRONT"
        assert result["classification"] == "motion"
        assert result["frames"] == []
        assert result["timestamp"] == "2026-09-07T12:00:00Z"
        assert "id" in result

    def test_flat_missing_keys_returns_none(self):
        """Flat payload missing required keys returns None."""
        partial = {"camera": "FRONT", "ip": "192.168.1.39"}
        assert normalize_flat(partial) is None


class TestNormalizeReolink:
    """Tests for normalize_reolink() helper."""

    def test_reolink_nested_shape(self):
        """Reolink default shape normalizes correctly."""
        payload = {
            "type": "motion",
            "alarm": {
                "type": "motion",
                "time": "2026-09-07T12:00:00Z",
                "channelName": "Front Door Outside",
                "device": "Front Door Outside",
            },
        }
        result = normalize_reolink(payload, "192.168.1.39")
        assert result is not None
        assert result["camera_id"] == "Front Door Outside"
        assert result["classification"] == "motion"
        assert result["frames"] == []

    def test_reolink_person_event(self):
        """Reolink person event normalizes correctly (outer=inner type)."""
        payload = {
            "type": "person",
            "alarm": {
                "type": "person",
                "time": "2026-09-07T14:30:00Z",
                "channelName": "Back Door Inside",
            },
        }
        result = normalize_reolink(payload, "192.168.1.85")
        assert result is not None
        assert result["classification"] == "person"
        assert result["camera_id"] == "Back Door Inside"

    def test_reolink_missing_alarm_returns_none(self):
        """Payload without alarm key returns None."""
        assert normalize_reolink({"type": "motion"}, "192.168.1.39") is None

    def test_reolink_alarm_not_dict_returns_none(self):
        """Payload with non-dict alarm returns None."""
        assert normalize_reolink({"alarm": "bad"}, "192.168.1.39") is None

    def test_reolink_outer_type_overrides(self):
        """Outer type overrides inner type when they differ."""
        payload = {
            "type": "vehicle",
            "alarm": {
                "type": "motion",
                "time": "2026-09-07T12:00:00Z",
                "channelName": "Front Door Outside",
            },
        }
        result = normalize_reolink(payload, "192.168.1.39")
        assert result is not None
        assert result["classification"] == "vehicle"

    def test_reolink_fallback_to_device_name(self):
        """When channelName missing, falls back to device field."""
        payload = {
            "type": "motion",
            "alarm": {
                "type": "motion",
                "time": "2026-09-07T12:00:00Z",
                "device": "Outside Front Garage",
            },
        }
        result = normalize_reolink(payload, "192.168.1.103")
        assert result is not None
        assert result["camera_id"] == "Outside Front Garage"


# ---------------------------------------------------------------------------
# Shared pipeline mock — returns a minimal pipeline result dict
# ---------------------------------------------------------------------------
def _pipeline_result(classification="motion", camera_id="FRONT"):
    """Return a minimal pipeline.run() result dict."""
    return {
        "status": "ok",
        "camera_id": camera_id,
        "classification": classification,
        "frames": [],
        "gate": {
            "decision": "pass",
            "class_label": "vehicle",
            "confidence": 0.9,
            "reason": "test",
        },
        "vm1_result": {"class": "vehicle", "confidence": "likely"},
        "tg1": {"caption": "Vehicle detected", "photos": []},
        "vm2_result": {
            "class_confirmed": "vehicle",
            "license_plate": "ABC123",
            "distinctive_features": ["roof_rack"],
        },
        "tg2": {"caption": "Vehicle ABC123", "photos": []},
        "match_result": {"matched": True, "license_plate": "ABC123"},
        "tg3": {"caption": "Recognized: ABC123", "photos": []},
    }


def _mock_pipeline():
    """Patch pipeline.run + get_recent_frames for /alert tests.

    Returns (mock_run, mock_frames) for inspection by callers.
    The pipeline mock preserves the classification from the alert dict.
    """

    def _run_side_effect(alert):
        return _pipeline_result(
            classification=alert.get("classification", "motion"),
            camera_id=alert.get("camera_id", "FRONT"),
        )

    mock_run = MagicMock(side_effect=_run_side_effect)
    mock_frames = MagicMock(return_value=["/tmp/frame_001.jpg"])
    return mock_run, mock_frames


class TestAlertRoute:
    """Tests for the /alert POST route."""

    def _client(self):
        """Return Flask test client with /alert route registered."""
        return app.test_client()

    def test_flat_payload_returns_200_with_pipeline_result(self):
        """Flat payload returns HTTP 200 with pipeline result dict."""
        c = self._client()
        payload = {
            "camera": "FRONT",
            "ip": "192.168.1.39",
            "event": "motion",
            "timestamp": "2026-09-07T12:00:00Z",
        }
        mock_run, mock_frames = _mock_pipeline()
        with (
            patch("infra.camera_creds.validate_source_ip", return_value=True),
            patch("listener.pipeline.run", mock_run),
            patch("infra.frame_capture.get_recent_frames", mock_frames),
        ):
            r = c.post("/alert", json=payload)
        assert r.status_code == 200
        body = r.get_json()
        assert body["status"] == "ok"
        assert body["camera_id"] == "FRONT"
        assert "gate" in body

    def test_reolink_payload_returns_200_with_pipeline_result(self):
        """Reolink nested payload returns HTTP 200 with pipeline result."""
        c = self._client()
        payload = {
            "type": "motion",
            "alarm": {
                "type": "motion",
                "time": "2026-09-07T12:00:00Z",
                "channelName": "Front Door Outside",
                "device": "Front Door Outside",
            },
        }
        mock_run, mock_frames = _mock_pipeline()
        with (
            patch("infra.camera_creds.validate_source_ip", return_value=True),
            patch("listener.pipeline.run", mock_run),
            patch("infra.frame_capture.get_recent_frames", mock_frames),
        ):
            r = c.post("/alert", json=payload)
        assert r.status_code == 200
        body = r.get_json()
        assert body["status"] == "ok"
        assert body["classification"] == "motion"

    def test_invalid_json_returns_400(self):
        """Invalid JSON body returns HTTP 400."""
        c = self._client()
        r = c.post(
            "/alert",
            data="not-json-at-all",
            content_type="application/json",
        )
        assert r.status_code == 400
        body = r.get_json()
        assert body["status"] == "error"

    def test_unknown_payload_shape_returns_400(self):
        """Payload with neither flat nor Reolink shape returns 400."""
        c = self._client()
        payload = {"foo": "bar", "baz": 42}
        r = c.post("/alert", json=payload)
        assert r.status_code == 400
        body = r.get_json()
        assert body["status"] == "error"

    def test_spoofed_ip_returns_403(self):
        """IP that doesn't match any camera returns HTTP 403."""
        c = self._client()
        payload = {
            "camera": "FRONT",
            "ip": "1.2.3.4",  # not FRONT's registered IP
            "event": "motion",
            "timestamp": "2026-09-07T12:00:00Z",
        }
        with patch("infra.camera_creds.validate_source_ip", return_value=False):
            r = c.post("/alert", json=payload)
        assert r.status_code == 403
        body = r.get_json()
        assert body["status"] == "error"

    def test_reolink_spoofed_ip_returns_403(self):
        """Reolink payload with spoofed IP returns 403."""
        c = self._client()
        payload = {
            "type": "motion",
            "alarm": {
                "type": "motion",
                "time": "2026-09-07T12:00:00Z",
                "channelName": "Front Door Outside",
            },
        }
        with (
            patch("infra.camera_creds.validate_source_ip", return_value=False),
            patch("infra.camera_creds.get_all_cameras", return_value={}),
        ):
            r = c.post("/alert", json=payload)
        assert r.status_code == 403

    def test_alert_has_pipeline_status_field(self):
        """Pipeline result status field appears in response."""
        c = self._client()
        payload = {
            "camera": "FRONT",
            "ip": "192.168.1.39",
            "event": "motion",
            "timestamp": "2026-09-07T12:00:00Z",
        }
        mock_run, mock_frames = _mock_pipeline()
        with (
            patch("infra.camera_creds.validate_source_ip", return_value=True),
            patch("listener.pipeline.run", mock_run),
            patch("infra.frame_capture.get_recent_frames", mock_frames),
        ):
            r = c.post("/alert", json=payload)
        body = r.get_json()
        assert "status" in body
        assert body["status"] in ("ok", "dropped", "suppressed")

    def test_pipeline_run_called_with_alert_dict(self):
        """pipeline.run receives the normalized alert dict."""
        c = self._client()
        payload = {
            "camera": "FRONT",
            "ip": "192.168.1.39",
            "event": "vehicle",
            "timestamp": "2026-09-07T12:00:00Z",
        }
        mock_run, mock_frames = _mock_pipeline()
        with (
            patch("infra.camera_creds.validate_source_ip", return_value=True),
            patch("listener.pipeline.run", mock_run),
            patch("infra.frame_capture.get_recent_frames", mock_frames),
        ):
            r = c.post("/alert", json=payload)
        assert r.status_code == 200
        # Verify pipeline.run was called
        mock_run.assert_called_once()
        call_args = mock_run.call_args[0][0]
        assert call_args["camera_id"] == "FRONT"
        assert call_args["classification"] == "vehicle"

    def test_get_recent_frames_called_with_camera_id(self):
        """infra.frame_capture.get_recent_frames called with camera_id."""
        c = self._client()
        payload = {
            "camera": "FRONT",
            "ip": "192.168.1.39",
            "event": "motion",
            "timestamp": "2026-09-07T12:00:00Z",
        }
        mock_run, mock_frames = _mock_pipeline()
        with (
            patch("infra.camera_creds.validate_source_ip", return_value=True),
            patch("listener.pipeline.run", mock_run),
            patch("infra.frame_capture.get_recent_frames", mock_frames),
        ):
            r = c.post("/alert", json=payload)
        assert r.status_code == 200
        # Verify get_recent_frames was called with correct args
        mock_frames.assert_called_once()
        call_args = mock_frames.call_args
        assert call_args[0][0] == "FRONT"
        assert call_args[1]["n"] == 4
        assert call_args[1]["offset_seconds"] == 6


# ---------------------------------------------------------------------------
# Integration / E2E tests
# ---------------------------------------------------------------------------


class TestAlertRouteIntegration:
    """Integration tests: full /alert flow with mocked pipeline."""

    def _client(self):
        return app.test_client()

    def test_full_pipeline_integration_flat(self):
        """Full pipeline integration: flat payload → 200 with complete result."""
        c = self._client()
        payload = {
            "camera": "FRONT",
            "ip": "192.168.1.39",
            "event": "motion",
            "timestamp": "2026-09-07T12:00:00Z",
        }
        mock_run, mock_frames = _mock_pipeline()
        with (
            patch("infra.camera_creds.validate_source_ip", return_value=True),
            patch("listener.pipeline.run", mock_run),
            patch("infra.frame_capture.get_recent_frames", mock_frames),
        ):
            r = c.post("/alert", json=payload)
        assert r.status_code == 200
        body = r.get_json()
        # Full pipeline result contains all expected keys
        for key in (
            "status",
            "camera_id",
            "classification",
            "gate",
            "vm1_result",
            "tg1",
            "vm2_result",
            "tg2",
            "match_result",
            "tg3",
        ):
            assert key in body, f"Missing key: {key}"

    def test_full_pipeline_integration_reolink(self):
        """Full pipeline integration: Reolink payload → 200 with result."""
        c = self._client()
        payload = {
            "type": "person",
            "alarm": {
                "type": "person",
                "time": "2026-09-07T14:30:00Z",
                "channelName": "Back Door Inside",
                "device": "Back Door Inside",
            },
        }
        mock_run, mock_frames = _mock_pipeline()
        with (
            patch("infra.camera_creds.validate_source_ip", return_value=True),
            patch("listener.pipeline.run", mock_run),
            patch("infra.frame_capture.get_recent_frames", mock_frames),
        ):
            r = c.post("/alert", json=payload)
        assert r.status_code == 200
        body = r.get_json()
        assert body["classification"] == "person"

    def test_end_to_end_pipeline_result_has_frames(self):
        """End-to-end: frames populated from get_recent_frames."""
        c = self._client()
        payload = {
            "camera": "FRONT",
            "ip": "192.168.1.39",
            "event": "motion",
            "timestamp": "2026-09-07T12:00:00Z",
        }
        expected_frames = ["/tmp/frame_001.jpg"]

        def _echo_frames(alert):
            """Pipeline mock that echoes frames back."""
            result = _pipeline_result(
                classification=alert.get("classification", "motion"),
                camera_id=alert.get("camera_id", "FRONT"),
            )
            result["frames"] = list(alert.get("frames", []))
            return result

        with (
            patch("infra.camera_creds.validate_source_ip", return_value=True),
            patch(
                "infra.frame_capture.get_recent_frames",
                return_value=expected_frames,
            ),
            patch("listener.pipeline.run", side_effect=_echo_frames),
        ):
            r = c.post("/alert", json=payload)
        assert r.status_code == 200
        body = r.get_json()
        # Frames were passed through pipeline
        assert body["frames"] == expected_frames

    def test_end_to_end_gated_alert(self):
        """End-to-end: gate-suppressed alert returns status 'dropped'."""
        c = self._client()
        payload = {
            "camera": "FRONT",
            "ip": "192.168.1.39",
            "event": "motion",
            "timestamp": "2026-09-07T12:00:00Z",
        }
        suppressed_result = {
            "status": "dropped",
            "camera_id": "FRONT",
            "classification": "motion",
            "reason": "no_vehicle",
            "gate": {
                "decision": "suppress",
                "class_label": "vehicle",
                "confidence": 0.3,
                "reason": "low_conf",
            },
        }
        with (
            patch("infra.camera_creds.validate_source_ip", return_value=True),
            patch(
                "infra.frame_capture.get_recent_frames",
                return_value=["/tmp/frame_001.jpg"],
            ),
            patch("listener.pipeline.run", return_value=suppressed_result),
        ):
            r = c.post("/alert", json=payload)
        assert r.status_code == 200
        body = r.get_json()
        assert body["status"] == "dropped"
