"""Tests for listener/motion_gate_pipeline.py — pre-Qwen motion gate (Phase.107).

Tests cover the gate logic and routing decision tree. We use mocked QuickClassifier
+ synthetic frames so tests run fast and don't depend on the YOLO model file.

Coverage:
  - Threshold resolution (load_thresholds for known + unknown cameras)
  - Routing decision tree (rules 1-5 from PLAN §11.37)
  - run() integration with synthetic frames
  - Edge cases: no motion, missing frames, wrong frame count
  - GateVerdict dataclass fields and helper properties

These tests use synthetic frames + a fake QuickClassifier. The probe script
(probe_motion_gate.py) covers end-to-end with real alert frames.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

_root = Path(__file__).resolve().parent.parent.parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

from infra.quick_classifier import QuickVerdict
from listener.motion_gate_pipeline import (
    ANIMAL_CLASSES,
    DEFAULT_OTHER_COCO_THRESHOLD,
    SURVEILLANCE_CLASSES,
    THRESHOLDS_BY_CLASS,
    VEHICLE_CLASSES,
    GateVerdict,
    _classify_crop,
    _route_decision,
    _threshold_for,
    load_thresholds,
    run,
)

# ---------------------------------------------------------------------------
# Fake classifier — returns scripted verdicts without loading YOLO
# ---------------------------------------------------------------------------


@dataclass
class FakeClassifier:
    """Scripted QuickClassifier replacement for tests.

    Pass `responses` as a list of QuickVerdict objects; each call to
    classify_frame() pops one off the front. If responses runs out, returns
    a suppress verdict with class='none'.

    Phase.116 (timestamp-fix): classify_frame accepts a `timestamp` kwarg
    so the dispatch layer's signature matches real QuickClassifier. We do not
    inspect it — the real heuristic lives in quick_classifier.py, which has
    its own tests. This test file exercises the gate dispatch + decision tree.
    """

    responses: list[QuickVerdict]

    def classify_frame(
        self, frame, timestamp=None
    ) -> QuickVerdict:
        if self.responses:
            return self.responses.pop(0)
        return QuickVerdict(top_class="none", top_confidence=0.0, decision="suppress")


def _verdict(class_name: str, confidence: float, decision: str = "pass_with_hint") -> QuickVerdict:
    return QuickVerdict(
        top_class=class_name,
        top_confidence=confidence,
        decision=decision,
    )


def _make_synthetic_frames(out_dir: Path, motion: bool = True) -> list[str]:
    """Create 4 synthetic frames in out_dir. If motion=True, simulate a moving
    object across frames (frame 1 to 4 — visible motion between adjacent pairs).

    Returns list of 4 paths.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for i in range(4):
        # Dark background
        frame = np.full((200, 200, 3), 50, dtype=np.uint8)
        if motion:
            # Bright white "vehicle" rectangle that moves across the frame
            x_offset = 30 + i * 30  # moves 30px per frame
            frame[80:130, x_offset : x_offset + 50] = (220, 220, 220)
        path = out_dir / f"frame_{i+1:03d}.jpg"
        cv2.imwrite(str(path), frame)
        paths.append(str(path))
    return paths


# ---------------------------------------------------------------------------
# Threshold resolution tests
# ---------------------------------------------------------------------------


def test_thresholds_by_class_has_expected_entries():
    assert THRESHOLDS_BY_CLASS["car"] == 0.50
    assert THRESHOLDS_BY_CLASS["person"] == 0.35
    assert "dog" in THRESHOLDS_BY_CLASS
    assert "cat" in THRESHOLDS_BY_CLASS


def test_vehicle_classes_set():
    assert "car" in VEHICLE_CLASSES
    assert "truck" in VEHICLE_CLASSES
    assert "bus" in VEHICLE_CLASSES
    assert "motorcycle" in VEHICLE_CLASSES
    assert "bicycle" in VEHICLE_CLASSES
    assert "person" not in VEHICLE_CLASSES


def test_animal_classes_set():
    for cls in ("dog", "cat", "horse", "sheep", "cow", "bear", "bird"):
        assert cls in ANIMAL_CLASSES


def test_surveillance_classes_includes_person():
    assert "person" in SURVEILLANCE_CLASSES


def test_load_thresholds_unknown_camera_returns_defaults():
    thresholds = load_thresholds("Nonexistent Camera XYZ")
    # Unknown camera → per-class defaults only (no per-camera overrides)
    assert thresholds["car"] == THRESHOLDS_BY_CLASS["car"]
    assert thresholds["person"] == THRESHOLDS_BY_CLASS["person"]


def test_load_thresholds_ofs_applies_overrides():
    thresholds = load_thresholds("CAM5")
    # Per-camera override: car=0.45, person=0.30
    assert thresholds["car"] == 0.45
    assert thresholds["person"] == 0.30
    # Per-class default still applies for classes not overridden
    assert thresholds["truck"] == THRESHOLDS_BY_CLASS["truck"]


def test_threshold_for_returns_class_threshold_or_default():
    thresholds = {"car": 0.5, "person": 0.35}
    assert _threshold_for(thresholds, "car") == 0.5
    assert _threshold_for(thresholds, "person") == 0.35
    # Unknown class → DEFAULT_OTHER_COCO_THRESHOLD
    assert _threshold_for(thresholds, "bench") == DEFAULT_OTHER_COCO_THRESHOLD


# ---------------------------------------------------------------------------
# Routing decision tree tests (rule 1-5)
# ---------------------------------------------------------------------------


def test_route_rule1_vehicle_high_conf_passes_vehicle():
    """Rule 1: ANY crop high-conf vehicle-class → vehicle."""
    va = _verdict("car", 0.85)
    vb = _verdict("none", 0.0, "suppress")
    decision, label, conf, reason = _route_decision(va, vb, THRESHOLDS_BY_CLASS)
    assert decision == "vehicle"
    assert label == "car"
    assert conf == 0.85
    assert reason == "high_conf_vehicle"


def test_route_rule2_both_person_high_conf_passes_person():
    """Rule 2: BOTH crops high-conf person → person."""
    va = _verdict("person", 0.80)
    vb = _verdict("person", 0.65)
    decision, label, conf, reason = _route_decision(va, vb, THRESHOLDS_BY_CLASS)
    assert decision == "person"
    assert label == "person"
    assert conf == 0.80
    assert reason == "high_conf_person"


def test_route_rule2_only_one_person_falls_through_to_rule5():
    """Only ONE crop high-conf person → not rule 2 → falls through to rule 5.

    Phase.137 (§11.59): the historical catchall returned decision="vehicle"
    regardless of whether a vehicle class was actually present. With no
    vehicle in the mix (only "person" + "none"), the verdict class is
    "person" and there is no rule 2 hit (rule 2 requires BOTH crops person
    in V1 mode). The correct behavior is now to suppress — the class is not
    a vehicle, there is no vehicle in either crop, and the vehicle pipeline
    cannot sensibly process a "person"-class alert. Routes the
    "high_conf_person_not_vehicle_no_pipeline" reason so postmortem can
    distinguish this from rule 4 "no_object_detected" suppressions.
    """
    va = _verdict("person", 0.80)
    vb = _verdict("none", 0.0, "suppress")
    decision, label, _conf, reason = _route_decision(va, vb, THRESHOLDS_BY_CLASS)
    assert decision == "suppress"
    assert label == "person"
    assert reason == "high_conf_person_not_vehicle_no_pipeline"


def test_route_rule3_animal_suppressed():
    """Rule 3: animal → suppress (no animal pipeline)."""
    va = _verdict("dog", 0.85)
    vb = _verdict("none", 0.0, "suppress")
    decision, label, conf, reason = _route_decision(va, vb, THRESHOLDS_BY_CLASS)
    assert decision == "suppress"
    assert label == "dog"
    assert conf == 0.85
    assert reason == "animal_suppressed_no_pipeline"


def test_route_rule4_all_low_conf_suppresses():
    """Rule 4: All crops below threshold → suppress."""
    va = _verdict("none", 0.0, "suppress")
    vb = _verdict("none", 0.0, "suppress")
    decision, label, conf, reason = _route_decision(va, vb, THRESHOLDS_BY_CLASS)
    assert decision == "suppress"
    assert label is None
    assert conf == 0.0
    assert reason == "no_object_detected"


def test_route_rule4_low_conf_known_class_still_suppresses():
    """Real class detected but below threshold → suppress."""
    va = _verdict("car", 0.30, "suppress")  # below 0.50 threshold for car
    vb = _verdict("none", 0.0, "suppress")
    decision, _label, _conf, reason = _route_decision(va, vb, THRESHOLDS_BY_CLASS)
    assert decision == "suppress"
    assert reason == "no_object_detected"


def test_route_rule5_mixed_vehicle_wins():
    """Rule 5 LEGITIMATE case: vehicle somewhere in the mix.

    Phase.137 (§11.59): this test now uses person + car (a vehicle IS in
    the mix). The historical variant person + bench (no vehicle anywhere)
    was actually the buggy case — that "mixed" was a misnomer because
    there was no vehicle for the rule to "win" on. The bug fix splits
    rule 5 into two sub-cases:

      - vehicle is in the mix → "mixed_vehicle_wins" (vehicle pipeline)
      - no vehicle in the mix → "high_conf_<class>_not_vehicle_no_pipeline"
        (suppress, with class name in the reason)

    This test verifies the LEGITIMATE branch is preserved.
    """
    va = _verdict("person", 0.75)
    vb = _verdict("car", 0.60, "pass_with_hint")
    decision, _label, _conf, reason = _route_decision(va, vb, THRESHOLDS_BY_CLASS)
    assert decision == "vehicle"
    assert reason == "mixed_vehicle_wins"


def test_route_higher_confidence_wins_on_vehicle_class():
    """When both crops detect a vehicle, the higher-confidence verdict drives
    the label."""
    va = _verdict("car", 0.60)
    vb = _verdict("truck", 0.90)
    decision, label, conf, _reason = _route_decision(va, vb, THRESHOLDS_BY_CLASS)
    assert decision == "vehicle"
    assert label == "truck"  # higher confidence
    assert conf == 0.90


# ---------------------------------------------------------------------------
# _classify_crop threshold application tests
# ---------------------------------------------------------------------------


def test_classify_crop_above_threshold_decision_pass_with_hint():
    """A real COCO keep-class with conf > threshold → pass_with_hint."""
    fake = FakeClassifier([_verdict("car", 0.85)])
    verdict = _classify_crop(fake, "/some/path.jpg", THRESHOLDS_BY_CLASS)
    assert verdict.decision == "pass_with_hint"


def test_classify_crop_below_threshold_decision_suppress():
    """Even if quick_classifier says pass_with_hint, we re-evaluate based on
    the per-class threshold."""
    fake = FakeClassifier([_verdict("car", 0.30, "pass_with_hint")])
    verdict = _classify_crop(fake, "/some/path.jpg", THRESHOLDS_BY_CLASS)
    assert verdict.decision == "suppress"


def test_classify_crop_none_path_returns_suppress():
    fake = FakeClassifier([])
    verdict = _classify_crop(fake, None, THRESHOLDS_BY_CLASS)
    assert verdict.top_class == "none"
    assert verdict.top_confidence == 0.0
    assert verdict.decision == "suppress"


# ---------------------------------------------------------------------------
# run() integration tests (with synthetic frames)
# ---------------------------------------------------------------------------


def test_run_wrong_frame_count_returns_suppress(tmp_path):
    paths = _make_synthetic_frames(tmp_path / "frames", motion=True)[:3]  # only 3
    classifier = FakeClassifier([])
    verdict = run(
        frame_paths=paths,
        camera_name="CAM5",
        alert_id="test-1",
        output_dir=str(tmp_path),
        classifier=classifier,
    )
    assert verdict.decision == "suppress"
    assert verdict.reason == "wrong_frame_count"


def test_run_no_motion_returns_suppress(tmp_path):
    # 4 frames with NO motion (motion=False)
    paths = _make_synthetic_frames(tmp_path / "frames", motion=False)
    classifier = FakeClassifier([_verdict("none", 0.0, "suppress"), _verdict("none", 0.0, "suppress")])
    verdict = run(
        frame_paths=paths,
        camera_name="CAM5",
        alert_id="test-2",
        output_dir=str(tmp_path),
        classifier=classifier,
    )
    assert verdict.decision == "suppress"
    assert verdict.reason == "no_server_motion"


def test_run_vehicle_detected_returns_vehicle(tmp_path, monkeypatch):
    # 4 frames with motion, classifier says "car" on both crops.
    # Set GATE_KEEP_DISK_ARTIFACTS=true so disk paths are populated
    # (default is False — production runs in-memory only).
    monkeypatch.setenv("GATE_KEEP_DISK_ARTIFACTS", "true")
    paths = _make_synthetic_frames(tmp_path / "frames", motion=True)
    classifier = FakeClassifier([
        _verdict("car", 0.85, "pass_with_hint"),
        _verdict("car", 0.78, "pass_with_hint"),
    ])
    verdict = run(
        frame_paths=paths,
        camera_name="CAM5",
        alert_id="test-3",
        output_dir=str(tmp_path),
        classifier=classifier,
    )
    assert verdict.decision == "vehicle"
    assert verdict.class_label == "car"
    assert verdict.confidence == 0.85
    assert verdict.reason == "high_conf_vehicle"
    # In-memory PIL crops (always set, even when disk is off)
    assert verdict.crop_a is not None
    assert verdict.crop_b is not None
    # Disk paths (only when GATE_KEEP_DISK_ARTIFACTS=true)
    assert verdict.crop_a_path is not None
    assert verdict.crop_b_path is not None
    assert Path(verdict.crop_a_path).is_file()
    assert Path(verdict.crop_b_path).is_file()


def test_run_person_detected_returns_person(tmp_path):
    """Both crops detect person → person pipeline (Rule 2 LOCKED).

    Note: only ONE crop detecting person → Rule 5 mixed-vehicle-wins.
    See test_run_one_person_only_routes_to_vehicle below for that case.
    """
    # Synthetic person (small blob, different color, moves across frames)
    out_dir = tmp_path / "frames"
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for i in range(4):
        frame = np.full((200, 200, 3), 50, dtype=np.uint8)
        # Moving person-shape blob (clearly different from background)
        x_offset = 30 + i * 30
        frame[60:180, x_offset : x_offset + 40] = (180, 140, 120)
        path = out_dir / f"frame_{i+1:03d}.jpg"
        cv2.imwrite(str(path), frame)
        paths.append(str(path))

    classifier = FakeClassifier([
        _verdict("person", 0.80, "pass_with_hint"),
        _verdict("person", 0.70, "pass_with_hint"),
    ])
    verdict = run(
        frame_paths=paths,
        camera_name="Front Door",
        alert_id="test-4",
        output_dir=str(tmp_path),
        classifier=classifier,
    )
    assert verdict.decision == "person"
    assert verdict.class_label == "person"
    assert verdict.confidence == 0.80


def test_run_one_person_only_routes_to_vehicle(tmp_path):
    """Phase.137 (§11.59): person + non-vehicle-other → SUPPRESS, not vehicle.

    Historical behavior (the bug): this case routed to the vehicle pipeline
    with class="person", causing "Vehicle in motion: <person>" Telegram
    alerts and downstream pipeline failures.

    Fixed behavior: no vehicle in the mix → suppress with
    "high_conf_person_not_vehicle_no_pipeline" reason.

    A separate test (test_route_rule5_mixed_vehicle_wins) covers the
    legitimate "vehicle is in the mix → mixed_vehicle_wins" path.
    """
    out_dir = tmp_path / "frames"
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for i in range(4):
        frame = np.full((200, 200, 3), 50, dtype=np.uint8)
        x_offset = 30 + i * 30
        frame[60:180, x_offset : x_offset + 40] = (180, 140, 120)
        path = out_dir / f"frame_{i+1:03d}.jpg"
        cv2.imwrite(str(path), frame)
        paths.append(str(path))

    classifier = FakeClassifier([
        _verdict("person", 0.80, "pass_with_hint"),
        _verdict("bench", 0.60, "pass_with_hint"),  # other-COCO class, no vehicle
    ])
    verdict = run(
        frame_paths=paths,
        camera_name="CAM5",
        alert_id="test-person-suppresses",
        output_dir=str(tmp_path),
        classifier=classifier,
    )
    # No vehicle anywhere in the mix → suppress with the class name in the reason.
    assert verdict.decision == "suppress"
    assert verdict.reason == "high_conf_person_not_vehicle_no_pipeline"


def test_run_animal_detected_returns_suppress(tmp_path):
    paths = _make_synthetic_frames(tmp_path / "frames", motion=True)
    classifier = FakeClassifier([
        _verdict("dog", 0.85, "pass_with_hint"),
        _verdict("none", 0.0, "suppress"),
    ])
    verdict = run(
        frame_paths=paths,
        camera_name="CAM2",
        alert_id="test-5",
        output_dir=str(tmp_path),
        classifier=classifier,
    )
    assert verdict.decision == "suppress"
    assert verdict.class_label == "dog"
    assert verdict.reason == "animal_suppressed_no_pipeline"


def test_run_threshold_blocks_vehicle_with_low_confidence(tmp_path):
    """Camera with high threshold + low-conf vehicle → suppress."""
    paths = _make_synthetic_frames(tmp_path / "frames", motion=True)
    # CAM5 requires car conf >= 0.45 (per-camera override).
    # We'll send 0.40 → should be suppressed.
    classifier = FakeClassifier([
        _verdict("car", 0.40, "pass_with_hint"),
        _verdict("car", 0.38, "pass_with_hint"),
    ])
    verdict = run(
        frame_paths=paths,
        camera_name="CAM5",
        alert_id="test-6",
        output_dir=str(tmp_path),
        classifier=classifier,
    )
    assert verdict.decision == "suppress"


def test_run_threshold_passes_vehicle_with_higher_confidence(tmp_path):
    """Same camera, higher conf → vehicle."""
    paths = _make_synthetic_frames(tmp_path / "frames", motion=True)
    classifier = FakeClassifier([
        _verdict("car", 0.50, "pass_with_hint"),
        _verdict("car", 0.48, "pass_with_hint"),
    ])
    verdict = run(
        frame_paths=paths,
        camera_name="CAM5",
        alert_id="test-7",
        output_dir=str(tmp_path),
        classifier=classifier,
    )
    # 0.50 ≥ 0.45 (CAM5 car threshold) → vehicle
    assert verdict.decision == "vehicle"
    assert verdict.confidence == 0.50


# ---------------------------------------------------------------------------
# GateVerdict dataclass tests
# ---------------------------------------------------------------------------


def test_gateverdict_is_suppress_property():
    v = GateVerdict(
        decision="suppress", class_label=None, confidence=0.0,
        crop_a_path=None, crop_b_path=None,
        bbox_a=None, bbox_b=None, reason="no_object_detected",
    )
    assert v.is_suppress is True
    assert v.is_pass is False


def test_gateverdict_is_pass_property_for_vehicle():
    v = GateVerdict(
        decision="vehicle", class_label="car", confidence=0.8,
        crop_a_path="/tmp/a.jpg", crop_b_path="/tmp/b.jpg",
        bbox_a=(10, 20, 100, 100), bbox_b=(30, 40, 100, 100),
        reason="high_conf_vehicle",
    )
    assert v.is_pass is True
    assert v.is_suppress is False


def test_gateverdict_is_pass_property_for_person():
    v = GateVerdict(
        decision="person", class_label="person", confidence=0.7,
        crop_a_path=None, crop_b_path=None,
        bbox_a=None, bbox_b=None, reason="high_conf_person",
    )
    assert v.is_pass is True
    assert v.is_suppress is False


def test_gateverdict_default_raw_verdicts_empty_list():
    """raw_verdicts should default to empty list (mutable default safety)."""
    v = GateVerdict(
        decision="vehicle", class_label="car", confidence=0.7,
        crop_a_path=None, crop_b_path=None,
        bbox_a=None, bbox_b=None,
    )
    assert v.raw_verdicts == []
    # Mutable default safety: each instance gets its own list.
    # Append a sentinel object (not a real QuickVerdict — this is a
    # list-aliasing smoke test, not a QuickVerdict round-trip).
    _sentinel = object()
    v.raw_verdicts.append(_sentinel)  # type: ignore[arg-type]
    v2 = GateVerdict(
        decision="suppress", class_label=None, confidence=0.0,
        crop_a_path=None, crop_b_path=None,
        bbox_a=None, bbox_b=None,
    )
    assert v2.raw_verdicts == []


def test_route_decision_prefers_per_crop_reason():
    """Phase.116 reason plumbing — when both crops suppress, the routing
    decision must surface the most informative per-crop reason (e.g.
    'night_low_confidence', 'class_below_threshold') rather than the
    generic 'no_object_detected' fallback. The test feeds a verdict with
    reason='class_below_threshold' and asserts the final reason preserves it.
    """
    from infra.quick_classifier import QuickVerdict
    from listener.motion_gate_pipeline import _route_decision

    # Both crops suppressed, crop_a carries the meaningful reason
    va = QuickVerdict(
        top_class="person", top_confidence=0.343, decision="suppress",
        reason="class_below_threshold",
    )
    vb = QuickVerdict(
        top_class="none", top_confidence=0.0, decision="suppress",
        reason="no_object_detected",
    )
    _, _, _, reason = _route_decision(va, vb, {})
    # Should prefer va's reason over vb's
    assert reason == "class_below_threshold", (
        f"Expected 'class_below_threshold' (the higher-conf crop's reason), got '{reason}'"
    )


def test_route_decision_falls_back_to_no_object_detected():
    """Phase.116 reason plumbing — when both crops suppress with no reason
    set, the routing decision must fall back to 'no_object_detected' as the
    safe default.
    """
    from infra.quick_classifier import QuickVerdict
    from listener.motion_gate_pipeline import _route_decision

    va = QuickVerdict(top_class="none", top_confidence=0.0, decision="suppress")
    vb = QuickVerdict(top_class="none", top_confidence=0.0, decision="suppress")
    _, _, _, reason = _route_decision(va, vb, {})
    assert reason == "no_object_detected"


# Phase.137 (§11.59) — see PLAN §11.59 for context.
#
# Before the fix, _route_decision's catchall returned decision="vehicle"
# regardless of whether a vehicle-class detection was present in the
# high_conf mix. This produced "decision=vehicle class=person" (and
# class=train, class=bench, class=zebra, etc.) in the listener log, and
# the vehicle pipeline crashed on those alerts. The 225 historical
# occurrences in logs/launchctl-stderr.log confirmed the pattern.
#
# The fix splits the catchall into two branches:
#   - vehicle in high_conf mix → vehicle pipeline (LOCKED rule 5)
#   - no vehicle in high_conf mix → suppress with class-named reason
#
# These tests pin both branches.
def test_route_catchall_no_vehicle_mix_suppresses_with_class_reason():
    """Phase.137 (§11.59): top is "train" with the OTHER crop also a
    non-vehicle (random COCO class). No vehicle anywhere in high_conf.

    Before the fix: catchall returned ("vehicle", "train", ..., "mixed_vehicle_wins").
    After the fix: catches and names the actual class in the reason.
    """
    va = _verdict("train", 0.55)
    vb = _verdict("bench", 0.50, "pass_with_hint")
    decision, label, conf, reason = _route_decision(va, vb, THRESHOLDS_BY_CLASS)
    assert decision == "suppress"
    assert label == "train"
    assert conf == 0.55
    assert reason == "high_conf_train_not_vehicle_no_pipeline"


def test_route_catchall_zebra_suppresses_with_class_reason():
    """Phase.137: 'zebra' detected in one crop (which YOLO occasionally
    produces on dappled sunlight / shadow patterns at dawn). No vehicle
    present → suppress.

    This case appeared in the production log: alert 6a517560 at 2026-08-27
    06:05:51 with class=zebra conf=0.57. That alert previously routed to
    the vehicle pipeline (decision=vehicle class=zebra) and crashed the
    pipeline. After the fix: suppressed cleanly.
    """
    va = _verdict("zebra", 0.57)
    vb = _verdict("none", 0.0, "suppress")
    decision, label, _conf, reason = _route_decision(va, vb, THRESHOLDS_BY_CLASS)
    assert decision == "suppress"
    assert label == "zebra"
    assert reason == "high_conf_zebra_not_vehicle_no_pipeline"


def test_route_catchall_fire_hydrant_suppresses_with_class_reason():
    """Phase.137: fire_hydrant (or any other small COCO class) detected
    with no vehicle present → suppress with class in reason.
    """
    va = _verdict("fire hydrant", 0.69)
    vb = _verdict("none", 0.0, "suppress")
    decision, label, _conf, reason = _route_decision(va, vb, THRESHOLDS_BY_CLASS)
    assert decision == "suppress"
    assert label == "fire hydrant"
    assert reason == "high_conf_fire hydrant_not_vehicle_no_pipeline"


def test_route_catchall_vehicle_in_other_position_still_wins():
    """Phase.137: 'fire hydrant' top + 'car' lower — vehicle IS in the
    mix, just not at the top. Rule 5 should still favor the vehicle.

    This is the LEGITIMATE "rule 5 mixed" case the LOCKED spec (§11.37 Q2)
    refers to: "vehicle somewhere in the mix wins on ambiguity".

    Implementation note: the catchall uses vehicle_top (computed at the
    top of rule 5, surviving all the other rule failures) so it doesn't
    matter where in the high_conf list the vehicle sits.
    """
    va = _verdict("fire hydrant", 0.69)
    vb = _verdict("car", 0.55, "pass_with_hint")
    decision, label, conf, reason = _route_decision(va, vb, THRESHOLDS_BY_CLASS)
    assert decision == "vehicle"
    assert label == "car"
    assert conf == 0.55
    assert reason == "mixed_vehicle_wins"


def test_route_decision_matches_production_log_9a78a254():
    """Phase.137 regression test — the specific alert that exposed the bug.

    Alert 9a78a254-aa11-4436-88fc-2564d8615594 at 2026-08-27 06:15:40 EDT
    was the LAST alert to fire the buggy catchall before the listener
    restart for 6B.134. Log line:

      motion_gate: pass (decision=vehicle class=person conf=0.48)
        — continuing to legacy routing

    Persona class person 0.48, other crop suppressed with no detection.
    Per pre-6B.137 catchall: 'mixed_vehicle_wins', vehicle pipeline crashes.
    Per 6B.137: 'high_conf_person_not_vehicle_no_pipeline', suppress cleanly.
    """
    va = _verdict("person", 0.48)
    vb = _verdict("none", 0.0, "suppress")
    decision, label, conf, reason = _route_decision(va, vb, THRESHOLDS_BY_CLASS)
    assert decision == "suppress"
    assert label == "person"
    assert conf == 0.48
    assert reason == "high_conf_person_not_vehicle_no_pipeline"



# Phase.152 — per-camera × per-event-type gate_enabled tests.
# These cover is_gate_enabled() which reads motion_gate_thresholds.json
# and returns True unless the camera explicitly disables this event_type.


class TestIsGateEnabled:
    """Phase.152 — per-camera gate_enabled matrix."""

    def test_default_all_true_when_camera_missing(self, monkeypatch, tmp_path):
        """Camera absent from config → all event types enabled (default)."""
        # Point config loader at an empty file
        monkeypatch.setattr(
            "infra.paths.PROJECT_ROOT", tmp_path,
        )
        empty_cfg = tmp_path / "config" / "motion_gate_thresholds.json"
        empty_cfg.parent.mkdir(parents=True, exist_ok=True)
        empty_cfg.write_text("{}")
        # Force re-cache
        # Reset the cache so each test reloads from PROJECT_ROOT.
        # Tests run as a package so listener.motion_gate_pipeline is
        # where _cached_thresholds lives (motion_gate_pipeline is the
        # bare-name alias used when running as __main__).
        try:
            import motion_gate_pipeline  # noqa: F401 — may not be on path
        except ImportError:
            pass
        monkeypatch.setattr("listener.motion_gate_pipeline._cached_thresholds", None)

        from listener.motion_gate_pipeline import is_gate_enabled

        # Camera missing → default True
        assert is_gate_enabled("Unknown Camera", "vehicle") is True
        assert is_gate_enabled("Unknown Camera", "person") is True
        assert is_gate_enabled("Unknown Camera", "motion") is True

    def test_default_when_field_missing(self, monkeypatch, tmp_path):
        """Camera in config but no gate_enabled field → defaults applied."""
        monkeypatch.setattr("infra.paths.PROJECT_ROOT", tmp_path)
        cfg = tmp_path / "config" / "motion_gate_thresholds.json"
        cfg.parent.mkdir(parents=True, exist_ok=True)
        cfg.write_text('{"CAM1": {"car": 0.4, "person": 0.5}}')
        # Reset the cache so each test reloads from PROJECT_ROOT.
        # Tests run as a package so listener.motion_gate_pipeline is
        # where _cached_thresholds lives (motion_gate_pipeline is the
        # bare-name alias used when running as __main__).
        try:
            import motion_gate_pipeline  # noqa: F401 — may not be on path
        except ImportError:
            pass
        monkeypatch.setattr("listener.motion_gate_pipeline._cached_thresholds", None)

        from listener.motion_gate_pipeline import is_gate_enabled

        # No gate_enabled field → defaults all True
        assert is_gate_enabled("CAM1", "vehicle") is True
        assert is_gate_enabled("CAM1", "person") is True

    def test_disabled_event_type_returns_false(self, monkeypatch, tmp_path):
        """Camera has gate_enabled with one event disabled → False for that."""
        monkeypatch.setattr("infra.paths.PROJECT_ROOT", tmp_path)
        cfg = tmp_path / "config" / "motion_gate_thresholds.json"
        cfg.parent.mkdir(parents=True, exist_ok=True)
        cfg.write_text(
            '{"CAM1": {"gate_enabled": '  # §13.4: was "CAM1"
            '{"vehicle": true, "person": false, "motion": true}}}'
        )
        # Reset the cache so each test reloads from PROJECT_ROOT.
        # Tests run as a package so listener.motion_gate_pipeline is
        # where _cached_thresholds lives (motion_gate_pipeline is the
        # bare-name alias used when running as __main__).
        try:
            import motion_gate_pipeline  # noqa: F401 — may not be on path
        except ImportError:
            pass
        monkeypatch.setattr("listener.motion_gate_pipeline._cached_thresholds", None)

        from listener.motion_gate_pipeline import is_gate_enabled

        assert is_gate_enabled("CAM1", "vehicle") is True
        assert is_gate_enabled("CAM1", "person") is False  # disabled
        assert is_gate_enabled("CAM1", "motion") is True

    def test_people_aliases_to_person(self, monkeypatch, tmp_path):
        """"people" (Reolink payload form) is treated as "person" key."""
        monkeypatch.setattr("infra.paths.PROJECT_ROOT", tmp_path)
        cfg = tmp_path / "config" / "motion_gate_thresholds.json"
        cfg.parent.mkdir(parents=True, exist_ok=True)
        cfg.write_text(
            '{"CAM3": {"gate_enabled": {"person": false}}}'
        )
        # Reset the cache so each test reloads from PROJECT_ROOT.
        # Tests run as a package so listener.motion_gate_pipeline is
        # where _cached_thresholds lives (motion_gate_pipeline is the
        # bare-name alias used when running as __main__).
        try:
            import motion_gate_pipeline  # noqa: F401 — may not be on path
        except ImportError:
            pass
        monkeypatch.setattr("listener.motion_gate_pipeline._cached_thresholds", None)

        from listener.motion_gate_pipeline import is_gate_enabled

        # "people" and "person" both consult the "person" key
        assert is_gate_enabled("CAM3", "people") is False
        assert is_gate_enabled("CAM3", "person") is False

    def test_partial_matrix_missing_event_defaults_true(
        self, monkeypatch, tmp_path
    ):
        """Camera has gate_enabled but missing one event → defaults True."""
        monkeypatch.setattr("infra.paths.PROJECT_ROOT", tmp_path)
        cfg = tmp_path / "config" / "motion_gate_thresholds.json"
        cfg.parent.mkdir(parents=True, exist_ok=True)
        cfg.write_text(
            '{"CAM3": {"gate_enabled": {"person": false}}}'
        )
        # Reset the cache so each test reloads from PROJECT_ROOT.
        # Tests run as a package so listener.motion_gate_pipeline is
        # where _cached_thresholds lives (motion_gate_pipeline is the
        # bare-name alias used when running as __main__).
        try:
            import motion_gate_pipeline  # noqa: F401 — may not be on path
        except ImportError:
            pass
        monkeypatch.setattr("listener.motion_gate_pipeline._cached_thresholds", None)

        from listener.motion_gate_pipeline import is_gate_enabled

        # "vehicle" missing → defaults True
        assert is_gate_enabled("CAM3", "vehicle") is True
        # "person" configured False → returns False
        assert is_gate_enabled("CAM3", "person") is False


# ---------------------------------------------------------------------------
# Phase.169 §11.93 — crop source frame regression tests
# ---------------------------------------------------------------------------


def _make_moving_object_frames(out_dir: Path) -> list[str]:
    """4 frames with a bright object at KNOWN positions per frame.

    Frame 1: object at x=30..80,  y=80..130
    Frame 2: object at x=60..110, y=80..130
    Frame 3: object at x=90..140, y=80..130
    Frame 4: object at x=120..170, y=80..130

    The diff bbox of frames (2,3) covers roughly x=60..140. The diff bbox
    of frames (3,4) covers roughly x=90..170.

    Pre-fix bug: crops written to frame_3_path / frame_4_path. For
    bbox_b = diff(3,4), the bbox covers x=90..170 — at frame_4 the
    object has moved to x=120..170, so part of the bbox (x=90..120)
    shows the empty region the object just left. For the
    "OBJECT_LEAVES_BBOX" case below, the object exits the bbox
    entirely by frame_4, leaving an empty crop.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for i in range(4):
        frame = np.full((200, 200, 3), 50, dtype=np.uint8)
        x_offset = 30 + i * 30
        frame[80:130, x_offset : x_offset + 50] = (220, 220, 220)
        path = out_dir / f"frame_{i+1:03d}.jpg"
        cv2.imwrite(str(path), frame)
        paths.append(str(path))
    return paths


def test_6B169_crops_written_to_earlier_frame_in_each_diff_pair(tmp_path, monkeypatch):
    """§11.93: bbox_a (diff of frame_2/3) crops frame_2; bbox_b (diff
    of frame_3/4) crops frame_3. Pre-fix: frame_3 and frame_4."""
    monkeypatch.setenv("GATE_KEEP_DISK_ARTIFACTS", "true")
    out_dir = tmp_path / "frames"
    paths = _make_moving_object_frames(out_dir)

    classifier = FakeClassifier([
        _verdict("car", 0.85, "pass_with_hint"),
        _verdict("car", 0.78, "pass_with_hint"),
    ])
    verdict = run(
        frame_paths=paths,
        camera_name="CAM5",
        alert_id="test-6B169-source",
        output_dir=str(tmp_path),
        classifier=classifier,
    )

    assert verdict.decision == "vehicle"
    assert verdict.crop_a_path is not None
    assert verdict.crop_b_path is not None

    # crop_a_path = bbox_a applied to frame_2 → filename starts "frame_002_"
    assert "frame_002_crop" in verdict.crop_a_path, (
        f"crop_a_path should be written to frame_2 (earlier of pair), "
        f"got {verdict.crop_a_path}"
    )
    # crop_b_path = bbox_b applied to frame_3 → filename starts "frame_003_"
    assert "frame_003_crop" in verdict.crop_b_path, (
        f"crop_b_path should be written to frame_3 (earlier of pair), "
        f"got {verdict.crop_b_path}"
    )
    # Sanity: NOT frame_3/frame_4 (the buggy pre-fix behavior)
    assert "frame_003_crop" not in verdict.crop_a_path
    assert "frame_004_crop" not in verdict.crop_b_path

    # And the files actually exist on disk
    assert Path(verdict.crop_a_path).is_file()
    assert Path(verdict.crop_b_path).is_file()


def _make_object_leaves_bbox_frames(out_dir: Path) -> list[str]:
    """4 frames where the moving object at frame_3 IS inside the diff
    bbox but at frame_4 has moved OUT of the bbox entirely.

    Frame 1: object at x=30..80
    Frame 2: object at x=60..110
    Frame 3: object at x=90..140 (still inside the diff(3,4) bbox x=90..170)
    Frame 4: object at x=160..210 (OUTSIDE the diff(3,4) bbox — the
             bbox covers x=90..170 because that's where pixels changed)

    Pre-fix: crop_b_path applies bbox_b to frame_4_path. At frame_4 the
    object is at x=160..210, but bbox_b only covers x=90..170. So the
    crop is empty.

    Post-fix: crop_b_path applies bbox_b to frame_3_path. At frame_3
    the object is at x=90..140, fully inside bbox_b. So the crop has
    the object visible.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for i, x_offset in enumerate([30, 60, 90, 160]):  # frame_4 jumps past bbox
        frame = np.full((200, 200, 3), 50, dtype=np.uint8)
        frame[80:130, x_offset : x_offset + 50] = (220, 220, 220)
        path = out_dir / f"frame_{i+1:03d}.jpg"
        cv2.imwrite(str(path), frame)
        paths.append(str(path))
    return paths


def test_6B169_crop_b_contains_object_when_object_leaves_later_frame(tmp_path, monkeypatch):
    """§11.93 end-to-end: when the object moves past the diff bbox by
    frame_4, the post-fix crop_b (taken from frame_3) MUST contain
    bright pixels where the object is. Pre-fix crop_b (taken from
    frame_4) would be entirely dark gray."""
    monkeypatch.setenv("GATE_KEEP_DISK_ARTIFACTS", "true")
    out_dir = tmp_path / "frames"
    paths = _make_object_leaves_bbox_frames(out_dir)

    classifier = FakeClassifier([
        _verdict("car", 0.85, "pass_with_hint"),
        _verdict("car", 0.78, "pass_with_hint"),
    ])
    verdict = run(
        frame_paths=paths,
        camera_name="CAM5",
        alert_id="test-6B169-leave",
        output_dir=str(tmp_path),
        classifier=classifier,
    )

    assert verdict.decision == "vehicle"
    assert verdict.crop_b_path is not None

    # Read the crop from disk and confirm it has bright pixels.
    crop = cv2.imread(verdict.crop_b_path)
    assert crop is not None, f"crop_b_path {verdict.crop_b_path} unreadable"
    # Mean brightness of crop should be > 100 — the object is at
    # brightness 220, background is 50. Pre-fix would be ~50 (empty
    # bbox where object already left).
    mean_brightness = float(crop.mean())
    assert mean_brightness > 100, (
        f"crop_b appears empty (mean brightness {mean_brightness:.1f}); "
        f"the bbox was applied to a frame where the object had moved past it"
    )
