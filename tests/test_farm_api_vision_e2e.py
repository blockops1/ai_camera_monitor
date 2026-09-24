"""test_farm_api_vision_e2e — E2E tests for farm_api vision endpoint.

E2E strategy:
    - Import get_vision_status and verify it reads env-level config from
      vision_analyzer module-level constants (no HTTP calls made).
    - Verify IP redaction on the base URL.
    - Verify the status dataclass has correct fields.

Bug classes covered:
    - Base URL not redacted in API response.
    - Runtime fields missing from status dataclass.
"""

from __future__ import annotations

from infra.farm_api.vision import VisionStatus, _redact_ip, get_vision_status


class TestGetVisionStatusE2E:
    """E2E: get_vision_status() reads from vision_analyzer constants."""

    def test_returns_vision_status(self):
        status = get_vision_status()
        assert isinstance(status, VisionStatus)

    def test_has_model_name(self):
        from infra import vision_analyzer

        status = get_vision_status()
        assert status.model_name == vision_analyzer._VISION_MODEL

    def test_has_base_url_redacted(self):
        """base_url_redacted is always populated; redaction only replaces IPs."""
        status = get_vision_status()
        assert status.base_url_redacted is not None

    def test_runtime_fields_none(self):
        """Runtime fields (queue_depth, latency, call count) are None
        when the module hasn't made any calls."""
        status = get_vision_status()
        assert status.queue_depth is None
        assert status.last_call_latency_ms is None
        assert status.calls_last_24h is None


class TestVisionIpRedactionE2E:
    """E2E: _redact_ip() correctly replaces IPs in base URL."""

    def test_redacts_127_ip(self):
        result = _redact_ip("http://127.0.0.1:8080/v1/chat")
        assert "127.0.0.1" not in result
        assert "[REDACTED-IP]" in result

    def test_redacts_lan_ip(self):
        result = _redact_ip("http://192.168.1.50:8080/v1/chat")
        assert "192.168.1.50" not in result
        assert "[REDACTED-IP]" in result

    def test_leaves_non_ip_alone(self):
        result = _redact_ip("https://myhost.com/v1/chat")
        assert result == "https://myhost.com/v1/chat"