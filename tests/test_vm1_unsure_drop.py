"""Regression tests for VM1=unsure short-circuit (operator 2026-09-20 ~20:30 EDT).

Precedent: alert b1d400da-df87-4102-8d04-bdd7a9c16a1d on 2026-09-20. YOLO cleared
it (cat 0.79). VM1 returned {"class": "unsure"} because there was no actual
animal in the frame (grass-blade motion misread as a cat). The pipeline still
built TG#1+TG#2+TG#3 with that unsure answer, sending a useless "Mode: unsure,
Class: unsure, subclass: ambiguous" message to Telegram.

After this patch: if VM1 returns class=unsure, _run_pipeline drops the alert
before TG#1/2/3 build. No Telegram traffic. Log line `pipeline: dropped ...
reason=vm1_unsure` records what happened.
"""
from __future__ import annotations

from unittest.mock import patch


# ── Stage-result constants ──────────────────────────────────────────────

_UNSURE_VM1_RESULT = {
    "class": "unsure",
    "subclass": "ambiguous",
    "confidence": "unlikely",
    "count": None,
    "face_visible": None,
    "notes": None,
}

_VEHICLE_VM1_RESULT = {
    "class": "vehicle",
    "subclass": "sedan",
    "confidence": "likely",
    "count": 1,
    "face_visible": False,
    "notes": None,
}


# ── Helpers ─────────────────────────────────────────────────────────────

def _make_alert():
    return {
        "id": "test-vm1-unsure-001",
        "camera_id": "OUTSIDE_BACK_SOLAR",
        "camera_label": "Outside Back Solar",
        "classification": "md",
        "frames": [
            "/tmp/frame_001.png",
            "/tmp/frame_002.png",
            "/tmp/frame_003.png",
            "/tmp/frame_004.png",
        ],
    }


def _make_gate_ok():
    """Stand-in for _stage_gate's `{"status": "ok", ...}` return."""
    gsum = {
        "classification": "pass",
        "class_label": "cat",
        "confidence": 0.79,
        "reason": "animal_classified",
    }
    return {
        "status": "ok",
        "gate_verdict": "MOCK_VERDICT",
        "gsum": gsum,
    }


def _make_artifacts():
    return {
        "crop_a_path": "/tmp/crop_a.png",
        "crop_b_path": "/tmp/crop_b.png",
        "composite_path": "/tmp/composite.png",
    }


# ── Tests ───────────────────────────────────────────────────────────────


def test_run_pipeline_drops_when_vm1_returns_unsure():
    """If VM1 returns class=unsure, _run_pipeline returns status=dropped.

    Regression: before the patch, _run_pipeline proceeded to TG#1/2/3 build
    with the unsure answer and the dispatcher sent a vague alert to Telegram.
    """
    from listener.daemon import _run_pipeline

    alert = _make_alert()

    with patch("listener.daemon.stage_load_frames", return_value=alert["frames"]), \
         patch("listener.daemon._stage_gate", return_value=_make_gate_ok()), \
         patch("listener.daemon.stage_cooldown_check",
               return_value={"status": "ok"}), \
         patch("listener.daemon.stage_prepare_artifacts", return_value=_make_artifacts()), \
         patch("listener.daemon.stage_verify_class_vm1",
               return_value=_UNSURE_VM1_RESULT), \
         patch("listener.daemon.stage_build_tg1") as mock_tg1, \
         patch("listener.daemon.stage_detail_class_vm2") as mock_vm2, \
         patch("listener.daemon.stage_build_tg2") as mock_tg2, \
         patch("listener.daemon.stage_perform_match") as mock_match, \
         patch("listener.daemon.stage_build_tg3") as mock_tg3:

        result = _run_pipeline(alert)

    # Drop semantics:
    assert result["status"] == "dropped", f"expected dropped, got {result.get('status')}"
    assert result["reason"] == "vm1_unsure"
    assert result["id"] == alert["id"]

    # VM1 output preserved for diagnostics:
    assert result["vm1_result"] == _UNSURE_VM1_RESULT

    # Critical: nothing TG-related was built.
    mock_tg1.assert_not_called()
    mock_vm2.assert_not_called()
    mock_tg2.assert_not_called()
    mock_match.assert_not_called()
    mock_tg3.assert_not_called()


def test_run_pipeline_proceeds_when_vm1_returns_vehicle():
    """When VM1 returns class=vehicle (high confidence), TG#1 IS built."""
    from listener.daemon import _run_pipeline

    alert = _make_alert()
    fake_tg1 = {"caption": "Vehicle alert", "photos": ["/tmp/crop_a.png"]}
    fake_tg2 = {"caption": "Sedan detected", "photos": ["/tmp/crop_b.png"]}
    fake_tg3 = {"caption": "Recognized: TEST123", "photos": []}
    fake_match = {"matched": False, "license_plate": None}

    with patch("listener.daemon.stage_load_frames", return_value=alert["frames"]), \
         patch("listener.daemon._stage_gate", return_value=_make_gate_ok()), \
         patch("listener.daemon.stage_cooldown_check",
               return_value={"status": "ok"}), \
         patch("listener.daemon.stage_prepare_artifacts", return_value=_make_artifacts()), \
         patch("listener.daemon.stage_verify_class_vm1",
               return_value=_VEHICLE_VM1_RESULT), \
         patch("listener.daemon.stage_build_tg1", return_value=fake_tg1), \
         patch("listener.daemon.stage_detail_class_vm2",
               return_value={"class_confirmed": "vehicle"}), \
         patch("listener.daemon.stage_build_tg2", return_value=fake_tg2), \
         patch("listener.daemon.stage_perform_match", return_value=fake_match), \
         patch("listener.daemon.stage_build_tg3", return_value=fake_tg3):

        result = _run_pipeline(alert)

    assert result["status"] == "ok"
    assert result["tg1"] is fake_tg1
    assert result["tg2"] is fake_tg2
    assert result["tg3"] is fake_tg3
    assert result["vm1_result"] == _VEHICLE_VM1_RESULT


def test_run_pipeline_proceeds_when_vm1_returns_person():
    """VM1 returns class=person — TG#1 must still be built (mode=person)."""
    from listener.daemon import _run_pipeline

    alert = _make_alert()
    person_vm1 = dict(_VEHICLE_VM1_RESULT, **{"class": "person", "subclass": "adult"})
    fake_tg1 = {"caption": "Person alert", "photos": []}
    fake_tg2 = {"caption": "Adult detected", "photos": []}
    fake_tg3 = {"caption": "Person unrecognized", "photos": []}
    fake_match = {"matched": False}

    with patch("listener.daemon.stage_load_frames", return_value=alert["frames"]), \
         patch("listener.daemon._stage_gate", return_value=_make_gate_ok()), \
         patch("listener.daemon.stage_cooldown_check",
               return_value={"status": "ok"}), \
         patch("listener.daemon.stage_prepare_artifacts", return_value=_make_artifacts()), \
         patch("listener.daemon.stage_verify_class_vm1", return_value=person_vm1), \
         patch("listener.daemon.stage_build_tg1", return_value=fake_tg1), \
         patch("listener.daemon.stage_detail_class_vm2",
               return_value={"class_confirmed": "person"}), \
         patch("listener.daemon.stage_build_tg2", return_value=fake_tg2), \
         patch("listener.daemon.stage_perform_match", return_value=fake_match), \
         patch("listener.daemon.stage_build_tg3", return_value=fake_tg3):

        result = _run_pipeline(alert)

    assert result["status"] == "ok"
    assert result["tg1"] is fake_tg1


def test_run_pipeline_proceeds_when_vm1_returns_animal():
    """VM1 returns class=animal — TG#1 must still be built (mode=animal)."""
    from listener.daemon import _run_pipeline

    alert = _make_alert()
    animal_vm1 = dict(_VEHICLE_VM1_RESULT, **{"class": "animal", "subclass": "cat"})
    fake_tg1 = {"caption": "Cat detected", "photos": []}
    fake_tg2 = {"caption": "Cat details", "photos": []}
    fake_tg3 = {"caption": "Cat unrecognised", "photos": []}
    fake_match = {"matched": False}

    with patch("listener.daemon.stage_load_frames", return_value=alert["frames"]), \
         patch("listener.daemon._stage_gate", return_value=_make_gate_ok()), \
         patch("listener.daemon.stage_cooldown_check",
               return_value={"status": "ok"}), \
         patch("listener.daemon.stage_prepare_artifacts", return_value=_make_artifacts()), \
         patch("listener.daemon.stage_verify_class_vm1", return_value=animal_vm1), \
         patch("listener.daemon.stage_build_tg1", return_value=fake_tg1), \
         patch("listener.daemon.stage_detail_class_vm2",
               return_value={"class_confirmed": "animal"}), \
         patch("listener.daemon.stage_build_tg2", return_value=fake_tg2), \
         patch("listener.daemon.stage_perform_match", return_value=fake_match), \
         patch("listener.daemon.stage_build_tg3", return_value=fake_tg3):

        result = _run_pipeline(alert)

    assert result["status"] == "ok"
    assert result["tg1"] is fake_tg1
