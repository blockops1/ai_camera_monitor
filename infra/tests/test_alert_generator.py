"""
Tests for infra/alert_generator.py — orchestrator smoke tests.

Covers:
    - DEFAULT_URL / TIMEOUT constants
    - generate_alert: error path (httpx fails → returns error sentinel
      with overrides applied)
    - generate_alert: success path (mocked LLM response)
    - generate_alert: parse failure (retry path)
    - generate_alert: re-exports preserved

Most tests mock httpx.post since the orchestrator's only stateful behavior
is the network call. The override chain is exercised via integration with
the real alert_overrides_* modules.
"""

from unittest.mock import MagicMock, patch

import pytest

from infra import llm_config  # Phase 6B.146
from infra.alert_generator import (
    DEFAULT_URL,
    OFF_HOURS_END_HOUR,
    OFF_HOURS_MIN_LEVEL,
    OFF_HOURS_START_HOUR,
    SYSTEM_PROMPT,
    TIMEOUT,
    _apply_baseline_overrides,
    _apply_distant_vehicle_baseline_override,
    _apply_off_hours_override,
    _apply_parked_vehicle_baseline_override,
    _apply_static_object_baseline_override,
    _apply_vision_none_baseline_override,
    _build_payload,
    _error_result,
    _get_distant_vehicle_cameras,
    _get_parked_vehicle_cameras,
    _get_static_object_cameras,
    _get_vision_none_cameras,
    _is_off_hours,
    _load_override_config,
    _parse_response,
    _to_local_iso,
    _vision_returns_none,
    _vision_sees_person,
    _vision_signals_distant_vehicle,
    generate_alert,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


class TestConstants:
    def test_default_url(self):
        # Phase 6B.158 / §11.81 (2026-08-28): default URL now points
        # at the unified Qwen3.6 server on port 8093 (was 8081 with
        # Qwen3.5-9B).
        assert DEFAULT_URL == "http://127.0.0.1:8093/v1/chat/completions"

    def test_timeout_is_generous(self):
        # 90s — local MPS is slow but reliable. Property-context prompt +
        # JSON output takes ~70s in worst case.
        assert TIMEOUT == 90


# ---------------------------------------------------------------------------
# Re-exports (backward compat)
# ---------------------------------------------------------------------------


class TestReExports:
    """Verify the orchestrator re-exports every extracted symbol.

    If any of these fail, an external caller that used
    `from infra.alert_generator import X` will break.
    """

    def test_all_offhours_symbols(self):
        assert callable(_is_off_hours)
        assert callable(_vision_sees_person)
        assert callable(_apply_off_hours_override)
        assert OFF_HOURS_START_HOUR == 20
        assert OFF_HOURS_END_HOUR == 6
        assert OFF_HOURS_MIN_LEVEL == 1

    def test_all_baseline_symbols(self):
        assert callable(_apply_parked_vehicle_baseline_override)
        assert callable(_apply_distant_vehicle_baseline_override)
        assert callable(_apply_static_object_baseline_override)
        assert callable(_apply_vision_none_baseline_override)
        assert callable(_apply_baseline_overrides)
        assert callable(_get_parked_vehicle_cameras)
        assert callable(_get_distant_vehicle_cameras)
        assert callable(_get_static_object_cameras)
        assert callable(_get_vision_none_cameras)
        assert callable(_vision_returns_none)
        assert callable(_vision_signals_distant_vehicle)
        assert callable(_load_override_config)

    def test_all_prompt_symbols(self):
        assert isinstance(SYSTEM_PROMPT, str)
        assert callable(_build_payload)
        assert callable(_parse_response)
        assert callable(_error_result)
        assert callable(_to_local_iso)


# ---------------------------------------------------------------------------
# generate_alert: error path
# ---------------------------------------------------------------------------


class TestGenerateAlertErrorPath:
    """When httpx fails, return error sentinel with overrides applied."""

    def test_http_error_returns_error_sentinel(self):
        import httpx

        with patch("infra.alert_generator.httpx.post") as mock_post:
            mock_post.side_effect = httpx.ConnectError("connection refused")
            result = generate_alert(
                {"primary_subject": "person"},
                "CAM1",
                "2026-07-20T14:00:00",
            )
        # Error sentinel shape.
        assert result["threat_level"] == -1
        assert result["title"] == "error"
        assert result["source"] == "error"

    def test_http_error_off_hours_escalates_to_l1(self):
        # Even on API failure, off-hours + person escalates to L1.
        import httpx

        with patch("infra.alert_generator.httpx.post") as mock_post:
            mock_post.side_effect = httpx.ConnectError("connection refused")
            result = generate_alert(
                {"primary_subject": "person"},
                "CAM1",
                "2026-07-20T22:00:00",  # off-hours
            )
        # Off-hours override fired: L1 from L-1.
        assert result["threat_level"] == 1
        assert "off-hours" in result["description"].lower()

    def test_http_error_response_with_500(self):
        # 500 raises httpx.HTTPStatusError (subclass of httpx.HTTPError).
        import httpx

        with patch("infra.alert_generator.httpx.post") as mock_post:
            mock_response = MagicMock()
            mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
                "500 Server Error",
                request=MagicMock(),
                response=MagicMock(),
            )
            mock_post.return_value = mock_response
            result = generate_alert(
                {"primary_subject": "person"},
                "CAM1",
                "2026-07-20T14:00:00",
            )
        assert result["threat_level"] == -1  # error sentinel


# ---------------------------------------------------------------------------
# generate_alert: success path
# ---------------------------------------------------------------------------


class TestGenerateAlertSuccessPath:
    """Successful LLM response with deterministic L1 verdict."""

    def _llm_response(self, threat_level: int = 1) -> dict:
        """Build a mock httpx response with reasoning_content JSON."""
        import json
        body = json.dumps(
            {"threat_level": threat_level, "title": "suspicious activity",
             "description": "person at door"}
        )
        return {
            "choices": [
                {
                    "message": {
                        "reasoning_content": body,
                        "content": "",
                    }
                }
            ]
        }

    def test_l1_response_preserved(self):
        with patch("infra.alert_generator.httpx.post") as mock_post:
            mock_response = MagicMock()
            mock_response.json.return_value = self._llm_response(threat_level=1)
            mock_response.raise_for_status.return_value = None
            mock_post.return_value = mock_response

            result = generate_alert(
                {"primary_subject": "vehicle", "objects_detected": ["car"]},
                "Inside Workshop",  # not in any baseline set
                "2026-07-20T14:00:00",  # work hours
            )

        assert result["threat_level"] == 1
        assert result["title"] == "suspicious activity"
        # Alert ID + camera + timestamp + source populated.
        assert result["camera"] == "Inside Workshop"
        assert result["timestamp"] == "2026-07-20T14:00:00"
        assert result["source"] == "rtsp_frames"
        # alert_id is a UUID string.
        assert len(result["alert_id"]) == 36

    def test_l2_response_preserved(self):
        with patch("infra.alert_generator.httpx.post") as mock_post:
            mock_response = MagicMock()
            mock_response.json.return_value = self._llm_response(threat_level=2)
            mock_response.raise_for_status.return_value = None
            mock_post.return_value = mock_response

            result = generate_alert(
                {"primary_subject": "person"},
                "Inside Workshop",
                "2026-07-20T14:00:00",
            )

        assert result["threat_level"] == 2

    def test_baseline_override_demotes_l1(self):
        # Off-hours + parked_vehicle camera + no person → L1 → L0.
        camera = next(iter(_get_parked_vehicle_cameras()), "")
        if not camera:
            pytest.skip("No parked_vehicle_baseline cameras in config")

        with patch("infra.alert_generator.httpx.post") as mock_post:
            mock_response = MagicMock()
            mock_response.json.return_value = self._llm_response(threat_level=1)
            mock_response.raise_for_status.return_value = None
            mock_post.return_value = mock_response

            result = generate_alert(
                {"primary_subject": "vehicle", "objects_detected": ["car"]},
                camera,
                "2026-07-20T22:00:00",  # off-hours
            )

        # Baseline override fired.
        assert result["threat_level"] == 0
        assert "suppressed_by" in result
        assert result["suppressed_by"] == "parked_vehicle_baseline_override"

    def test_off_hours_escalates_l0_to_l1_with_person(self):
        with patch("infra.alert_generator.httpx.post") as mock_post:
            mock_response = MagicMock()
            mock_response.json.return_value = self._llm_response(threat_level=0)
            mock_response.raise_for_status.return_value = None
            mock_post.return_value = mock_post.return_value
            mock_post.return_value = mock_response

            result = generate_alert(
                {"primary_subject": "person", "objects_detected": ["person"]},
                "Inside Workshop",  # not in any baseline set
                "2026-07-20T22:00:00",  # off-hours
            )

        # Off-hours safety net fired: L0 → L1.
        assert result["threat_level"] == 1


# ---------------------------------------------------------------------------
# generate_alert: retry path
# ---------------------------------------------------------------------------


class TestGenerateAlertRetry:
    """When parse fails on first attempt, retry once."""

    def test_first_parse_fails_retry_succeeds(self):
        # First response: malformed. Second response: valid.
        call_count = {"n": 0}

        def fake_post(*args, **kwargs):
            call_count["n"] += 1
            mock_response = MagicMock()
            if call_count["n"] == 1:
                mock_response.json.return_value = {
                    "choices": [{"message": {"reasoning_content": "{garbage", "content": ""}}]
                }
            else:
                mock_response.json.return_value = {
                    "choices": [
                        {
                            "message": {
                                "reasoning_content": '{"threat_level": 1, "title": "ok"}',
                                "content": "",
                            }
                        }
                    ]
                }
            mock_response.raise_for_status.return_value = None
            return mock_response

        with patch("infra.alert_generator.httpx.post", side_effect=fake_post):
            result = generate_alert(
                {"primary_subject": "vehicle", "objects_detected": ["car"]},
                "Inside Workshop",
                "2026-07-20T14:00:00",
            )

        assert call_count["n"] == 2  # retried once
        assert result["threat_level"] == 1
        assert result["title"] == "ok"

    def test_both_parses_fail_returns_error_sentinel(self):
        # Both first and retry return unparseable garbage.
        with patch("infra.alert_generator.httpx.post") as mock_post:
            mock_response = MagicMock()
            mock_response.json.return_value = {
                "choices": [{"message": {"reasoning_content": "no json at all", "content": ""}}]
            }
            mock_response.raise_for_status.return_value = None
            mock_post.return_value = mock_response

            result = generate_alert(
                {"primary_subject": "vehicle", "objects_detected": ["car"]},
                "Inside Workshop",
                "2026-07-20T14:00:00",
            )

        assert result["threat_level"] == -1  # error sentinel
        assert result["title"] == "error"


# ---------------------------------------------------------------------------
# generate_alert: HTTP call shape
# ---------------------------------------------------------------------------


class TestGenerateAlertHttpCall:
    """Verify the HTTP POST has the right payload shape."""

    def test_post_url_is_default_url(self):
        with patch("infra.alert_generator.httpx.post") as mock_post:
            mock_response = MagicMock()
            mock_response.json.return_value = {
                "choices": [
                    {"message": {"reasoning_content": '{"threat_level": 0}', "content": ""}}
                ]
            }
            mock_response.raise_for_status.return_value = None
            mock_post.return_value = mock_response

            generate_alert({}, "cam", "2026-07-20T14:00:00")

            args, kwargs = mock_post.call_args
            assert args[0] == DEFAULT_URL
            assert "json" in kwargs
            assert kwargs["timeout"] == TIMEOUT

    def test_post_uses_custom_api_url(self):
        with patch("infra.alert_generator.httpx.post") as mock_post:
            mock_response = MagicMock()
            mock_response.json.return_value = {
                "choices": [
                    {"message": {"reasoning_content": '{"threat_level": 0}', "content": ""}}
                ]
            }
            mock_response.raise_for_status.return_value = None
            mock_post.return_value = mock_response

            custom = "http://example.com:9999/v1/chat/completions"
            generate_alert({}, "cam", "2026-07-20T14:00:00", api_url=custom)

            args, _kwargs = mock_post.call_args
            assert args[0] == custom


# ---------------------------------------------------------------------------
# Phase 6B.146 — Bearer auth header on text LLM call
# ---------------------------------------------------------------------------


class TestGenerateAlertAuth:
    """Phase 6B.146: Authorization: Bearer *** sent when token is set."""

    def test_no_authorization_header_when_token_empty(self):
        # Local llama-server (default) has no auth. Empty token →
        # headers={} → no Authorization key.
        with patch("infra.alert_generator.httpx.post") as mock_post:
            mock_response = MagicMock()
            mock_response.json.return_value = {
                "choices": [
                    {
                        "message": {
                            "reasoning_content": '{"threat_level": 0}',
                            "content": "",
                        }
                    }
                ]
            }
            mock_response.raise_for_status.return_value = None
            mock_post.return_value = mock_response

            fake_config = llm_config.TextLLMConfig(
                url="http://127.0.0.1:8093/v1/chat/completions",
                token="",
                model="qwen3.6",
            )
            with patch(
                "infra.alert_generator.load_text_config",
                return_value=fake_config,
            ):
                generate_alert({}, "cam", "2026-07-20T14:00:00")

            _args, kwargs = mock_post.call_args
            assert "Authorization" not in kwargs.get("headers", {})

    def test_bearer_header_when_token_set(self):
        # When TEXT_LLM_TOKEN is set, send `Authorization: Bearer ***.
        with patch("infra.alert_generator.httpx.post") as mock_post:
            mock_response = MagicMock()
            mock_response.json.return_value = {
                "choices": [
                    {
                        "message": {
                            "reasoning_content": '{"threat_level": 0}',
                            "content": "",
                        }
                    }
                ]
            }
            mock_response.raise_for_status.return_value = None
            mock_post.return_value = mock_response

            fake_config = llm_config.TextLLMConfig(
                url="http://127.0.0.1:8093/v1/chat/completions",
                token="sk-test-text-token-67890",
                model="qwen3.6",
            )
            with patch(
                "infra.alert_generator.load_text_config",
                return_value=fake_config,
            ):
                generate_alert({}, "cam", "2026-07-20T14:00:00")

            _args, kwargs = mock_post.call_args
            assert "Authorization" in kwargs["headers"]
            assert (
                kwargs["headers"]["Authorization"]
                == "Bearer sk-test-text-token-67890"
            )
