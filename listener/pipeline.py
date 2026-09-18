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
    stage_load_frames(alert) -> list[str]
    stage_cooldown_check(camera_id, classification) -> dict
    stage_prepare_artifacts(gate_verdict, camera_id, alert_id) -> dict
    stage_verify_class_vm1(crop_a_path, crop_b_path) -> dict
    stage_build_tg1(gate_verdict, vm1_result, ..., alert) -> dict
    stage_detail_class_vm2(mode, crop_a_path, crop_b_path) -> dict
    stage_build_tg2(mode, vm2_result, ..., alert) -> dict
    stage_perform_match(mode, vm2_result) -> dict
    stage_build_tg3(mode, match_result, ..., alert) -> dict

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
    - infra.animal_embedder: embed_image() for MegaDescriptor Tier-2 embeddings
      (US-045d wiring: embed_image → cosine vs enrolled animals)
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from animal_matcher.match import match_animal
from infra.alert_artifacts import prepare_alert_artifacts
from infra.gate import GateVerdict
from infra.paths import PERSON_KNOWN_FILE, VEHICLE_KNOWN_FILE, data_dir_for
from infra.pipeline_cooldown import should_suppress
from infra.vision_analyzer import detail_class, verify_class
from person_matcher.match import match_person
from telegram_formatter.alert import build_alert_message
from telegram_formatter.detail import build_detail_message
from telegram_formatter.match_alert import build_match_message
from vehicle_matcher import match_vehicle

log = logging.getLogger(__name__)


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


# ---------------------------------------------------------------------------
# Stage functions (1-10)
# ---------------------------------------------------------------------------

def stage_load_frames(alert: dict) -> list[str]:
    """Stage 3: load frames from the alert payload.

    Returns the list of frame paths. Raises RuntimeError if empty.
    """
    camera_id = alert.get("camera_id", "unknown")
    frames = list(alert.get("frames", []))
    if not frames:
        raise RuntimeError(
            f"no frames captured from camera {camera_id}, refusing to process alert {alert.get('id')}"
        )
    return frames


def stage_cooldown_check(
    camera_id: str,
    classification: str,
) -> dict:
    """Stage 7a: cooldown check after gate, before cascade1 VM1.

    Returns a 'dropped' dict if suppressed, else a 'proceed' marker.
    """
    if should_suppress(camera_id, classification, time.monotonic()):
        log.info(
            f"pipeline: dropped alert_id={camera_id} "
            f"camera={camera_id} classification={classification} "
            f"reason=cooldown_active"
        )
        return {
            "status": "dropped",
            "reason": "cooldown_active",
            "classification": classification,
        }
    return {"status": "proceed"}


def stage_prepare_artifacts(
    gate_verdict: GateVerdict,
    camera_id: str,
    alert_id: str,
) -> dict:
    """Stage 8: prepare alert artifacts (crops + composite).

    Returns a dict with crop_a_path, crop_b_path, composite_path keys.
    """
    output_dir = str(data_dir_for(camera_id, alert_id))
    artifacts = prepare_alert_artifacts(
        gate_verdict=gate_verdict,
        output_dir=output_dir,
    )
    return {
        "crop_a_path": artifacts.crop_a_path,
        "crop_b_path": artifacts.crop_b_path,
        "composite_path": artifacts.composite_path,
    }


def stage_verify_class_vm1(
    crop_a_path: str,
    crop_b_path: str,
) -> dict:
    """Stage 9: verify_class on crop paths.

    Returns the VM1 result dict.
    """
    return verify_class(crop_a_path, crop_b_path)


def stage_build_tg1(
    gate_verdict: GateVerdict,
    vm1_result: dict,
    crop_a_path: str,
    crop_b_path: str,
    composite_path: str | None,
    camera_label: str,
    alert: dict,
) -> dict:
    """Stage 10: build TG#1 alert message.

    Reconstructs an AlertArtifacts object from the path pieces and
    passes everything to the TG#1 formatter.
    """
    from infra.alert_artifacts import AlertArtifacts

    artifacts = AlertArtifacts(
        crop_a_path=crop_a_path,
        crop_b_path=crop_b_path,
        composite_path=composite_path,
    )
    return build_alert_message(
        verdict=gate_verdict,
        vm1_result=vm1_result,
        artifacts=artifacts,
        camera_label=camera_label,
        alert=alert,
    )


def stage_detail_class_vm2(
    mode: str,
    crop_a_path: str,
    crop_b_path: str,
) -> dict:
    """Stage 10b: detail_class (VM2) — mode from vm1_result["class"].

    Returns the VM2 result dict.
    """
    return detail_class(mode, crop_a_path, crop_b_path)


def stage_build_tg2(
    mode: str,
    vm2_result: dict,
    crop_a_path: str,
    crop_b_path: str,
    camera_label: str,
    alert: dict,
) -> dict:
    """Stage 11: build TG#2 detail message."""
    return build_detail_message(
        mode,
        vm2_result,
        Path(crop_a_path),
        Path(crop_b_path),
        camera_label=camera_label,
        alert=alert,
    )


def stage_perform_match(
    mode: str,
    vm2_result: dict,
) -> dict:
    """Stage 12: per-class vehicle/person/animal match.

    Returns the match result dict.
    """
    if mode == "vehicle":
        candidates = _load_candidates()
        return match_vehicle(vm2_result, candidates)
    elif mode == "person":
        candidates = _load_person_candidates()
        return match_person(vm2_result, candidates)
    elif mode == "animal":
        return match_animal(vm2_result, [])
    return {"matched": False}


def stage_build_tg3(
    mode: str,
    match_result: dict,
    vm2_result: dict,
    crop_a_path: str,
    crop_b_path: str,
    camera_label: str,
    alert: dict,
) -> dict:
    """Stage 13: build TG#3 match message.

    Returns the tg3 dict. The send_match_alert responsibility has been
    moved to daemon.py (per-stage dispatch, US-050b).
    """
    if mode not in ("vehicle", "person", "animal"):
        return {}

    return build_match_message(
        match_result,
        vm2_result,
        camera_label=camera_label,
        alert=alert,
    )
