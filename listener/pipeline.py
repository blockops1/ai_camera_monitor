"""
pipeline.py — Stages 1-13 of the 13-stage linear alert pipeline.

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
    - infra.pipeline_cooldown: should_suppress(), record_hit()
    - infra.vision_analyzer: verify_class(), detail_class()
    - infra.paths: VEHICLE_KNOWN_FILE for candidates
    - telegram_formatter.alert: build_alert_message() for TG#1
    - telegram_formatter.detail: build_detail_message() for TG#2
    - telegram_formatter.match_alert: build_match_message() for TG#3
    - vehicle_matcher: match_vehicle() for vehicle matching
    - animal_matcher: match_animal() for animal matching (stub until US-045d)
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

from infra.alert_artifacts import prepare_alert_artifacts
from infra.gate import GateVerdict, run as run_gate
from infra.paths import PERSON_KNOWN_FILE, VEHICLE_KNOWN_FILE, data_dir_for
from infra.pipeline_cooldown import record_hit, should_suppress
from infra.vision_analyzer import detail_class, verify_class
from animal_matcher.match import match_animal
from person_matcher.match import match_person
from telegram_formatter.alert import build_alert_message
from telegram_formatter.detail import build_detail_message
from telegram_formatter.match_alert import build_match_message, send_match_alert
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


def _load_person_candidates() -> list[dict]:
    """Load known-person candidates from disk. Returns empty list on miss.

    Same wrapper/legacy format as _load_candidates.
    """
    p = Path(PERSON_KNOWN_FILE)
    if not p.is_file():
        return []
    try:
        data = json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return []
    if isinstance(data, dict) and "entries" in data:
        return data["entries"]
    if isinstance(data, list):
        return data
    return []


def run(alert: dict) -> dict:
    """Stages 1-12: extract, frames, gate, drop-if-none, cooldown,
    prepare-artifacts, verify, TG#1, VM2 detail, TG#2, vehicle match, TG#3, record."""
    camera_id = alert.get("camera_id", "unknown")
    alert_id = alert.get("id", camera_id)
    classification = alert.get("classification", "motion")
    camera_label = alert.get("camera_label", camera_id)

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
            "id": alert.get("id", camera_id),
            "status": "dropped",
            "reason": "no_class",
            "classification": "none",
        }

    # Stage 7: cooldown check — after gate + drop-on-none, before cascade1 VM1.
    if should_suppress(camera_id, classification, time.monotonic()):
        log.info(
            f"pipeline: dropped alert_id={alert.get('id', camera_id)} "
            f"camera={camera_id} classification={classification} "
            f"reason=cooldown_active"
        )
        return {
            "id": alert.get("id", camera_id),
            "status": "dropped",
            "reason": "cooldown_active",
            "classification": classification,
        }

    # Stage 7: log that we proceeded past the gate (operator signal).
    log.info(
        f"pipeline: proceeded alert_id={alert.get('id', camera_id)} "
        f"camera={camera_id} classification={classification} "
        f"top_class='{gate_verdict.top_class}' "
        f"top_confidence={gate_verdict.top_confidence}"
    )

    # Stage 8: prepare alert artifacts (crops + composite) via prepare_alert_artifacts.
    output_dir = str(data_dir_for(camera_id, alert_id))
    artifacts = prepare_alert_artifacts(
        gate_verdict=gate_verdict,
        frame_paths=frames,
        output_dir=output_dir,
    )
    alert["artifacts"] = artifacts
    a_p = artifacts.crop_a_path
    b_p = artifacts.crop_b_path

    # Stage 9: verify_class + build TG#1 (reads crop paths from artifacts).
    if a_p is None or b_p is None:
        raise RuntimeError(f"gate produced no crops for alert {alert.get('id')}")
    vm1_result = verify_class(a_p, b_p)

    tg1 = build_alert_message(
        verdict=gate_verdict,
        vm1_result=vm1_result,
        artifacts=artifacts,
        camera_label=camera_label,
        alert=alert,
    )

    # Stage 10: detail_class (VM2) — mode from vm1_result["class"].
    mode = vm1_result.get("class", "vehicle")
    vm2_result = detail_class(mode, a_p, b_p)

    # Stage 11: build TG#2 via detail formatter.
    tg2 = build_detail_message(
        mode,
        vm2_result,
        Path(a_p),
        Path(b_p),
        camera_label=camera_label,
        alert=alert,
    )

    # Stage 12: per-class match (vehicle -> match_vehicle, person -> match_person, animal -> match_animal).
    match_result: dict = {"matched": False}
    if mode == "vehicle":
        candidates = _load_candidates()
        match_result = match_vehicle(vm2_result, candidates)
    elif mode == "person":
        candidates = _load_person_candidates()
        match_result = match_person(vm2_result, candidates)
    elif mode == "animal":
        match_result = match_animal(vm2_result, [])

    # Stage 13: build TG#3 (vehicle + person + animal).
    tg3 = {}
    if mode in ("vehicle", "person", "animal"):
        tg3 = build_match_message(
            match_result,
            vm2_result,
            camera_label=camera_label,
            alert=alert,
        )
        # Send text body + photo (pick_alert_image_path via send_match_alert).
        try:
            send_match_alert(
                bot_token=os.environ["TELEGRAM_BOT_TOKEN"],
                chat_id=os.environ.get("TELEGRAM_HOME_CHAT_ID", ""),
                match_result=match_result,
                vm2_result=vm2_result,
                crop_a_path=a_p,
                crop_b_path=b_p,
                camera_label=camera_label,
                alert=alert,
            )
        except Exception:
            log.exception(
                "pipeline: TG#3 send_match_alert failed for alert %s",
                alert.get("id", "unknown"),
            )

    # Stage 14: record_hit — only on full pipeline success (TG#1+TG#2+TG#3).
    record_hit(camera_id, classification, time.monotonic())

    return {
        "id": alert.get("id", camera_id),
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
