"""
test_pipeline_synthetic.py — Fixtures for synthetic pipeline tests.
"""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_DATA_DIR = os.path.join(os.path.dirname(__file__), "data")


@pytest.fixture()
def make_alert():
    """Callable that builds a synthetic alert dict."""
    def _make(camera_id="CAM1", classification="vehicle"):
        return {
            "id": f"evt-synth-{camera_id}",
            "camera_id": camera_id,
            "camera_label": f"{camera_id} Label",
            "timestamp": "2026-09-07T12:00:00Z",
            "classification": classification,
            "frames": [
                os.path.join(_DATA_DIR, "frame1.jpg"),
                os.path.join(_DATA_DIR, "frame2.jpg"),
            ],
        }
    return _make


@pytest.fixture()
def make_frames():
    """Callable that builds a list of fake frame dicts."""
    def _make(n=4):
        return [{"index": i, "path": os.path.join(_DATA_DIR, f"frame{i}.jpg")}
                for i in range(n)]
    return _make


@pytest.fixture()
def mock_llama_server():
    """Patches httpx.Client. Append to .responses for VM1+VM2."""
    responses = []
    mock_resp = MagicMock()

    def _post(*args, **kwargs):
        mock_resp.json.return_value = {
            "choices": [{"message": {"content": str(responses.pop(0) or "{}")}}]
        }
        return mock_resp

    mc = MagicMock()
    mc.post.side_effect = _post
    mc.__enter__ = MagicMock(return_value=mc)
    mc.__exit__ = MagicMock(return_value=False)
    with patch("httpx.Client", return_value=mc):
        mc.responses = responses
        yield mc


@pytest.fixture()
def mock_telegram():
    """Captures telegram.Bot.send_message calls via AsyncMock."""
    mb = MagicMock()
    mb.send_message = AsyncMock()
    with patch("telegram.Bot", return_value=mb):
        yield mb
