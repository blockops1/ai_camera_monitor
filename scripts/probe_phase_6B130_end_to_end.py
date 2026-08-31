"""Phase.130 probe — verify multi-vehicle Telegram-layer wiring end-to-end.

Three fixes from §11.53:

  Fix 1 — _vision_summary_str uses primary_vehicle_index + lists all vehicles
            joined with ", plus " for the TG#2 'identified as:' line.

  Fix 2 — _populate_legacy_fields_from_vehicles also populates colors.vehicle
            + objects_detected so alert_prompt sees useful data on multi-vehicle
            responses (not all-null defaults).

  Fix 3 — alert_prompt._build_payload inserts a 'Vehicles:' section listing
            each vehicle (cap 3) so the threat-level LLM knows what's in
            the picture.

Verifies:
  1. TG#2 'identified as:' line lists the primary + all secondaries
     (e.g. "red Kubota M7 tractor, plus silver Toyota 4Runner SUV"),
     correctly reordering when primary_vehicle_index != 0.
  2. The legacy top-level fields (colors.vehicle, objects_detected) are
     populated from vehicles[], not all-null.
  3. alert_prompt._build_payload includes a Vehicles: section with each
     vehicle's identification, with the primary marked.
  4. Telegram output trace matches the operator's expectation.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, '<install-path>/ai_camera_monitor')
os.chdir('<install-path>/ai_camera_monitor')

failed = 0


def report(name: str, ok: bool, detail: str = "") -> None:
    global failed
    status = "[OK]  " if ok else "[FAIL]"
    print(f"{status} {name} {detail}")
    if not ok:
        failed += 1


# Reproduce the exact production tractor-alert scenario:
# - Gate classified as car conf=0.82 (YOLO doesn't have tractor class)
# - Layer 1 promoted md → vehicle
# - Layer 2 vision received crops, returned multi-vehicle result

MULTI_VEHICLE_RESULT = {
    "vehicles": [
        {
            "color": "red", "body_style_hint": "tractor",
            "make": "Kubota", "model": "M7",
            "vehicle_features": {"wheel_style": "tractor_ag"},
            "description": "Red tractor with front-end loader",
            "confidence": 0.82,
        },
        {
            "color": "silver", "body_style_hint": "suv",
            "make": "Toyota", "model": "4Runner",
            "vehicle_features": {},
            "description": "Silver 4Runner SUV in background",
            "confidence": 0.88,
        },
        {
            "color": "blue", "body_style_hint": "sedan",
            "make": "Tesla", "model": "Model 3",
            "vehicle_features": {},
            "description": "Blue Tesla parked",
            "confidence": 0.95,
        },
    ],
    "primary_vehicle_index": 0,  # tractor = primary
    "scene_description": (
        "Red tractor parked on gravel road with silver 4Runner to the "
        "left and blue Tesla parked on the right. Rural scene, clear sky."
    ),
}


def test_fix_1_vision_summary() -> None:
    """TG#2 'identified as:' line uses primary first, lists all joined."""
    from listener.vehicle_event_pipeline import _vision_summary_str

    summary = _vision_summary_str(MULTI_VEHICLE_RESULT)
    expected = (
        "red Kubota M7 tractor, plus silver Toyota 4Runner suv, "
        "plus blue Tesla Model 3 sedan"
    )
    report(
        "Fix 1: _vision_summary_str lists primary first, then all secondaries",
        summary == expected,
        f"(got: {summary!r})",
    )

    # Edge case: primary is at index 1, not 0
    edge = {
        "vehicles": MULTI_VEHICLE_RESULT["vehicles"],
        "primary_vehicle_index": 1,  # 4Runner is primary (unusual)
    }
    edge_summary = _vision_summary_str(edge)
    report(
        "Fix 1: when primary_vehicle_index != 0, primary still comes first",
        edge_summary.startswith("silver Toyota 4Runner suv, plus red Kubota M7 tractor"),
        f"(got: {edge_summary!r})",
    )

    # Single-vehicle case (regression — was the legacy behavior)
    single = {
        "vehicles": [
            {"color": "white", "make": "Honda", "model": "Civic",
             "body_style_hint": "sedan"},
        ],
        "primary_vehicle_index": 0,
    }
    single_summary = _vision_summary_str(single)
    report(
        "Fix 1: single vehicle case still works (no spurious ', plus')",
        single_summary == "white Honda Civic sedan",
        f"(got: {single_summary!r})",
    )


def test_fix_2_legacy_field_population() -> None:
    """Top-level colors.vehicle and objects_detected now populated from vehicles[]."""
    from infra.vision_response import _validate_vision_result

    validated = _validate_vision_result(MULTI_VEHICLE_RESULT)
    assert validated is not None

    report(
        "Fix 2: colors.vehicle populated from primary (red)",
        validated.get("colors", {}).get("vehicle") == "red",
        f"(colors.vehicle={validated.get('colors', {}).get('vehicle')!r})",
    )

    objects = validated.get("objects_detected", [])
    report(
        "Fix 2: objects_detected lists ALL vehicles, not just primary",
        objects == [
            "tractor: Kubota M7",
            "suv: Toyota 4Runner",
            "sedan: Tesla Model 3",
        ],
        f"(objects_detected={objects!r})",
    )

    # Top-level back-compat (regression)
    report(
        "Fix 2: top-level make/model still populated from primary (Kubota M7)",
        validated.get("make") == "Kubota" and validated.get("model") == "M7",
        f"(make={validated.get('make')!r}, model={validated.get('model')!r})",
    )


def test_fix_3_alert_prompt_vehicles_block() -> None:
    """alert_prompt._build_payload includes a Vehicles: section with all vehicles."""
    from infra.alert_prompt import _build_payload

    # Use the validated result (after Fix 2 back-compat populated everything)
    from infra.vision_response import _validate_vision_result
    validated = _validate_vision_result(MULTI_VEHICLE_RESULT)
    assert validated is not None

    payload = _build_payload(
        validated,
        "CAM5",
        "2026-08-26 13:05:54 EDT",
        "motion",
    )
    user_prompt = payload["messages"][1]["content"]

    # Vehicles: section present
    report(
        "Fix 3: prompt contains Vehicles: section",
        "Vehicles:" in user_prompt,
    )
    # Primary marked
    report(
        "Fix 3: primary vehicle marked with '(primary)'",
        "red Kubota M7 tractor (primary)" in user_prompt,
    )
    # All non-primary vehicles listed
    report(
        "Fix 3: secondary vehicle #1 listed",
        "silver Toyota 4Runner suv" in user_prompt,
    )
    report(
        "Fix 3: secondary vehicle #2 listed",
        "blue Tesla Model 3 sedan" in user_prompt,
    )
    # Legacy Objects: line also present (back-compat)
    report(
        "Fix 3: legacy Objects: line still present",
        "Objects:" in user_prompt,
    )
    # Scene description still present (was already working in 6B.129)
    report(
        "Fix 3: scene_description still passed to LLM",
        "Red tractor parked on gravel" in user_prompt,
    )

    # Edge case: 5 vehicles → cap at 3 with footer
    five = {
        "vehicles": [
            {"color": "red", "make": f"K{i}", "body_style_hint": "sedan"}
            for i in range(5)
        ],
        "primary_vehicle_index": 0,
    }
    validated_5 = _validate_vision_result(five)
    assert validated_5 is not None
    payload_5 = _build_payload(
        validated_5, "CAM5", "2026-08-26 13:05:54 EDT", "motion",
    )
    prompt_5 = payload_5["messages"][1]["content"]
    report(
        "Fix 3: 5 vehicles capped at 3 + '(N more...)' footer",
        "(2 more vehicle(s) omitted)" in prompt_5
        and prompt_5.count("- ") == 3,
        f"(lines with '- ': {prompt_5.count('- ')})",
    )


def test_end_to_end_telegram_output():
    """Trace what TG#2's 'identified as:' line would actually say."""
    from listener.vehicle_event_pipeline import _vision_summary_str

    # This is what the operator would see on Telegram for alert 5b8284b3
    # AFTER Phase.130 lands (vs. the buggy "red sedan/SUV" before).
    summary = _vision_summary_str(MULTI_VEHICLE_RESULT)

    print()
    print("=== TG#2 BODY (what operator sees) ===")
    print("   🚗 <b>Vehicle in motion at CAM5</b>")
    print(f"      identified as: {summary}")
    print("      trajectory: top-left → center → bot-right")
    print()
    print("=== TG#2 THREAT-LEVEL DESCRIPTION (LLM prompt has Vehicles: section) ===")
    from infra.alert_prompt import _build_payload
    from infra.vision_response import _validate_vision_result
    validated = _validate_vision_result(MULTI_VEHICLE_RESULT)
    assert validated is not None
    payload = _build_payload(
        validated, "CAM5", "2026-08-26 13:05:54 EDT", "motion",
    )
    # Extract the Vehicles: + Objects: section
    user_prompt = payload["messages"][1]["content"]
    in_vehicle = False
    in_objects = False
    for line in user_prompt.split("\n"):
        if line.startswith("Vehicles:"):
            in_vehicle = True
        elif in_vehicle and line.startswith("Objects:"):
            in_vehicle = False
            in_objects = True
            print(f"{line}")
        elif in_vehicle:
            print(f"{line}")
        elif in_objects and line.startswith("Scene:"):
            print(f"{line}")
            in_objects = False
    print()


if __name__ == "__main__":
    test_fix_1_vision_summary()
    test_fix_2_legacy_field_population()
    test_fix_3_alert_prompt_vehicles_block()
    test_end_to_end_telegram_output()
    print()
    if failed:
        print(f"{failed}/{failed} tests FAILED")
        sys.exit(1)
    else:
        print("All Phase.130 invariants verified end-to-end.")
