"""Phase.139 probe — verify gate_aware_person_capture wiring (§11.60).

Production bug (2026-08-23 to 2026-08-27): person pipeline used a
gate-aware capture stub that delegated to a 6-second-late fresh RTSP
pull (`person_capture_stage` → `infra.frame_capture.capture_frames`).
Qwen analyzed the WRONG moment — the user may have left by the time
fresh frames arrived. Telegram then showed the operator standing in the wide
shot with body text "no person in frame" because Qwen's late analysis
found nothing.

Evidence: 6 person Telegrams on 2026-08-26, 5 of 6 with
`reason: no_person_in_frame` despite the image clearly showing a person.

The fix mirrors the vehicle path's gate-aware capture (Phase.115):
- gate_aware_person_capture reads 4 PIL.Image from verdict.frames
- Selects the middle two (frames[1], frames[2]) for Qwen
- Writes the 2 selected frames to disk under ctx.output_dir
  (or reuses verdict.frame_paths when GATE_KEEP_DISK_ARTIFACTS=true)
- Sets ctx.capture_source = "gate"
- The 6-second-late fresh RTSP pull is removed

This probe:
  §1. Drives gate_aware_person_capture across 4 representative cases
      (gate_verdict with frames, gate_verdict with no frames, no
      gate_verdict, gate_verdict with frame_paths=[])
  §2. Verifies process_person_event end-to-end (with stubbed Qwen +
      stubbed Telegram) sets capture_source = "gate"
  §3. Static check: source-embedded regression notes are present

Usage: just run it.
    python scripts/probe_phase_6B139_person_gate_capture.py

Exits 0 on PASS, 1 on any FAIL.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]

sys.path.insert(0, str(ROOT / ".venv" / "lib" / "python3.14" / "site-packages"))
sys.path.insert(0, str(ROOT))

from PIL import Image as _PILImage

from listener._gate_aware_capture import SkipEvent
from listener.motion_gate_pipeline import GateVerdict
from listener.person_event_pipeline import (
    PersonContext,
    person_capture_stage,
    process_person_event,
)


def _make_gate_verdict(out_dir: Path, *, with_disk_paths: bool = True) -> GateVerdict:
    """Build a GateVerdict with 4 in-memory PIL frames."""
    out_dir.mkdir(parents=True, exist_ok=True)
    pil_frames = []
    for i, color in enumerate(
        [(255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 0)], start=1
    ):
        if with_disk_paths:
            path = out_dir / f"frame_{i:03d}.jpg"
            img = _PILImage.new("RGB", (640, 480), color=color)
            img.save(str(path), "JPEG")
            pil_frames.append(img)
        else:
            img = _PILImage.new("RGB", (640, 480), color=color)
            pil_frames.append(img)
    crop_a_pil = pil_frames[2].copy()
    crop_b_pil = pil_frames[3].copy()
    frame_paths = (
        [str(out_dir / f"frame_{i:03d}.jpg") for i in (1, 2, 3, 4)]
        if with_disk_paths
        else []
    )
    return GateVerdict(
        decision="person",
        class_label="person",
        confidence=0.82,
        crop_a_path=str(out_dir / "frame_003.jpg") if with_disk_paths else "",
        crop_b_path=str(out_dir / "frame_004.jpg") if with_disk_paths else "",
        bbox_a=(180, 200, 320, 480),
        bbox_b=(200, 220, 300, 460),
        frames=pil_frames,
        crop_a=crop_a_pil,
        crop_b=crop_b_pil,
        frame_paths=frame_paths,
        raw_verdicts=[],
        reason="high_conf_person",
    )


def _make_ctx(output_dir: Path) -> PersonContext:
    return PersonContext(
        alert_id="probe-6B139",
        camera_name="CAM1",
        timestamp="2026-08-27T10:00:00-04:00",
        event_type="person",
        rtsp_url="rtsp://test/front-door-outside",
        output_dir=str(output_dir),
        bot_token="",
        chat_id="",
        api_url="",
    )


def case_fast_path(out_dir: Path) -> bool:
    """§1.1 — gate_verdict with 4 PIL + 4 paths → ctx.capture_source='gate'."""
    from listener._gate_aware_capture import gate_aware_person_capture

    ctx = _make_ctx(out_dir / "case1")
    ctx.gate_verdict = _make_gate_verdict(out_dir / "case1", with_disk_paths=True)
    try:
        gate_aware_person_capture(ctx)
    except SkipEvent as e:
        print(f"  [FAIL] §1.1 fast path unexpectedly raised SkipEvent: {e}")
        return False
    ok = (
        ctx.capture_source == "gate"
        and len(ctx.frames) == 4
        and len(ctx.frame_paths) == 2
        and len(ctx.selected_frames) == 2
        and ctx.selected_frames[0] is ctx.gate_verdict.frames[1]
        and ctx.selected_frames[1] is ctx.gate_verdict.frames[2]
        and ctx.frame_paths == [
            ctx.gate_verdict.frame_paths[1],
            ctx.gate_verdict.frame_paths[2],
        ]
    )
    print(
        f"  [{'PASS' if ok else 'FAIL'}] §1.1 fast path: "
        f"capture_source={ctx.capture_source} frames={len(ctx.frames)} "
        f"frame_paths={len(ctx.frame_paths)} selected={len(ctx.selected_frames)}"
    )
    return ok


def case_no_disk_paths(out_dir: Path) -> bool:
    """§1.2 — gate_verdict frame_paths=[] → gate_aware_person_capture writes to ctx.output_dir."""
    from listener._gate_aware_capture import gate_aware_person_capture

    ctx = _make_ctx(out_dir / "case2")
    ctx.gate_verdict = _make_gate_verdict(out_dir / "case2", with_disk_paths=False)
    try:
        gate_aware_person_capture(ctx)
    except SkipEvent as e:
        print(f"  [FAIL] §1.2 disk-write unexpectedly raised SkipEvent: {e}")
        return False
    p1 = Path(ctx.frame_paths[0]) if ctx.frame_paths else None
    p2 = Path(ctx.frame_paths[1]) if len(ctx.frame_paths) > 1 else None
    ok = (
        ctx.capture_source == "gate"
        and p1 is not None
        and p2 is not None
        and p1.name == "frame_gate_001.jpg"
        and p2.name == "frame_gate_002.jpg"
        and p1.is_file()
        and p2.is_file()
    )
    print(
        f"  [{'PASS' if ok else 'FAIL'}] §1.2 disk-write: "
        f"p1={p1.name if p1 else 'MISSING'} exists={p1.is_file() if p1 else False} "
        f"p2={p2.name if p2 else 'MISSING'} exists={p2.is_file() if p2 else False}"
    )
    return ok


def case_no_verdict(out_dir: Path) -> bool:
    """§1.3 — gate_verdict=None → SkipEvent raised, capture_source='missing'."""
    from listener._gate_aware_capture import SkipEvent, gate_aware_person_capture

    ctx = _make_ctx(out_dir / "case3")
    ctx.gate_verdict = None
    try:
        gate_aware_person_capture(ctx)
        print("  [FAIL] §1.3 no verdict: should have raised SkipEvent")
        return False
    except SkipEvent:
        ok = ctx.capture_source == "missing"
        print(
            f"  [{'PASS' if ok else 'FAIL'}] §1.3 no verdict: "
            f"SkipEvent raised, capture_source={ctx.capture_source}"
        )
        return ok


def case_short_frames(out_dir: Path) -> bool:
    """§1.4 — verdict with 2 frames (not 4) → SkipEvent raised."""
    from listener._gate_aware_capture import SkipEvent, gate_aware_person_capture

    ctx = _make_ctx(out_dir / "case4")
    short_verdict = GateVerdict(
        decision="person",
        class_label="person",
        confidence=0.7,
        crop_a_path="",
        crop_b_path="",
        bbox_a=None,
        bbox_b=None,
        frames=[_PILImage.new("RGB", (640, 480)), _PILImage.new("RGB", (640, 480))],
        crop_a=None,
        crop_b=None,
        frame_paths=[],
        raw_verdicts=[],
        reason="high_conf_person",
    )
    ctx.gate_verdict = short_verdict
    try:
        gate_aware_person_capture(ctx)
        print("  [FAIL] §1.4 short frames: should have raised SkipEvent")
        return False
    except SkipEvent:
        ok = ctx.capture_source == "missing"
        print(
            f"  [{'PASS' if ok else 'FAIL'}] §1.4 short frames: "
            f"SkipEvent raised, capture_source={ctx.capture_source}"
        )
        return ok


def case_pipeline_end_to_end(out_dir: Path) -> bool:
    """§2 — process_person_event end-to-end sets capture_source='gate'.

    Phase.141 (2026-08-27): person_emit now uses send_message +
    send_photo_group. Both are mocked so the test doesn't try to
    send real Telegrams (which would 404 with test tokens).
    """
    ctx = _make_ctx(out_dir / "case5")
    ctx.gate_verdict = _make_gate_verdict(out_dir / "case5", with_disk_paths=True)

    with patch(
        "infra.vision_analyzer.analyze_frames_queued",
        return_value={"persons": []},
    ), patch(
        "infra.send_telegram.send_message",
        return_value=True,  # Phase.141: body path
    ), patch(
        "infra.send_telegram.send_photo_group",
        return_value=True,  # Phase.141: album path
    ), patch(
        "infra.alert_history.append_alert"
    ):
        result = process_person_event(ctx)

    ok = ctx.capture_source == "gate" and result["telegram_sent"] is True
    print(
        f"  [{'PASS' if ok else 'FAIL'}] §2 end-to-end: "
        f"capture_source={ctx.capture_source} telegram_sent={result['telegram_sent']}"
    )
    return ok


def case_capture_stage_does_not_pull_rtsp(out_dir: Path) -> bool:
    """§3 — person_capture_stage stub does NOT call capture_frames (regression)."""
    ctx = _make_ctx(out_dir / "case6")
    with patch("infra.frame_capture.capture_frames") as mock_capture:
        person_capture_stage(ctx)
    ok = ctx.frame_paths == [] and mock_capture.call_count == 0
    print(
        f"  [{'PASS' if ok else 'FAIL'}] §3 capture_stage stub: "
        f"frame_paths={ctx.frame_paths} capture_calls={mock_capture.call_count}"
    )
    return ok


def check_source_regression_notes() -> bool:
    """§4 — static check: source contains the regression notes."""
    src_gate = (ROOT / "listener" / "_gate_aware_capture.py").read_text()
    src_person = (ROOT / "listener" / "person_event_pipeline.py").read_text()

    required = [
        (src_gate, "Phase.139 (§11.60", "section anchor in _gate_aware_capture"),
        (src_gate, "gate_aware_person_capture", "function defined"),
        (src_gate, "selected_frames", "selected_frames field"),
        (src_gate, "frame_gate_001.jpg", "disk-write fallback filename"),
        (src_person, "DEPRECATED 2026-08-27", "stub deprecation marker"),
        (src_person, "Capture frames from RTSP", "module header note about removed responsibility"),
        (src_person, "Phase.139 (§11.60", "section anchor in person_event_pipeline"),
    ]
    failed = []
    for src, phrase, descr in required:
        if phrase.lower() in src.lower():
            print(f"  [PASS] {descr}: {phrase!r}")
        else:
            print(f"  [FAIL] {descr}: {phrase!r}")
            failed.append(phrase)
    return len(failed) == 0


def main() -> int:
    # Quiet down the listener logger during probe
    logging.basicConfig(level=logging.WARNING)

    out_dir = ROOT / "data" / "frames" / "_probe_6B139"
    out_dir.mkdir(parents=True, exist_ok=True)

    print("=" * 78)
    print("Probe: Phase.139 gate_aware_person_capture wiring (§11.60)")
    print("=" * 78)

    results: list[bool] = []
    print("\n§1. gate_aware_person_capture unit cases:")
    results.append(case_fast_path(out_dir))
    results.append(case_no_disk_paths(out_dir))
    results.append(case_no_verdict(out_dir))
    results.append(case_short_frames(out_dir))

    print("\n§2. process_person_event end-to-end:")
    results.append(case_pipeline_end_to_end(out_dir))

    print("\n§3. Legacy stub regression:")
    results.append(case_capture_stage_does_not_pull_rtsp(out_dir))

    print("\n§4. Source-embedded regression notes:")
    results.append(check_source_regression_notes())

    failures = sum(1 for r in results if not r)
    total = len(results)
    print(f"\n{'=' * 78}")
    if failures:
        print(f"FAIL — {failures} of {total} checks failed.")
        return 1
    print(f"PASS — {total} of {total} checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
