"""
Tests for infra/vision_client.py — HTTP transport for vision LLM.

Covers:
    - DEFAULT_URL / TIMEOUT constants
    - _post_to_vision: routes DEFAULT_URL/None → configured URL via
      httpx (Phase 6B.146; pool removed in 6B.147)
    - Custom URL paths bypass the configured URL
    - Bearer auth header sent when token set
    - Error propagation (httpx errors raise)

These tests mock httpx to avoid making real HTTP calls.
"""

from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest

from infra import llm_config
from infra.vision_client import DEFAULT_URL, TIMEOUT, _post_to_vision

# =============================================================================
# Constants
# =============================================================================


class TestConstants:
    """Verify exported constants are sane."""

    def test_default_url_is_localhost_8093(self):
        # Phase 6B.158 / §11.81 (2026-08-28): default URL now points at
        # the unified Qwen3.6 server on port 8093. Production keeps
        # running unchanged when defaults are not overridden.
        assert DEFAULT_URL == "http://127.0.0.1:8093/v1/chat/completions"

    def test_timeout_is_generous(self):
        # Phase 6B.61 bumped 60s → 150s for 6-frame combined prompts.
        # The minimum reasonable value is 60s; below that, legitimate
        # long-running analysis gets cut off.
        assert TIMEOUT >= 60
        assert TIMEOUT <= 300  # don't let it grow unbounded either


# =============================================================================
# _post_to_vision — direct httpx routing
# =============================================================================


class TestPostToVisionRouting:
    """Phase 6B.146: single httpx path. Pool removed in 6B.147.

    DEFAULT_URL and None both route through the same httpx.post call
    pointing at the configured URL. Custom URLs use the caller's URL.
    """

    def test_default_url_uses_httpx_directly(self):
        mock_response = MagicMock()
        mock_response.json.return_value = {"choices": [{"message": {"content": "x"}}]}
        mock_response.raise_for_status = MagicMock()

        with patch(
            "infra.vision_client.httpx.post",
            return_value=mock_response,
        ) as mock_post:
            payload: dict[str, Any] = {"messages": [], "model": "qwen3.6"}
            result = _post_to_vision(DEFAULT_URL, payload)

        assert result == {"choices": [{"message": {"content": "x"}}]}
        mock_post.assert_called_once()
        # httpx.post signature: (url, *, content, json, headers, timeout, ...)
        # url is positional; everything else is keyword.
        call_args = mock_post.call_args.args
        call_kwargs = mock_post.call_args.kwargs
        assert call_args[0] == DEFAULT_URL
        assert call_kwargs["timeout"] == TIMEOUT
        assert call_kwargs["json"] == payload
        # Phase 6B.146: Authorization header is always sent (empty dict
        # when no token — httpx accepts an empty dict).
        assert "headers" in call_kwargs

    def test_none_url_substitutes_default(self):
        # api_url=None means "use the configured default" — same behavior
        # as before, but now via httpx instead of the vision pool.
        mock_response = MagicMock()
        mock_response.json.return_value = {"choices": []}
        mock_response.raise_for_status = MagicMock()

        with patch(
            "infra.vision_client.httpx.post",
            return_value=mock_response,
        ) as mock_post:
            payload: dict[str, Any] = {"messages": []}
            _post_to_vision(None, payload)

        call_args = mock_post.call_args.args
        call_kwargs = mock_post.call_args.kwargs
        assert call_args[0] == DEFAULT_URL
        assert call_kwargs["json"] == payload

    def test_custom_url_bypasses_default(self):
        mock_response = MagicMock()
        mock_response.json.return_value = {"choices": []}
        mock_response.raise_for_status = MagicMock()

        with patch(
            "infra.vision_client.httpx.post",
            return_value=mock_response,
        ) as mock_post:
            payload: dict[str, Any] = {"messages": []}
            _post_to_vision(
                "http://custom.test:9999/v1/chat/completions",
                payload,
            )

        call_args = mock_post.call_args.args
        assert call_args[0] == "http://custom.test:9999/v1/chat/completions"


# =============================================================================
# _post_to_vision — Bearer auth header
# =============================================================================


class TestPostToVisionAuth:
    """Phase 6B.146: Authorization: Bearer *** sent when token is set."""

    def test_no_authorization_header_when_token_empty(self):
        # Local llama-server (default) has no auth. Empty token →
        # headers={} → no Authorization key.
        mock_response = MagicMock()
        mock_response.json.return_value = {"choices": []}
        mock_response.raise_for_status = MagicMock()

        # Force config to have empty token (in case env had one).
        # Patch where the import is USED, not where it lives.
        fake_config = llm_config.VisionLLMConfig(
            url="http://127.0.0.1:8093/v1/chat/completions",
            token="",
            model="qwen3.6",
        )
        with patch(
            "infra.vision_client.load_vision_config",
            return_value=fake_config,
        ), patch(
            "infra.vision_client.httpx.post",
            return_value=mock_response,
        ) as mock_post:
            _post_to_vision(DEFAULT_URL, {"messages": []})

        call_kwargs = mock_post.call_args.kwargs
        assert "Authorization" not in call_kwargs.get("headers", {})

    def test_bearer_header_when_token_set(self):
        # When VISION_LLM_TOKEN is set (via env or llm-creds.env),
        # the call must include `Authorization: Bearer *** header.
        mock_response = MagicMock()
        mock_response.json.return_value = {"choices": []}
        mock_response.raise_for_status = MagicMock()

        # Patch the config to return a token. We patch where the
        # import is USED (vision_client) — patching infra.llm_config
        # directly wouldn't work because vision_client holds its own
        # reference to load_vision_config.
        fake_config = llm_config.VisionLLMConfig(
            url="http://127.0.0.1:8093/v1/chat/completions",
            token="sk-tes...2345",
            model="qwen3.6",
        )
        with patch(
            "infra.vision_client.load_vision_config",
            return_value=fake_config,
        ), patch(
            "infra.vision_client.httpx.post",
            return_value=mock_response,
        ) as mock_post:
            _post_to_vision(DEFAULT_URL, {"messages": []})

        call_kwargs = mock_post.call_args.kwargs
        assert "Authorization" in call_kwargs["headers"]
        assert call_kwargs["headers"]["Authorization"] == "Bearer sk-tes...2345"


# =============================================================================
# _post_to_vision — error propagation
# =============================================================================


class TestPostToVisionErrorHandling:
    """HTTP errors must propagate to the caller (which decides whether to retry)."""

    def test_direct_httpx_error_status_raises(self):
        # If the URL returns 4xx/5xx, raise_for_status triggers.
        mock_response = MagicMock()
        mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
            "500 Server Error",
            request=MagicMock(),
            response=MagicMock(),
        )
        with patch(
            "infra.vision_client.httpx.post",
            return_value=mock_response,
        ), pytest.raises(httpx.HTTPStatusError):
            _post_to_vision(
                "http://custom.test/v1/chat/completions",
                {"messages": []},
            )

    def test_direct_httpx_network_error_propagates(self):
        with patch(
            "infra.vision_client.httpx.post",
            side_effect=httpx.ConnectError("connection refused"),
        ), pytest.raises(httpx.ConnectError):
            _post_to_vision(
                "http://custom.test/v1/chat/completions",
                {"messages": []},
            )

    def test_successful_url_returns_parsed_json(self):
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "choices": [{"message": {"content": "ok"}}],
            "usage": {"prompt_tokens": 100},
        }
        mock_response.raise_for_status = MagicMock()

        with patch(
            "infra.vision_client.httpx.post",
            return_value=mock_response,
        ):
            result = _post_to_vision(
                "http://custom.test/v1/chat/completions",
                {"messages": []},
            )

        assert result["usage"]["prompt_tokens"] == 100
        assert result["choices"][0]["message"]["content"] == "ok"