"""
pipeline.py — Stages 1-11 of the 11-stage linear alert pipeline.

STATUS: stable
THREAD SAFETY: single-threaded

INPUTS:
    - alert: dict (required) — webhook alert payload

OUTPUTS:
    - return dict: {status, camera_id, classification, frames, gate,
                     vm1_result, tg1, vm2_result, tg2, match_result, tg3}
PUBLIC API:
    run(alert: dict) -> dict

DOES NOT DO:
    - Send Telegram messages (transport handled by listener)
    - Severity scoring (no severity fields in pipeline)
    - Per-class dispatch (single linear flow; mode dispatch in detail_class)
CALLS INTO:
    - infra.gate: run_gate() for YOLO motion gate
    - infra.pipeline_cooldown: PipelineCooldown.record_hit
    - infra.vision_analyzer: verify_class(), detail_class()
    - infra.paths: VEHICLE_KNOWN_FILE for candidates
    - telegram_formatter.alert: build_alert_message() for TG#1
    - telegram_formatter.detail: build_detail_message() for TG#2
    - telegram_formatter.match_alert: build_match_message() for TG#3
    - vehicle_matcher: match_vehicle() for vehicle matching
"""

from __future__ import annotations

import json
from pathlib import Path

from infra.gate import GateVerdict, run as run_gate
from infra.paths import VEHICLE_KNOWN_FILE
from infra.pipeline_cooldown import PipelineCooldown
from infra.vision_analyzer import detail_class, verify_class
from telegram_formatter.alert import build_alert_message
from telegram_formatter.detail import build_detail_message
from telegram_formatter.match_alert import build_match_message
from vehicle_matcher import match_vehicle


def _gsum(v: GateVerdict) -> dict:
    return {
        "decision": v.decision,
        "class_label": v.class_label,
        "confidence": v.confidence,
        "reason": v.reason,
    }


def _crop_paths(crop_a, crop_b) -> tuple[str, str]:
    a_p, b_p = "/tmp/_ga.png", "/tmp/_gb.png"
    if crop_a is not None:
        crop_a.save(a_p, format="PNG", optimize=True)
    else:
        a_p = _tiny_png()
    if crop_b is not None:
        crop_b.save(b_p, format="PNG", optimize=True)
    else:
        b_p = _tiny_png()
    return a_p, b_p


def _tiny_png() -> str:
    p = "/tmp/_empty.png"
    _P = (
        b"\x89PNG\r\n\x1a\n"
        b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
        b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
        b"\x00\x00\x00\rIDATx\x9cc\xfc\xff\xff?\x00\x05\xfe\x02"
        b"\xfeA\xb6\x95"
        b"\x00\x00\x00\x00IEND\xaeB`\x82"
    )
    Path(p).write_bytes(_P)
    return p


def _load_candidates() -> list[dict]:
    """Load known-vehicle candidates from disk. Returns empty list on miss.

    Supports two formats:
    - Wrapper format: {"_anonymization_note": "...", "entries": [...]} (current US-018e)
    - Legacy format: [...]  (pre-US-018e plain list)
    """
    p = Path(VEHICLE_KNOWN_FILE)
    if not p.is_file():
        return []
    try:
        data = json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return []
    # Unwrap if format is {entries: [...]}
    if isinstance(data, dict) and "entries" in data:
        return data["entries"]
    if isinstance(data, list):
        return data
    return []


def run(alert: dict) -> dict:
    """Stages 1-11: extract, cooldown, frames, gate, record, verify, TG#1,
    VM2 detail, TG#2, vehicle match, TG#3."""
    camera_id = alert.get("camera_id", "unknown")
    classification = alert.get("classification", "motion")
    camera_label = alert.get("camera_label", camera_id)

    # Stage 2: cooldown check.
    cooldown = PipelineCooldown()
    if cooldown.should_suppress(camera_id, classification):
        return {
            "status": "suppressed",
            "camera_id": camera_id,
            "classification": classification,
        }

    # Stage 3: load 4 frames.
    frames = list(alert.get("frames", []))
    if not frames:
        raise RuntimeError(
            f"no frames captured from camera {camera_id}, refusing to process alert {alert.get('id')}"
        )

    # Stage 4: run YOLO gate.
    gate_verdict = run_gate(
        frame_paths=frames,
        camera_name=camera_id,
        alert_id=alert.get("id", camera_id),
        output_dir="/tmp",
    )

    # Stage 5: if gate suppresses, drop.
    if gate_verdict.decision == "suppress":
        return {
            "status": "dropped",
            "camera_id": camera_id,
            "classification": classification,
            "reason": gate_verdict.reason,
            "gate": _gsum(gate_verdict),
        }

    # Stage 6: record_hit.
    cooldown.record_hit(camera_id, classification)

    # Stage 7: verify_class + build TG#1.
    a_p, b_p = _crop_paths(gate_verdict.crop_a, gate_verdict.crop_b)
    vm1_result = verify_class(a_p, b_p)

    if gate_verdict.pairwise_diff_path is None:
        raise RuntimeError(
            f"gate produced no pairwise_diff for alert {alert.get('id')}"
        )
    diff = gate_verdict.pairwise_diff_path
    tg1 = build_alert_message(
        verdict=gate_verdict,
        vm1_result=vm1_result,
        frames=frames,
        diff_image=Path(diff),
        camera_label=camera_label,
        alert=alert,
    )

    # Stage 8: detail_class (VM2) — mode from vm1_result["class"].
    mode = vm1_result.get("class", "vehicle")
    vm2_result = detail_class(mode, a_p, b_p)

    # Stage 9: build TG#2 via detail formatter.
    tg2 = build_detail_message(
        mode, vm2_result, Path(a_p), Path(b_p), camera_label=camera_label
    )

    # Stage 10: vehicle match (vehicle only).
    match_result: dict = {"matched": False}
    if mode == "vehicle":
        candidates = _load_candidates()
        match_result = match_vehicle(vm2_result, candidates)

    # Stage 11: build TG#3 (vehicle only).
    tg3 = {}
    if mode == "vehicle":
        tg3 = build_match_message(match_result, vm2_result)

    return {
        "status": "ok",
        "camera_id": camera_id,
        "classification": classification,
        "frames": frames,
        "gate": _gsum(gate_verdict),
        "vm1_result": vm1_result,
        "tg1": tg1,
        "vm2_result": vm2_result,
        "tg2": tg2,
        "match_result": match_result,
        "tg3": tg3,
    }
