"""Tests for per-camera subjects routing (US-055b).

Covers the operator's routing intent: FRONT/BACK suppress vehicles and route
solo-person to the person pipeline; the 4 perimeter cameras keep current
behavior unchanged.

Tests use synthetic QuickVerdict fixtures against infra.gate._route_decision --
no YOLO model spin-up, no log writes, no webhook calls. Sub-5-second runtime.

Each test asserts both classification AND reason string.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Ensure infra is importable (worktree may not be on sys.path).
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from infra.gate import _route_decision
from infra.quick_classifier import QuickVerdict

# ---------------------------------------------------------------------------
# Fixtures — synthetic QuickVerdict objects (no classifier needed)
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


# ===========================================================================
# Tests 1-3: FRONT camera -- subjects=["person", "animal"]
# ===========================================================================


def test_front_solo_person_routes_to_person():
    """FRONT, solo person (0.61), no vehicle -> classification='person',
    reason is NOT 'high_conf_person_not_vehicle'."""
    subjects = ["person", "animal"]
    thresholds = {"person": 0.35, "truck": 0.50}  # any reasonable thresholds

    verdict_a = _qv("person", 0.61)
    verdict_b = _qv("car", 0.10, "suppress")  # below threshold -> suppress

    classification, class_label, confidence, reason = _route_decision(
        verdict_a, verdict_b, thresholds, subjects=subjects,
    )

    assert classification == "person"
    assert class_label == "person"
    assert confidence == 0.61
    assert reason != "high_conf_person_not_vehicle"


def test_front_solo_vehicle_suppressed():
    """FRONT, solo truck (0.76) -> classification='none',
    reason='top_class_truck_not_in_subjects'."""
    subjects = ["person", "animal"]
    thresholds = {"truck": 0.50, "person": 0.35}

    verdict_a = _qv("truck", 0.76)
    verdict_b = _qv("car", 0.05, "suppress")

    classification, class_label, confidence, reason = _route_decision(
        verdict_a, verdict_b, thresholds, subjects=subjects,
    )

    assert classification == "none"
    assert class_label == "truck"
    assert confidence == 0.76
    assert reason == "top_class_truck_not_in_subjects"


def test_front_mixed_person_vehicle_routes_to_person():
    """FRONT, [(person, 0.61), (car, 0.55)] -> classification='person'.

    The mixed vehicle wins path (Rule 5) is overridden by the subjects list:
    vehicle is not in subjects, so it is suppressed; the person (which IS in
    subjects) wins and routes to the person pipeline."""
    subjects = ["person", "animal"]
    thresholds = {"car": 0.50, "person": 0.35, "truck": 0.50}

    verdict_a = _qv("person", 0.61)
    verdict_b = _qv("car", 0.55)

    classification, class_label, confidence, reason = _route_decision(
        verdict_a, verdict_b, thresholds, subjects=subjects,
    )

    assert classification == "person"
    assert class_label == "person"
    assert confidence == 0.61
    assert reason == "subject_allowed"


# ===========================================================================
# Test 4: OUTSIDE_FRONT_SOLAR -- no subjects (perimeter), unchanged behavior
# ===========================================================================


def test_outside_front_solar_unchanged():
    """OUTSIDE_FRONT_SOLAR, truck (0.76) -> classification='vehicle'.

    Perimeter cameras have subjects=None, so the old vehicle-priority
    behavior applies unchanged: a vehicle-class detection routes to vehicle."""
    subjects = None  # perimeter camera, no subject filter
    thresholds = {"truck": 0.55, "car": 0.55}

    verdict_a = _qv("truck", 0.76)
    verdict_b = _qv("car", 0.05, "suppress")

    classification, class_label, confidence, reason = _route_decision(
        verdict_a, verdict_b, thresholds, subjects=subjects,
    )

    assert classification == "vehicle"
    assert class_label == "truck"
    assert confidence == 0.76
    assert reason == "high_conf_vehicle"


# ===========================================================================
# Test 5: OUTSIDE_FRONT_SOLAR solo person -- suppressed as before
# ===========================================================================


def test_outside_front_solar_solo_person_suppressed_as_before():
    """OUTSIDE_FRONT_SOLAR, solo person (0.61) -> classification='none',
    reason='top_class_person_not_in_subjects'.

    Perimeter cameras with subjects=None: when there is a non-vehicle,
    non-animal, non-person top-class (or even person alone without vehicle
    in the mix), the old catchall suppresses. With subjects=None the
    fallback at line 802 returns (none, top_class, conf,
    top_class_not_in_subjects)."""
    subjects = None  # perimeter camera, no subject filter
    thresholds = {"person": 0.50, "truck": 0.55}

    verdict_a = _qv("person", 0.61)
    verdict_b = _qv("dog", 0.05, "suppress")

    classification, class_label, confidence, reason = _route_decision(
        verdict_a, verdict_b, thresholds, subjects=subjects,
    )

    assert classification == "none"
    assert class_label == "person"
    assert confidence == 0.61
    assert reason == "top_class_person_not_in_subjects"


# ===========================================================================
# Test 6: BACK camera -- mirrors FRONT behavior
# ===========================================================================


def test_back_mirror_front():
    """BACK, solo-person (0.65) -> classification='person'.

    BACK has the same subjects config as FRONT: ['person', 'animal'].
    Solo person should route to the person pipeline."""
    subjects = ["person", "animal"]
    thresholds = {"person": 0.35, "truck": 0.50}

    verdict_a = _qv("person", 0.65)
    verdict_b = _qv("cat", 0.05, "suppress")

    classification, class_label, confidence, reason = _route_decision(
        verdict_a, verdict_b, thresholds, subjects=subjects,
    )

    assert classification == "person"
    assert class_label == "person"
    assert confidence == 0.65
    assert reason == "subject_allowed"
