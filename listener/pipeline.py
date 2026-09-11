"""
pipeline.py — Stages 1-12 of the 12-stage linear alert pipeline.

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
    - infra.alert_artifacts: prepare_alert_artifacts() for crops + composite
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
import logging
from pathlib import Path

from infra.alert_artifacts import prepare_alert_artifacts
from infra.gate import GateVerdict, run as run_gate
from infra.paths import VEHICLE_KNOWN_FILE, data_dir_for
from infra.pipeline_cooldown import PipelineCooldown
from infra.vision_analyzer import detail_class, verify_class
from telegram_formatter.alert import build_alert_message
from telegram_formatter.detail import build_detail_message
from telegram_formatter.match_alert import build_match_message
from vehicle_matcher import match_vehicle

log = logging.getLogger(__name__)


def _gsum(v: GateVerdict) -> dict:
    return {
        "classification": v.classification,
        "class_label": v.class_label,
        "confidence": v.confidence,
        "reason": v.reason,
    }


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
    """Stages 1-12: extract, cooldown, frames, gate, drop-if-suppressed,
    record, prepare-artifacts, verify, TG#1, VM2 detail, TG#2, vehicle match, TG#3."""
    camera_id = alert.get("camera_id", "unknown")
    alert_id = alert.get("id", camera_id)
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
        output_dir=str(data_dir_for(camera_id, alert.get("id", camera_id))),
    )

    # Stage 5: if gate returns classification='none', drop immediately.
    # NO cooldown check, NO record_hit, NO cascade call, NO Telegram dispatch.
    if gate_verdict.is_none():
        log.info(
            f"pipeline: dropped alert_id={alert.get('id', camera_id)} "
            f"camera={camera_id} classification=none "
            f"top_class='{gate_verdict.top_class}' "
            f"top_confidence={gate_verdict.top_confidence} "
            f"reason=no_class"
        )
        return {
            "status": "dropped",
            "reason": "no_class",
            "classification": "none",
        }

    # Stage 6: record_hit.
    cooldown.record_hit(camera_id, classification)

    # Stage 7: prepare alert artifacts (crops + composite) via prepare_alert_artifacts.
    output_dir = str(data_dir_for(camera_id, alert_id))
    artifacts = prepare_alert_artifacts(
        gate_verdict=gate_verdict,
        frame_paths=frames,
        output_dir=output_dir,
    )
    alert["artifacts"] = artifacts
    a_p = artifacts.crop_a_path
    b_p = artifacts.crop_b_path

    # Stage 8: verify_class + build TG#1 (reads crop paths from artifacts).
    if a_p is None or b_p is None:
        raise RuntimeError(
            f"gate produced no crops for alert {alert.get('id')}"
        )
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

    # Stage 9: detail_class (VM2) — mode from vm1_result["class"].
    mode = vm1_result.get("class", "vehicle")
    vm2_result = detail_class(mode, a_p, b_p)

    # Stage 10: build TG#2 via detail formatter.
    tg2 = build_detail_message(
        mode, vm2_result, Path(a_p), Path(b_p), camera_label=camera_label
    )

    # Stage 11: vehicle match (vehicle only).
    match_result: dict = {"matched": False}
    if mode == "vehicle":
        candidates = _load_candidates()
        match_result = match_vehicle(vm2_result, candidates)

    # Stage 12: build TG#3 (vehicle only).
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
