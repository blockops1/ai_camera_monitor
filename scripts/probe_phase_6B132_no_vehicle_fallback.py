"""Phase.132 probe — verify the vehicle non-crop fallback is gone (§11.54).

the operator 2026-08-26: "I don't want the non-crop fallback to exist. We keep
working on designing straight paths and I keep finding that there's these
backup systems that do something completely different and kick in at
strange times."

Invariants:
  1. Old name `_fallback_single_frame_vision` is gone from the module.
  2. New name `_non_vehicle_first_pass` exists.
  3. identify_stage source has no call to `_fallback_single_frame_vision`.
  4. identify_stage source has no call to `analyze_frames_queued`.
  5. `_non_vehicle_first_pass` source DOES call `analyze_frames_queued`.
  6. `_non_vehicle_first_pass` calls it with mode='motion'.
  7. match_stage gracefully handles vision_result=None/empty (signature
     is empty → match stage logs and returns).

Usage:
    .venv/bin/python scripts/probe_phase_6B132_no_vehicle_fallback.py
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

PROJECT_ROOT = Path("<install-path>/ai_camera_monitor")
PIPELINE_PATH = PROJECT_ROOT / "listener" / "vehicle_event_pipeline.py"
sys.path.insert(0, str(PROJECT_ROOT))


def _check(name: str, ok: bool, detail: str = "") -> bool:
    flag = "✅" if ok else "❌"
    print(f"  {flag} {name}" + (f" — {detail}" if detail else ""))
    return ok


def check_old_name_gone() -> bool:
    """`_fallback_single_frame_vision` is removed from the module."""
    from listener import vehicle_event_pipeline as v
    return _check(
        "_fallback_single_frame_vision removed from module",
        not hasattr(v, "_fallback_single_frame_vision"),
    ) and _check(
        "_non_vehicle_first_pass exists",
        hasattr(v, "_non_vehicle_first_pass"),
    )


def check_source_has_no_old_fallback_ref() -> bool:
    """No remaining reference to `_fallback_single_frame_vision` in source."""
    src = PIPELINE_PATH.read_text()
    return _check(
        "no `_fallback_single_frame_vision` string in pipeline source",
        "_fallback_single_frame_vision" not in src,
    )


def check_analyze_frames_only_in_non_vehicle_path() -> bool:
    """analyze_frames_queued appears only in _non_vehicle_first_pass."""
    src = PIPELINE_PATH.read_text()
    tree = ast.parse(src)

    locations: list[tuple[str, int]] = []

    def walk(node: ast.AST, scope: str) -> None:
        if isinstance(node, ast.FunctionDef):
            scope = node.name
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "analyze_frames_queued":
            locations.append((scope, node.lineno))
        for child in ast.iter_child_nodes(node):
            walk(child, scope)

    walk(tree, "(module)")

    in_identify = [line for fn, line in locations if fn == "identify_stage"]
    in_non_vehicle = [line for fn, line in locations if fn == "_non_vehicle_first_pass"]

    return _check(
        "analyze_frames_queued NOT in identify_stage",
        len(in_identify) == 0,
        f"lines={in_identify}",
    ) and _check(
        "analyze_frames_queued IS in _non_vehicle_first_pass",
        len(in_non_vehicle) >= 1,
        f"lines={in_non_vehicle}",
    )


def check_vehicle_branch_skips_when_vision_result_none() -> bool:
    """match_stage must short-circuit cleanly on empty vision_result."""
    from listener.vehicle_event_pipeline import AlertContext, match_stage

    ctx = AlertContext(
        alert_id="probe-ms-1",
        camera_name="CAM5",
        timestamp="2026-08-26 14:00:00 EDT",
        event_type="vehicle",
        rtsp_url="rtsp://x/oftest",
        output_dir="/tmp/probe",
        is_vehicle_event=True,
        known_vehicles=[],
        bot_token="x", chat_id="x", api_url="http://x",
        gatekeeper_cameras=frozenset({"CAM5"}),
        motion_result=None,
        frame_paths=["/tmp/frame_001.jpg"],
        vision_result=None,  # ← the suppressed case
    )

    # match_stage reads ctx.known_vehicles if not pre-set
    ctx.known_vehicles = []

    # Should not raise.
    match_stage(ctx)

    return _check(
        "match_stage handles vision_result=None without raising",
        ctx.match_verdict is None,
    )


def check_non_vehicle_uses_motion_mode() -> bool:
    """_non_vehicle_first_pass calls analyze_frames_queued with mode='motion'."""
    import infra.vision_analyzer as va

    captured: dict = {}

    def fake_analyze(
        frame_paths: list[str],
        camera_name: str,
        api_url: str = "",
        timeout_s: float = 0.0,
        alert_id: str | None = None,
        event_hint: str | None = None,
        captured_at: str | None = None,
        mode: str | None = None,
    ):
        captured["mode"] = mode
        captured["frame_count"] = len(frame_paths)
        return {"primary_subject": "person", "scene_description": "x"}

    orig = va.analyze_frames_queued
    va.analyze_frames_queued = fake_analyze

    try:
        from listener.vehicle_event_pipeline import (
            AlertContext,
            _non_vehicle_first_pass,
        )
        ctx = AlertContext(
            alert_id="probe-nv-1",
            camera_name="CAM5",
            timestamp="2026-08-26 14:00:00 EDT",
            event_type="motion",
            rtsp_url="rtsp://x/oftest",
            output_dir="/tmp/probe",
            is_vehicle_event=False,
            known_vehicles=[],
            bot_token="x", chat_id="x", api_url="http://x",
            gatekeeper_cameras=frozenset(),
            frame_paths=["/tmp/frame_001.jpg"],
        )
        _non_vehicle_first_pass(ctx)
        return _check(
            "_non_vehicle_first_pass: mode='motion' preserved",
            captured.get("mode") == "motion",
            f"mode={captured.get('mode')!r}",
        )
    finally:
        va.analyze_frames_queued = orig


def check_match_handles_empty_signature() -> bool:
    """When vision_result is {}, _extract_signature returns {} → match_stage
    returns None without crashing. This is the suppression end-state."""
    from listener.vehicle_event_pipeline import AlertContext, _extract_signature

    ctx = AlertContext(
        alert_id="probe-es-1",
        camera_name="CAM5",
        timestamp="2026-08-26 14:00:00 EDT",
        event_type="vehicle",
        rtsp_url="rtsp://x/oftest",
        output_dir="/tmp/probe",
        is_vehicle_event=True,
        known_vehicles=[],
        bot_token="x", chat_id="x", api_url="http://x",
        gatekeeper_cameras=frozenset({"CAM5"}),
        motion_result=None,
        frame_paths=["/tmp/frame_001.jpg"],
        vision_result={},
    )
    sig = _extract_signature(ctx)
    return _check(
        "_extract_signature({}) returns empty dict",
        sig == {},
        f"sig={sig!r}",
    )


def main() -> int:
    print("=" * 70)
    print("Phase.132 probe — vehicle non-crop fallback deleted (§11.54)")
    print("=" * 70)

    results = []
    results.append(check_old_name_gone())
    print()
    results.append(check_source_has_no_old_fallback_ref())
    print()
    results.append(check_analyze_frames_only_in_non_vehicle_path())
    print()
    results.append(check_non_vehicle_uses_motion_mode())
    print()
    results.append(check_match_handles_empty_signature())
    print()
    results.append(check_vehicle_branch_skips_when_vision_result_none())
    print()

    n_pass = sum(1 for r in results if r)
    n_total = len(results)
    print("=" * 70)
    print(f"{n_pass}/{n_total} checks passed")
    print("=" * 70)
    return 0 if n_pass == n_total else 1


if __name__ == "__main__":
    sys.exit(main())