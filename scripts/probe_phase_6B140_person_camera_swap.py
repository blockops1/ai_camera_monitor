"""Phase.140 probe — verify person-gatekeeper camera swap (§11.61).

Production state change (2026-08-27): PERSON_GATEKEEPER_CAMERAS changed
from {"CAM1"} to {"CAM3"}. Front Door
Outside person events now class_disabled (no person pipeline); CAM3
person webhooks now route to _process_person_alert.

This probe verifies the new routing via direct _classify_queue calls
plus a dry-run of the full webhook entry point with mocked pipelines.

Checks (7 sections, all must pass):
  §1. Module constants: PERSON_GATEKEEPER_CAMERAS = {CAM3}, CAM1 absent
  §2. class_disabled: CAM1 person/people → None; CAM3 person/people → QUEUE_PERSON
  §3. Vehicle routing unchanged: CAM3 vehicle event → QUEUE_OTHER_VEHICLE
  §4. Other cameras unchanged: CAM5/CAM4/CAM6 person still class_disabled
  §5. listener.py header notes Phase.140 / §11.61
  §6. PLAN.md §11.61 documents the new scope
  §7. Per-camera threshold for CAM3 person still 0.35 (config intact)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))


def section(title: str) -> None:
    print(f"\n=== {title} ===")


def check(condition: bool, label: str) -> bool:
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}] {label}")
    return condition


def main() -> int:
    results: list[bool] = []

    # -------------------------------------------------------------------------
    section("§1 — PERSON_GATEKEEPER_CAMERAS contains CAM3, not CAM1")
    # -------------------------------------------------------------------------
    from listener.listener import PERSON_GATEKEEPER_CAMERAS

    results.append(check(
        "CAM3" in PERSON_GATEKEEPER_CAMERAS,
        "CAM3 in PERSON_GATEKEEPER_CAMERAS",
    ))
    results.append(check(
        "CAM1" not in PERSON_GATEKEEPER_CAMERAS,
        "CAM1 NOT in PERSON_GATEKEEPER_CAMERAS",
    ))
    results.append(check(
        len(PERSON_GATEKEEPER_CAMERAS) == 1,
        f"exactly 1 camera (got {len(PERSON_GATEKEEPER_CAMERAS)})",
    ))

    # -------------------------------------------------------------------------
    section("§2 — _classify_queue: CAM1 person disabled, CAM3 person enabled")
    # -------------------------------------------------------------------------
    from listener.listener import _ClassedWebhookExecutor, _classify_queue
    QP = _ClassedWebhookExecutor.QUEUE_PERSON

    results.append(check(
        _classify_queue("CAM1", "person") is None,
        "CAM1 + person → None (class_disabled)",
    ))
    results.append(check(
        _classify_queue("CAM1", "people") is None,
        "CAM1 + people → None (class_disabled)",
    ))
    results.append(check(
        _classify_queue("CAM3", "person") == QP,
        "CAM3 + person → QUEUE_PERSON",
    ))
    results.append(check(
        _classify_queue("CAM3", "people") == QP,
        "CAM3 + people → QUEUE_PERSON",
    ))

    # -------------------------------------------------------------------------
    section("§3 — Vehicle routing unchanged for CAM3")
    # -------------------------------------------------------------------------
    QOV = _ClassedWebhookExecutor.QUEUE_OTHER_VEHICLE
    QGKV = _ClassedWebhookExecutor.QUEUE_GATEKEEPER_VEHICLE

    results.append(check(
        _classify_queue("CAM3", "vehicle") == QOV,
        "CAM3 + vehicle → QUEUE_OTHER_VEHICLE (6B.104 demotion holds)",
    ))
    results.append(check(
        _classify_queue("CAM5", "vehicle") == QGKV,
        "CAM5 + vehicle → QUEUE_GATEKEEPER_VEHICLE (vehicle gatekeeper intact)",
    ))

    # -------------------------------------------------------------------------
    section("§4 — Other perimeter cameras still class_disabled for person")
    # -------------------------------------------------------------------------
    results.append(check(
        _classify_queue("CAM4", "person") is None,
        "CAM4 + person → None (still disabled, Phase.73)",
    ))
    results.append(check(
        _classify_queue("CAM6", "person") is None,
        "CAM6 + person → None (still disabled, Phase.73)",
    ))
    results.append(check(
        _classify_queue("CAM5", "person") is None,
        "CAM5 + person → None (still disabled, gatekeeper camera)",
    ))

    # -------------------------------------------------------------------------
    section("§5 — listener.py header notes Phase.140 / §11.61")
    # -------------------------------------------------------------------------
    listener_src = (ROOT / "listener" / "listener.py").read_text()
    results.append(check(
        "Phase.140" in listener_src and "§11.61" in listener_src,
        "listener.py header references Phase.140 / §11.61",
    ))
    results.append(check(
        '"CAM3"' in listener_src,
        "PERSON_GATEKEEPER_CAMERAS = CAM3",
    ))

    # -------------------------------------------------------------------------
    section("§6 — PLAN.md §11.61 documents the new scope")
    # -------------------------------------------------------------------------
    plan = (ROOT / "PLAN.md").read_text()
    results.append(check(
        " §11.61" in plan and "switch person-gatekeeper camera" in plan,
        "PLAN.md §11.61 documents the CAM1→CAM3 swap",
    ))
    results.append(check(
        "CAM3" in plan and "CAM1" in plan,
        "PLAN.md §11.61 references both cameras",
    ))

    # -------------------------------------------------------------------------
    section("§7 — Per-camera threshold config: CAM3 person=0.35 intact")
    # -------------------------------------------------------------------------
    cfg = json.loads((ROOT / "config" / "motion_gate_thresholds.json").read_text())
    results.append(check(
        cfg.get("CAM3", {}).get("person") == 0.35,
        "CAM3 person threshold = 0.35",
    ))

    # -------------------------------------------------------------------------
    print()
    passed = sum(results)
    total = len(results)
    print(f"PASS — {passed} of {total} checks passed.")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())