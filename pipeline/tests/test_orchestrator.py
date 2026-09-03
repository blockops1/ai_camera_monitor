"""Unit tests for run_pipeline.

The orchestrator is a pure composition: it calls detect_motion,
identify_from_crops, match_signature, and the telegram_formatter
functions. We stub the I/O adapters (motion detection is real but
uses synthetic frames; vision is stubbed) and verify the orchestrator
wires them together correctly.
"""

from __future__ import annotations

import sys
from pathlib import Path

_root = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_root))

import pytest

from known_vehicles.store import KnownVehicleStore
from pipeline.orchestrator import (
    PipelineConfig,
    PipelineResult,
    _vision_result_to_dict,
    run_pipeline,
)
from vehicle_identifier.identifier import IdentifierResult
from vehicle_identifier.vision_client import VisionResult
from vehicle_matcher.matcher import MatchVerdict, NoMatch


def _synthetic_frames(tmp_path, n=4, moving=True):
    """Create N synthetic frames with a moving rectangle.

    Phase.115: 4 frames (gate's count) instead of 6.
    Phase.115 (§11.46.6): returns BOTH disk paths AND in-memory
    PIL.Image list (the orchestrator's new signature takes PIL).
    """
    try:
        import cv2
        import numpy as np
        from PIL import Image as _PILImage
    except ImportError:
        pytest.skip("numpy/cv2/PIL not available")
    height, width = 480, 640
    paths = []
    pil_frames = []
    for i in range(n):
        # 3-channel JPEG so cv2.imwrite matches the live OFS format.
        img = np.zeros((height, width, 3), dtype=np.uint8)
        if moving:
            x0 = 50 + i * 30
            cv2.rectangle(img, (x0, 200), (x0 + 80, 280), (255, 255, 255), -1)
        path = tmp_path / f"frame_{i+1:03d}.jpg"
        cv2.imwrite(str(path), img)
        paths.append(str(path))
        pil_frames.append(_PILImage.open(str(path)).convert("RGB"))
    return paths, pil_frames


def _gate_bboxes_for_synthetic_frames(moving=True):
    """Compute the gate's bbox_a + bbox_b for the synthetic moving rectangle.

    The synthetic rectangle moves 30 px to the right per frame. With
    4 frames (i=0..3), positions are x0=50, 80, 110, 140, rect (x0, 200, 80, 80).

    Gate diff: diff(frame_2, frame_3) → bbox_a covers rectangle at i=2 → bbox_a=(110, 200, 80, 80)
    Gate diff: diff(frame_3, frame_4) → bbox_b covers rectangle at i=3 → bbox_b=(140, 200, 80, 80)
    """
    if not moving:
        return None, None
    # The synthetic rectangle at i=2 is at (110, 200, 80, 80).
    bbox_a = (110, 200, 80, 80)
    # The synthetic rectangle at i=3 is at (140, 200, 80, 80).
    bbox_b = (140, 200, 80, 80)
    return bbox_a, bbox_b


def _fake_gate_crops(out_dir):
    """Return a list of 2 placeholder crop paths (empty files, vision is stubbed).

    Phase.115: tests that exercise the vision path need a non-empty
    crop_paths list. The crop files don't need to be valid JPEGs because
    the identifier's call_vision is stubbed in those tests.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    crops = []
    for i in range(2):
        p = out_dir / f"crop_{i}.jpg"
        p.write_bytes(b"")
        crops.append(str(p))
    return crops


def _stub_identifier_vision(monkeypatch, vision_result_content):
    """Stub call_vision in the identifier so the orchestrator gets
    a specific Qwen response without running the real model."""
    def fake_call(**kwargs):
        return VisionResult(
            content=vision_result_content,
            elapsed_ms=42.0,
            raw_text="{}",
        )
    monkeypatch.setattr(
        "vehicle_identifier.identifier.call_vision", fake_call,
    )


def _carson_known_store():
    return KnownVehicleStore([{
        "id": "v_carson_white",
        "label": "name two's white pickup",
        "owner": "name two",
        "color": "white",
        "type": "pickup",
        "make": "GMC",
        "model": "Sierra 1500",
        "vehicle_features": {
            "wheel_style": "black steel",
            "cab_marker_lights": False,
            "bed_cover": "none",
        },
    }])


def _carson_signature_vision():
    return {
        "color": "white",
        "body_style_hint": "pickup",
        "make": "GMC",
        "model": "Sierra 1500",
        "vehicle_features": {
            "wheel_style": "black steel",
            "cab_marker_lights": False,
            "bed_cover": "none",
        },
        "description": "A white GMC Sierra 1500 pickup.",
        "confidence": 0.92,
    }


def _mismatch_signature_vision():
    """A vision result that doesn't match any known vehicle."""
    return {
        "color": "purple",
        "body_style_hint": "spaceship",
        "make": "Aliens",
        "model": "X-9",
        "vehicle_features": {
            "cab_marker_lights": True,
            "bed_cover": "tonneau",
            "wheel_style": "chrome",
        },
        "description": "A purple spaceship.",
        "confidence": 0.5,
    }


# --- run_pipeline: integration ---------------------------------------------


def test_run_pipeline_with_motion_and_match(tmp_path, monkeypatch):
    _frames_paths, _pil_frames = _synthetic_frames(tmp_path, moving=True)
    bbox_a, bbox_b = _gate_bboxes_for_synthetic_frames(moving=True)
    out = tmp_path / "out"
    crops = _fake_gate_crops(out)
    _stub_identifier_vision(monkeypatch, _carson_signature_vision())

    result = run_pipeline(
        frames=_pil_frames,
        output_dir=out,
        alert_id="test_match",
        camera_name="OFS",
        captured_at_iso="2026-08-11T18:00:00+00:00",
        known_vehicles=_carson_known_store(),
        bbox_a=bbox_a,
        bbox_b=bbox_b,
        crop_paths=crops,
    )

    assert isinstance(result, PipelineResult)
    assert result.motion_telegram_body is not None
    assert "OFS" in result.motion_telegram_body
    # Position section present (any 4x4 grid label).
    assert "Position:" in result.motion_telegram_body
    assert "white" in result.motion_telegram_body
    assert second_body_present(result)
    assert isinstance(result.match_verdict, MatchVerdict)
    assert result.match_verdict.known_vehicle["id"] == "v_carson_white"
    # Phase.121: the slim match body shows the human label, not
    # the raw vehicle id. The id is still accessible via
    # result.match_verdict.known_vehicle["id"] (asserted above).
    assert result.second_telegram_body is not None
    assert "name two's white pickup" in result.second_telegram_body
    assert result.elapsed_ms > 0
    assert result.alert_id == "test_match"


def test_run_pipeline_no_motion_still_returns_result(tmp_path, monkeypatch):
    """Even with no motion, the orchestrator returns a PipelineResult.
    The motion_telegram_body is still populated. The second_telegram_body
    may be None or a no-match."""
    _frames_paths, _pil_frames = _synthetic_frames(tmp_path, moving=False)
    out = tmp_path / "out"

    result = run_pipeline(
        frames=_pil_frames,
        output_dir=out,
        alert_id="no_motion",
        camera_name="OFS",
        captured_at_iso="t",
        known_vehicles=_carson_known_store(),
    )
    assert isinstance(result, PipelineResult)
    assert "no motion detected" in result.motion_telegram_body


def test_run_pipeline_vision_but_no_match(tmp_path, monkeypatch):
    """Vision sees something, but it doesn't match any known vehicle."""
    _frames_paths, _pil_frames = _synthetic_frames(tmp_path, moving=True)
    bbox_a, bbox_b = _gate_bboxes_for_synthetic_frames(moving=True)
    out = tmp_path / "out"
    crops = _fake_gate_crops(out)
    _stub_identifier_vision(monkeypatch, _mismatch_signature_vision())

    result = run_pipeline(
        frames=_pil_frames,
        output_dir=out,
        alert_id="no_match",
        camera_name="OFS",
        captured_at_iso="t",
        known_vehicles=_carson_known_store(),
        bbox_a=bbox_a,
        bbox_b=bbox_b,
        crop_paths=crops,
    )
    assert isinstance(result, PipelineResult)
    assert isinstance(result.match_verdict, NoMatch)
    assert second_body_present(result)
    assert result.second_telegram_body is not None
    assert "❌ No match" in result.second_telegram_body


def test_run_pipeline_empty_known_vehicles(tmp_path, monkeypatch):
    """No known vehicles → empty signature after vision runs → NoMatch(empty_signature).

    Phase.115: the gate is the sole producer of crops. To exercise
    the no-known-vehicles path, we feed a fake crop path that the
    identifier will use to call vision (which we stub).
    """
    _frames_paths, _pil_frames = _synthetic_frames(tmp_path, moving=True)
    bbox_a, bbox_b = _gate_bboxes_for_synthetic_frames(moving=True)
    out = tmp_path / "out"
    _fake_gate_crops(out)  # creates the directory tree the test relies on
    _stub_identifier_vision(monkeypatch, _carson_signature_vision())
    fake_crop = str(out / "fake_crop.jpg")
    # The crop doesn't need to exist on disk — vision is stubbed.
    open(fake_crop, "w").close()

    result = run_pipeline(
        frames=_pil_frames,
        output_dir=out,
        alert_id="empty_kv",
        camera_name="OFS",
        captured_at_iso="t",
        known_vehicles=KnownVehicleStore([]),
        bbox_a=bbox_a,
        bbox_b=bbox_b,
        crop_paths=[fake_crop],
    )
    # When no known vehicles exist AND vision returns a real signature,
    # the matcher runs and finds no candidates → NoMatch with reason
    # determined by the matcher, not "empty_signature".
    assert isinstance(result.match_verdict, NoMatch)


def test_run_pipeline_vision_failure_returns_empty_signature(tmp_path, monkeypatch):
    """If vision fails, the identifier returns empty signature,
    which the orchestrator converts to NoMatch(empty_signature)."""
    _frames_paths, _pil_frames = _synthetic_frames(tmp_path, moving=True)
    bbox_a, bbox_b = _gate_bboxes_for_synthetic_frames(moving=True)
    out = tmp_path / "out"
    crops = _fake_gate_crops(out)
    # Stub call_vision to return an error.
    from vehicle_identifier.vision_client import VisionError
    def fake_call(**kwargs):
        return VisionError("timeout", "slow", elapsed_ms=100.0)
    monkeypatch.setattr(
        "vehicle_identifier.identifier.call_vision", fake_call,
    )
    out = tmp_path / "out"

    result = run_pipeline(
        frames=_pil_frames,
        output_dir=out,
        alert_id="vision_fail",
        camera_name="OFS",
        captured_at_iso="t",
        known_vehicles=_carson_known_store(),
        bbox_a=bbox_a,
        bbox_b=bbox_b,
        crop_paths=crops,
    )
    assert isinstance(result, PipelineResult)
    # No vision result → no match verdict.
    assert result.identifier_result.signature == {}
    assert isinstance(result.match_verdict, NoMatch)
    assert result.match_verdict.reason == "empty_signature"


def test_run_pipeline_passes_vision_api_url_through(tmp_path, monkeypatch):
    """PipelineConfig.vision_api_url flows to identify_from_crops."""
    _frames_paths, _pil_frames = _synthetic_frames(tmp_path, moving=True)
    bbox_a, bbox_b = _gate_bboxes_for_synthetic_frames(moving=True)
    captured = {}

    def fake_call(**kwargs):
        captured["api_url"] = kwargs.get("api_url")
        return VisionResult(
            content=_carson_signature_vision(),
            elapsed_ms=42.0,
            raw_text="{}",
        )

    monkeypatch.setattr(
        "vehicle_identifier.identifier.call_vision", fake_call,
    )

    out = tmp_path / "out"
    crops = _fake_gate_crops(out)
    config = PipelineConfig(vision_api_url="http://custom:9999/v1/chat/completions")
    run_pipeline(
        frames=_pil_frames,
        output_dir=out,
        alert_id="custom_url",
        camera_name="OFS",
        captured_at_iso="t",
        known_vehicles=_carson_known_store(),
        bbox_a=bbox_a,
        bbox_b=bbox_b,
        crop_paths=crops,
        config=config,
    )
    assert captured["api_url"] == "http://custom:9999/v1/chat/completions"


def test_run_pipeline_does_not_pass_api_url_when_none(tmp_path, monkeypatch):
    """If vision_api_url is None, the orchestrator doesn't pass it."""
    _frames_paths, _pil_frames = _synthetic_frames(tmp_path, moving=True)
    bbox_a, bbox_b = _gate_bboxes_for_synthetic_frames(moving=True)
    out = tmp_path / "out"
    crops = _fake_gate_crops(out)
    captured = {}

    def fake_call(**kwargs):
        captured["all_keys"] = list(kwargs.keys())
        return VisionResult(
            content=_carson_signature_vision(),
            elapsed_ms=42.0,
            raw_text="{}",
        )

    monkeypatch.setattr(
        "vehicle_identifier.identifier.call_vision", fake_call,
    )

    out = tmp_path / "out"
    run_pipeline(
        frames=_pil_frames,
        output_dir=out,
        alert_id="default_url",
        camera_name="OFS",
        captured_at_iso="t",
        known_vehicles=_carson_known_store(),
        bbox_a=bbox_a,
        bbox_b=bbox_b,
        crop_paths=crops,
    )
    assert "api_url" not in captured["all_keys"]


def test_run_pipeline_custom_thresholds(tmp_path, monkeypatch):
    """A high threshold should cause even a perfect match to fail."""
    _frames_paths, _pil_frames = _synthetic_frames(tmp_path, moving=True)
    bbox_a, bbox_b = _gate_bboxes_for_synthetic_frames(moving=True)
    out = tmp_path / "out"
    crops = _fake_gate_crops(out)
    _stub_identifier_vision(monkeypatch, _carson_signature_vision())

    config = PipelineConfig(match_threshold=100.0, gap_threshold=100.0)
    result = run_pipeline(
        frames=_pil_frames,
        output_dir=out,
        alert_id="high_threshold",
        camera_name="OFS",
        captured_at_iso="t",
        known_vehicles=_carson_known_store(),
        bbox_a=bbox_a,
        bbox_b=bbox_b,
        crop_paths=crops,
        config=config,
    )
    assert isinstance(result.match_verdict, NoMatch)


def test_run_pipeline_alert_id_removed_phase_6b114(tmp_path, monkeypatch):
    """Phase.114: alert_id no longer in user-facing Telegram bodies.

    The alert_id is still available internally for log correlation,
    but the body builders (motion, match, no-match) do not include
    the [uuid] prefix. Captured_at_iso IS shown as a footer at the
    end of the body.
    """
    _frames_paths, _pil_frames = _synthetic_frames(tmp_path, moving=True)
    bbox_a, bbox_b = _gate_bboxes_for_synthetic_frames(moving=True)
    out = tmp_path / "out"
    crops = _fake_gate_crops(out)
    _stub_identifier_vision(monkeypatch, _carson_signature_vision())

    result = run_pipeline(
        frames=_pil_frames,
        output_dir=out,
        alert_id="abc123",
        camera_name="OFS",
        captured_at_iso="t",
        known_vehicles=_carson_known_store(),
        bbox_a=bbox_a,
        bbox_b=bbox_b,
        crop_paths=crops,
    )
    # alert_id removed from user-facing bodies
    assert result.motion_telegram_body is not None
    assert result.second_telegram_body is not None
    assert "[abc123]" not in result.motion_telegram_body
    assert "[abc123]" not in result.second_telegram_body
    # captured_at_iso is in the footer
    assert result.motion_telegram_body.endswith("t")
    assert result.second_telegram_body.endswith("t")


def test_run_pipeline_motion_telegram_always_present(tmp_path, monkeypatch):
    """The motion_telegram_body is always populated, even on no match."""
    _frames_paths, _pil_frames = _synthetic_frames(tmp_path, moving=True)
    bbox_a, bbox_b = _gate_bboxes_for_synthetic_frames(moving=True)
    out = tmp_path / "out"
    crops = _fake_gate_crops(out)
    _stub_identifier_vision(monkeypatch, _mismatch_signature_vision())

    result = run_pipeline(
        frames=_pil_frames,
        output_dir=out,
        alert_id="always_motion",
        camera_name="OFS",
        captured_at_iso="t",
        known_vehicles=_carson_known_store(),
        bbox_a=bbox_a,
        bbox_b=bbox_b,
        crop_paths=crops,
    )
    assert result.motion_telegram_body is not None
    assert result.motion_telegram_body != ""


# --- _vision_result_to_dict -------------------------------------------------


def test_vision_result_to_dict_none():
    """None vision_result → None."""
    ir = IdentifierResult(
        vision_result=None,
        signature={},
        best_crop_path=None,
        crops_used=0,
        fallback_used="no_motion",
        elapsed_ms=0.0,
    )
    assert _vision_result_to_dict(ir) is None


def test_vision_result_to_dict_vision_result():
    """VisionResult → its content dict (plus elapsed_ms)."""
    vr = VisionResult(
        content={"color": "white"},
        elapsed_ms=42.0,
        raw_text="{}",
    )
    ir = IdentifierResult(
        vision_result=vr,
        signature={"color": "white"},
        best_crop_path="/tmp/c.jpg",
        crops_used=1,
        fallback_used=None,
        elapsed_ms=42.0,
    )
    out = _vision_result_to_dict(ir)
    assert out is not None
    assert out["color"] == "white"
    assert out["elapsed_ms"] == 42.0


def test_vision_result_to_dict_vision_error():
    """VisionError → error dict."""
    from vehicle_identifier.vision_client import VisionError
    ve = VisionError("timeout", "slow", elapsed_ms=100.0)
    ir = IdentifierResult(
        vision_result=ve,
        signature={},
        best_crop_path=None,
        crops_used=0,
        fallback_used="vision_failed",
        elapsed_ms=100.0,
    )
    out = _vision_result_to_dict(ir)
    assert out is not None
    assert out["error"]["kind"] == "timeout"
    assert out["error"]["message"] == "slow"


# --- PipelineConfig ---------------------------------------------------------


def test_pipeline_config_defaults():
    config = PipelineConfig()
    assert config.match_threshold == 0.6
    assert config.gap_threshold == 0.15
    assert config.top_n_crops == 3
    assert config.top_n_no_match == 3
    assert config.vision_api_url is None
    assert config.vision_timeout is None
    assert config.identifier_top_n is None


def test_pipeline_config_custom_values():
    config = PipelineConfig(
        match_threshold=0.8,
        gap_threshold=0.25,
        top_n_crops=1,
        top_n_no_match=5,
        vision_api_url="http://x",
        vision_timeout=60.0,
        identifier_top_n=2,
    )
    assert config.match_threshold == 0.8
    assert config.top_n_crops == 1
    assert config.vision_api_url == "http://x"


def test_pipeline_config_is_frozen():
    config = PipelineConfig()
    with pytest.raises(Exception):
        config.match_threshold = 99.0  # type: ignore[misc]


# --- Helpers ----------------------------------------------------------------


def second_body_present(result):
    return result.second_telegram_body is not None and result.second_telegram_body != ""
