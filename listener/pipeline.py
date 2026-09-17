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
from infra.gate import GateVerdict, run as run_gate
from infra.paths import PERSON_KNOWN_FILE, VEHICLE_KNOWN_FILE, data_dir_for
from infra.pipeline_cooldown import should_suppress
from infra.vision_analyzer import detail_class, verify_class
from person_matcher.match import match_person
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


def stage_gate(
    frames: list[str],
    camera_id: str,
    alert_id: str,
) -> dict:
    """Stage 4: run YOLO gate on frames.

    Returns a dict with two keys:
      - 'gate_verdict': the raw GateVerdict object (for downstream stages)
      - 'gsum': the _gsum serialisable dict (for the run() return value)
      - 'status': 'dropped' or 'ok'
      - 'reason': present when status is 'dropped'
    """
    gate_verdict = run_gate(
        frame_paths=frames,
        camera_name=camera_id,
        alert_id=alert_id,
        output_dir=str(data_dir_for(camera_id, alert_id)),
    )

    if gate_verdict.is_none():
        log.info(
            f"pipeline: dropped alert_id={alert_id} "
            f"camera={camera_id} classification=none "
            f"top_class='{gate_verdict.top_class}' "
            f"top_confidence={gate_verdict.top_confidence} "
            f"reason=no_class"
        )
        return {
            "status": "dropped",
            "reason": "no_class",
            "classification": "none",
            "gate_verdict": gate_verdict,
            "gsum": _gsum(gate_verdict),
        }

    return {
        "status": "ok",
        "gate_verdict": gate_verdict,
        "gsum": _gsum(gate_verdict),
    }


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
    frames: list[str],
    camera_id: str,
    alert_id: str,
) -> dict:
    """Stage 8: prepare alert artifacts (crops + composite).

    Returns a dict with crop_a_path, crop_b_path, composite_path,
    full_frame_path keys.
    """
    output_dir = str(data_dir_for(camera_id, alert_id))
    artifacts = prepare_alert_artifacts(
        gate_verdict=gate_verdict,
        frame_paths=frames,
        output_dir=output_dir,
    )
    return {
        "crop_a_path": artifacts.crop_a_path,
        "crop_b_path": artifacts.crop_b_path,
        "composite_path": artifacts.composite_path,
        "full_frame_path": artifacts.full_frame_path,
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
    full_frame_path: str,
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
        full_frame_path=full_frame_path,
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


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def run(alert: dict) -> dict:
    """Stages 1-12: orchestrate the 10 named stage functions.

    Each stage function is a thin, testable unit. This orchestrator
    wires them together in the original pipeline order and returns
    the same dict shape as before.
    """
    camera_id = alert.get("camera_id", "unknown")
    alert_id = alert.get("id", camera_id)
    classification = alert.get("classification", "motion")
    camera_label = alert.get("camera_label", camera_id)

    # Stage 3: load frames.
    frames = stage_load_frames(alert)

    # Stage 4: YOLO gate.
    gate_result = stage_gate(frames, camera_id, alert_id)
    if gate_result["status"] == "dropped":
        return {
            "id": alert_id,
            "status": "dropped",
            "reason": gate_result["reason"],
            "classification": "none",
            "gate": gate_result["gsum"],
        }

    # Stage 7a: cooldown check.
    cooldown_result = stage_cooldown_check(camera_id, classification)
    if cooldown_result["status"] == "dropped":
        return {
            "id": alert_id,
            "status": "dropped",
            "reason": cooldown_result["reason"],
            "classification": classification,
        }

    # Stage 7b: log proceeded past gate (operator signal).
    gsum = gate_result["gsum"]
    log.info(
        f"pipeline: proceeded alert_id={alert_id} "
        f"camera={camera_id} classification={classification} "
        f"top_class='{gsum['class_label']}' "
        f"top_confidence={gsum['confidence']}"
    )

    # Stage 8: prepare artifacts.
    art = stage_prepare_artifacts(
        gate_result["gate_verdict"], frames, camera_id, alert_id
    )
    a_p = art["crop_a_path"]
    b_p = art["crop_b_path"]

    # Guard: gate must have produced crops.
    if a_p is None or b_p is None:
        raise RuntimeError(f"gate produced no crops for alert {alert.get('id')}")

    # Stage 9: verify_class VM1.
    vm1_result = stage_verify_class_vm1(a_p, b_p)

    # Stage 10: build TG#1.
    tg1 = stage_build_tg1(
        gate_result["gate_verdict"], vm1_result, a_p, b_p,
        art["composite_path"], art["full_frame_path"],
        camera_label, alert,
    )

    # Stage 10b: detail_class VM2.
    mode = vm1_result.get("class", "vehicle")
    vm2_result = stage_detail_class_vm2(mode, a_p, b_p)

    # Stage 11: build TG#2.
    tg2 = stage_build_tg2(mode, vm2_result, a_p, b_p, camera_label, alert)

    # Stage 12: per-class match.
    match_result = stage_perform_match(mode, vm2_result)

    # Stage 13: build + send TG#3.
    tg3 = stage_build_tg3(
        mode, match_result, vm2_result,
        a_p, b_p, camera_label, alert,
    )

    return {
        "id": alert_id,
        "status": "ok",
        "camera_id": camera_id,
        "classification": classification,
        "frames": frames,
        "gate": gate_result["gsum"],
        "vm1_result": vm1_result,
        "tg1": tg1,
        "vm2_result": vm2_result,
        "tg2": tg2,
        "match_result": match_result,
        "tg3": tg3,
    }
