#!/usr/bin/env python3
"""smoke_per_camera_subjects.py -- End-to-end smoke test for per-camera subjects routing.

Imports the real infra.gate module (no mocks), loads the real
config/motion_gate_thresholds.json, and exercises all 6 routing cases
from US-055c with synthetic QuickVerdict fixtures.

Prints 'SMOKE OK' and exits 0 on success; prints the failing case and
exits 1 on failure. No side effects, no log writes, no webhook calls.

Usage:
    python scripts/smoke_per_camera_subjects.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# Ensure the repo root is on sys.path so `from infra...` imports resolve
# when this script is run as `python scripts/smoke_per_camera_subjects.py`
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from infra.gate import _route_decision, load_thresholds
from infra.quick_classifier import QuickVerdict

# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

def _qv(top_class: str, top_confidence: float, decision: str = "pass") -> QuickVerdict:
    """Build a QuickVerdict fixture with minimal fields."""
    return QuickVerdict(
        top_class=top_class,
        top_confidence=top_confidence,
        decision=decision,
        n_detections=1,
        raw_predictions=[],
        reason=None,
    )


# ---------------------------------------------------------------------------
# Test cases (6 cases from US-055c / V2-055b)
# ---------------------------------------------------------------------------

CASES = [
    # 1) FRONT solo person -> person
    {
        "name": "FRONT solo person -> person",
        "camera": "FRONT",
        "verdict_a": _qv("person", 0.61),
        "verdict_b": _qv("car", 0.10, "suppress"),
        "expected_classification": "person",
        "expected_class_label": "person",
        "expected_reason": None,  # any reason is OK (not the suppress one)
    },
    # 2) FRONT solo vehicle -> suppressed (vehicle not in subjects)
    {
        "name": "FRONT solo vehicle -> suppressed",
        "camera": "FRONT",
        "verdict_a": _qv("truck", 0.76),
        "verdict_b": _qv("car", 0.05, "suppress"),
        "expected_classification": "none",
        "expected_class_label": "truck",
        "expected_reason": "top_class_truck_not_in_subjects",
    },
    # 3) FRONT mixed person+vehicle -> person (subject_allowed)
    {
        "name": "FRONT mixed person+vehicle -> person",
        "camera": "FRONT",
        "verdict_a": _qv("person", 0.61),
        "verdict_b": _qv("car", 0.55),
        "expected_classification": "person",
        "expected_class_label": "person",
        "expected_reason": "subject_allowed",
    },
    # 4) OUTSIDE_FRONT_SOLAR solo truck -> vehicle (no subjects, unchanged)
    {
        "name": "OUTSIDE_FRONT_SOLAR solo truck -> vehicle",
        "camera": "OUTSIDE_FRONT_SOLAR",
        "verdict_a": _qv("truck", 0.76),
        "verdict_b": _qv("car", 0.05, "suppress"),
        "expected_classification": "vehicle",
        "expected_class_label": "truck",
        "expected_reason": "high_conf_vehicle",
    },
    # 5) OUTSIDE_FRONT_SOLAR solo person -> suppressed (no subjects)
    {
        "name": "OUTSIDE_FRONT_SOLAR solo person -> suppressed",
        "camera": "OUTSIDE_FRONT_SOLAR",
        "verdict_a": _qv("person", 0.61),
        "verdict_b": _qv("dog", 0.05, "suppress"),
        "expected_classification": "none",
        "expected_class_label": "person",
        "expected_reason": "top_class_person_not_in_subjects",
    },
    # 6) BACK solo person -> person (mirrors FRONT)
    {
        "name": "BACK solo person -> person",
        "camera": "BACK",
        "verdict_a": _qv("person", 0.65),
        "verdict_b": _qv("cat", 0.05, "suppress"),
        "expected_classification": "person",
        "expected_class_label": "person",
        "expected_reason": "subject_allowed",
    },
]


def main() -> int:
    """Run the smoke test. Return 0 on success, 1 on failure."""
    print("=== smoke: per-camera subjects routing ===")

    # Step 1: Load thresholds and verify config structure.
    print("\n[1] Loading config/motion_gate_thresholds.json ...")
    cfg_path = _REPO_ROOT / "config" / "motion_gate_thresholds.json"
    with open(cfg_path, "r") as f:
        cfg = json.load(f)

    # Verify FRONT and BACK have subjects, perimeter cameras do not.
    front_subjects = cfg["FRONT"].get("subjects")
    back_subjects = cfg["BACK"].get("subjects")
    assert front_subjects == ["person", "animal"], \
        f"FRONT subjects unexpected: {front_subjects}"
    assert back_subjects == ["person", "animal"], \
        f"BACK subjects unexpected: {back_subjects}"

    for cam in ["OUTSIDE_FRONT_GARAGE", "OUTSIDE_FRONT_POWER",
                 "OUTSIDE_FRONT_SOLAR", "OUTSIDE_BACK_SOLAR"]:
        assert "subjects" not in cfg.get(cam, {}), \
            f"{cam} should not have subjects key"
    print("  -> config structure verified (FRONT/BACK have subjects; "
          "perimeter cameras do not)")

    # Step 2: Verify load_thresholds works for all cameras.
    print("\n[2] Verifying load_thresholds() for all cameras ...")
    for cam_name in {c["camera"] for c in CASES}:
        # Just call load_thresholds to ensure it doesn't crash
        thresholds, subjects = load_thresholds(cam_name)
        assert isinstance(thresholds, dict), f"{cam_name}: thresholds not a dict"
        print(f"  -> {cam_name}: {len(thresholds)} thresholds, subjects={subjects}")

    # Step 3: Run all 6 test cases.
    print("\n[3] Running 6 routing cases ...")
    failures: list[str] = []
    for i, case in enumerate(CASES, 1):
        cam = case["camera"]
        thresholds, subjects = load_thresholds(cam)
        classification, class_label, confidence, reason = _route_decision(
            case["verdict_a"],
            case["verdict_b"],
            thresholds,
            v2=False,
            subjects=subjects,
        )

        ok = True
        if classification != case["expected_classification"]:
            ok = False
            failures.append(
                f"  Case {i}: {case['name']} - "
                f"classification={classification!r} "
                f"expected={case['expected_classification']!r}"
            )
        if class_label != case["expected_class_label"]:
            ok = False
            failures.append(
                f"  Case {i}: {case['name']} - "
                f"class_label={class_label!r} "
                f"expected={case['expected_class_label']!r}"
            )
        if case["expected_reason"] is not None and reason != case["expected_reason"]:
            ok = False
            failures.append(
                f"  Case {i}: {case['name']} - "
                f"reason={reason!r} "
                f"expected={case['expected_reason']!r}"
            )

        status = "PASS" if ok else "FAIL"
        print(f"  [{status}] Case {i}: {case['name']} "
              f"(classification={classification!r}, class_label={class_label!r}, "
              f"confidence={confidence}, reason={reason!r})")

    # Step 4: Final verdict.
    if failures:
        print("\n=== FAILURES ===")
        for f in failures:
            print(f)
        return 1

    print("\n=== SMOKE OK ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
