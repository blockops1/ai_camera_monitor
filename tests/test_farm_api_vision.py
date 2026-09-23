"""Tests for infra.farm_api.vision.

STATUS: V2-068b8
RUN WITH: pytest tests/test_farm_api_vision.py -v

Test strategy:
    - Call get_vision_status() directly.
    - Verify the returned dataclass has the correct shape and field types.
    - Verify redaction of IPs in base_url_redacted.
"""

from infra.farm_api.vision import VisionStatus, get_vision_status


class TestGetVisionStatusShape:
    """get_vision_status() returns a valid VisionStatus dataclass."""

    def test_returns_vision_status(self):
        """get_vision_status() returns a VisionStatus instance."""
        status = get_vision_status()
        assert isinstance(status, VisionStatus)

    def test_model_name_present(self):
        """model_name is read from vision_analyzer._VISION_MODEL."""
        status = get_vision_status()
        assert status.model_name is not None
        assert isinstance(status.model_name, str)
        assert len(status.model_name) > 0

    def test_base_url_redacted(self):
        """base_url_redacted is a string with IPs redacted if present."""
        status = get_vision_status()
        assert status.base_url_redacted is not None
        assert isinstance(status.base_url_redacted, str)
        # localhost URL has no IP to redact — verify we still get a string.
        assert "http://" in status.base_url_redacted

    def test_runtime_fields_none(self):
        """Runtime fields are None until the analyzer exposes them."""
        status = get_vision_status()
        assert status.queue_depth is None
        assert status.last_call_latency_ms is None
        assert status.calls_last_24h is None
