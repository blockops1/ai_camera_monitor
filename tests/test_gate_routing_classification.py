"""
test_gate_routing_classification.py — Wired gate tests for the new classification field.

Tests the classification routing logic introduced in US-025b/c:
  - GateVerdict.classification is Literal['vehicle', 'person', 'animal', 'none']
  - _route_decision() maps QuickVerdict crops to a classification value
  - QuickClassifier._class_to_bucket() maps COCO class_id → bucket

Each test mocks the QuickClassifier dependency (FakeClassifier) — no real
YOLO or llama-server calls.

Acceptance criteria (US-025c):
  1. person at conf 0.85 → classification == 'person'
  2. car at conf 0.91 → classification == 'vehicle'
  3. dog at conf 0.78 → classification == 'animal'
  4. bench at conf 0.92 → classification == 'none'
  5. no detection → classification == 'none'
  6. pytest -v 100% pass; full suite still green.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import pytest
from PIL import Image

from infra.gate import (
    GateVerdict,
    _classify_crop,
    _route_decision,
    load_thresholds,
    run,
)
from infra.quick_classifier import QuickVerdict

# ---------------------------------------------------------------------------
# FakeClassifier — returns a QuickVerdict for any crop, driven by test params
# ---------------------------------------------------------------------------

@dataclass
class FakeVerdict:
    """Test helper to construct a QuickVerdict with desired properties."""
    top_class: str = "person"
    top_confidence: float = 0.85
    decision: str = "pass_with_hint"
    reason: str | None = None


class FakeClassifier:
    """A classifier that returns a deterministic QuickVerdict.

    Tests set `verdict` before calling classify_frame(). The classifier
    ignores the frame content and always returns the same verdict.
    """
    verdict: FakeVerdict = FakeVerdict()

    def classify_frame(self, frame, timestamp: datetime | None = None) -> QuickVerdict:
        return QuickVerdict(
            top_class=self.verdict.top_class,
            top_confidence=self.verdict.top_confidence,
            decision=self.verdict.decision,
            n_detections=1,
            raw_predictions=[],
            reason=self.verdict.reason,
        )


@pytest.fixture(autouse=True)
def _reset_fake_classifier():
    """Reset FakeClassifier.verdict before each test."""
    FakeClassifier.verdict = FakeVerdict()
    yield


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_verdict_a(
    top_class: str = "person",
    top_confidence: float = 0.85,
    decision: str = "pass_with_hint",
    reason: str | None = None,
) -> QuickVerdict:
    """Build a QuickVerdict for crop_a."""
    return QuickVerdict(
        top_class=top_class,
        top_confidence=top_confidence,
        decision=decision,
        reason=reason,
    )


def _make_verdict_b(
    top_class: str = "person",
    top_confidence: float = 0.7,
    decision: str = "pass_with_hint",
    reason: str | None = None,
) -> QuickVerdict:
    """Build a QuickVerdict for crop_b."""
    return QuickVerdict(
        top_class=top_class,
        top_confidence=top_confidence,
        decision=decision,
        reason=reason,
    )


# ---------------------------------------------------------------------------
# AC1: Five classification routing cases
# ---------------------------------------------------------------------------

class TestClassificationRouting:
    """Five wired cases for the classification field (US-025c AC1)."""

    # ------------------------------------------------------------------ AC1a
    def test_ac1a_person_at_0_85_routes_to_person(self):
        """YOLO sees a person at top_conf 0.85 → classification == 'person'."""
        FakeClassifier.verdict = FakeVerdict(
            top_class="person", top_confidence=0.85, decision="pass_with_hint"
        )
        classifier = FakeClassifier()
        thresholds, _ = load_thresholds("TEST_CAM")
        verdict_a = classifier.classify_frame("fake_crop_a")
        verdict_b = _make_verdict_b(top_class="person", top_confidence=0.70)
        classification, _, _, _ = _route_decision(verdict_a, verdict_b, thresholds)
        assert classification == "person"

    # ------------------------------------------------------------------ AC1b
    def test_ac1b_car_at_0_91_routes_to_vehicle(self):
        """YOLO sees a car at top_conf 0.91 → classification == 'vehicle'."""
        FakeClassifier.verdict = FakeVerdict(
            top_class="car", top_confidence=0.91, decision="pass_with_hint"
        )
        classifier = FakeClassifier()
        thresholds, _ = load_thresholds("TEST_CAM")
        verdict_a = classifier.classify_frame("fake_crop_a")
        verdict_b = _make_verdict_b(top_class="car", top_confidence=0.80)
        classification, class_label, _, _ = _route_decision(verdict_a, verdict_b, thresholds)
        assert classification == "vehicle"
        assert class_label == "car"

    # ------------------------------------------------------------------ AC1c
    def test_ac1c_dog_at_0_78_routes_to_animal(self):
        """YOLO sees a dog at top_conf 0.78 → classification == 'animal'."""
        FakeClassifier.verdict = FakeVerdict(
            top_class="dog", top_confidence=0.78, decision="pass_with_hint"
        )
        classifier = FakeClassifier()
        thresholds, _ = load_thresholds("TEST_CAM")
        verdict_a = classifier.classify_frame("fake_crop_a")
        verdict_b = _make_verdict_b(top_class="dog", top_confidence=0.65)
        classification, class_label, _, _ = _route_decision(verdict_a, verdict_b, thresholds)
        assert classification == "animal"
        assert class_label == "dog"

    # ------------------------------------------------------------------ AC1d
    def test_ac1d_bench_at_0_92_routes_to_none(self):
        """YOLO sees a bench at top_conf 0.92 → classification == 'none'."""
        FakeClassifier.verdict = FakeVerdict(
            top_class="bench", top_confidence=0.92, decision="pass",
        )
        classifier = FakeClassifier()
        thresholds, _ = load_thresholds("TEST_CAM")
        verdict_a = classifier.classify_frame("fake_crop_a")
        verdict_b = _make_verdict_b(top_class="bench", top_confidence=0.80, decision="pass")
        classification, _class_label, _, reason = _route_decision(
            verdict_a, verdict_b, thresholds
        )
        assert classification == "none"
        # The reason should name the non-vehicle class
        assert "bench" in reason

    # ------------------------------------------------------------------ AC1e
    def test_ac1e_no_detection_routes_to_none(self):
        """YOLO finds nothing → classification == 'none'."""
        thresholds, _ = load_thresholds("TEST_CAM")
        # Both verdicts suppressed (no detection)
        verdict_a = QuickVerdict(
            top_class="none", top_confidence=0.0, decision="suppress",
            reason="no_object_detected",
        )
        verdict_b = QuickVerdict(
            top_class="none", top_confidence=0.0, decision="suppress",
            reason="no_object_detected",
        )
        classification, class_label, confidence, reason = _route_decision(
            verdict_a, verdict_b, thresholds
        )
        assert classification == "none"
        assert class_label is None
        assert confidence == 0.0
        assert reason == "no_object_detected"


# ---------------------------------------------------------------------------
# Additional classification edge cases
# ---------------------------------------------------------------------------

class TestClassificationEdgeCases:
    """Additional edge cases to ensure classification coverage is thorough."""

    def test_single_crop_animal_with_empty_other(self):
        """Non-V2: one animal crop, other suppressed → animal (rule 3: ANY crop)."""
        FakeClassifier.verdict = FakeVerdict(
            top_class="cat", top_confidence=0.80, decision="pass_with_hint"
        )
        classifier = FakeClassifier()
        thresholds, _ = load_thresholds("TEST_CAM")
        verdict_a = classifier.classify_frame("fake_crop_a")
        # Second crop suppressed (empty/none)
        verdict_b = QuickVerdict(
            top_class="none", top_confidence=0.0, decision="suppress",
        )
        classification, _, _, _ = _route_decision(verdict_a, verdict_b, thresholds)
        # Rule 3: ANY crop high-conf animal → animal (non-V2 mode)
        assert classification == "animal"

    def test_high_conf_non_vehicle_no_vehicle_routes_to_none(self):
        """High-conf non-vehicle class without vehicle → 'none' not 'vehicle'."""
        FakeClassifier.verdict = FakeVerdict(
            top_class="teddy bear", top_confidence=0.95, decision="pass",
        )
        classifier = FakeClassifier()
        thresholds, _ = load_thresholds("TEST_CAM")
        verdict_a = classifier.classify_frame("fake_crop_a")
        verdict_b = _make_verdict_b(top_class="teddy bear", top_confidence=0.88, decision="pass")
        classification, _, _, reason = _route_decision(verdict_a, verdict_b, thresholds)
        assert classification == "none"
        assert "teddy" in reason and "not_in_subjects" in reason

    def test_low_conf_person_suppressed_routes_to_none(self):
        """Both crops low-confidence person → 'none'."""
        thresholds, _ = load_thresholds("TEST_CAM")
        verdict_a = _make_verdict_a(top_class="person", top_confidence=0.20, decision="suppress")
        verdict_b = _make_verdict_b(top_class="person", top_confidence=0.25, decision="suppress")
        classification, _, _, _ = _route_decision(verdict_a, verdict_b, thresholds)
        assert classification == "none"

    def test_both_crops_vehicle_routes_to_vehicle(self):
        """Two vehicle crops → classification == 'vehicle'."""
        FakeClassifier.verdict = FakeVerdict(
            top_class="truck", top_confidence=0.90, decision="pass_with_hint"
        )
        thresholds, _ = load_thresholds("TEST_CAM")
        verdict_a = FakeClassifier().classify_frame("fake_crop_a")
        FakeClassifier.verdict = FakeVerdict(
            top_class="car", top_confidence=0.85, decision="pass_with_hint"
        )
        verdict_b = FakeClassifier().classify_frame("fake_crop_b")
        classification, _, _, _ = _route_decision(verdict_a, verdict_b, thresholds)
        assert classification == "vehicle"


# ---------------------------------------------------------------------------
# AC2: Classification field on GateVerdict
# ---------------------------------------------------------------------------

class TestGateVerdictClassificationField:
    """Verify GateVerdict.dataclass has the classification field and is None."""

    def test_gate_verdict_has_classification_field(self):
        """GateVerdict must have a classification attribute."""
        g = GateVerdict(
            classification="vehicle",
            class_label="car",
            confidence=0.9,
            top_class="car",
            top_confidence=0.9,
        )
        assert hasattr(g, "classification")
        assert g.classification == "vehicle"

    def test_gate_verdict_classification_is_none_for_none(self):
        """GateVerdict.classification == 'none' → is_none() is True."""
        g = GateVerdict(
            classification="none",
            class_label=None,
            confidence=0.0,
            top_class="",
            top_confidence=0.0,
        )
        assert g.is_none() is True

    def test_gate_verdict_classification_vehicle_is_not_none(self):
        """GateVerdict.classification == 'vehicle' → is_none() is False."""
        g = GateVerdict(
            classification="vehicle",
            class_label="car",
            confidence=0.9,
            top_class="car",
            top_confidence=0.9,
        )
        assert g.is_none() is False

    def test_gate_verdict_classification_animal_is_not_none(self):
        """GateVerdict.classification == 'animal' → is_pass is True."""
        g = GateVerdict(
            classification="animal",
            class_label="dog",
            confidence=0.78,
            top_class="dog",
            top_confidence=0.78,
        )
        assert g.is_pass is True


# ---------------------------------------------------------------------------
# AC2 (continued): _classify_crop respects per-class thresholds
# ---------------------------------------------------------------------------

class TestClassifyCropThresholds:
    """Test that _classify_crop correctly applies per-class thresholds."""

    def test_classify_crop_below_threshold_suppressed(self):
        """Crop with conf below threshold → decision='suppress'."""
        classifier = FakeClassifier()
        FakeClassifier.verdict = FakeVerdict(
            top_class="dog", top_confidence=0.50, decision="pass_with_hint",
        )
        thresholds = {"dog": 0.60}  # dog threshold is 0.60, conf is 0.50
        result = _classify_crop(classifier, "fake_crop", thresholds)
        assert result.decision == "suppress"
        assert result.reason == "class_below_threshold"

    def test_classify_crop_above_threshold_passes(self):
        """Crop with conf above threshold → decision='pass_with_hint'."""
        classifier = FakeClassifier()
        FakeClassifier.verdict = FakeVerdict(
            top_class="dog", top_confidence=0.70, decision="pass_with_hint",
        )
        thresholds = {"dog": 0.60}
        result = _classify_crop(classifier, "fake_crop", thresholds)
        assert result.decision == "pass_with_hint"


# ---------------------------------------------------------------------------
# Full gate run with FakeClassifier
# ---------------------------------------------------------------------------

def _make_tiny_jpeg(path, size=(64, 64), color=(100, 100, 100)):
    """Write a tiny valid JPEG image to `path`."""
    img = Image.new("RGB", size, color)
    img.save(path, format="JPEG")
    return str(path)


def _make_fake_frames(tmp_path, offset=0, base_color=(0, 0, 0)):
    """Create 4 fake JPEG frames on disk. Each frame differs enough for diff to detect motion.

    Uses a 640x480 size so cv2 diff works reliably. Frame 0 is all-black;
    frames 1-3 shift the entire image to bright colors so pairwise diff
    finds motion bboxes for the AND-subject logic.
    """
    frame_paths = []
    for i in range(4):
        fp = tmp_path / f"frame_{i + offset:04d}.jpg"
        # Frame 0 = black; frames 1-3 = progressively brighter
        # to guarantee diff detects motion (threshold=25).
        color = (i * 60, i * 60, i * 60)
        _make_tiny_jpeg(fp, size=(640, 480), color=color)
        frame_paths.append(str(fp))
    return frame_paths


class TestGateRunWithFakeClassifier:
    """Test the full gate run() using FakeClassifier to validate end-to-end."""

    def test_full_gate_run_person_classification(self, tmp_path):
        """Full gate run: person detected → GateVerdict.classification == 'person'."""
        frame_paths = _make_fake_frames(tmp_path)

        FakeClassifier.verdict = FakeVerdict(
            top_class="person", top_confidence=0.85, decision="pass_with_hint",
        )
        classifier = FakeClassifier()

        verdict = run(
            frame_paths=frame_paths,
            camera_name="TEST_CAM",
            alert_id="test-alert-001",
            output_dir=str(tmp_path),
            classifier=classifier,
        )

        assert verdict.classification == "person"
        assert verdict.class_label == "person"
        assert verdict.confidence == 0.85
        assert verdict.top_class == "person"
        assert verdict.is_none() is False

    def test_full_gate_run_animal_classification(self, tmp_path):
        """Full gate run: dog detected → GateVerdict.classification == 'animal'."""
        frame_paths = _make_fake_frames(tmp_path)

        FakeClassifier.verdict = FakeVerdict(
            top_class="dog", top_confidence=0.78, decision="pass_with_hint",
        )
        classifier = FakeClassifier()

        verdict = run(
            frame_paths=frame_paths,
            camera_name="TEST_CAM",
            alert_id="test-alert-002",
            output_dir=str(tmp_path),
            classifier=classifier,
        )

        assert verdict.classification == "animal"
        assert verdict.class_label == "dog"
        assert verdict.is_none() is False

    def test_full_gate_run_vehicle_classification(self, tmp_path):
        """Full gate run: car detected → GateVerdict.classification == 'vehicle'."""
        frame_paths = _make_fake_frames(tmp_path)

        FakeClassifier.verdict = FakeVerdict(
            top_class="car", top_confidence=0.91, decision="pass_with_hint",
        )
        classifier = FakeClassifier()

        verdict = run(
            frame_paths=frame_paths,
            camera_name="TEST_CAM",
            alert_id="test-alert-003",
            output_dir=str(tmp_path),
            classifier=classifier,
        )

        assert verdict.classification == "vehicle"
        assert verdict.class_label == "car"
        assert verdict.is_none() is False

    def test_full_gate_run_no_motion_suppress(self, tmp_path):
        """Full gate run: identical frames → no motion → classification='none'."""
        # All frames identical → diff sees no motion
        frame_paths = []
        for i in range(4):
            fp = tmp_path / f"frame_{i:04d}.jpg"
            _make_tiny_jpeg(fp, size=(640, 480), color=(128, 128, 128))
            frame_paths.append(str(fp))

        FakeClassifier.verdict = FakeVerdict(
            top_class="none", top_confidence=0.0, decision="suppress",
        )
        classifier = FakeClassifier()

        verdict = run(
            frame_paths=frame_paths,
            camera_name="TEST_CAM",
            alert_id="test-alert-004",
            output_dir=str(tmp_path),
            classifier=classifier,
        )

        assert verdict.classification == "none"
        assert "no_server_motion" in verdict.reason


# ---------------------------------------------------------------------------
# _class_to_bucket helper
# ---------------------------------------------------------------------------

class TestClassToBucket:
    """Test _class_to_bucket maps COCO class_id → correct bucket."""

    def test_person_class_id_maps_to_person(self):
        """COCO class_id for 'person' (index 0) → 'person'."""
        from infra.quick_classifier import _class_to_bucket
        assert _class_to_bucket(0) == "person"

    def test_car_class_id_maps_to_vehicle(self):
        """COCO class_id for 'car' (index 2) → 'vehicle'."""
        from infra.quick_classifier import _class_to_bucket
        assert _class_to_bucket(2) == "vehicle"

    def test_dog_class_id_maps_to_animal(self):
        """COCO class_id for 'dog' (index 17) → 'animal'."""
        from infra.quick_classifier import _class_to_bucket
        assert _class_to_bucket(17) == "animal"

    def test_bench_class_id_maps_to_none(self):
        """COCO class_id for 'bench' (index 13) → 'none'."""
        from infra.quick_classifier import _class_to_bucket
        assert _class_to_bucket(13) == "none"

    def test_unknown_class_id_maps_to_none(self):
        """COCO class_id out of range → 'none'."""
        from infra.quick_classifier import _class_to_bucket
        assert _class_to_bucket(999) == "none"
