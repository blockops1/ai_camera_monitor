"""test_alert_route_e2e — E2E tests for listener /alert route.

E2E strategy:
    - Use Flask test_client (no subprocess) to exercise the full
      /alert POST → normalize_reolink → _run_pipeline → response path.
    - Mock only the heavy-lifting parts (vision classifier, telegram
      dispatcher, frame capture) so tests are fast and deterministic.
    - Each test produces a verifiable response dict that proves the
      route handler correctly:
      (1) validates the source IP,
      (2) normalizes the Reolink payload shape,
      (3) calls _run_pipeline with the correct alert dict,
      (4) returns the pipeline result as JSON.

Bug classes covered:
    - Malformed Reolink payload → 400 error
    - Spoofed source IP → 403 error
    - Missing alarm dict → 400 error
    - Pipeline suppression → "dropped" status in response

Note: All IP addresses use the RFC 5737 documentation prefix
(192.0.2.0/24, TEST-NET-1) to avoid leaking production camera
addresses into the source tree. See RFC 5737, section 3.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from listener.daemon import app, normalize_reolink

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _pipeline_result(
    classification="motion",
    camera_id="FRONT",
    status="ok",
):
    """Return a minimal pipeline result dict for mocking."""
    return {
        "status": status,
        "camera_id": camera_id,
        "classification": classification,
        "frames": [],
        "gate": {
            "classification": "pass",
            "class_label": "vehicle",
            "confidence": 0.9,
            "reason": "test",
        },
        "vm1_result": {
            "class": "vehicle",
            "subclass": "sedan",
            "confidence": "likely",
            "count": 1,
            "face_visible": False,
            "notes": None,
        },
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


def _apply_daemon_pipeline_mock(mock_pipeline, mock_frames):
    """Patch the daemon's _run_pipeline + get_recent_frames."""
    from listener import daemon as daemon_mod

    daemon_mod._run_pipeline = mock_pipeline
    return patch("infra.frame_capture.get_recent_frames", mock_frames)


# ---------------------------------------------------------------------------
# normalize_reolink unit tests (still useful — pure function)
# ---------------------------------------------------------------------------

class TestNormalizeReolink:
    """Tests for normalize_reolink() — pure function, no mocking needed."""

    def test_nested_shape(self):
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

    def test_person_event(self):
        """Reolink person event normalizes correctly."""
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

    def test_missing_alarm_returns_none(self):
        """Payload without alarm key returns None."""
        assert normalize_reolink({"type": "motion"}, "192.0.2.1") is None

    def test_alarm_not_dict_returns_none(self):
        """Payload with non-dict alarm returns None."""
        assert normalize_reolink({"alarm": "bad"}, "192.0.2.1") is None

    def test_outer_type_overrides_inner(self):
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

    def test_fallback_to_device_name(self):
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

    def test_missing_alarm_type_raises(self):
        """Missing alarm.type raises KeyError."""
        payload = {
            "type": "motion",
            "alarm": {
                "time": "2026-09-07T12:00:00Z",
                "channelName": "Front Door",
                # No 'type' key
            },
        }
        try:
            normalize_reolink(payload, "192.0.2.1")
        except KeyError as exc:
            assert "type" in str(exc)
        else:
            assert False, "Expected KeyError"


# ---------------------------------------------------------------------------
# /alert route E2E tests
# ---------------------------------------------------------------------------

class TestAlertRouteE2E:
    """E2E tests for the /alert POST route.

    Exercises the full route handler path:
    POST /alert → source IP check → normalize_reolink → _run_pipeline → response.
    """

    def _client(self):
        """Return Flask test client with /alert route registered."""
        return app.test_client()

    def test_valid_reolink_returns_200(self):
        """Valid Reolink payload returns HTTP 200 with pipeline result."""
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
        mock_pipeline = MagicMock(return_value=_pipeline_result(
            classification="motion", camera_id="Front Door Outside"
        ))
        mock_frames = MagicMock(return_value=["/tmp/frame_001.jpg"])
        with (
            patch("infra.camera_creds.validate_source_ip", return_value=True),
            _apply_daemon_pipeline_mock(mock_pipeline, mock_frames),
        ):
            r = c.post("/alert", json=payload)
        assert r.status_code == 200
        body = r.get_json()
        assert body["status"] == "ok"
        assert body["classification"] == "motion"
        assert body["camera_id"] == "Front Door Outside"

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

    def test_pipeline_called_with_normalized_alert(self):
        """_run_pipeline receives the normalized Reolink alert dict."""
        c = self._client()
        payload = {
            "type": "vehicle",
            "alarm": {
                "type": "vehicle",
                "time": "2026-09-07T12:00:00Z",
                "channelName": "Front Door Outside",
            },
        }
        mock_pipeline = MagicMock(return_value=_pipeline_result(
            classification="vehicle", camera_id="Front Door Outside"
        ))
        mock_frames = MagicMock(return_value=["/tmp/frame_001.jpg"])
        with (
            patch("infra.camera_creds.validate_source_ip", return_value=True),
            _apply_daemon_pipeline_mock(mock_pipeline, mock_frames),
        ):
            r = c.post("/alert", json=payload)
        assert r.status_code == 200
        mock_pipeline.assert_called_once()
        call_args = mock_pipeline.call_args[0][0]
        assert call_args["camera_id"] == "Front Door Outside"
        assert call_args["classification"] == "vehicle"
        assert "frames" in call_args

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
        mock_pipeline = MagicMock(return_value=_pipeline_result(
            camera_id="Front Door Outside"
        ))
        mock_frames = MagicMock(return_value=["/tmp/frame_001.jpg"])
        with (
            patch("infra.camera_creds.validate_source_ip", return_value=True),
            _apply_daemon_pipeline_mock(mock_pipeline, mock_frames),
        ):
            r = c.post("/alert", json=payload)
        assert r.status_code == 200
        mock_frames.assert_called_once()
        call_args = mock_frames.call_args
        assert call_args[0][0] == "Front Door Outside"
        assert call_args[1]["n"] == 4

    def test_suppressed_alert_returns_dropped(self):
        """Pipeline-suppressed alert returns status 'dropped' in response."""
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
        assert body["reason"] == "no_vehicle"

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
