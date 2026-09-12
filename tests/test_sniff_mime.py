"""Tests for telegram_formatter/dispatcher.py::_sniff_mime.

Covers:
  (a) PNG bytes -> (image/png, .png)
  (b) JPEG bytes -> (image/jpeg, .jpg)
  (c) ASCII/text bytes -> (application/octet-stream, .bin) + logger.warning
  (d) empty file -> (application/octet-stream, .bin) + logger.warning
  (e) PNG file passed through dispatch -> correct MIME + .png suffix in multipart
  (f) JPEG file passed through dispatch -> correct MIME + .jpg suffix in multipart
"""

from __future__ import annotations

import logging
from unittest.mock import MagicMock

from telegram_formatter.dispatcher import _sniff_mime

# PNG magic bytes (8 bytes: IHDR start)
PNG_BYTES = b"\x89PNG\r\n\x1a\n"

# JPEG magic bytes (SOI + JFIF or SOI + JFXX)
JPEG_BYTES = b"\xff\xd8\xff\xe0"

# Unknown ASCII text
ASCII_BYTES = b"hello world\n"


def _write_tmp(tmp_path, name, data):
    """Helper to write bytes to a temp file and return the path."""
    p = tmp_path / name
    p.write_bytes(data)
    return str(p)


# ---------------------------------------------------------------------------
# Direct _sniff_mime tests
# ---------------------------------------------------------------------------


def test_sniff_mime_png(tmp_path):
    """(a) PNG bytes -> ('image/png', '.png')."""
    path = _write_tmp(tmp_path, "foo.png", PNG_BYTES)
    mime, suffix = _sniff_mime(path)
    assert mime == "image/png"
    assert suffix == ".png"


def test_sniff_mime_jpeg(tmp_path):
    """(b) JPEG bytes -> ('image/jpeg', '.jpg')."""
    path = _write_tmp(tmp_path, "foo.jpg", JPEG_BYTES)
    mime, suffix = _sniff_mime(path)
    assert mime == "image/jpeg"
    assert suffix == ".jpg"


def test_sniff_mime_unknown(tmp_path, caplog):
    """(c) ASCII/text bytes -> ('application/octet-stream', '.bin') + logger.warning."""
    with caplog.at_level(logging.WARNING):
        path = _write_tmp(tmp_path, "foo.txt", ASCII_BYTES)
        mime, suffix = _sniff_mime(path)
    assert mime == "application/octet-stream"
    assert suffix == ".bin"
    assert "unknown magic bytes" in caplog.text


def test_sniff_mime_empty(tmp_path, caplog):
    """(d) Empty file -> ('application/octet-stream', '.bin') + logger.warning."""
    with caplog.at_level(logging.WARNING):
        path = _write_tmp(tmp_path, "empty", b"")
        mime, suffix = _sniff_mime(path)
    assert mime == "application/octet-stream"
    assert suffix == ".bin"
    assert "empty file" in caplog.text


# ---------------------------------------------------------------------------
# Integration: dispatch with real MIME sniffing
# ---------------------------------------------------------------------------


def test_dispatch_png_file_uses_png_mime(mock_llama_server, tmp_path):
    """(e) PNG file sent via dispatch gets correct MIME and .png suffix."""
    photo1 = _write_tmp(tmp_path, "photo1.png", PNG_BYTES)
    mock_llama_server.post.return_value = MagicMock(
        status_code=200, json=MagicMock(return_value={"ok": True})
    )

    from telegram_formatter import dispatcher

    dispatcher.dispatch(
        [{"caption": "alert", "photos": [photo1]}],
        bot_token="test_token",
        chat_id="12345",
    )

    files = mock_llama_server.post.call_args.kwargs["files"]
    assert files[0][1][2] == "image/png"  # MIME
    assert files[0][1][0].endswith(".png")  # suffix


def test_dispatch_jpeg_file_uses_jpeg_mime(mock_llama_server, tmp_path):
    """(f) JPEG file sent via dispatch gets correct MIME and .jpg suffix."""
    photo1 = _write_tmp(tmp_path, "photo1.jpg", JPEG_BYTES)
    mock_llama_server.post.return_value = MagicMock(
        status_code=200, json=MagicMock(return_value={"ok": True})
    )

    from telegram_formatter import dispatcher

    dispatcher.dispatch(
        [{"caption": "alert", "photos": [photo1]}],
        bot_token="test_token",
        chat_id="12345",
    )

    files = mock_llama_server.post.call_args.kwargs["files"]
    assert files[0][1][2] == "image/jpeg"  # MIME
    assert files[0][1][0].endswith(".jpg")  # suffix
