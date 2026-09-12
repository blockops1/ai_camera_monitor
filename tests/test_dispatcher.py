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
  - photo path → uses sendMediaGroup with type=photo (pre-converted JPEG)
  - empty token / chat_id → raises ConfigError
  - 4xx/5xx response → raises DeliveryError
"""

from __future__ import annotations

from pathlib import Path

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


def test_dispatch_with_photos_uses_sendMediaGroup_with_photo(
    mock_llama_server, tmp_path
):
    """TG#1/TG#2 (with photos) route to sendMediaGroup with type=photo.

    Photos are pre-converted JPEGs (q=88, max-dim 1920) cached on disk.
    type=photo ensures Telegram renders them inline (not as files).
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
    # The 'media' descriptor uses type=photo (NOT document).
    media_descriptor = data["media"]
    assert '"photo"' in media_descriptor
    assert '"document"' not in media_descriptor


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
    mock_llama_server.post.return_value = httpx.Response(
        400, text="bad request: chat not found"
    )

    with pytest.raises(DeliveryError) as exc_info:
        dispatcher.dispatch(
            [{"caption": "x", "photos": []}],
            bot_token="test_token",
            chat_id="12345",
        )
    assert "400" in str(exc_info.value)


def test_dispatch_raises_on_5xx(mock_llama_server):
    """5xx response from Telegram raises DeliveryError."""
    mock_llama_server.post.return_value = httpx.Response(
        503, text="service unavailable"
    )

    with pytest.raises(DeliveryError) as exc_info:
        dispatcher.dispatch(
            [{"caption": "x", "photos": []}],
            bot_token="test_token",
            chat_id="12345",
        )
    assert "503" in str(exc_info.value)


# ---------------------------------------------------------------------------
# US-030a: JPEG conversion cache, structured log, codec integration
# ---------------------------------------------------------------------------


def test_codec_encodes_png_to_jpeg(tmp_path):
    """codec.encode_jpeg reads a PNG-like source, writes a JPEG."""
    import struct
    import zlib

    from telegram_formatter import codec

    def _make_png(r, g, b):
        sig = b"\x89PNG\r\n\x1a\n"
        # IHDR
        ihdr_data = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
        ihdr_crc = zlib.crc32(b"IHDR" + ihdr_data) & 0xFFFFFFFF
        ihdr = struct.pack(">I", 13) + b"IHDR" + ihdr_data + struct.pack(">I", ihdr_crc)
        # IDAT (raw RGB row)
        raw = bytes([0, r, g, b])  # filter=0, then RGB
        compressed = zlib.compress(raw)
        idat_crc = zlib.crc32(b"IDAT" + compressed) & 0xFFFFFFFF
        idat = (
            struct.pack(">I", len(compressed))
            + b"IDAT"
            + compressed
            + struct.pack(">I", idat_crc)
        )
        # IEND
        iend_crc = zlib.crc32(b"IEND") & 0xFFFFFFFF
        iend = struct.pack(">I", 0) + b"IEND" + struct.pack(">I", iend_crc)
        return sig + ihdr + idat + iend

    src = tmp_path / "input.png"
    src.write_bytes(_make_png(255, 128, 64))

    cache_dir = str(tmp_path / "cache" / "2026-09-13" / "alert-001")
    result = codec.encode_jpeg(str(src), cache_dir, jpeg_quality=88, max_dim=1920)

    assert result is not None
    assert result.endswith(".jpg")
    assert Path(result).is_file()
    # Verify it's a valid JPEG.
    jpeg_data = Path(result).read_bytes()
    assert jpeg_data[:2] == b"\xff\xd8"


def test_codec_creates_cache_dir(tmp_path):
    """codec.encode_jpeg creates nested cache directories if missing."""
    from PIL import Image

    from telegram_formatter import codec

    # Create a tiny real JPEG via PIL (avoids hand-crafting invalid JPEG bytes).
    src = tmp_path / "input.jpg"
    img = Image.new("RGB", (2, 2), color="red")
    img.save(str(src), format="JPEG")

    cache_dir = str(tmp_path / "deep" / "nested" / "cache")
    result = codec.encode_jpeg(str(src), cache_dir, jpeg_quality=88, max_dim=1920)

    assert result is not None
    assert Path(result).parent == Path(cache_dir)
    assert Path(cache_dir).is_dir()


def test_codec_respects_max_dim(tmp_path):
    """codec.encode_jpeg downscales images that exceed max_dim."""
    from PIL import Image

    from telegram_formatter import codec

    # Create a 4000x3000 PNG.
    src = tmp_path / "big.png"
    img = Image.new("RGB", (4000, 3000), color="red")
    img.save(str(src), format="PNG")

    cache_dir = str(tmp_path / "cache")
    result = codec.encode_jpeg(str(src), cache_dir, jpeg_quality=88, max_dim=1920)

    assert result is not None
    with Image.open(result) as out:
        w, h = out.size
        assert w <= 1920
        assert h <= 1920
        # Aspect ratio should be preserved.
        assert h / w == 3000 / 4000


def test_codec_returns_none_for_missing_source(tmp_path):
    """codec.encode_jpeg returns None when source file does not exist."""
    from telegram_formatter import codec

    result = codec.encode_jpeg(
        str(tmp_path / "nonexistent.png"), str(tmp_path / "cache")
    )
    assert result is None
