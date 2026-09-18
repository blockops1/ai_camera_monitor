"""
test_alert_route.py — Tests for listener/daemon.py /alert route.

Tests: Reolink nested shape, invalid JSON, bad source IP (spoof),
unknown payload shape, learned camera map, frame pull, pipeline
integration, end-to-end full pipeline.

Note: All IP addresses use the RFC 5737 documentation prefix
(192.0.2.0/24, TEST-NET-1) to avoid leaking production camera
addresses into the source tree. See RFC 5737, section 3.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from listener.daemon import app, normalize_reolink


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
        result = normalize_reolink(payload, "192.0.2.1")
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
        result = normalize_reolink(payload, "192.0.2.2")
        assert result is not None
        assert result["classification"] == "person"
        assert result["camera_id"] == "Back Door Inside"

    def test_reolink_missing_alarm_returns_none(self):
        """Payload without alarm key returns None."""
        assert normalize_reolink({"type": "motion"}, "192.0.2.1") is None

    def test_reolink_alarm_not_dict_returns_none(self):
        """Payload with non-dict alarm returns None."""
        assert normalize_reolink({"alarm": "bad"}, "192.0.2.1") is None

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
        result = normalize_reolink(payload, "192.0.2.1")
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
        result = normalize_reolink(payload, "192.0.2.3")
        assert result is not None
        assert result["camera_id"] == "Outside Front Garage"


# ---------------------------------------------------------------------------
# Shared pipeline mock — returns a minimal pipeline result dict
# ---------------------------------------------------------------------------
def _pipeline_result(classification="motion", camera_id="FRONT"):
    """Return a minimal pipeline result dict."""
    return {
        "status": "ok",
        "camera_id": camera_id,
        "classification": classification,
        "frames": [],
        "gate": {
            "classification": "pass",
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
    """Create a mock _run_pipeline + get_recent_frames for /alert tests.

    Returns (mock_pipeline, mock_frames) for inspection by callers.
    The mock preserves the classification from the alert dict.
    """

    def _run_side_effect(alert):
        return _pipeline_result(
            classification=alert.get("classification", "motion"),
            camera_id=alert.get("camera_id", "FRONT"),
        )

    mock_pipeline = MagicMock(side_effect=_run_side_effect)
    mock_frames = MagicMock(return_value=["/tmp/frame_001.jpg"])
    return mock_pipeline, mock_frames


def _apply_daemon_pipeline_mock(mock_pipeline, mock_frames):
    """Patch the daemon's _run_pipeline + get_recent_frames for /alert tests.

    Since listener.pipeline.run was deleted in US-050e, we attach
    a mock to the daemon's _run_pipeline function directly.
    Also patches infra.frame_capture.get_recent_frames.
    """
    from listener import daemon as daemon_mod

    daemon_mod._run_pipeline = mock_pipeline
    return patch("infra.frame_capture.get_recent_frames", mock_frames)


class TestAlertRoute:
    """Tests for the /alert POST route."""

    def _client(self):
        """Return Flask test client with /alert route registered."""
        return app.test_client()

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
        mock_pipeline, mock_frames = _mock_pipeline()
        with (
            patch("infra.camera_creds.validate_source_ip", return_value=True),
            _apply_daemon_pipeline_mock(mock_pipeline, mock_frames),
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
        """Pipeline result status field appears in response (Reolink payload)."""
        c = self._client()
        payload = {
            "type": "motion",
            "alarm": {
                "type": "motion",
                "time": "2026-09-07T12:00:00Z",
                "channelName": "Front Door Outside",
            },
        }
        mock_pipeline, mock_frames = _mock_pipeline()
        with (
            patch("infra.camera_creds.validate_source_ip", return_value=True),
            _apply_daemon_pipeline_mock(mock_pipeline, mock_frames),
        ):
            r = c.post("/alert", json=payload)
        assert r.status_code == 200
        body = r.get_json()
        assert "status" in body
        assert body["status"] in ("ok", "dropped", "suppressed")

    def test_pipeline_run_called_with_alert_dict(self):
        """pipeline.run receives the normalized Reolink alert dict."""
        c = self._client()
        payload = {
            "type": "vehicle",
            "alarm": {
                "type": "vehicle",
                "time": "2026-09-07T12:00:00Z",
                "channelName": "Front Door Outside",
            },
        }
        mock_pipeline, mock_frames = _mock_pipeline()
        with (
            patch("infra.camera_creds.validate_source_ip", return_value=True),
            _apply_daemon_pipeline_mock(mock_pipeline, mock_frames),
        ):
            r = c.post("/alert", json=payload)
        assert r.status_code == 200
        # Verify _run_pipeline was called with a Reolink-normalized alert dict
        mock_pipeline.assert_called_once()
        call_args = mock_pipeline.call_args[0][0]
        assert call_args["camera_id"] == "Front Door Outside"
        assert call_args["classification"] == "vehicle"

    def test_get_recent_frames_called_with_camera_id(self):
        """infra.frame_capture.get_recent_frames called with Reolink camera_id."""
        c = self._client()
        payload = {
            "type": "motion",
            "alarm": {
                "type": "motion",
                "time": "2026-09-07T12:00:00Z",
                "channelName": "Front Door Outside",
            },
        }
        mock_pipeline, mock_frames = _mock_pipeline()
        with (
            patch("infra.camera_creds.validate_source_ip", return_value=True),
            _apply_daemon_pipeline_mock(mock_pipeline, mock_frames),
        ):
            r = c.post("/alert", json=payload)
        assert r.status_code == 200
        # Verify get_recent_frames was called with the Reolink camera_id
        mock_frames.assert_called_once()
        call_args = mock_frames.call_args
        assert call_args[0][0] == "Front Door Outside"
        assert call_args[1]["n"] == 4

    def test_unknown_route_returns_404(self):
        """POST to an unregistered path returns 404."""
        c = self._client()
        payload = {
            "type": "motion",
            "alarm": {
                "type": "motion",
                "time": "2026-09-07T12:00:00Z",
                "channelName": "Front Door Outside",
            },
        }
        r = c.post("/some_unregistered_path", json=payload)
        assert r.status_code == 404


# ---------------------------------------------------------------------------
# Integration / E2E tests
# ---------------------------------------------------------------------------


class TestAlertRouteIntegration:
    """Integration tests: full /alert flow with mocked pipeline."""

    def _client(self):
        return app.test_client()

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
        mock_pipeline, mock_frames = _mock_pipeline()
        with (
            patch("infra.camera_creds.validate_source_ip", return_value=True),
            _apply_daemon_pipeline_mock(mock_pipeline, mock_frames),
        ):
            r = c.post("/alert", json=payload)
        assert r.status_code == 200
        body = r.get_json()
        assert body["classification"] == "person"

    def test_end_to_end_pipeline_result_has_frames(self):
        """End-to-end: Reolink payload → frames populated from get_recent_frames."""
        c = self._client()
        payload = {
            "type": "motion",
            "alarm": {
                "type": "motion",
                "time": "2026-09-07T12:00:00Z",
                "channelName": "Front Door Outside",
            },
        }
        expected_frames = ["/tmp/frame_001.jpg"]

        def _echo_frames(alert):
            """Pipeline mock that echoes frames back."""
            result = _pipeline_result(
                classification=alert.get("classification", "motion"),
                camera_id=alert.get("camera_id", "Front Door Outside"),
            )
            result["frames"] = list(alert.get("frames", []))
            return result

        mock_pipeline = MagicMock(side_effect=_echo_frames)
        mock_frames = MagicMock(return_value=expected_frames)
        with (
            patch("infra.camera_creds.validate_source_ip", return_value=True),
            _apply_daemon_pipeline_mock(mock_pipeline, mock_frames),
        ):
            r = c.post("/alert", json=payload)
        assert r.status_code == 200
        body = r.get_json()
        # Frames were passed through pipeline
        assert body["frames"] == expected_frames

    def test_end_to_end_gated_alert(self):
        """End-to-end: Reolink payload, gate-suppressed alert returns status 'dropped'."""
        c = self._client()
        payload = {
            "type": "motion",
            "alarm": {
                "type": "motion",
                "time": "2026-09-07T12:00:00Z",
                "channelName": "Front Door Outside",
            },
        }
        suppressed_result = {
            "status": "dropped",
            "camera_id": "Front Door Outside",
            "classification": "motion",
            "reason": "no_vehicle",
            "gate": {
                "classification": "none",
                "class_label": "vehicle",
                "confidence": 0.3,
                "reason": "low_conf",
            },
        }
        mock_pipeline = MagicMock(return_value=suppressed_result)
        mock_frames = MagicMock(return_value=["/tmp/frame_001.jpg"])
        with (
            patch("infra.camera_creds.validate_source_ip", return_value=True),
            _apply_daemon_pipeline_mock(mock_pipeline, mock_frames),
        ):
            r = c.post("/alert", json=payload)
        assert r.status_code == 200
        body = r.get_json()
        assert body["status"] == "dropped"
