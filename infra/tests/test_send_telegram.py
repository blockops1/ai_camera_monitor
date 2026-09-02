"""
Tests for infra/send_telegram.py — Telegram Bot API transport.

Pure unit tests with httpx mocked. No network calls.

Covered:
    - send_message returns True on 2xx {"ok": True}
    - send_message returns False on 4xx (no retry — client error)
    - send_message retries on 5xx (up to MAX_ATTEMPTS) then returns False
    - send_message retries on connection error then succeeds
    - send_message retries on bad-JSON response then succeeds
    - send_message returns False on API-level ok=False
    - send_photo returns True on 2xx ok response
    - send_photo returns False on file-not-found
    - send_photo_group truncates to 10 photos (Telegram limit)
    - send_photo_group returns False on empty list
    - send_photo_with_caption single call when caption ≤ 1024
    - send_photo_with_caption falls back to sendMessage when caption > 1024
    - send_photo_with_caption falls back to text-only when photo leg fails
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, mock_open, patch

import httpx

from infra.send_telegram import (
    MAX_ATTEMPTS,
    TELEGRAM_CAPTION_LIMIT,
    TELEGRAM_MEDIA_GROUP_LIMIT,
    send_message,
    send_photo,
    send_photo_group,
    send_photo_with_caption,
)

# Dummy JPEG bytes for mocked file reads. Real decode not needed —
# the transport just hands them to httpx as multipart.
_JPEG_BYTES = b"\xff\xd8\xff\xe0fake-jpeg-bytes"


def _response(
    status_code: int = 200,
    json_data: dict | None = None,
    raise_json: bool = False,
) -> MagicMock:
    """Build a mock httpx response.

    By default returns a 200 OK with {"ok": True} so most tests can ignore
    json_data. Pass json_data for explicit control (4xx, ok=False, etc.).
    Pass raise_json=True to simulate a decode failure.
    """
    r = MagicMock()
    r.status_code = status_code
    if raise_json:
        r.json.side_effect = ValueError("bad json")
    elif json_data is not None:
        r.json.return_value = json_data
    return r


# --------------------------------------------------------------------------
# send_message
# --------------------------------------------------------------------------


def test_send_message_success_first_attempt() -> None:
    """2xx with ok=True → returns True."""
    resp = _response(200, {"ok": True})
    with patch("infra.send_telegram.httpx.post", return_value=resp) as mock_post:
        ok = send_message("bot-token", "chat-id", "hello",
            alert_id="test-alert",
            channel="test",
            event="test_event",
        )
    assert ok is True
    assert mock_post.call_count == 1


def test_send_message_client_error_no_retry() -> None:
    """4xx → returns False immediately (no retry)."""
    resp = _response(400, {"ok": False, "description": "bad chat_id"})
    with patch("infra.send_telegram.httpx.post", return_value=resp) as mock_post:
        ok = send_message("bot-token", "bad-chat", "hello",
            alert_id="test-alert",
            channel="test",
            event="test_event",
        )
    assert ok is False
    assert mock_post.call_count == 1  # no retry


def test_send_message_api_error_no_retry() -> None:
    """200 with ok=False → returns False (treated as client error)."""
    resp = _response(200, {"ok": False, "description": "rate limited"})
    with patch("infra.send_telegram.httpx.post", return_value=resp) as mock_post:
        ok = send_message("bot-token", "chat-id", "hello",
            alert_id="test-alert",
            channel="test",
            event="test_event",
        )
    assert ok is False
    assert mock_post.call_count == 1


def test_send_message_retries_on_5xx_then_fails() -> None:
    """Persistent 5xx → retries MAX_ATTEMPTS times, returns False."""
    resp = _response(502)
    with (
        patch("infra.send_telegram.httpx.post", return_value=resp) as mock_post,
        patch("infra.send_telegram.time.sleep"),  # skip backoff in tests
    ):
        ok = send_message("bot-token", "chat-id", "hello",
            alert_id="test-alert",
            channel="test",
            event="test_event",
        )
    assert ok is False
    assert mock_post.call_count == MAX_ATTEMPTS


def test_send_message_recovers_on_5xx() -> None:
    """5xx then 2xx → returns True on the second attempt."""
    bad = _response(502)
    good = _response(200, {"ok": True})
    with (
        patch("infra.send_telegram.httpx.post", side_effect=[bad, good]) as mock_post,
        patch("infra.send_telegram.time.sleep"),
    ):
        ok = send_message("bot-token", "chat-id", "hello",
            alert_id="test-alert",
            channel="test",
            event="test_event",
        )
    assert ok is True
    assert mock_post.call_count == 2


def test_send_message_retries_on_connection_error() -> None:
    """Connection error then success → returns True."""
    good = _response(200, {"ok": True})
    with (
        patch(
            "infra.send_telegram.httpx.post",
            side_effect=[httpx.ConnectError("boom"), good],
        ) as mock_post,
        patch("infra.send_telegram.time.sleep"),
    ):
        ok = send_message("bot-token", "chat-id", "hello",
            alert_id="test-alert",
            channel="test",
            event="test_event",
        )
    assert ok is True
    assert mock_post.call_count == 2


def test_send_message_retries_on_bad_json() -> None:
    """Bad JSON response then success → returns True."""
    bad = _response(200, raise_json=True)
    good = _response(200, {"ok": True})
    with (
        patch("infra.send_telegram.httpx.post", side_effect=[bad, good]) as mock_post,
        patch("infra.send_telegram.time.sleep"),
    ):
        ok = send_message("bot-token", "chat-id", "hello",
            alert_id="test-alert",
            channel="test",
            event="test_event",
        )
    assert ok is True
    assert mock_post.call_count == 2


def test_send_message_passes_correct_payload() -> None:
    """Verifies the API URL, chat_id, text, parse_mode."""
    resp = _response(200, {"ok": True})
    with patch("infra.send_telegram.httpx.post", return_value=resp) as mock_post:
        send_message("MYBOT", "12345", "<b>hi</b>",
            alert_id="test-alert",
            channel="test",
            event="test_event",
        )
    args, kwargs = mock_post.call_args
    url = args[0] if args else kwargs.get("url", "")
    assert "MYBOT" in url
    assert "sendMessage" in url
    assert kwargs.get("json") == {
        "chat_id": "12345",
        "text": "<b>hi</b>",
        "parse_mode": "HTML",
    }


# --------------------------------------------------------------------------
# send_photo
# --------------------------------------------------------------------------


def test_send_photo_success() -> None:
    """Happy path."""
    resp = _response(200, {"ok": True})
    with (
        patch("builtins.open", mock_open(read_data=_JPEG_BYTES)),
        patch("infra.send_telegram.httpx.post", return_value=resp),
    ):
        ok = send_photo("bot", "chat", "/tmp/pic.jpg",
            alert_id="test-alert",
            channel="test",
            event="test_event",
        )
    assert ok is True


def test_send_photo_returns_false_on_missing_file() -> None:
    """File doesn't exist → returns False."""
    with patch("builtins.open", side_effect=FileNotFoundError):
        ok = send_photo("bot", "chat", "/tmp/missing.jpg",
            alert_id="test-alert",
            channel="test",
            event="test_event",
        )
    assert ok is False


def test_send_photo_returns_false_on_api_error() -> None:
    """ok=False in response body → returns False."""
    resp = _response(200, {"ok": False, "description": "file too big"})
    with (
        patch("builtins.open", mock_open(read_data=_JPEG_BYTES)),
        patch("infra.send_telegram.httpx.post", return_value=resp),
    ):
        ok = send_photo("bot", "chat", "/tmp/pic.jpg",
            alert_id="test-alert",
            channel="test",
            event="test_event",
        )
    assert ok is False


# --------------------------------------------------------------------------
# send_photo_group
# --------------------------------------------------------------------------


def test_send_photo_group_returns_false_on_empty_list() -> None:
    """No frames → returns False without making a network call."""
    with patch("infra.send_telegram.httpx.post") as mock_post:
        ok = send_photo_group("bot", "chat", [],
            alert_id="test-alert",
            channel="test",
            event="test_event",
        )
    assert ok is False
    mock_post.assert_not_called()


def test_send_photo_group_truncates_to_10() -> None:
    """More than 10 frames → truncated to TELEGRAM_MEDIA_GROUP_LIMIT."""
    paths = [f"/tmp/f{i}.jpg" for i in range(15)]
    resp = _response(200, {"ok": True})
    with (
        patch("builtins.open", mock_open(read_data=_JPEG_BYTES)),
        patch("infra.send_telegram.httpx.post", return_value=resp) as mock_post,
    ):
        ok = send_photo_group("bot", "chat", paths, caption="hello",
            alert_id="test-alert",
            channel="test",
            event="test_event",
        )
    assert ok is True
    _, kwargs = mock_post.call_args
    media = json.loads(kwargs["data"]["media"])
    assert len(media) == TELEGRAM_MEDIA_GROUP_LIMIT


def test_send_photo_group_includes_caption_on_first_item() -> None:
    """Caption attached to media[0] only (Telegram convention)."""
    paths = ["/tmp/a.jpg", "/tmp/b.jpg"]
    resp = _response(200, {"ok": True})
    with (
        patch("builtins.open", mock_open(read_data=_JPEG_BYTES)),
        patch("infra.send_telegram.httpx.post", return_value=resp) as mock_post,
    ):
        send_photo_group("bot", "chat", paths, caption="alert text",
            alert_id="test-alert",
            channel="test",
            event="test_event",
        )
    _, kwargs = mock_post.call_args
    media = json.loads(kwargs["data"]["media"])
    assert "caption" in media[0]
    assert "caption" not in media[1]


def test_send_photo_group_returns_false_on_missing_creds() -> None:
    """No creds → short-circuit False."""
    assert send_photo_group("", "chat", ["/tmp/a.jpg"],
            alert_id="test-alert",
            channel="test",
            event="test_event",
        ) is False
    assert send_photo_group("bot", "", ["/tmp/a.jpg"],
            alert_id="test-alert",
            channel="test",
            event="test_event",
        ) is False


# --------------------------------------------------------------------------
# send_photo_with_caption
# --------------------------------------------------------------------------


def test_send_photo_with_caption_single_call_when_short() -> None:
    """Caption ≤ 1024 → single sendPhoto call, no fallback sendMessage."""
    photo_resp = _response(200, {"ok": True})
    with (
        patch("builtins.open", mock_open(read_data=_JPEG_BYTES)),
        patch("infra.send_telegram.httpx.post", return_value=photo_resp) as mock_post,
    ):
        ok = send_photo_with_caption("bot", "chat", "/tmp/pic.jpg", "short caption",
            alert_id="test-alert",
            channel="test",
            event="test_event",
        )
    assert ok is True
    assert mock_post.call_count == 1


def test_send_photo_with_caption_falls_back_when_long() -> None:
    """Caption > 1024 → sendPhoto(truncated) + sendMessage(full) = 2 calls."""
    photo_resp = _response(200, {"ok": True})
    text_resp = _response(200, {"ok": True})
    long_caption = "X" * (TELEGRAM_CAPTION_LIMIT + 100)
    with (
        patch("builtins.open", mock_open(read_data=_JPEG_BYTES)),
        patch(
            "infra.send_telegram.httpx.post",
            side_effect=[photo_resp, text_resp],
        ) as mock_post,
    ):
        ok = send_photo_with_caption("bot", "chat", "/tmp/pic.jpg", long_caption,
            alert_id="test-alert",
            channel="test",
            event="test_event",
        )
    assert ok is True
    assert mock_post.call_count == 2


def test_send_photo_with_caption_photo_failure_falls_back_to_text() -> None:
    """Photo leg fails 4x (photo exhausted MAX_ATTEMPTS) → final send_message succeeds."""
    bad_photo = _response(500, {"ok": False, "description": "server error"})
    # sendPhoto retries 4 times inside send_message, all return 500.
    # Then send_photo_with_caption falls back to send_message.
    # send_message's own 4 500s before any success — both layers
    # converge to all-bad, but the FALLBACK path is what we're testing.
    # Simplification: photo leg returns 4 bad, then text leg gets good
    # on the first attempt. Total = 4 + 1 = 5 (matches MAX_ATTEMPTS + 1).
    text_resp = _response(200, {"ok": True})
    with (
        patch("builtins.open", mock_open(read_data=_JPEG_BYTES)),
        patch(
            "infra.send_telegram.httpx.post",
            side_effect=[bad_photo] * MAX_ATTEMPTS + [text_resp],
        ) as mock_post,
        patch("infra.send_telegram.time.sleep"),
    ):
        ok = send_photo_with_caption("bot", "chat", "/tmp/pic.jpg", "alert body",
            alert_id="test-alert",
            channel="test",
            event="test_event",
        )
    # photo leg: MAX_ATTEMPTS calls (all 500). Then sendMessage fallback:
    # 1 call to send_message gets the 200-ok response and returns True
    # without retrying (it's already a 200). Total: MAX_ATTEMPTS + 1.
    assert ok is True
    assert mock_post.call_count == MAX_ATTEMPTS + 1


def test_send_photo_with_caption_message_returns_false_after_max_retries() -> None:
    """When ALL legs (photo + text fallback) keep failing → returns False.

    The photo leg does NOT retry inside send_photo_with_caption itself —
    it makes a single sendPhoto call. If that returns ok=False, the
    function falls back to send_message, which IS expected to retry
    up to MAX_ATTEMPTS times. So total calls = 1 (photo) + MAX_ATTEMPTS
    (text) = 5.
    """
    bad = _response(500, {"ok": False, "description": "server error"})
    with (
        patch("builtins.open", mock_open(read_data=_JPEG_BYTES)),
        patch(
            "infra.send_telegram.httpx.post",
            side_effect=[bad] * (MAX_ATTEMPTS + 1),
        ) as mock_post,
        patch("infra.send_telegram.time.sleep"),
    ):
        ok = send_photo_with_caption("bot", "chat", "/tmp/pic.jpg", "alert body",
            alert_id="test-alert",
            channel="test",
            event="test_event",
        )
    assert ok is False
    # 1 photo attempt + MAX_ATTEMPTS text retries inside send_message.
    assert mock_post.call_count == 1 + MAX_ATTEMPTS


def test_send_photo_with_caption_returns_false_for_missing_creds() -> None:
    """No creds → short-circuit False."""
    assert send_photo_with_caption("", "chat", "/tmp/pic.jpg", "x",
            alert_id="test-alert",
            channel="test",
            event="test_event",
        ) is False
    assert send_photo_with_caption("bot", "", "/tmp/pic.jpg", "x",
            alert_id="test-alert",
            channel="test",
            event="test_event",
        ) is False
