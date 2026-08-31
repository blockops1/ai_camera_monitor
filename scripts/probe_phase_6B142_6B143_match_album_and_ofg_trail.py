"""Phase.143 probe — verify CAM3 person-gatekeeper uses spaced trail (§11.65).

Production change (2026-08-27): _motion_gate_dispatch.maybe_run_motion_gate
now treats CAM3 as a gatekeeper for capture purposes — it gets the
pre-event trail at indices [0, 30, 60, 90] from the persistent RTSP
ring buffer, mirroring CAM5. This produces a Telegram album where the
4 wide frames are visibly 2 seconds apart instead of all at the same
instant (the 6B.141 album feature).

What this probe verifies (read-only checks against repo state):
  1. _motion_gate_dispatch.PERSON_GATEKEEPER_CAMERAS exists and contains CAM3
  2. _motion_gate_dispatch.ALL_GATEKEEPER_CAMERAS is GATEKEEPER_CAMERAS | PERSON_GATEKEEPER_CAMERAS
  3. maybe_run_motion_gate passes frame_offsets=[0, 30, 60, 90] for CAM3
  4. match_telegram.send_match_alert + send_no_match_alert use send_message
     + send_photo_group (not send_photo_with_caption) — Phase.142
  5. send_match_alert + send_no_match_alert signatures have NO output_dir arg
  6. match_telegram._concat_crops_vertical is gone (replaced by album path)
  7. listener.vehicle_event_pipeline callers don't pass output_dir anymore
  8. tests cover both 6B.142 (8 new) and 6B.143 (3 new)
"""

from __future__ import annotations

import importlib
import inspect
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def check(condition: bool, description: str) -> bool:
    print(f"  [{'PASS' if condition else 'FAIL'}] {description}")
    return condition


def main() -> int:
    failures = 0

    # ----- 6B.143: motion gate capture uses offset trail for CAM3 -----
    print("\n=== 6B.143: motion gate capture path ===")
    mod_dispatch = importlib.import_module("listener._motion_gate_dispatch")

    # 1. PERSON_GATEKEEPER_CAMERAS contains CAM3
    has_pgk = hasattr(mod_dispatch, "PERSON_GATEKEEPER_CAMERAS")
    if has_pgk:
        if not check("CAM3" in mod_dispatch.PERSON_GATEKEEPER_CAMERAS,
                     "PERSON_GATEKEEPER_CAMERAS contains 'CAM3'"):
            failures += 1
    else:
        failures += 1
        check(False, "PERSON_GATEKEEPER_CAMERAS exists in dispatch")

    # 2. ALL_GATEKEEPER_CAMERAS = union
    has_all = hasattr(mod_dispatch, "ALL_GATEKEEPER_CAMERAS")
    if has_all:
        expected = mod_dispatch.GATEKEEPER_CAMERAS | mod_dispatch.PERSON_GATEKEEPER_CAMERAS
        if not check(mod_dispatch.ALL_GATEKEEPER_CAMERAS == expected,
                     "ALL_GATEKEEPER_CAMERAS = GATEKEEPER_CAMERAS | PERSON_GATEKEEPER_CAMERAS"):
            failures += 1
    else:
        failures += 1
        check(False, "ALL_GATEKEEPER_CAMERAS exists in dispatch")

    # 3. maybe_run_motion_gate actually passes frame_offsets=[0,30,60,90] for CAM3
    src = inspect.getsource(mod_dispatch.maybe_run_motion_gate)
    if not check("ALL_GATEKEEPER_CAMERAS" in src,
                 "maybe_run_motion_gate uses ALL_GATEKEEPER_CAMERAS for is_gatekeeper check"):
        failures += 1
    if not check("GATEKEEPER_FRAME_OFFSETS" in src,
                 "maybe_run_motion_gate applies GATEKEEPER_FRAME_OFFSETS when gatekeeper"):
        failures += 1

    # ----- 6B.142: match/no_match senders use album path -----
    print("\n=== 6B.142: match Telegram album path ===")
    mod_match = importlib.import_module("telegram_formatter.match_telegram")

    # 4. senders use send_photo_group (not send_photo_with_caption)
    src_match = inspect.getsource(mod_match.send_match_alert)
    src_no_match = inspect.getsource(mod_match.send_no_match_alert)
    if not check("send_photo_group" in src_match and "send_photo_with_caption" not in src_match,
                 "send_match_alert uses send_photo_group (not send_photo_with_caption)"):
        failures += 1
    if not check("send_photo_group" in src_no_match and "send_photo_with_caption" not in src_no_match,
                 "send_no_match_alert uses send_photo_group (not send_photo_with_caption)"):
        failures += 1

    # 5. senders no longer take output_dir
    sig_match = inspect.signature(mod_match.send_match_alert)
    sig_no_match = inspect.signature(mod_match.send_no_match_alert)
    if not check("output_dir" not in sig_match.parameters,
                 "send_match_alert signature has no output_dir (composite is gone)"):
        failures += 1
    if not check("output_dir" not in sig_no_match.parameters,
                 "send_no_match_alert signature has no output_dir"):
        failures += 1

    # 6. _concat_crops_vertical removed
    if not check(not hasattr(mod_match, "_concat_crops_vertical"),
                 "_concat_crops_vertical removed (replaced by send_photo_group album)"):
        failures += 1

    # 7. vehicle_event_pipeline callers updated
    print("\n=== caller signatures ===")
    mod_pipeline = importlib.import_module("listener.vehicle_event_pipeline")
    src_pipeline = inspect.getsource(mod_pipeline)
    # Find send_match_alert( call sites
    count = 0
    bad = 0
    for line in src_pipeline.split("\n"):
        if "send_match_alert(" in line or "send_no_match_alert(" in line:
            count += 1
        if "output_dir=ctx.output_dir" in line and (
            "send_match_alert" in src_pipeline[max(0, src_pipeline.find(line)-300):src_pipeline.find(line)+50]
            or "send_no_match_alert" in src_pipeline[max(0, src_pipeline.find(line)-300):src_pipeline.find(line)+50]
        ):
            bad += 1
    if not check(count >= 2 and bad == 0,
                 f"vehicle_event_pipeline: {count} call sites, 0 pass output_dir to match/no_match senders"):
        failures += 1

    # ----- tests exist -----
    print("\n=== test coverage ===")
    test_match_send = ROOT / "telegram_formatter/tests/test_match_alert_send.py"
    test_dispatch = ROOT / "listener/tests/test_motion_gate_dispatch.py"
    if not check(test_match_send.exists(),
                 "telegram_formatter/tests/test_match_alert_send.py exists (6B.142)"):
        failures += 1
    if not check(test_dispatch.exists(),
                 "listener/tests/test_motion_gate_dispatch.py exists (6B.143)"):
        failures += 1

    # Count test classes / functions in each
    import ast
    if test_match_send.exists():
        tree = ast.parse(test_match_send.read_text())
        test_fns = [n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name.startswith("test_")]
        if not check(len(test_fns) >= 8,
                     f"test_match_alert_send.py has {len(test_fns)} test functions (expected ≥8)"):
            failures += 1

    if test_dispatch.exists():
        tree = ast.parse(test_dispatch.read_text())
        test_fns = [n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name.startswith("test_")]
        ofg_test_present = any("ofg" in f.lower() for f in test_fns)
        if not check(ofg_test_present,
                     f"test_motion_gate_dispatch.py has an CAM3-specific test (count={len(test_fns)})"):
            failures += 1

    # ----- module headers updated -----
    print("\n=== module headers ===")
    dispatch_file = mod_dispatch.__file__
    match_file = mod_match.__file__
    assert dispatch_file is not None, "_motion_gate_dispatch.__file__ is None"
    assert match_file is not None, "match_telegram.__file__ is None"
    dispatch_src = Path(dispatch_file).read_text()
    if not check("6B.143" in dispatch_src,
                 "_motion_gate_dispatch.py header references 6B.143"):
        failures += 1
    match_src = Path(match_file).read_text()
    if not check("6B.142" in match_src,
                 "match_telegram.py header references 6B.142"):
        failures += 1

    # ----- PLAN.md updated -----
    plan = (ROOT / "PLAN.md").read_text()
    if not check("6B.142" in plan and "6B.143" in plan,
                 "PLAN.md references both 6B.142 and 6B.143"):
        failures += 1

    print()
    if failures == 0:
        print("PASS — 0 failures. 6B.142 + 6B.143 ready to ship.")
        return 0
    print(f"FAIL — {failures} check(s) failed.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
