"""Tests for telegram_formatter/dispatcher.py.

Uses the autouse `mock_llama_server` fixture from conftest.py, which:
  - patches httpx.Client to return a MagicMock
  - yields the MagicMock (which we capture and inspect for call_args)

The dispatcher calls httpx.Client().post(...) and the autouse fixture
intercepts that. We drive the dispatcher directly and assert the mock
captures the right URL + body.

These tests do NOT need the dispatcher to be installed in a real
environment. They verify dispatcher.dispatch():
  - text-only path → uses sendMessage
  - photo path → uses sendMediaGroup with type=document (lossless PNG)
  - empty token / chat_id → raises ConfigError
  - 4xx/5xx response → raises DeliveryError
"""
from __future__ import annotations

from unittest.mock import MagicMock

import httpx
import pytest

from telegram_formatter import dispatcher
from telegram_formatter.dispatcher import ConfigError, DeliveryError


def test_dispatch_text_only_sends_sendMessage(mock_llama_server):
    """TG#3 (text-only) routes to sendMessage, not sendPhoto/sendMediaGroup."""
    mock_llama_server.post.return_value = httpx.Response(200, json={"ok": True})

    responses = dispatcher.dispatch(
        [{"caption": "hello world", "photos": []}],
        bot_token="test_token",
        chat_id="12345",
    )

    assert len(responses) == 1
    assert responses[0].status_code == 200
    # Last (only) call to post should have been to sendMessage.
    last_call = mock_llama_server.post.call_args_list[-1]
    url = last_call.args[0]
    assert url.endswith("/bottest_token/sendMessage")
    # Body is json={"chat_id":..., "text":...}
    body = last_call.kwargs["json"]
    assert body == {"chat_id": "12345", "text": "hello world"}


def test_dispatch_with_photos_uses_sendMediaGroup_with_document(mock_llama_server, tmp_path):
    """TG#1/TG#2 (with photos) route to sendMediaGroup with type=document (not photo).

    This is the lossless-PNG guarantee. sendPhoto would re-encode to JPEG
    server-side (operator zero-JPEG directive 2026-09-09).
    """
    # Write two fake JPEG files (matching pipeline output).
    jpeg_bytes = b"\xff\xd8\xff\xe0"
    photo1 = tmp_path / "photo1.jpg"
    photo2 = tmp_path / "photo2.jpg"
    photo1.write_bytes(jpeg_bytes)
    photo2.write_bytes(jpeg_bytes)
    mock_llama_server.post.return_value = httpx.Response(200, json={"ok": True})

    responses = dispatcher.dispatch(
        [{"caption": "alert", "photos": [str(photo1), str(photo2)]}],
        bot_token="test_token",
        chat_id="12345",
    )

    assert len(responses) == 1
    assert responses[0].status_code == 200
    # Last call to post should have been to sendMediaGroup.
    last_call = mock_llama_server.post.call_args_list[-1]
    url = last_call.args[0]
    assert url.endswith("/bottest_token/sendMediaGroup")
    # Multipart body: 'data' has chat_id, 'files' has the photo attachments.
    files = last_call.kwargs["files"]
    assert len(files) == 2
    # Each file tuple: (form_field_name, (filename, bytes, mime))
    assert files[0][1][2] == "image/jpeg"
    assert files[1][1][2] == "image/jpeg"
    # File suffixes match the actual MIME.
    assert files[0][1][0].endswith(".jpg")
    assert files[1][1][0].endswith(".jpg")
    # The data field carries chat_id + media descriptor as JSON.
    data = last_call.kwargs["data"]
    assert data["chat_id"] == "12345"
    # The 'media' descriptor is a JSON string with type=document (NOT photo).
    media_descriptor = data["media"]
    assert '"document"' in media_descriptor
    assert '"photo"' not in media_descriptor


def test_dispatch_raises_on_empty_token(mock_llama_server):
    """Empty bot_token raises ConfigError; no HTTP call is made."""
    with pytest.raises(ConfigError) as exc_info:
        dispatcher.dispatch(
            [{"caption": "x", "photos": []}],
            bot_token="",
            chat_id="12345",
        )
    assert "TELEGRAM_BOT_TOKEN" in str(exc_info.value)
    # The autouse mock was never called.
    assert mock_llama_server.post.call_count == 0


def test_dispatch_raises_on_empty_chat_id(mock_llama_server):
    """Empty chat_id raises ConfigError; no HTTP call is made."""
    with pytest.raises(ConfigError) as exc_info:
        dispatcher.dispatch(
            [{"caption": "x", "photos": []}],
            bot_token="test_token",
            chat_id="",
        )
    assert "TELEGRAM_HOME_CHAT_ID" in str(exc_info.value)
    assert mock_llama_server.post.call_count == 0


def test_dispatch_raises_on_4xx(mock_llama_server):
    """4xx response from Telegram raises DeliveryError."""
    mock_llama_server.post.return_value = httpx.Response(400, text="bad request: chat not found")

    with pytest.raises(DeliveryError) as exc_info:
        dispatcher.dispatch(
            [{"caption": "x", "photos": []}],
            bot_token="test_token",
            chat_id="12345",
        )
    assert "400" in str(exc_info.value)


def test_dispatch_raises_on_5xx(mock_llama_server):
    """5xx response from Telegram raises DeliveryError."""
    mock_llama_server.post.return_value = httpx.Response(503, text="service unavailable")

    with pytest.raises(DeliveryError) as exc_info:
        dispatcher.dispatch(
            [{"caption": "x", "photos": []}],
            bot_token="test_token",
            chat_id="12345",
        )
    assert "503" in str(exc_info.value)
