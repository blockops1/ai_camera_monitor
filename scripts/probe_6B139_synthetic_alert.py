"""Verification probe — synthetic person event through _process_person_alert.

Exercises the full new path with a GateVerdict and confirms:
- Telegram is NOT actually sent (mocked)
- gate_aware_person_capture produces ctx.capture_source='gate'
- ctx.frame_paths has 2 entries (the 2 selected frames)

Run via:
    .venv/bin/python -m pytest listener/tests/test_person_event_pipeline_6B106.py::TestProcessPersonEvent -v
OR as a standalone probe (this file):
    .venv/bin/python scripts/probe_6B139_synthetic_alert.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import uuid
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Ensure no real Telegram send
os.environ.setdefault("FARMSURV_TESTING", "1")

from PIL import Image as _PILImage

from listener.motion_gate_pipeline import GateVerdict
from listener.person_event_pipeline import PersonContext


def main() -> int:
    from listener.listener import _process_person_alert

    alert_id = f"probe-{uuid.uuid4().hex[:8]}"
    out_dir = Path(tempfile.mkdtemp(prefix="probe_6B139_"))

    pil_frames = []
    for i, color in enumerate(
        [(255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 0)], start=1
    ):
        img = _PILImage.new("RGB", (640, 480), color=color)
        pil_frames.append(img)

    verdict = GateVerdict(
        decision="person",
        class_label="person",
        confidence=0.85,
        crop_a_path="",
        crop_b_path="",
        bbox_a=(180, 200, 320, 480),
        bbox_b=(200, 220, 300, 460),
        frames=pil_frames,
        crop_a=pil_frames[2].copy(),
        crop_b=pil_frames[3].copy(),
        frame_paths=[],
        raw_verdicts=[],
        reason="high_conf_person",
    )

    print(f"Alert ID: {alert_id}")
    print(f"Output dir: {out_dir}")
    print()

    with patch(
        "infra.send_telegram.send_photo_with_caption"
    ) as mock_tg, patch(
        "infra.send_telegram.send_message"
    ) as mock_msg, patch(
        "infra.vision_analyzer.analyze_frames_queued",
        return_value={"persons": [], "scene_description": "synthetic probe"},
    ), patch(
        "infra.alert_history.append_alert"
    ) as mock_hist, patch(
        "listener.listener._load_telegram_creds",
        return_value=("dummy", "dummy"),
    ):
        # Note: the ctx is built inside _process_person_alert, so we
        # intercept by capturing the PersonContext via a side-effect.
        captured: dict = {}

        real_person_ctx = PersonContext

        def capture_ctx(*args, **kwargs):
            ctx = real_person_ctx(*args, **kwargs)
            captured["ctx"] = ctx
            return ctx

        with patch("listener.person_event_pipeline.PersonContext", side_effect=capture_ctx):
            _process_person_alert(
                alert_id=alert_id,
                camera_name="CAM1",
                timestamp="2026-08-27T10:18:00-04:00",
                event="people",
                rtsp_url="rtsp://test/front-door-outside",
                gate_verdict=verdict,
            )

    print("=" * 60)
    print("Verification results:")
    print(f"  Telegram send_photo_with_caption: {mock_tg.call_count} (mocked)")
    print(f"  Telegram send_message: {mock_msg.call_count} (mocked)")
    print(f"  alert_history.append_alert: {mock_hist.call_count}")

    ctx = captured.get("ctx")
    if ctx is None:
        print("FAIL: PersonContext was not captured")
        return 1

    print()
    print(f"  ctx.capture_source: {ctx.capture_source!r}")
    print(f"  ctx.frame_paths: {len(ctx.frame_paths)} paths")
    print(f"  ctx.frames (PIL count): {len(ctx.frames)}")
    print(f"  ctx.selected_frames (PIL count): {len(ctx.selected_frames)}")
    print(f"  ctx.crop_a is None: {ctx.crop_a is None}")
    print(f"  ctx.crop_b is None: {ctx.crop_b is None}")

    if ctx.frame_paths:
        print(f"  first frame path: {ctx.frame_paths[0]}")
        print(f"    file exists: {Path(ctx.frame_paths[0]).is_file()}")

    print()
    checks = [
        (ctx.capture_source == "gate", "capture_source=='gate'"),
        (len(ctx.frames) == 4, "4 PIL frames in ctx.frames"),
        (len(ctx.selected_frames) == 2, "2 PIL frames in ctx.selected_frames"),
        (len(ctx.frame_paths) == 2, "2 disk paths in ctx.frame_paths"),
        (ctx.crop_a is not None, "crop_a populated from verdict"),
        (ctx.crop_b is not None, "crop_b populated from verdict"),
        # The synthetic alert's Qwen stub returns no persons, so the
        # person pipeline emits an "Unknown person" Telegram alert.
        # That's the correct behavior for a person-event-with-no-Qwen-match.
        # What we're verifying is that the path was exercised end-to-end.
        (mock_tg.call_count >= 1, "Telegram send_photo invoked (alert flow ran)"),
        (mock_hist.call_count == 1, "alert_history.append_alert called exactly once"),
    ]
    fails = []
    for ok, descr in checks:
        marker = "PASS" if ok else "FAIL"
        print(f"  [{marker}] {descr}")
        if not ok:
            fails.append(descr)
    print()
    if fails:
        print(f"FAIL — {len(fails)} checks failed: {fails}")
        return 1
    print("PASS — synthetic person event drove the new path end-to-end.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())