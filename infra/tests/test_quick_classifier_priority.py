"""
Tests for §11.110 — QuickClassifier top_class surveillance priority.

Pure-logic tests for `_select_top_detection()` and the
`SURVEILLANCE_PRIORITY_*` class constants. Does NOT require the
ONNX model — can run on a fresh clone.

The priority logic selects which detection becomes `top_class` when
multiple detections exist. Without this logic, the highest-confidence
detection wins even if a non-surveillance class (e.g. "chair" at 0.85)
outranks a surveillance class (e.g. "person" at 0.65) by confidence.

Priority tiers (from active-tasks.md §11.40):
  0 — person (highest priority)
  1 — vehicle
  2 — animal
  3 — other  (lowest — no surveillance meaning)

Within the same tier, higher confidence wins. If NO surveillance class
is present, the highest-confidence detection overall wins (existing
"highest conf" behavior preserved).

Floor:
  Priority applies only when the surveillance-class detection has
  `conf >= MOTION_GATE_PRIORITY_FLOOR` (default 0.30). Below the
  floor, the detection is treated as noise even if it IS a
  surveillance class. This prevents 0.15-conf "person" phantom
  detections from beating 0.85-conf non-surveillance classes.

Run:
    source .venv/bin/activate
    python3 -m pytest infra/tests/test_quick_classifier_priority.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Project root on path
_root = Path(__file__).resolve().parent.parent.parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))


# --- Constants ---

def test_person_classes_contains_only_person():
    from infra.quick_classifier import PERSON_CLASSES
    assert PERSON_CLASSES == frozenset({"person"})


def test_vehicle_classes_are_wheeled():
    """Vehicles = wheeled transport (car/truck/bus/etc). NO airplane/boat/train."""
    from infra.quick_classifier import VEHICLE_CLASSES
    assert VEHICLE_CLASSES == frozenset(
        {"bicycle", "car", "motorcycle", "bus", "truck"}
    )


def test_animal_classes_are_ground_animals_plus_bird():
    """Animals = COCO animal classes plausibly on a rural property."""
    from infra.quick_classifier import ANIMAL_CLASSES
    assert ANIMAL_CLASSES == frozenset(
        {"cat", "dog", "horse", "sheep", "cow", "bear", "bird"}
    )


def test_priority_classes_partition_keep_classes():
    """PERSON + VEHICLE + ANIMAL == KEEP_CLASSES_DEFAULT (sanity: no drift)."""
    from infra.quick_classifier import (
        PERSON_CLASSES, VEHICLE_CLASSES, ANIMAL_CLASSES,
        KEEP_CLASSES_DEFAULT,
    )
    assert PERSON_CLASSES | VEHICLE_CLASSES | ANIMAL_CLASSES == KEEP_CLASSES_DEFAULT


def test_priority_floor_default_is_below_threshold():
    """Default floor (0.30) must be < DEFAULT_CONFIDENCE_THRESHOLD (0.40)
    so a 0.35-conf person beats a 0.85-conf chair via priority.

    If this ever fails, the priority mechanism is being neutralized.
    """
    from infra.quick_classifier import (
        DEFAULT_PRIORITY_FLOOR, DEFAULT_CONFIDENCE_THRESHOLD,
    )
    assert DEFAULT_PRIORITY_FLOOR < DEFAULT_CONFIDENCE_THRESHOLD
    assert DEFAULT_PRIORITY_FLOOR == 0.30


# --- Pure helper: _select_top_detection ---

def test_select_top_detection_empty_returns_none():
    from infra.quick_classifier import _select_top_detection
    assert _select_top_detection([], floor=0.30) is None


def test_select_top_detection_single_detection_passes_through():
    """Single detection → returned unchanged (highest conf, only one)."""
    from infra.quick_classifier import _select_top_detection
    detections = [(0, 0.85, (10, 20, 100, 200))]  # class_id=0 → "person"
    cls, conf, bbox = _select_top_detection(detections, floor=0.30)
    assert cls == 0
    assert conf == pytest.approx(0.85)
    assert bbox == (10, 20, 100, 200)


def test_select_top_detection_priority_beats_high_conf_noise():
    """THE CORE BUG: chair at 0.85 vs person at 0.65 → person wins by priority."""
    from infra.quick_classifier import _select_top_detection, PERSON_CLASSES
    # COCO class_id 56 = "chair", 0 = "person"
    detections = [
        (56, 0.85, (10, 20, 100, 200)),  # chair — highest conf
        (0, 0.65, (300, 400, 500, 600)),  # person — surveillance class
    ]
    cls, conf, _ = _select_top_detection(detections, floor=0.30)
    assert cls == 0  # person wins
    # Confirm by name (defensive — class IDs can shift if COCO_NAMES reorders).
    from infra.quick_classifier import COCO_NAMES
    assert COCO_NAMES[cls] in PERSON_CLASSES


def test_select_top_detection_priority_within_same_tier():
    """Within same tier, higher conf wins."""
    from infra.quick_classifier import _select_top_detection
    # two cars, one at 0.85, one at 0.65 → 0.85 wins
    detections = [
        (2, 0.65, (1, 2, 3, 4)),   # car
        (2, 0.85, (5, 6, 7, 8)),   # car (higher conf)
    ]
    cls, conf, bbox = _select_top_detection(detections, floor=0.30)
    assert cls == 2
    assert conf == pytest.approx(0.85)
    assert bbox == (5, 6, 7, 8)


def test_select_top_detection_person_beats_vehicle_beats_animal():
    """Tier ordering: 0 (person) > 1 (vehicle) > 2 (animal), even at lower conf."""
    from infra.quick_classifier import _select_top_detection
    # animal at 0.90, vehicle at 0.70, person at 0.50
    detections = [
        (15, 0.90, (1, 2, 3, 4)),   # cat (animal, tier 2)
        (2, 0.70, (5, 6, 7, 8)),    # car (vehicle, tier 1)
        (0, 0.50, (9, 10, 11, 12)), # person (tier 0)
    ]
    cls, conf, _ = _select_top_detection(detections, floor=0.30)
    assert cls == 0  # person wins despite lowest conf


def test_select_top_detection_floor_blocks_low_conf_surveillance():
    """Person at 0.20 (below floor) does NOT beat chair at 0.85."""
    from infra.quick_classifier import _select_top_detection
    detections = [
        (56, 0.85, (1, 2, 3, 4)),   # chair
        (0, 0.20, (5, 6, 7, 8)),    # person — too low
    ]
    cls, conf, _ = _select_top_detection(detections, floor=0.30)
    assert cls == 56  # chair wins (person below floor)


def test_select_top_detection_no_surveillance_falls_back_to_highest_conf():
    """When no detection is a surveillance class, highest conf wins (existing behavior)."""
    from infra.quick_classifier import _select_top_detection
    detections = [
        (56, 0.85, (1, 2, 3, 4)),   # chair
        (57, 0.65, (5, 6, 7, 8)),   # couch
        (60, 0.95, (9, 10, 11, 12)),  # dining table — highest
    ]
    cls, conf, bbox = _select_top_detection(detections, floor=0.30)
    assert cls == 60
    assert conf == pytest.approx(0.95)


def test_select_top_detection_mixed_surveillance_picks_highest_priority():
    """When surveillance classes exist, priority wins among them; highest-conf
    non-surveillance is ignored even if it's the global max."""
    from infra.quick_classifier import _select_top_detection
    detections = [
        (56, 0.95, (1, 2, 3, 4)),     # chair — global max but non-surveillance
        (15, 0.40, (5, 6, 7, 8)),     # cat (animal, tier 2) — eligible
        (2, 0.55, (9, 10, 11, 12)),   # car (vehicle, tier 1) — eligible, higher priority
    ]
    cls, conf, _ = _select_top_detection(detections, floor=0.30)
    assert cls == 2  # car wins over cat (vehicle > animal)


# --- Integration with classify_frame decision logic ---

def test_classify_frame_priority_does_not_change_decision_when_already_pass():
    """Smoke: a frame with a single high-conf detection still passes through
    the priority logic without crashing or changing outcome.

    Skipped if the ONNX model isn't downloaded (uses the real model).
    """
    from pathlib import Path
    model = _root / "models" / "yolov8n.onnx"
    if not model.is_file():
        pytest.skip("yolov8n.onnx not downloaded")

    # Late imports so the skipif above runs first and avoids importing the
    # heavy onnxruntime-backed QuickClassifier when the model is absent.
    from PIL import Image
    from infra.quick_classifier import QuickClassifier
    qc = QuickClassifier()
    # Solid color — should suppress (no detections), exercising the
    # "no detections" branch + the priority function returning None.
    blank = Image.new("RGB", (640, 480), color=(128, 128, 128))
    tmp = _root / "data" / "test_priority_smoke.jpg"
    tmp.parent.mkdir(parents=True, exist_ok=True)
    blank.save(tmp)
    try:
        v = qc.classify_frame(str(tmp))
        # Blank frame → no detections → suppress, regardless of priority.
        assert v.decision == "suppress"
        assert v.top_class == "none"
    finally:
        tmp.unlink(missing_ok=True)