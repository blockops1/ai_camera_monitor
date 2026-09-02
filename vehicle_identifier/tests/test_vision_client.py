"""Unit tests for the vision client.

Uses httpx's MockTransport to stub HTTP responses without touching
the network. Tests cover all error paths so the orchestrator can
rely on the call_vision contract.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_root = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_root))

import httpx
import pytest

from vehicle_identifier.vision_client import (
    VisionError,
    VisionResult,
    _parse_response_content,
    call_vision,
    is_vision_error,
)


def _mock_transport(handler):
    """Build an httpx.MockTransport from a function(req) -> Response."""
    return httpx.MockTransport(handler)


def _ok_response(content: dict) -> dict:
    """Build a chat completion response envelope."""
    return {
        "choices": [{
            "message": {"role": "assistant", "content": json.dumps(content)},
            "finish_reason": "stop",
            "index": 0,
        }],
    }


def _write_temp_image(tmp_path: Path, name: str = "img.jpg") -> Path:
    p = tmp_path / name
    p.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 100)  # fake JPEG bytes
    return p


def test_is_vision_error_on_visionerror_instance():
    err = VisionError("timeout", "too slow")
    assert is_vision_error(err) is True


def test_is_vision_error_on_error_dict():
    assert is_vision_error({"objects_detected": ["error"]}) is True
    assert is_vision_error({"error": {"kind": "timeout"}}) is True


def test_is_vision_error_on_normal_dict():
    assert is_vision_error({"color": "white"}) is False
    assert is_vision_error({}) is False


def test_parse_response_content_plain_json():
    content = _parse_response_content('{"color": "white"}')
    assert content == {"color": "white"}


def test_parse_response_content_strips_markdown_fence():
    raw = '```json\n{"color": "white"}\n```'
    content = _parse_response_content(raw)
    assert content == {"color": "white"}


def test_parse_response_content_strips_plain_fence():
    raw = '```\n{"color": "white"}\n```'
    content = _parse_response_content(raw)
    assert content == {"color": "white"}


def test_parse_response_content_invalid_raises():
    with pytest.raises(json.JSONDecodeError):
        _parse_response_content("not json at all")


def test_call_vision_no_images_returns_validation_error(tmp_path):
    result = call_vision(
        image_paths=[],
        camera_name="Cam",
        captured_at="2026-08-11T18:10:38.000+0000",
    )
    assert isinstance(result, VisionError)
    assert result.kind == "validation"
    assert "no images" in result.message


def test_call_vision_missing_image_returns_validation_error(tmp_path):
    result = call_vision(
        image_paths=[tmp_path / "does_not_exist.jpg"],
        camera_name="Cam",
        captured_at="2026-08-11T18:10:38.000+0000",
    )
    assert isinstance(result, VisionError)
    assert result.kind == "validation"


def test_call_vision_success(tmp_path, monkeypatch):
    img = _write_temp_image(tmp_path)

    def handler(req):
        return httpx.Response(200, json=_ok_response({
            "color": "white",
            "body_style_hint": "pickup",
            "make": "GMC",
            "model": "Sierra 1500",
            "vehicle_features": {"wheel_style": "alloy"},
            "description": "A white pickup truck.",
            "confidence": 0.85,
        }))

    # Patch httpx.Client to use our mock transport.
    import vehicle_identifier.vision_client as vc
    orig_client = httpx.Client

    def mock_client(*args, **kwargs):
        kwargs["transport"] = _mock_transport(handler)
        return orig_client(*args, **kwargs)

    monkeypatch.setattr(vc.httpx, "Client", mock_client)

    result = call_vision(
        image_paths=[img],
        camera_name="Outside Front Solar",
        captured_at="2026-08-11T18:10:38.000+0000",
    )
    assert isinstance(result, VisionResult)
    assert result.content["color"] == "white"
    assert result.content["make"] == "GMC"
    assert result.content["confidence"] == 0.85
    assert result.elapsed_ms > 0


def test_call_vision_timeout_returns_timeout_error(tmp_path, monkeypatch):
    img = _write_temp_image(tmp_path)

    def handler(req):
        raise httpx.TimeoutException("simulated")

    import vehicle_identifier.vision_client as vc
    orig_client = httpx.Client

    def mock_client(*args, **kwargs):
        kwargs["transport"] = _mock_transport(handler)
        return orig_client(*args, **kwargs)

    monkeypatch.setattr(vc.httpx, "Client", mock_client)

    result = call_vision(
        image_paths=[img],
        camera_name="Cam",
        captured_at="now",
        timeout_seconds=0.1,
    )
    assert isinstance(result, VisionError)
    assert result.kind == "timeout"


def test_call_vision_http_error_returns_network_error(tmp_path, monkeypatch):
    img = _write_temp_image(tmp_path)

    def handler(req):
        return httpx.Response(503, text="Service Unavailable")

    import vehicle_identifier.vision_client as vc
    orig_client = httpx.Client

    def mock_client(*args, **kwargs):
        kwargs["transport"] = _mock_transport(handler)
        return orig_client(*args, **kwargs)

    monkeypatch.setattr(vc.httpx, "Client", mock_client)

    result = call_vision(
        image_paths=[img],
        camera_name="Cam",
        captured_at="now",
    )
    assert isinstance(result, VisionError)
    assert result.kind == "network"
    assert "503" in result.message


def test_call_vision_non_json_content_returns_parse_error(tmp_path, monkeypatch):
    img = _write_temp_image(tmp_path)

    def handler(req):
        envelope = {
            "choices": [{
                "message": {"role": "assistant",
                            "content": "I don't know what this is"},
                "finish_reason": "stop",
                "index": 0,
            }],
        }
        return httpx.Response(200, json=envelope)

    import vehicle_identifier.vision_client as vc
    orig_client = httpx.Client

    def mock_client(*args, **kwargs):
        kwargs["transport"] = _mock_transport(handler)
        return orig_client(*args, **kwargs)

    monkeypatch.setattr(vc.httpx, "Client", mock_client)

    result = call_vision(
        image_paths=[img],
        camera_name="Cam",
        captured_at="now",
    )
    assert isinstance(result, VisionError)
    assert result.kind == "parse"


def test_call_vision_malformed_envelope_returns_parse_error(tmp_path, monkeypatch):
    img = _write_temp_image(tmp_path)

    def handler(req):
        # No "choices" key.
        return httpx.Response(200, json={"weird": "shape"})

    import vehicle_identifier.vision_client as vc
    orig_client = httpx.Client

    def mock_client(*args, **kwargs):
        kwargs["transport"] = _mock_transport(handler)
        return orig_client(*args, **kwargs)

    monkeypatch.setattr(vc.httpx, "Client", mock_client)

    result = call_vision(
        image_paths=[img],
        camera_name="Cam",
        captured_at="now",
    )
    assert isinstance(result, VisionError)
    assert result.kind == "parse"


def test_call_vision_markdown_fenced_response_is_parsed(tmp_path, monkeypatch):
    """Qwen sometimes returns ```json ... ``` even when told not to.
    call_vision must strip the fence and parse successfully."""
    img = _write_temp_image(tmp_path)

    def handler(req):
        envelope = {
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": '```json\n{"color": "white", "confidence": 0.9}\n```',
                },
                "finish_reason": "stop",
                "index": 0,
            }],
        }
        return httpx.Response(200, json=envelope)

    import vehicle_identifier.vision_client as vc
    orig_client = httpx.Client

    def mock_client(*args, **kwargs):
        kwargs["transport"] = _mock_transport(handler)
        return orig_client(*args, **kwargs)

    monkeypatch.setattr(vc.httpx, "Client", mock_client)

    result = call_vision(
        image_paths=[img],
        camera_name="Cam",
        captured_at="now",
    )
    assert isinstance(result, VisionResult)
    assert result.content["color"] == "white"


def test_visionerror_to_dict():
    err = VisionError("timeout", "too slow", elapsed_ms=1234.0)
    d = err.to_dict()
    assert d["objects_detected"] == ["error"]
    assert d["error"]["kind"] == "timeout"
    assert d["error"]["message"] == "too slow"
    assert d["elapsed_ms"] == 1234.0


def test_visionresult_to_dict():
    r = VisionResult({"color": "white"}, elapsed_ms=500.0, raw_text="{}")
    d = r.to_dict()
    assert d["color"] == "white"
    assert d["elapsed_ms"] == 500.0
