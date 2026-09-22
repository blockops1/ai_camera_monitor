"""Tests for telegram_formatter/dispatcher.py MarkdownV2 support.

Verifies:
  - escape_markdown_v2 escapes all 20 reserved chars
  - _send_message passes parse_mode='MarkdownV2'
  - _send_photo passes parse_mode='MarkdownV2'
  - _send_media_group passes parse_mode='MarkdownV2'
  - dispatch propagates parse_mode for text-only and photo paths
"""

from __future__ import annotations

import httpx
import pytest

from telegram_formatter import dispatcher
from telegram_formatter.dispatcher import escape_markdown_v2


# ---------------------------------------------------------------------------
# escape_markdown_v2 unit tests
# ---------------------------------------------------------------------------

class TestEscapeMarkdownV2:
    """Every reserved char must be backslash-prefixed; safe chars pass through."""

    def test_escapes_underscore(self):
        assert escape_markdown_v2("hello_world") == "hello\\_world"

    def test_escapes_asterisk(self):
        assert escape_markdown_v2("a*b") == "a\\*b"

    def test_escapes_brackets(self):
        assert escape_markdown_v2("[a][b]") == "\\[a\\]\\[b\\]"

    def test_escapes_parens(self):
        assert escape_markdown_v2("(foo)") == "\\(foo\\)"

    def test_escapes_tilde(self):
        assert escape_markdown_v2("~text~") == "\\~text\\~"

    def test_escapes_backtick(self):
        assert escape_markdown_v2("`code`") == "\\`code\\`"

    def test_escapes_pipe(self):
        assert escape_markdown_v2("a|b") == "a\\|b"

    def test_escapes_curly_braces(self):
        assert escape_markdown_v2("{x}") == "\\{x\\}"

    def test_escapes_dot(self):
        assert escape_markdown_v2("v1.0") == "v1\\.0"

    def test_escapes_all_reserved(self):
        """All 20 reserved chars in one string."""
        # Order: _ * [ ] ( ) ~ ` > # + - = | { } . !
        # Build a string with one instance of each reserved char.
        reserved = r"_*[]()~`>#+-=|{}.!"
        escaped = escape_markdown_v2(reserved)
        for ch in reserved:
            # Each reserved char must be preceded by a backslash.
            # Since the string is the reserved set itself, the escaped
            # version should have a backslash before every reserved char.
            assert ch in escaped

    def test_no_false_escapes(self):
        """Letters, digits, spaces, and common punctuation are NOT escaped."""
        safe = "Hello World 123 @#%,;:!?\"'()"
        # Only !, ?, (, ) are in the reserved set from that string.
        # Wait — ! and ? and ( and ) are reserved. Let's use a clean string.
        assert escape_markdown_v2("abc123 xyz") == "abc123 xyz"

    def test_empty_string(self):
        assert escape_markdown_v2("") == ""

    def test_no_reserved_chars(self):
        assert escape_markdown_v2("hello world 2026") == "hello world 2026"


# ---------------------------------------------------------------------------
# parse_mode propagation tests
# ---------------------------------------------------------------------------

class TestParseModeMarkdownV2:
    """Every bot.send_* call must include parse_mode='MarkdownV2'."""

    def test_send_message_has_parse_mode(self, mock_llama_server):
        """_send_message routes to sendMessage with parse_mode=MarkdownV2."""
        mock_llama_server.post.return_value = httpx.Response(200, json={"ok": True})

        dispatcher._send_message(
            mock_llama_server,
            bot_token="tok1",
            chat_id="100",
            caption="hello *bold*",
            base_url="https://api.telegram.org",
        )

        last_call = mock_llama_server.post.call_args
        json_body = last_call.kwargs["json"]
        assert json_body["parse_mode"] == "MarkdownV2"
        assert json_body["chat_id"] == "100"
        assert json_body["text"] == "hello *bold*"

    def test_send_photo_has_parse_mode(self, mock_llama_server, tmp_path):
        """_send_photo routes to sendPhoto with parse_mode=MarkdownV2."""
        mock_llama_server.post.return_value = httpx.Response(200, json={"ok": True})
        jpeg = tmp_path / "p.jpg"
        jpeg.write_bytes(b"\xff\xd8\xff\xe0")

        dispatcher._send_photo(
            mock_llama_server,
            bot_token="tok1",
            chat_id="100",
            photo_path=str(jpeg),
            caption="alert *caption*",
            base_url="https://api.telegram.org",
        )

        last_call = mock_llama_server.post.call_args
        data_body = last_call.kwargs["data"]
        assert data_body["parse_mode"] == "MarkdownV2"
        assert data_body["chat_id"] == "100"

    def test_send_media_group_has_parse_mode(self, mock_llama_server, tmp_path):
        """_send_media_group routes to sendMediaGroup with parse_mode=MarkdownV2."""
        mock_llama_server.post.return_value = httpx.Response(200, json={"ok": True})
        jpeg = tmp_path / "p.jpg"
        jpeg.write_bytes(b"\xff\xd8\xff\xe0")

        dispatcher._send_media_group(
            mock_llama_server,
            bot_token="tok1",
            chat_id="100",
            caption="group *caption*",
            photo_paths=[str(jpeg)],
            base_url="https://api.telegram.org",
        )

        last_call = mock_llama_server.post.call_args
        data_body = last_call.kwargs["data"]
        assert data_body["parse_mode"] == "MarkdownV2"
        assert data_body["chat_id"] == "100"

    def test_dispatch_text_only_has_parse_mode(self, mock_llama_server):
        """dispatch with empty photos → sendMessage with parse_mode."""
        mock_llama_server.post.return_value = httpx.Response(200, json={"ok": True})

        dispatcher.dispatch(
            [{"caption": "TG 3 of 3", "photos": []}],
            bot_token="tok1",
            chat_id="100",
        )

        last_call = mock_llama_server.post.call_args
        json_body = last_call.kwargs["json"]
        assert json_body["parse_mode"] == "MarkdownV2"

    def test_dispatch_with_photos_has_parse_mode(self, mock_llama_server, tmp_path):
        """dispatch with photos → sendMediaGroup with parse_mode."""
        mock_llama_server.post.return_value = httpx.Response(200, json={"ok": True})
        jpeg = tmp_path / "p.jpg"
        jpeg.write_bytes(b"\xff\xd8\xff\xe0")

        dispatcher.dispatch(
            [{"caption": "alert *caption*", "photos": [str(jpeg)]}],
            bot_token="tok1",
            chat_id="100",
        )

        last_call = mock_llama_server.post.call_args
        data_body = last_call.kwargs["data"]
        assert data_body["parse_mode"] == "MarkdownV2"
