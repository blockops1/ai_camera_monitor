"""
test_pipeline_synthetic.py — Synthetic E2E pipeline test.

Tests the full alert flow: handle_webhook -> pipeline -> Telegram sends.
"""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Stub telegram module (listener imports "from telegram import Bot").
import sys

if "telegram" not in sys.modules:
    _stub = sys.modules["telegram"] = MagicMock()
    _stub.Bot = MagicMock

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
                os.path.join(_DATA_DIR, "frame3.jpg"),
                os.path.join(_DATA_DIR, "frame4.jpg"),
            ],
        }

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


def test_synthetic_webhook_reaches_all_3_telegram_stages(
    make_alert,
    mock_llama_server,
    synthetic_webhook_patchers,
):
    """End-to-end: handle_webhook -> pipeline -> 3 Telegram sends."""
    from listener import listener as lm

    FakeBot, mb = synthetic_webhook_patchers

    # VM responses
    mock_llama_server.responses.append(
        '{"class": "vehicle", "confidence": "likely"}'
    )
    mock_llama_server.responses.append(
        '{"class_confirmed": "vehicle", "license_plate": "ABC123", '
        '"distinctive_features": ["roof_rack"]}'
    )

    alert = make_alert(camera_id="CAM1", classification="vehicle")
    lm.Bot = FakeBot

    result = lm.handle_webhook(alert)

    assert result["status"] == "ok"
    assert result["classification"] == "vehicle"
    assert mb.send_message.call_count == 3

    tg2_caption = mb.send_message.call_args_list[1][1]["text"]
    assert "ABC123" in tg2_caption
    assert "roof_rack" in tg2_caption

    vm2_result = {
        "class_confirmed": "vehicle",
        "license_plate": "ABC123",
        "distinctive_features": ["roof_rack"],
    }
    assert "threat" not in vm2_result
