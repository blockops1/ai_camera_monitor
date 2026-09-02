"""Tests for the Phase 6B.144 (§11.66) changes:
  - motion_gate_pipeline no longer tightens streak crops via YOLO
  - the gate writes a pairwise_diff.png at the output dir
    (§11.88 2026-09-01: PNG lossless, was pairwise_diff.jpg)
  - identify_from_crops appends the diff path to the image list
  - the vision prompt describes a 3-image payload

Run: `.venv/bin/python -m pytest listener/tests/test_motion_gate_pipeline_6B144.py`
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
from PIL import Image

_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_ROOT))


def _test_frame(H=240, W=320, *, seed=42) -> Image.Image:
    """Build a deterministic test frame."""
    np.random.seed(seed)
    arr = np.random.randint(0, 255, (H, W, 3), dtype=np.uint8)
    return Image.fromarray(arr, mode="RGB")


def test_yolo_tighten_helpers_removed():
    """6B.144 reverted 6B.134's YOLO-driven tighten step."""
    from listener import motion_gate_pipeline as mod

    assert not hasattr(mod, "_tighten_streak_crop_with_yolo"), (
        "_tighten_streak_crop_with_yolo should be removed (6B.144)"
    )
    assert not hasattr(mod, "_write_tight_crop_to_disk"), (
        "_write_tight_crop_to_disk should be removed (6B.144)"
    )
    assert not hasattr(mod, "TIGHTEN_MIN_CONF"), (
        "TIGHTEN_MIN_CONF constant should be removed (6B.144)"
    )
    assert not hasattr(mod, "TIGHTEN_PADDING_PX"), (
        "TIGHTEN_PADDING_PX constant should be removed (6B.144)"
    )


def test_pairwise_diff_writes_png(tmp_path):
    """_write_pairwise_diff_image produces a PNG at output_dir/pairwise_diff.png.

    §11.88 (2026-09-01): PNG lossless, NOT JPEG q90. Pairwise diff is a
    visualization artifact used as a prompt-time visual context for the
    vision LLM — we want it lossless so the bbox outlines are crisp.
    """
    from listener.motion_gate_pipeline import _write_pairwise_diff_image

    frame_a = _test_frame(seed=1)
    frame_b = _test_frame(seed=2)
    bbox_a = (50, 50, 100, 100)
    bbox_b = (60, 60, 110, 110)

    out = _write_pairwise_diff_image(
        frame_a, frame_b, bbox_a, bbox_b, str(tmp_path), "test-alert"
    )

    assert out is not None
    p = Path(out)
    assert p.exists()
    assert p.name == "pairwise_diff.png"
    assert p.parent == tmp_path
    assert p.stat().st_size > 500  # not a blank/empty image


def test_pairwise_diff_handles_none_bboxes(tmp_path):
    """Both bboxes None → still writes a valid diff JPEG (no overlay)."""
    from listener.motion_gate_pipeline import _write_pairwise_diff_image

    frame_a = _test_frame(seed=3)
    frame_b = _test_frame(seed=4)

    out = _write_pairwise_diff_image(
        frame_a, frame_b, None, None, str(tmp_path), "test-alert"
    )

    assert out is not None
    assert Path(out).exists()
    assert Path(out).stat().st_size > 500


def test_pairwise_diff_handles_one_bbox(tmp_path):
    """Only bbox_a present (frame_2-3 motion, no frame_3-4 motion)."""
    from listener.motion_gate_pipeline import _write_pairwise_diff_image

    frame_a = _test_frame(seed=5)
    frame_b = _test_frame(seed=6)
    bbox_a = (10, 10, 50, 50)

    out = _write_pairwise_diff_image(
        frame_a, frame_b, bbox_a, None, str(tmp_path), "test-alert"
    )

    assert out is not None
    assert Path(out).exists()


def test_gate_verdict_has_pairwise_diff_path():
    """GateVerdict dataclass exposes pairwise_diff_path (defaults None)."""
    from listener.motion_gate_pipeline import GateVerdict

    verdict = GateVerdict(decision="vehicle", class_label="car", confidence=0.5)
    assert hasattr(verdict, "pairwise_diff_path")
    assert verdict.pairwise_diff_path is None

    verdict.pairwise_diff_path = "/tmp/foo.jpg"
    assert verdict.pairwise_diff_path == "/tmp/foo.jpg"


def test_identify_from_crops_accepts_pairwise_diff_path():
    """identify_from_crops signature includes pairwise_diff_path kwarg."""
    import inspect

    from vehicle_identifier.identifier import identify_from_crops

    sig = inspect.signature(identify_from_crops)
    assert "pairwise_diff_path" in sig.parameters, (
        "pairwise_diff_path should be a parameter of identify_from_crops"
    )


def test_identify_from_crops_appends_existing_diff(monkeypatch):
    """When pairwise_diff_path points at an existing file, it's appended
    to the image list sent to vision. When it's missing, the call is
    unchanged (back-compat)."""
    from vehicle_identifier import identifier as mod
    from vehicle_identifier.vision_client import VisionResult

    captured: dict[str, list[str]] = {}

    fake_vision_result = VisionResult(
        content={
            "color": "red",
            "make": "Yanmar",
            "model": None,
            "vehicle_features": {},
            "description": "compact tractor",
            "confidence": 0.85,
        },
        elapsed_ms=10.0,
        raw_text="{}",
    )

    def fake_call_vision(**kwargs):
        captured["image_paths"] = kwargs.get("image_paths", [])
        captured["camera_name"] = kwargs.get("camera_name", "")
        return fake_vision_result

    monkeypatch.setattr(mod, "call_vision", fake_call_vision)
    monkeypatch.setattr(mod, "_persist_raw_vision", lambda **_: None)

    # Case 1: diff path exists on disk → appended
    from pathlib import Path
    diff_path = Path("/tmp/pairwise_diff_test.jpg")
    diff_path.write_bytes(b"\xff\xd8\xff\xe0")  # minimal JPEG header

    try:
        result = mod.identify_from_crops(
            crop_paths=["/tmp/a.jpg", "/tmp/b.jpg"],
            camera_name="OFS",
            captured_at="2026-08-27",
            pairwise_diff_path=str(diff_path),
        )
        # mark used so ruff F841 doesn't fire
        assert result is not None
        # confirm call_vision saw 3 images (2 streak + 1 diff)
        sent = captured.get("image_paths", [])
        assert len(sent) == 3, f"expected 3 images, got {len(sent)}"
        assert sent[0] == "/tmp/a.jpg"
        assert sent[1] == "/tmp/b.jpg"
        assert sent[2] == str(diff_path)
    finally:
        diff_path.unlink(missing_ok=True)

    # Case 2: diff path points to nonexistent file → only streak crops sent
    captured.clear()
    mod.identify_from_crops(
        crop_paths=["/tmp/a.jpg", "/tmp/b.jpg"],
        camera_name="OFS",
        captured_at="2026-08-27",
        pairwise_diff_path="/tmp/nonexistent_diff.jpg",
    )
    sent = captured.get("image_paths", [])
    assert len(sent) == 2, f"expected 2 images (no diff), got {len(sent)}"

    # Case 3: diff path is None → only streak crops sent
    captured.clear()
    mod.identify_from_crops(
        crop_paths=["/tmp/a.jpg", "/tmp/b.jpg"],
        camera_name="OFS",
        captured_at="2026-08-27",
        pairwise_diff_path=None,
    )
    sent = captured.get("image_paths", [])
    assert len(sent) == 2, f"expected 2 images (no diff), got {len(sent)}"


def test_vision_prompt_describes_three_images():
    """Prompt template now describes streak_A, streak_B, pairwise diff,
    and explicitly tells Qwen to identify the MOVING subject."""
    from vehicle_identifier.prompt_template import render_crop_prompt

    prompt = render_crop_prompt("OFS", "2026-08-27 12:00:00 EDT")

    # Describe the 3 images
    assert "STREAK CROP A" in prompt, "prompt should describe streak crop A"
    assert "STREAK CROP B" in prompt, "prompt should describe streak crop B"
    assert "PAIRWISE DIFFERENTIAL" in prompt, "prompt should describe diff image"

    # Ask for moving subject
    assert "moving subject" in prompt.lower(), "prompt should ask for moving subject"
    assert (
        "stationary vehicles" in prompt.lower()
    ), "prompt should warn about stationary vehicles"

    # No more lies
    assert (
        "tight crop of the subject vehicle" not in prompt
    ), "prompt should no longer claim 'tight crop of the subject vehicle'"
    assert (
        "the cropped image is the only vehicle in the frame" not in prompt
    ), "prompt should no longer lie 'only vehicle in frame'"


def test_alert_context_has_pairwise_diff_path():
    """AlertContext exposes pairwise_diff_path (defaults None)."""
    from listener.vehicle_pipeline import AlertContext

    ctx = AlertContext(
        alert_id="test",
        camera_name="OFS",
        timestamp="2026-08-27 12:00:00",
        event_type="vehicle_in_motion",
        rtsp_url="rtsp://test",
        output_dir="/tmp",
        is_vehicle_event=True,
        known_vehicles=[],
        bot_token="",
        chat_id="",
        api_url="",
        gatekeeper_cameras=frozenset(),
    )
    assert hasattr(ctx, "pairwise_diff_path")
    assert ctx.pairwise_diff_path is None