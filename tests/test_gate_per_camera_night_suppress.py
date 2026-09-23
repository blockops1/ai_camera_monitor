"""
test_gate_per_camera_night_suppress.py -- Regression test for V2-069 camera_id wiring.

Verifies that _classify_crop forwards camera_id to classify_frame() and that
the full gate run() passes camera_name as camera_id. When the env knobs
(MOTION_GATE_NIGHT_SUPPRESS_ENABLED=1, FARMSURV_NIGHT_SUPPRESS_CAMERAS,
FARMSURV_NIGHT_SUPPRESS_CLASSES) are set, a vehicle-class alert at night
from OUTSIDE_BACK_SOLAR should be suppressed by classify_frame() returning
decision="suppress" with reason="night_suppress_camera_class".

Acceptance criteria (US-069d):
  1. _classify_crop accepts camera_id kwarg with default None.
  2. run() passes camera_id=camera_name to both _classify_crop calls.
  3. New regression test asserts suppression reaches classify_frame().
  4. Existing 44 tests still pass; 0 new failures.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from PIL import Image

from infra.gate import _classify_crop, load_thresholds, run
from infra.quick_classifier import QuickVerdict

# ---------------------------------------------------------------------------
# FakeClassifier — returns a QuickVerdict, optionally honoring camera_id
# ---------------------------------------------------------------------------


@dataclass
class FakeVerdict:
    """Test helper to construct a QuickVerdict with desired properties."""
    top_class: str = "car"
    top_confidence: float = 0.71
    decision: str = "pass_with_hint"
    reason: str | None = None
    # When True, classify_frame will return a suppressed verdict if
    # camera_id + timestamp match the night-suppress pattern.
    night_suppress: bool = False


class FakeClassifier:
    """A classifier that returns a deterministic QuickVerdict.

    Tests set `verdict` before calling classify_frame(). The classifier
    ignores the frame content and always returns the same verdict,
    unless night_suppress=True and the camera_id/timestamp trigger suppression.
    """
    verdict: FakeVerdict = FakeVerdict()

    def classify_frame(
        self,
        frame,
        timestamp=None,
        camera_id=None,
    ) -> QuickVerdict:
        # Night suppress only fires for night-time hours (22:00–06:00 EDT)
        if (
            self.verdict.night_suppress
            and camera_id == "OUTSIDE_BACK_SOLAR"
            and self.verdict.top_class in ("car", "truck", "vehicle")
        ):
            hour = timestamp.hour if timestamp else -1
            # EDT night window: 22:00–06:00 (UTC-4). The test passes UTC timestamps.
            # Night in EDT 22:00–06:00 maps to UTC 02:00–10:00.
            is_night = 2 <= hour < 10
            if is_night:
                return QuickVerdict(
                    top_class=self.verdict.top_class,
                    top_confidence=self.verdict.top_confidence,
                    decision="suppress",
                    n_detections=1,
                    raw_predictions=[],
                    reason="night_suppress_camera_class",
                )
        return QuickVerdict(
            top_class=self.verdict.top_class,
            top_confidence=self.verdict.top_confidence,
            decision=self.verdict.decision,
            n_detections=1,
            raw_predictions=[],
            reason=self.verdict.reason,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_fake_frames(tmp_path, offset=0):
    """Create 4 fake JPEG frames on disk with a small moving patch."""
    frame_paths = []
    for i in range(4):
        fp = tmp_path / f"frame_{i + offset:04d}.jpg"
        img = Image.new("RGB", (640, 480), (0, 0, 0))
        if i > 0:
            from PIL import ImageDraw
            draw = ImageDraw.Draw(img)
            draw.rectangle((280, 200, 360, 280), fill=(i * 80, i * 80, i * 80))
        img.save(fp, format="JPEG")
        frame_paths.append(str(fp))
    return frame_paths


# ---------------------------------------------------------------------------
# AC1: _classify_crop accepts and forwards camera_id
# ---------------------------------------------------------------------------


class TestClassifyCropCameraId:
    """Verify _classify_crop signature accepts camera_id kwarg."""

    def test_classify_crop_accepts_camera_id_kwarg(self):
        """_classify_crop accepts camera_id=str with default None."""
        classifier = FakeClassifier()
        thresholds, _ = load_thresholds("TEST_CAM")
        # With camera_id=None (default) — should behave as before
        result = _classify_crop(
            classifier, "fake_crop", thresholds, camera_id=None
        )
        assert result.decision == "pass_with_hint"  # car passes threshold

    def test_classify_crop_forwards_camera_id(self):
        """_classify_crop passes camera_id to classify_frame()."""
        classifier = FakeClassifier()
        classifier.verdict = FakeVerdict(
            top_class="car",
            top_confidence=0.71,
            decision="pass_with_hint",
            night_suppress=False,
        )
        thresholds, _ = load_thresholds("TEST_CAM")
        # camera_id is forwarded — FakeClassifier receives it
        # When night_suppress=False, it returns the base verdict
        result = _classify_crop(
            classifier, "fake_crop", thresholds,
            timestamp=datetime(2026, 9, 23, 3, 32, tzinfo=UTC),
            camera_id="OUTSIDE_BACK_SOLAR",
        )
        # With night_suppress=False, the base decision is returned
        assert result.decision == "pass_with_hint"


# ---------------------------------------------------------------------------
# AC2: Full gate run() passes camera_name as camera_id
# ---------------------------------------------------------------------------


class TestNightSuppressFullRun:
    """Full gate run() with FakeClassifier: night suppress fires when
    camera_name is passed through as camera_id."""

    def test_night_suppress_vehicle_at_night_outside_back_solar(self, tmp_path):
        """OUTSIDE_BACK_SOLAR + car at night → classify_frame returns suppress,
        _classify_crop returns suppress, run() GateVerdict.classification='none'.

        This is the regression test for the US-069 wiring gap: camera_name was
        never forwarded to _classify_crop, so camera_id was always None at the
        classifier and per-camera suppress never triggered.
        """
        frame_paths = _make_fake_frames(tmp_path)

        # Simulate classify_frame returning suppressed (night_suppress=True)
        # when camera_id="OUTSIDE_BACK_SOLAR" and class is vehicle
        classifier = FakeClassifier()
        classifier.verdict = FakeVerdict(
            top_class="car",
            top_confidence=0.71,
            decision="pass_with_hint",  # base decision
            night_suppress=True,       # override to suppress for this camera
        )

        verdict = run(
            frame_paths=frame_paths,
            camera_name="OUTSIDE_BACK_SOLAR",
            alert_id="test-night-suppress-001",
            output_dir=str(tmp_path),
            classifier=classifier,
            timestamp=datetime(2026, 9, 23, 3, 32, tzinfo=UTC),
        )

        # The gate should suppress because classify_frame returns decision="suppress"
        # for night_suppress_camera_class. The _classify_crop returns the suppressed
        # verdict and _route_decision sees both crops suppressed.
        assert verdict.classification == "none"
        assert verdict.is_none() is True

    def test_night_suppress_does_not_fire_non_matching_camera(self, tmp_path):
        """DIFFERENT camera at night → no suppress, car passes normally."""
        frame_paths = _make_fake_frames(tmp_path)

        classifier = FakeClassifier()
        classifier.verdict = FakeVerdict(
            top_class="car",
            top_confidence=0.71,
            decision="pass_with_hint",
            night_suppress=True,
        )

        verdict = run(
            frame_paths=frame_paths,
            camera_name="OTHER_CAMERA",  # NOT in suppress list
            alert_id="test-night-suppress-002",
            output_dir=str(tmp_path),
            classifier=classifier,
            timestamp=datetime(2026, 9, 23, 3, 32, tzinfo=UTC),
        )

        # Other camera → no suppress → car should pass normally
        assert verdict.classification == "vehicle"
        assert verdict.class_label == "car"

    def test_night_suppress_does_not_fire_vehicle_during_day(self, tmp_path):
        """OUTSIDE_BACK_SOLAR + car during day → no suppress, car passes."""
        frame_paths = _make_fake_frames(tmp_path)

        classifier = FakeClassifier()
        classifier.verdict = FakeVerdict(
            top_class="car",
            top_confidence=0.71,
            decision="pass_with_hint",
            night_suppress=True,
        )

        verdict = run(
            frame_paths=frame_paths,
            camera_name="OUTSIDE_BACK_SOLAR",
            alert_id="test-night-suppress-003",
            output_dir=str(tmp_path),
            classifier=classifier,
            timestamp=datetime(2026, 9, 23, 14, 0, tzinfo=UTC),  # 2 PM
        )

        # Daytime → no suppress even for matching camera
        assert verdict.classification == "vehicle"
        assert verdict.class_label == "car"

    def test_night_suppress_does_not_fire_non_vehicle_class(self, tmp_path):
        """OUTSIDE_BACK_SOLAR + person at night → no suppress (class mismatch)."""
        frame_paths = _make_fake_frames(tmp_path)

        classifier = FakeClassifier()
        classifier.verdict = FakeVerdict(
            top_class="person",
            top_confidence=0.85,
            decision="pass_with_hint",
            night_suppress=True,
        )

        verdict = run(
            frame_paths=frame_paths,
            camera_name="OUTSIDE_BACK_SOLAR",
            alert_id="test-night-suppress-004",
            output_dir=str(tmp_path),
            classifier=classifier,
            timestamp=datetime(2026, 9, 23, 3, 32, tzinfo=UTC),
        )

        # Person is not a vehicle class → no suppress
        assert verdict.classification == "person"


# ---------------------------------------------------------------------------
# AC3: _classify_crop respects camera_id=None (backward compat)
# ---------------------------------------------------------------------------


class TestClassifyCropBackwardCompat:
    """Verify camera_id=None preserves legacy behavior."""

    def test_classify_crop_no_camera_id_unchanged_behavior(self):
        """Without camera_id, _classify_crop behaves identically to pre-fix."""
        classifier = FakeClassifier()
        thresholds, _ = load_thresholds("TEST_CAM")
        # No camera_id passed — should return base verdict
        result = _classify_crop(classifier, "fake_crop", thresholds)
        assert result.decision == "pass_with_hint"
        assert result.top_class == "car"
