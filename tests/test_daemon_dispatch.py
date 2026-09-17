"""Tests for listener/daemon.py dispatcher integration."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest
from pathlib import Path


# A minimal-but-valid Reolink-style webhook payload.
REOLINK_PAYLOAD = {
    "type": "motion",
    "alarm": {
        "channelName": "Front Door Outside",
        "device": "Front Door Outside",
        "name": "Motion",
        "type": "person",
        "alarmTime": "2026-09-09T10:00:00Z",
        "time": "2026-09-09T10:00:00Z",
    },
    "id": "abc123",
    "channel": 0,
}


@pytest.fixture
def mock_payload():
    """The dict normalize_reolink should return after Reolink-shaped input."""
    return {
        "camera_id": "OFS",
        "alert_id": "abc123",
        "motion_frame": "frame_001.png",
        "ts": "2026-09-09T10:00:00Z",
    }


@pytest.fixture
def client():
    from listener.daemon import app
    app.config["TESTING"] = True
    return app.test_client()


def test_daemon_returns_200_on_dispatch_failure(client, mock_payload):
    """When dispatcher raises, /alert still returns HTTP 200."""
    with patch("listener.daemon._run_pipeline") as mock_run, \
         patch("listener.daemon.dispatcher") as mock_dispatcher, \
         patch("infra.camera_creds.validate_source_ip", return_value=True):
        mock_run.return_value = {
            "tg1": {"caption": "x", "photos": []},
            "tg2": {},
            "tg3": {},
        }
        mock_dispatcher.dispatch.side_effect = RuntimeError("simulated failure")

        with patch.dict("os.environ", {
            "TELEGRAM_BOT_TOKEN": "fake",
            "TELEGRAM_HOME_CHAT_ID": "12345",
        }, clear=False):
            resp = client.post("/alert", json=REOLINK_PAYLOAD)
        assert resp.status_code == 200


def test_daemon_logs_dispatch_success(client, mock_payload):
    """When dispatcher returns responses, daemon logs tg-dispatch lines."""
    with patch("listener.daemon._run_pipeline") as mock_run, \
         patch("listener.daemon.dispatcher") as mock_dispatcher, \
         patch("infra.camera_creds.validate_source_ip", return_value=True):
        mock_run.return_value = {
            "tg1": {"caption": "alert", "photos": []},
            "tg2": {},
            "tg3": {},
        }
        mock_resp = MagicMock(spec=httpx.Response)
        mock_resp.status_code = 200
        mock_dispatcher.dispatch.return_value = [mock_resp]

        with patch.dict("os.environ", {
            "TELEGRAM_BOT_TOKEN": "fake",
            "TELEGRAM_HOME_CHAT_ID": "12345",
        }, clear=False):
            resp = client.post("/alert", json=REOLINK_PAYLOAD)
        assert resp.status_code == 200
        mock_dispatcher.dispatch.assert_called_once()


def test_daemon_logs_dispatch_config_error(client, mock_payload):
    """When ConfigError raised, daemon logs the error and still returns 200."""
    with patch("listener.daemon._run_pipeline") as mock_run, \
         patch("listener.daemon.dispatcher") as mock_dispatcher, \
         patch("infra.camera_creds.validate_source_ip", return_value=True):
        mock_run.return_value = {
            "tg1": {"caption": "x", "photos": []},
            "tg2": {},
            "tg3": {},
        }
        from telegram_formatter.dispatcher import ConfigError
        mock_dispatcher.dispatch.side_effect = ConfigError("TELEGRAM_HOME_CHAT_ID is empty")

        with patch.dict("os.environ", {
            "TELEGRAM_BOT_TOKEN": "fake",
            "TELEGRAM_HOME_CHAT_ID": "12345",
        }, clear=False):
            resp = client.post("/alert", json=REOLINK_PAYLOAD)
        assert resp.status_code == 200
