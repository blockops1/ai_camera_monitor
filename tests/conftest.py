"""
conftest.py — Shared fixtures for farm-surveillance-v2 tests.

STATUS: stable
THREAD SAFETY: single-threaded (pytest fixtures are single-threaded)

INPUTS:
    - None (pytest discovers fixtures automatically)

OUTPUTS:
    - pytest fixtures: sample_alert, sample_frames, mock_llama_server

DOES NOT DO:
    - Make live network calls — all HTTP is mocked via httpx

CALLED BY:
    - tests/test_pipeline_cooldown.py
    - tests/test_vision_analyzer.py
    - tests/test_pipeline.py
    - tests/test_telegram_formatter.py
"""

from __future__ import annotations

import base64
import os
from unittest.mock import MagicMock, patch

import pytest

# Directory for test data assets.
_DATA_DIR = os.path.join(os.path.dirname(__file__), "data")


@pytest.fixture()
def sample_alert():
    """Return a minimal sample alert dict as received from a camera webhook."""
    return {
        "id": "evt-test-001",
        "camera_id": "CAM1",
        "camera_label": "Front Gate",
        "timestamp": "2026-09-07T10:00:00Z",
        "frames": [
            os.path.join(_DATA_DIR, "frame1.jpg"),
            os.path.join(_DATA_DIR, "frame2.jpg"),
        ],
    }


@pytest.fixture()
def sample_frames():
    """Return a list of two dummy JPEG paths pointing into tests/data/."""
    return [
        os.path.join(_DATA_DIR, "frame1.jpg"),
        os.path.join(_DATA_DIR, "frame2.jpg"),
    ]


@pytest.fixture(autouse=True)
def mock_llama_server(monkeypatch):
    """Mock httpx.post to prevent any live HTTP call to the llama-server.

    Returns the mock so tests can inspect call_args.
    """
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "choices": [{"message": {"content": '{"class": "vehicle"}'}}]
    }
    mock_client = MagicMock()
    mock_client.post.return_value = mock_response
    mock_client.__enter__ = MagicMock(return_value=mock_client)
    mock_client.__exit__ = MagicMock(return_value=False)

    with patch("httpx.Client", return_value=mock_client):
        yield mock_client
