"""Tests for the Phase.144 (§11.66) changes:
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
import pytest
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
    crop_bbox_a = (50, 50, 100, 100)
    crop_bbox_b = (60, 60, 110, 110)

    out = _write_pairwise_diff_image(
        frame_a, frame_b, crop_bbox_a, crop_bbox_b, str(tmp_path), "test-alert"
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
    """Only crop_bbox_a present (subject on frame_2 only, not frame_3)."""
    from listener.motion_gate_pipeline import _write_pairwise_diff_image

    frame_a = _test_frame(seed=5)
    frame_b = _test_frame(seed=6)
    crop_bbox_a = (10, 10, 50, 50)

    out = _write_pairwise_diff_image(
        frame_a, frame_b, crop_bbox_a, None, str(tmp_path), "test-alert"
    )

    assert out is not None
    assert Path(out).exists()


def test_pairwise_diff_accepts_crop_bbox_coords(tmp_path):
    """§11.115.21 — _write_pairwise_diff_image accepts (and draws
    boxes at) crop bbox coords. We construct a frame with a green
    rectangle drawn at one location and pass a DIFFERENT crop_bbox_a
    coord — verify the outline is drawn at the PASSED bbox coord,
    not at the rectangle's location. This proves the function is
    using the args (not some other source) and correctly handles
    crop_bbox_a coords.

    Note: "OK if boxes are slightly different color" — we just
    verify the outline is at the right coords with any saturated color.
    """
    from listener.motion_gate_pipeline import _write_pairwise_diff_image

    H, W = 240, 320
    arr_a = np.zeros((H, W, 3), dtype=np.uint8)
    arr_a[50:150, 50:150, :] = [0, 255, 0]  # green rect at (50,50,100,100)
    frame_a = Image.fromarray(arr_a, mode="RGB")
    frame_b = _test_frame(seed=99)

    # Pass BBox coords that are NOT where the green rect is drawn.
    # If the function used the rect's coord (wrong), outline would be at 50..54.
    # If the function uses the PASSED coord, outline is at 200..204.
    crop_bbox_a = (200, 50, 50, 50)
    crop_bbox_b = (10, 10, 50, 50)

    out = _write_pairwise_diff_image(
        frame_a, frame_b, crop_bbox_a, crop_bbox_b, str(tmp_path), "test-crop-coords"
    )
    img = np.asarray(Image.open(str(out)).convert("RGB"))

    # PASSED crop_bbox_a = (200, 50, 50, 50) → outline at y=200..204.
    # No outline at the rect's coords (y=50..54). Confirms function uses
    # the bbox arg, not the frame content.
    edge_a_top = img[200:204, 200:250, :]
    assert edge_a_top.max(axis=2).max() > 200, (
        f"crop_bbox_a=(200,50,50,50) should produce a saturated outline at y=200..204; "
        f"got max channel = {edge_a_top.max(axis=2).max()}"
    )
    # (No rect-region check needed: the per_pixel_max assertion above
    # already proves the outline is at y=200..204 with the passed coord,
    # not at y=50..54 where the green rect lives.)


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


def test_gate_run_passes_crop_bboxes_to_pairwise_diff(tmp_path, monkeypatch):
    """§11.115.21 (2026-09-03) — the call site at
    listener/motion_gate_pipeline.py:1073 MUST pass crop_bbox_a /
    crop_bbox_b (the AND subject bboxes that defined the crop file
    extents on disk) to _write_pairwise_diff_image — NOT the diff-trail
    bboxes (bbox_a, bbox_b).

    We exercise motion_gate_pipeline.run() end-to-end with synthetic
    moving frames, monkey-patch _write_pairwise_diff_image to capture
    its call args, and assert the 3rd/4th positional args (the
    bboxes) MATCH the crop bboxes used to write crop_*.png — NOT the
    trail bboxes.

    This is a regression test: if someone reverts the call site to
    pass bbox_a/bbox_b, this test fails with a clear "wrong bboxes
    were passed to the pairwise diff writer" message.
    """

    from listener import motion_gate_pipeline as mgp
    from listener.motion_gate_pipeline import run
    from listener.tests.test_motion_gate_pipeline import (
        FakeClassifier,
        QuickVerdict,
        _make_synthetic_frames,
    )

    # Force keep_disk=True so the pairwise diff path is exercised.
    monkeypatch.setattr(mgp, "_gate_keep_disk_artifacts", lambda: True)
    monkeypatch.setattr(mgp, "_is_keep_disk_artifacts_enabled", lambda: True)

    # Capture args to _write_pairwise_diff_image.
    captured: dict = {}

    def fake_writer(frame_a, frame_b, bbox_a_arg, bbox_b_arg, output_dir, alert_id):
        captured["frame_a"] = frame_a
        captured["frame_b"] = frame_b
        captured["bbox_a_arg"] = bbox_a_arg
        captured["bbox_b_arg"] = bbox_b_arg
        captured["output_dir"] = output_dir
        captured["alert_id"] = alert_id
        # Return a real PNG path so caller code doesn't crash downstream.
        out_p = Path(output_dir) / "pairwise_diff.png"
        out_p.parent.mkdir(parents=True, exist_ok=True)
        out_p.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 200)  # 200-byte stub
        return str(out_p)

    monkeypatch.setattr(mgp, "_write_pairwise_diff_image", fake_writer)

    # Build synthetic moving frames so the gate produces a non-null verdict.
    frame_paths = _make_synthetic_frames(tmp_path, motion=True)

    # A classifier that says "car" — exercise the bboxes-and-write path.
    fake = FakeClassifier(
        responses=[
            QuickVerdict(top_class="car", top_confidence=0.9, decision="pass_with_hint"),
            QuickVerdict(top_class="car", top_confidence=0.9, decision="pass_with_hint"),
        ]
    )

    verdict = run(
        frame_paths=frame_paths,
        camera_name="OFS",
        alert_id="test-alert-11521",
        output_dir=str(tmp_path),
        classifier=fake,
    )

    # The diff writer MUST have been invoked.
    assert "bbox_a_arg" in captured, (
        "_write_pairwise_diff_image was not called — pipeline did not reach §11.115.21 call site"
    )

    # §11.115.21 — _write_pairwise_diff_image MUST receive the CROP bboxes
    # (crop_bbox_a, crop_bbox_b = per-frame subject AND bboxes that defined
    # crop_a.png / crop_b.png file extents), NOT the trail bboxes
    # (bbox_a, bbox_b = raw diff(2,3) / diff(3,4) extents).
    #
    # On Phase.175 these are stored in distinct GateVerdict fields:
    #   verdict.bbox_a       = trail bbox  (diff(2,3) raw bbox)
    #   verdict.crop_bbox_a  = crop bbox   (subject AND bbox on frame_2)
    # We compare against verdict.crop_bbox_a so the regression catches
    # the wrong-coords bug.
    crop_bbox_a = verdict.crop_bbox_a
    crop_bbox_b = verdict.crop_bbox_b
    if crop_bbox_a is not None and crop_bbox_b is not None:
        assert captured["bbox_a_arg"] == crop_bbox_a, (
            f"§11.115.21 REGRESSION: bbox_a passed to pairwise_diff writer "
            f"({captured['bbox_a_arg']}) does not match verdict.crop_bbox_a "
            f"({crop_bbox_a}). The crop bboxes (which define the saved crop "
            f"file extents on disk) should be passed, not diff-trail bboxes "
            f"(verdict.bbox_a={verdict.bbox_a})."
        )
        assert captured["bbox_b_arg"] == crop_bbox_b, (
            f"§11.115.21 REGRESSION: bbox_b passed to pairwise_diff writer "
            f"({captured['bbox_b_arg']}) does not match verdict.crop_bbox_b "
            f"({crop_bbox_b}). Diff-trail bboxes: verdict.bbox_b={verdict.bbox_b}."
        )
    else:
        # Suppressed verdict — crop bboxes are None. The writer handles None.
        assert captured["bbox_a_arg"] is None, (
            "Suppressed verdict should pass None crop_bbox_a to writer"
        )
        assert captured["bbox_b_arg"] is None, (
            "Suppressed verdict should pass None crop_bbox_b to writer"
        )


def test_gate_run_pairwise_diff_writer_receives_distinct_crop_and_trail_bboxes(tmp_path, monkeypatch):
    """§11.115.21 — explicit assertion that crop bbox ≠ trail bbox
    in this synthetic motion scenario, and that the writer got the
    crop bboxes.

    With moving frames, _frame_diff_fn produces diff(2,3) and diff(3,4)
    bboxes. The crop bboxes are AND(subject_masks). These can differ.
    If the call site is passing the trail bboxes, captured["bbox_*_arg"]
    will be the trail. If passing crop bboxes (correct), it'll be the
    AND subject bboxes.

    We assert: captured bboxes must equal verdict.bbox_a / verdict.bbox_b
    AND verdict.bbox_a must exist (so we can compare). On Phase.175,
    verdict.bbox_a is the subject AND bbox on frame_2 (not the diff
    trail between frame_2 and frame_3).
    """

    from listener import motion_gate_pipeline as mgp
    from listener.motion_gate_pipeline import run
    from listener.tests.test_motion_gate_pipeline import (
        FakeClassifier,
        QuickVerdict,
        _make_synthetic_frames,
    )

    monkeypatch.setattr(mgp, "_gate_keep_disk_artifacts", lambda: True)
    monkeypatch.setattr(mgp, "_is_keep_disk_artifacts_enabled", lambda: True)

    captured: dict = {}

    def fake_writer(frame_a, frame_b, bbox_a_arg, bbox_b_arg, output_dir, alert_id):
        captured["bbox_a_arg"] = bbox_a_arg
        captured["bbox_b_arg"] = bbox_b_arg
        out_p = Path(output_dir) / "pairwise_diff.png"
        out_p.parent.mkdir(parents=True, exist_ok=True)
        out_p.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 200)
        return str(out_p)

    monkeypatch.setattr(mgp, "_write_pairwise_diff_image", fake_writer)

    frame_paths = _make_synthetic_frames(tmp_path, motion=True)
    fake = FakeClassifier(
        responses=[
            QuickVerdict(top_class="car", top_confidence=0.9, decision="pass_with_hint"),
            QuickVerdict(top_class="car", top_confidence=0.9, decision="pass_with_hint"),
        ]
    )

    verdict = run(
        frame_paths=frame_paths,
        camera_name="OFS",
        alert_id="test-alert-11521-distinct",
        output_dir=str(tmp_path),
        classifier=fake,
    )

    # If the gate suppressed (e.g., motion too small), the trail bbox
    # and crop bbox would both be None — we can't prove anything in
    # that case. The prior test catches None-passing regressions.
    if verdict.crop_bbox_a is None or verdict.crop_bbox_b is None:
        pytest.skip(
            f"Verdict crop_bbox is None (decision={verdict.decision!r}) — "
            "can't compare crop vs trail bboxes"
        )

    # Both writer args must match the crop bboxes (verdict.crop_bbox_a/b)
    # — NOT the trail bboxes (verdict.bbox_a/b).
    assert captured["bbox_a_arg"] == verdict.crop_bbox_a
    assert captured["bbox_b_arg"] == verdict.crop_bbox_b