"""
pipeline.py — Stages 1-7 of the 11-stage linear alert pipeline.

STATUS: provisional (stages 1-7; stages 8-11 in US-008c)
THREAD SAFETY: single-threaded

INPUTS:
    - alert: dict (required) — webhook alert payload

OUTPUTS:
    - return dict: {status, camera_id, classification, frames, gate, tg1}
PUBLIC API:
    run(alert: dict) -> dict

DOES NOT DO:
    - Vision model 2, TG#2, matcher, TG#3
    - Severity scoring (no severity fields in pipeline)
    - Per-class dispatch (single linear flow)
CALLS INTO:
    - infra.gate: run_gate() for YOLO motion gate
    - infra.pipeline_cooldown: PipelineCooldown.record_hit
    - infra.vision_analyzer: verify_class() for VM1 classification
    - telegram_formatter.alert: build_alert_message() for TG#1
"""

from __future__ import annotations

from pathlib import Path

from infra.gate import GateVerdict, run as run_gate
from infra.pipeline_cooldown import PipelineCooldown
from infra.vision_analyzer import verify_class
from telegram_formatter.alert import build_alert_message

def _gsum(v: GateVerdict) -> dict:
    return {"decision": v.decision, "class_label": v.class_label,
            "confidence": v.confidence, "reason": v.reason}


def _crop_paths(crop_a, crop_b) -> tuple[str, str]:
    a_p, b_p = "/tmp/_ga.jpg", "/tmp/_gb.jpg"
    if crop_a is not None:
        crop_a.save(a_p, format="JPEG")
    else:
        a_p = _tiny_jpeg()
    if crop_b is not None:
        crop_b.save(b_p, format="JPEG")
    else:
        b_p = _tiny_jpeg()
    return a_p, b_p

def _tiny_jpeg() -> str:
    p = "/tmp/_empty.jpg"
    _J = (
        b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01"
        b"\x00\x00\x01\x00\x01\x00\x00\xff\xc0\x00"
        b"\x09\x01\x01\x01\x01\x01\x11\x00\xff\xdb"
        b"\x00\x43\x00" + b"\x01" * 64 + b"\xff\xda"
        b"\x00\x08\x01\x01\x00\x00\x3f\x00\x00\x7f"
        b"\xff\xff\xd9"
    )
    Path(p).write_bytes(_J)
    return p

def run(alert: dict) -> dict:
    """Stages 1-7: extract, cooldown, frames, gate, record, verify, TG#1."""
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

    # Stage 4: run YOLO gate.
    gate_verdict = run_gate(
        frame_paths=frames, camera_name=camera_id,
        alert_id=alert.get("id", camera_id), output_dir="/tmp",
    )

    # Stage 5: if gate suppresses, drop.
    if gate_verdict.decision == "suppress":
        return {
            "status": "dropped", "camera_id": camera_id,
            "classification": classification,
            "reason": gate_verdict.reason,
            "gate": _gsum(gate_verdict),
        }

    # Stage 6: record_hit.
    cooldown.record_hit(camera_id, classification)

    # Stage 7: verify_class + build TG#1.
    a_p, b_p = _crop_paths(gate_verdict.crop_a, gate_verdict.crop_b)
    vm1_result = verify_class(a_p, b_p)

    diff = gate_verdict.pairwise_diff_path or (
        frames[-1] if frames else "/tmp/diff.jpg"
    )
    tg1 = build_alert_message(
        verdict=gate_verdict, vm1_result=vm1_result,
        frames=frames, diff_image=Path(diff),
        camera_label=camera_label,
    )

    return {
        "status": "ok", "camera_id": camera_id,
        "classification": classification, "frames": frames,
        "gate": _gsum(gate_verdict),
        "vm1_result": vm1_result, "tg1": tg1,
    }
