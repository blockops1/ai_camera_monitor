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
import sys
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# PyAV stub — the project venv may lack av; tests mock av.open() anyway.
# Must run at import time (before test collection) so infra.frame_capture
# can `import av` successfully.
# ---------------------------------------------------------------------------
import sys as _sys
_av_stub = MagicMock()
_av_stub.__version__ = "15.1.0"
_av_stub.container = MagicMock()
_av_stub.container.InputContainer = MagicMock
_sys.modules.setdefault("av", _av_stub)

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


# ---------------------------------------------------------------------------
# Fixtures for synthetic E2E test (US-013b)
# ---------------------------------------------------------------------------

@pytest.fixture()
def make_gate_verdict():
    """Build a mock GateVerdict with decision='vehicle'."""
    from unittest.mock import MagicMock, PropertyMock

    from infra.gate import GateVerdict

    v = MagicMock(spec=GateVerdict)
    v.decision = "vehicle"
    v.class_label = "car"
    v.confidence = 0.95
    v.reason = "high_conf_vehicle"
    v.crop_a = None
    v.crop_b = None
    v.pairwise_diff_path = None
    return v


@pytest.fixture()
def mock_cooldown():
    """Return a PipelineCooldown mock that never suppresses."""
    from unittest.mock import MagicMock

    mc = MagicMock()
    mc.should_suppress.return_value = False
    return mc


@pytest.fixture()
def fake_bot_message():
    """Callable: (text, photos) -> {text, photos}."""
    from unittest.mock import AsyncMock, MagicMock

    mb = MagicMock()
    mb.send_message = AsyncMock()
    mb.send_photo = AsyncMock()

    class FakeBot:
        def __init__(self, token):
            pass

        send_message = mb.send_message
        send_photo = mb.send_photo

    return FakeBot, mb


@pytest.fixture(autouse=False)
def synthetic_webhook_patchers(
    make_gate_verdict,
    mock_cooldown,
    fake_bot_message,
    monkeypatch,
):
    """Patch everything needed to run handle_webhook end-to-end.

    Returns the (FakeBot, mock_bot) pair for inspection.
    """
    from unittest.mock import MagicMock, patch

    from listener import listener as lm
    from listener.pipeline import (
        build_alert_message,
        build_detail_message,
        build_match_message,
    )

    FakeBot, mb = fake_bot_message

    # Patch asyncio.run: sync values pass through; coroutines are
    # actually awaited so _send_telegram's Telegram sends work.
    import asyncio as _real_asyncio
    _running = False

    def _fake_run(main):
        if _real_asyncio.iscoroutine(main):
            loop = _real_asyncio.new_event_loop()
            try:
                return loop.run_until_complete(main)
            finally:
                loop.close()
        return main

    fake_asyncio = MagicMock()
    fake_asyncio.run = _fake_run

    # Patch module-level Bot and asyncio
    with (
        patch.object(lm, "Bot", FakeBot),
        patch.object(lm, "asyncio", fake_asyncio),
        patch.object(lm, "BOT_TOKEN", "fake-token"),
        patch.object(lm, "HOME_CHAT_ID", "12345"),
        patch("listener.pipeline.PipelineCooldown", return_value=mock_cooldown),
        patch(
            "listener.pipeline.run_gate", return_value=make_gate_verdict
        ),
        patch(
            "listener.pipeline.verify_class",
            return_value={"class": "vehicle", "confidence": "likely"},
        ),
        patch(
            "listener.pipeline.build_alert_message",
            return_value={"caption": "Vehicle detected", "photos": []},
        ),
        patch(
            "listener.pipeline.detail_class",
            return_value={
                "class_confirmed": "vehicle",
                "license_plate": "ABC123",
                "distinctive_features": ["roof_rack"],
            },
        ),
        patch(
            "listener.pipeline.build_detail_message",
            return_value={
                "caption": (
                    "Camera: CAM1 Label\nClass confirmed: vehicle\n"
                    "License plate: ABC123\nDistinctive features: roof_rack"
                ),
                "photos": [],
            },
        ),
        patch(
            "listener.pipeline._load_candidates",
            return_value=[
                {
                    "license_plate": "ABC123",
                    "distinctive_features": ["roof_rack"],
                    "make": "Toyota",
                    "model": "Sequoia",
                    "color": "silver",
                }
            ],
        ),
        patch(
            "listener.pipeline.match_vehicle",
            return_value={
                "license_plate": "ABC123",
                "distinctive_features": ["roof_rack"],
                "matched": True,
            },
        ),
        patch(
            "listener.pipeline.build_match_message",
            return_value={
                "caption": (
                    "Recognized: ABC123\nStatus: recognized vehicle\n"
                    "Distinctive features: roof_rack"
                ),
                "photos": [],
            },
        ),
    ):
        yield FakeBot, mb
