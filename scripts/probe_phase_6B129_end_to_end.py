"""Phase.129 probe — replay alert 5b8284b3 (the red tractor alert from
2026-08-26 13:05:54 EDT) end-to-end through both layers:

  Layer 1 — event promotion (md → vehicle when gate says vehicle)
  Layer 2 — multi-vehicle crop vision (vehicles[] with full per-vehicle
            identification; backward-compat population at top level)

Verifies:
  1. Layer 1: when event=md and gate verdict says vehicle, AlertContext
     gets event_type="vehicle" and is_vehicle_event=True
  2. Layer 2: a multi-vehicle vision result (tractor + SUV) parses
     correctly and populates top-level fields via backward-compat
  3. Layer 3: _extract_signature picks vehicles[primary_vehicle_index]
     and returns the dominant vehicle's fields
"""
from __future__ import annotations

import os
import sys

# Force the crop prompt + multi-vehicle schema path
os.environ.setdefault("PIPELINE_USES_GATE_CROPS", "1")

sys.path.insert(0, '<install-path>/ai_camera_monitor')
os.chdir('<install-path>/ai_camera_monitor')

failed = 0


def report(name: str, ok: bool, detail: str = "") -> None:
    global failed
    status = "[OK]  " if ok else "[FAIL]"
    print(f"{status} {name} {detail}")
    if not ok:
        failed += 1


# === Layer 1: event promotion logic =======================================

def test_layer1_md_to_vehicle_promotion() -> None:
    """When event=md and gate verdict says vehicle, AlertContext gets
    event_type='vehicle' and is_vehicle_event=True."""
    # Build a mock listener._process_alert flow
    from unittest.mock import patch

    from listener.listener import _process_alert
    from listener.motion_gate_pipeline import GateVerdict

    gate_verdict = GateVerdict(
        decision="vehicle",
        class_label="car",
        confidence=0.82,  # matches the actual 5b8284b3 gate run
        crop_a_path="<install-path>/ai_camera_monitor/data/frames/5b8284b3-1664-4fd3-9976-d5d4bb2da352/frame_003_crop896_579_683x182.jpg",
        crop_b_path="<install-path>/ai_camera_monitor/data/frames/5b8284b3-1664-4fd3-9976-d5d4bb2da352/frame_004_crop782_565_582x185.jpg",
        bbox_a=(896, 579, 683, 182),
        bbox_b=(782, 565, 582, 185),
        reason="high_conf_vehicle",
    )

    captured: dict = {}

    def fake_vehicle_process_alert(ctx):
        captured["event_type"] = ctx.event_type
        captured["is_vehicle_event"] = ctx.is_vehicle_event
        return {"telegram_sent": False, "alert_id": ctx.alert_id}

    import listener.listener as L

    with patch.object(L, "_process_person_alert"), \
         patch(
             "listener._motion_gate_dispatch.maybe_run_motion_gate",
             return_value=gate_verdict,
         ):
        try:
            with patch("vehicle_event_pipeline.process_alert", side_effect=fake_vehicle_process_alert):
                _process_alert(
                    alert_id="5b8284b3-probe",
                    camera_name="CAM5",
                    timestamp="2026-08-26 13:05:54 EDT",
                    event="md",  # the original event type from Reolink
                    rtsp_url="rtsp://<CAM_IP_REDACTED>:554/h264Preview_01_main",
                )
        except ImportError:
            with patch("listener.vehicle_event_pipeline.process_alert", side_effect=fake_vehicle_process_alert):
                _process_alert(
                    alert_id="5b8284b3-probe",
                    camera_name="CAM5",
                    timestamp="2026-08-26 13:05:54 EDT",
                    event="md",
                    rtsp_url="rtsp://<CAM_IP_REDACTED>:554/h264Preview_01_main",
                )

    report(
        "Layer 1: event=md promoted to vehicle",
        captured.get("event_type") == "vehicle",
        f"(event_type={captured.get('event_type')!r})",
    )
    report(
        "Layer 1: is_vehicle_event=True after promotion",
        captured.get("is_vehicle_event") is True,
        f"(is_vehicle_event={captured.get('is_vehicle_event')!r})",
    )


# === Layer 2: multi-vehicle schema + parser =================================

def test_layer2_multi_vehicle_schema() -> None:
    """The new schema accepts vehicles[] and has primary_vehicle_index."""
    from infra.prompt_templates import VEHICLE_CROP_SCHEMA_JSON
    props = VEHICLE_CROP_SCHEMA_JSON["properties"]
    report(
        "Layer 2: schema has vehicles[]",
        "vehicles" in props and props["vehicles"]["type"] == "array",
    )
    report(
        "Layer 2: schema has primary_vehicle_index",
        "primary_vehicle_index" in props,
    )
    report(
        "Layer 2: schema has scene_description",
        "scene_description" in props,
    )
    required = set(VEHICLE_CROP_SCHEMA_JSON["required"])
    report(
        "Layer 2: vehicles is required",
        "vehicles" in required,
    )
    report(
        "Layer 2: backward-compat top-level fields still required",
        all(f in required for f in ["color", "make", "model"]),
    )


def test_layer2_multi_vehicle_parser_populates_legacy_fields() -> None:
    """A multi-vehicle vision response correctly populates top-level
    fields via backward-compat (so legacy consumers keep working)."""
    from infra.vision_response import _validate_vision_result

    raw = {
        "vehicles": [
            {
                "color": "red", "body_style_hint": "tractor",
                "make": "Kubota", "model": "M7",
                "vehicle_features": {"wheel_style": "tractor_ag"},
                "description": "Red tractor with front loader",
                "confidence": 0.82,
            },
            {
                "color": "silver", "body_style_hint": "suv",
                "make": "Toyota", "model": "4Runner",
                "vehicle_features": {},
                "description": "Silver 4Runner",
                "confidence": 0.88,
            },
        ],
        "primary_vehicle_index": 0,
    }
    result = _validate_vision_result(raw)
    assert result is not None
    report(
        "Layer 2: backward-compat color=red (primary = tractor)",
        result.get("color") == "red",
        f"(color={result.get('color')!r})",
    )
    report(
        "Layer 2: backward-compat make=Kubota",
        result.get("make") == "Kubota",
        f"(make={result.get('make')!r})",
    )
    report(
        "Layer 2: backward-compat type=tractor (slim match alias)",
        result.get("type") == "tractor",
        f"(type={result.get('type')!r})",
    )
    report(
        "Layer 2: vehicles[] preserved with 2 entries",
        len(result.get("vehicles", [])) == 2,
        f"(len={len(result.get('vehicles', []))})",
    )


# === Layer 3: _extract_signature consumes multi-vehicle schema ============

def test_layer3_extract_signature_uses_primary_vehicle() -> None:
    """_extract_signature reads vehicles[primary_vehicle_index]."""
    from listener.vehicle_event_pipeline import AlertContext, _extract_signature

    ctx = AlertContext(
        alert_id="probe", camera_name="CAM5",
        timestamp="2026-08-26 13:05:54", event_type="vehicle",
        rtsp_url="", output_dir="/tmp",
        is_vehicle_event=True, known_vehicles=[],
        bot_token="t", chat_id="c", api_url="http://x",
        gatekeeper_cameras=frozenset({"CAM5"}),
    )
    ctx.vision_result = {
        "vehicles": [
            {"color": "red", "body_style_hint": "tractor",
             "make": "Kubota", "model": "M7", "vehicle_features": {}},
            {"color": "silver", "body_style_hint": "suv",
             "make": "Toyota", "model": "4Runner", "vehicle_features": {}},
        ],
        "primary_vehicle_index": 0,
    }
    sig = _extract_signature(ctx)
    report(
        "Layer 3: sig.color = red (primary = tractor)",
        sig.get("color") == "red",
        f"(sig.color={sig.get('color')!r})",
    )
    report(
        "Layer 3: sig.make = Kubota",
        sig.get("make") == "Kubota",
        f"(sig.make={sig.get('make')!r})",
    )
    report(
        "Layer 3: sig.type = tractor (body_style_hint alias)",
        sig.get("type") == "tractor",
        f"(sig.type={sig.get('type')!r})",
    )


# === Run all ===============================================================

if __name__ == "__main__":
    test_layer1_md_to_vehicle_promotion()
    test_layer2_multi_vehicle_schema()
    test_layer2_multi_vehicle_parser_populates_legacy_fields()
    test_layer3_extract_signature_uses_primary_vehicle()
    print()
    if failed:
        print(f"{failed}/{failed} tests FAILED")
        sys.exit(1)
    else:
        print("All Phase.129 invariants verified end-to-end.")
