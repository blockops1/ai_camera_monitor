"""
test_vision_analyzer.py — Tests for infra.vision_analyzer.

Tests four behaviors:
  1. verify_class returns parsed dict on 200.
  2. verify_class raises VisionAnalyzerError on non-200.
  3. detail_class dispatch keys match mode names.
  4. detail_class raises on unknown mode.
"""

from unittest.mock import MagicMock, patch

from infra.vision_analyzer import (
    DISPATCH,
    VisionAnalyzerError,
    detail_class,
    verify_class,
)


def _mock_b64(_path: str):
    """Return a dummy base64 image dict so _b64 never touches the filesystem."""
    return {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,xx"}}


def _mock_httpx_post(status_code=200, json_content=None, text="ok"):
    """Build a mock response for patching httpx.post."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = text
    if json_content is not None:
        resp.json.return_value = json_content
    return resp


class TestVerifyClass:
    """Tests for verify_class()."""

    def test_verify_class_returns_parsed_dict(self, sample_frames):
        """verify_class returns the parsed JSON dict on a 200 response."""
        mock_resp = _mock_httpx_post(
            200,
            {"choices": [{"message": {"content": '{"class": "vehicle"}'}}]},
        )
        with (
            patch("infra.vision_analyzer._b64", _mock_b64),
            patch("infra.vision_analyzer.httpx.post", return_value=mock_resp),
        ):
            result = verify_class(sample_frames[0], sample_frames[1])
        assert result == {"class": "vehicle"}

    def test_verify_class_raises_on_non_200(self, sample_frames):
        """verify_class raises VisionAnalyzerError on non-200 status."""
        mock_resp = _mock_httpx_post(500, text="internal error")
        with (
            patch("infra.vision_analyzer._b64", _mock_b64),
            patch("infra.vision_analyzer.httpx.post", return_value=mock_resp),
        ):
            try:
                verify_class(sample_frames[0], sample_frames[1])
                assert False, "should have raised"
            except VisionAnalyzerError:
                pass


class TestDetailClass:
    """Tests for detail_class()."""

    def test_detail_class_dispatch_keys_match_mode_names(self):
        """DISPATCH keys are exactly 'vehicle', 'person', 'animal'."""
        assert sorted(DISPATCH.keys()) == ["animal", "person", "vehicle"]

    def test_detail_class_raises_on_unknown_mode(self, sample_frames):
        """detail_class raises VisionAnalyzerError on an unknown mode."""
        try:
            detail_class("unknown", sample_frames[0], sample_frames[1])
            assert False, "should have raised"
        except VisionAnalyzerError:
            pass
