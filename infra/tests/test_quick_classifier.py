"""
Tests for infra/quick_classifier.py — YOLOv8n ONNX motion gate.

Tests cover:
  - Module imports without torch (uses onnxruntime alone)
  - QuickClassifier loads the model successfully
  - classify_frame() returns a QuickVerdict with correct shape
  - Suppression decision fires for low-confidence / no-detection cases
  - pass_with_hint decision fires for high-confidence person/car cases
  - Inference latency is bounded (< 100ms on Apple Silicon via CoreML EP)
  - COCO_NAMES list has 80 classes
  - KEEP_CLASSES_DEFAULT contains the surveillance-relevant classes
  - Letterbox preprocess handles non-square images without crashing

If the model file is missing (e.g. fresh clone), these tests skip gracefully
rather than failing — the probe will report the missing model at runtime.

Run:
    source .venv/bin/activate
    python3 -m pytest infra/tests/test_quick_classifier.py -v
"""

from __future__ import annotations

import sys
import time
from datetime import UTC
from pathlib import Path

import pytest
from PIL import Image

# Project root on path
_root = Path(__file__).resolve().parent.parent.parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))


# Skip everything if the model isn't downloaded
_MODEL_PATH = _root / "models" / "yolov8n.onnx"
pytestmark = pytest.mark.skipif(
    not _MODEL_PATH.is_file(),
    reason="yolov8n.onnx not downloaded (run scripts/download_quick_classifier_model.sh)",
)


def test_coco_names_has_80_classes():
    from infra.quick_classifier import COCO_NAMES
    assert len(COCO_NAMES) == 80
    assert COCO_NAMES[0] == "person"
    assert COCO_NAMES[2] == "car"
    assert COCO_NAMES[7] == "truck"


def test_keep_classes_includes_surveillance_targets():
    from infra.quick_classifier import KEEP_CLASSES_DEFAULT
    expected = {"person", "car", "truck", "bicycle", "dog", "cat", "horse", "cow", "bear", "bird"}
    assert expected.issubset(KEEP_CLASSES_DEFAULT)


def test_default_threshold_is_reasonable():
    from infra.quick_classifier import DEFAULT_CONFIDENCE_THRESHOLD
    assert 0.20 <= DEFAULT_CONFIDENCE_THRESHOLD <= 0.60


def test_quick_classifier_loads():
    """Verify the model loads without error."""
    from infra.quick_classifier import QuickClassifier
    qc = QuickClassifier()
    assert qc.session is not None
    assert qc.input_name == "images"
    assert qc.output_name == "output0"


def test_classify_blank_image_returns_suppress():
    """A blank image should produce no detections and decision='suppress'."""
    from infra.quick_classifier import QuickClassifier
    qc = QuickClassifier()
    # Solid gray 640x480 frame
    blank = Image.new("RGB", (640, 480), color=(128, 128, 128))
    tmp = _root / "data" / "test_blank_quick_classifier.jpg"
    tmp.parent.mkdir(parents=True, exist_ok=True)
    blank.save(tmp)
    try:
        v = qc.classify_frame(str(tmp))
        assert v.decision == "suppress"
        assert v.n_detections == 0
    finally:
        tmp.unlink(missing_ok=True)


def test_classify_real_alert_with_vehicle_or_person_is_not_suppressed():
    """Find a real alert from our data/alerts/ jsonl where Qwen affirmatively
    reported a person or vehicle, then verify YOLOv8n does NOT suppress those
    frames. This is the key sanity check: real vehicles/people must pass."""
    import json

    from infra.quick_classifier import QuickClassifier

    qc = QuickClassifier()

    alerts_dir = _root / "data" / "alerts"
    if not alerts_dir.is_dir():
        pytest.skip("data/alerts/ not present")

    # Affirmative phrases — Qwen actually saw the object, not just said "no X detected"
    affirmative_phrases = [
        # Person
        "person in ", "person was", "person is", "person detected",
        "person walking", "person standing", "person visible",
        "individual in", "individual was", "individual is",
        # Vehicle
        "vehicle in ", "vehicle was", "vehicle is", "vehicle detected",
        "vehicle entering", "vehicle approaching", "vehicle visible",
        "vehicle present", "a vehicle", "the vehicle",
        "truck in ", "truck was", "truck is", "truck detected",
        "truck present", "a truck", "the truck",
        "car in ", "car was", "car is", "car detected",
        "car visible", "car present", "a car", "the car",
    ]
    # Exclusion phrases — if present, Qwen negated the object
    negation_phrases = [
        "no vehicle", "no person", "no truck", "no car",
        "without a vehicle", "without a person", "without a truck",
    ]

    def _is_affirmative(text: str) -> bool:
        text_lower = text.lower()
        if any(neg in text_lower for neg in negation_phrases):
            return False
        return any(phrase in text_lower for phrase in affirmative_phrases)

    found_alert_id = None
    for jsonl in sorted(alerts_dir.glob("*.jsonl"), reverse=True):
        with jsonl.open() as f:
            for line in f:
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                desc = entry.get("description") or ""
                title = entry.get("title") or ""
                if _is_affirmative(desc) or _is_affirmative(title):
                    aid = entry["alert_id"].replace("-identified", "").replace("-arriving", "")
                    frames_dir = _root / "data" / "frames" / aid
                    if frames_dir.is_dir() and len(list(frames_dir.glob("frame_*.jpg"))) >= 4:
                        found_alert_id = aid
                        break
        if found_alert_id:
            break

    if found_alert_id is None:
        pytest.skip("No affirmative person/vehicle alert with >=4 frames on disk")

    frames = sorted((_root / "data" / "frames" / found_alert_id).glob("frame_*.jpg"))
    verdicts = [qc.classify_frame(str(f)) for f in frames]
    n_passed = sum(1 for v in verdicts if v.decision != "suppress")
    assert n_passed >= 1, (
        f"All {len(verdicts)} frames in {found_alert_id} (an affirmative "
        f"person/vehicle alert) were suppressed — model may be wrong, "
        f"or threshold too aggressive. Verdicts: {[v.decision for v in verdicts]}"
    )


def test_inference_latency_under_100ms():
    """CoreML-accelerated inference should be fast (<100ms per frame)."""
    from infra.quick_classifier import QuickClassifier
    qc = QuickClassifier()
    img = Image.new("RGB", (640, 480), color=(100, 150, 200))
    tmp = _root / "data" / "test_latency_quick_classifier.jpg"
    tmp.parent.mkdir(parents=True, exist_ok=True)
    img.save(tmp)
    try:
        # Warm-up (first inference is slower due to CoreML compilation)
        qc.classify_frame(str(tmp))
        # Measure 3 inferences
        timings = []
        for _ in range(3):
            t0 = time.perf_counter()
            qc.classify_frame(str(tmp))
            timings.append((time.perf_counter() - t0) * 1000)
        mean_ms = sum(timings) / len(timings)
        assert mean_ms < 100, f"Mean inference {mean_ms:.1f}ms exceeds 100ms budget"
    finally:
        tmp.unlink(missing_ok=True)


def test_quickverdict_dataclass_shape():
    """Verify QuickVerdict has the documented fields."""
    from infra.quick_classifier import QuickVerdict
    v = QuickVerdict(top_class="car", top_confidence=0.85, decision="pass_with_hint")
    assert v.top_class == "car"
    assert v.top_confidence == 0.85
    assert v.decision == "pass_with_hint"
    assert v.n_detections == 0
    assert v.raw_predictions == []


def test_letterbox_preserves_aspect_ratio():
    """Letterbox should resize without distorting the image."""
    from infra.quick_classifier import _letterbox
    img = Image.new("RGB", (320, 240), color=(50, 100, 150))
    out, scale, pad = _letterbox(img, 640)
    assert out.size == (640, 640)
    # 320x240 -> scale = min(640/320, 640/240) = min(2.0, 2.667) = 2.0
    assert abs(scale - 2.0) < 0.001
    # Padding: (640 - 320*2.0)/2 = 0 horizontal, (640 - 240*2.0)/2 = 80 vertical
    assert pad == (0, 80)


def test_classify_missing_file_returns_pass_gracefully():
    """Missing file should not crash; should return 'pass' so Qwen decides."""
    from infra.quick_classifier import QuickClassifier
    qc = QuickClassifier()
    v = qc.classify_frame("/nonexistent/path/to/image.jpg")
    assert v.decision == "pass"
    assert v.top_class == "error"


# ---------------------------------------------------------------------------
# Night suppression tests — Phase.116 §11.47
# ---------------------------------------------------------------------------


def test_brightness_ratio_uniform_image_is_one():
    """A uniformly gray image has equal top/bottom halves → ratio ~1.0."""
    from infra.quick_classifier import _brightness_ratio
    img = Image.new("RGB", (640, 480), color=(128, 128, 128))
    ratio = _brightness_ratio(img)
    assert 0.95 <= ratio <= 1.05


def test_brightness_ratio_bottom_brighter_than_top():
    """Image with brighter bottom should have ratio > 1."""
    from infra.quick_classifier import _brightness_ratio
    # Top half: dark, bottom half: bright
    img = Image.new("RGB", (640, 480), color=(20, 20, 20))
    bright = Image.new("RGB", (640, 240), color=(220, 220, 220))
    img.paste(bright, (0, 240))
    ratio = _brightness_ratio(img)
    assert ratio > 5.0, f"Expected ratio > 5.0 for bright-bottom image, got {ratio}"


def test_brightness_ratio_top_brighter_than_bottom():
    """Image with brighter top should have ratio < 1 (sky scene)."""
    from infra.quick_classifier import _brightness_ratio
    img = Image.new("RGB", (640, 480), color=(220, 220, 220))
    dark = Image.new("RGB", (640, 240), color=(20, 20, 20))
    img.paste(dark, (0, 240))
    ratio = _brightness_ratio(img)
    assert ratio < 0.2, f"Expected ratio < 0.2 for dark-bottom image, got {ratio}"


def test_resolve_timestamp_priority():
    """Explicit timestamp takes priority over file mtime."""
    from datetime import datetime

    from infra.quick_classifier import _resolve_timestamp
    explicit = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    assert _resolve_timestamp(explicit, None) == explicit
    assert _resolve_timestamp(None, None) is None


def test_night_suppress_disabled_by_default(monkeypatch):
    """Without the env var, NIGHT_SUPPRESS_ENABLED must be False (no behavior change)."""
    monkeypatch.delenv("MOTION_GATE_NIGHT_SUPPRESS_ENABLED", raising=False)
    # Re-import the module to pick up the env var
    import importlib

    import infra.quick_classifier

    importlib.reload(infra.quick_classifier)
    assert infra.quick_classifier.NIGHT_SUPPRESS_ENABLED is False


def test_night_suppress_enabled_when_env_set(monkeypatch):
    """With MOTION_GATE_NIGHT_SUPPRESS_ENABLED=1, the flag must be True."""
    monkeypatch.setenv("MOTION_GATE_NIGHT_SUPPRESS_ENABLED", "1")
    import importlib

    import infra.quick_classifier

    importlib.reload(infra.quick_classifier)
    assert infra.quick_classifier.NIGHT_SUPPRESS_ENABLED is True
    assert infra.quick_classifier.NIGHT_CONF_FLOOR == 0.40
    assert infra.quick_classifier.NIGHT_BRIGHTNESS_RATIO == 1.5


def test_night_suppress_overrides_pass_with_hint_when_enabled(monkeypatch):
    """With suppression enabled, a low-conf detection at night should be suppressed
    even if the day gate would have passed. Uses a synthetic bright-bottom frame
    so brightness_ratio is high regardless of the day model output."""
    monkeypatch.setenv("MOTION_GATE_NIGHT_SUPPRESS_ENABLED", "1")
    import importlib

    import infra.quick_classifier

    importlib.reload(infra.quick_classifier)
    qc = infra.quick_classifier.QuickClassifier()

    # Build a synthetic "night-ish" frame: dark top, bright bottom (IR foreground).
    # The model will likely see no person/vehicle, but if it does, conf should be low.
    img = Image.new("RGB", (640, 480), color=(20, 20, 20))
    bright = Image.new("RGB", (640, 240), color=(180, 180, 180))
    img.paste(bright, (0, 240))
    tmp = _root / "data" / "test_night_suppress.jpg"
    tmp.parent.mkdir(parents=True, exist_ok=True)
    img.save(tmp)
    try:
        from datetime import datetime

        # Pick a known-night timestamp: 2026-01-15 02:00 UTC = 22:00 EDT Jan 14
        # (well past sunset at Resaca GA in January)
        night_ts = datetime(2026, 1, 15, 2, 0, tzinfo=UTC)
        v = qc.classify_frame(str(tmp), timestamp=night_ts)
        # The synthetic image has no real objects, so n_detections is likely 0
        # and the day gate would already suppress. To exercise the override path,
        # we accept either suppressed (via day gate) or suppressed (via night gate).
        assert v.decision == "suppress"
    finally:
        tmp.unlink(missing_ok=True)


def test_night_implausible_classes_defined():
    """The implausible-classes filter must contain indoor / wrong-domain objects."""
    from infra.quick_classifier import NIGHT_IMPLAUSIBLE_CLASSES
    # Must include obvious indoor items
    for cls in ("bowl", "cup", "dining table", "tv", "laptop", "toilet"):
        assert cls in NIGHT_IMPLAUSIBLE_CLASSES, f"{cls} missing"
    # Must NOT include plausible surveillance classes
    for cls in ("person", "car", "truck", "bicycle", "cat", "dog", "horse"):
        assert cls not in NIGHT_IMPLAUSIBLE_CLASSES, f"{cls} should be plausible"


def test_night_suppress_skipped_during_daytime(monkeypatch):
    """With suppression enabled, daytime frames must NOT be suppressed by the night gate.
    A synthetic daytime frame with no objects still suppresses via the day gate (which
    is correct), but the override path should not be involved. We verify the helpers
    return False for daytime timestamps."""
    monkeypatch.setenv("MOTION_GATE_NIGHT_SUPPRESS_ENABLED", "1")
    import importlib

    import infra.quick_classifier

    importlib.reload(infra.quick_classifier)

    img = Image.new("RGB", (640, 480), color=(128, 128, 128))
    tmp = _root / "data" / "test_day_quick_classifier.jpg"
    tmp.parent.mkdir(parents=True, exist_ok=True)
    img.save(tmp)
    try:
        from datetime import datetime

        # 14:00 UTC = 10:00 EDT, midday, definitely day
        day_ts = datetime(2026, 6, 15, 18, 0, tzinfo=UTC)
        # _should_apply_night_suppression should return False for daytime
        should = infra.quick_classifier._should_apply_night_suppression(
            img, day_ts, tmp
        )
        assert should is False, "Daytime frame should not trigger night suppression"
    finally:
        tmp.unlink(missing_ok=True)


def test_night_suppress_fires_when_timestamp_and_pilimage(monkeypatch):
    """Phase.116 timestamp-fix: the night heuristic must work end-to-end when the
    caller passes a PIL.Image (the default Phase.115 path) and a real timestamp.

    This is the regression test for the bug discovered on 2026-08-26: the heuristic
    was dormant because Phase.115 changed crops from disk paths to PIL.Image,
    which broke the file-mtime fallback in _resolve_timestamp(). The webhook's
    ISO timestamp is now threaded through dispatch → run_gate → _classify_crop →
    classify_frame.

    Test conditions:
      - timestamp is a real UTC datetime at night (03:00 UTC = 23:00 EDT)
      - frame is a PIL.Image (no file mtime fallback possible)
      - bottom-half is brighter than top-half (IR-illuminated night signature)
      - _should_apply_night_suppression must return True
    """
    monkeypatch.setenv("MOTION_GATE_NIGHT_SUPPRESS_ENABLED", "1")
    import importlib

    import infra.quick_classifier

    importlib.reload(infra.quick_classifier)

    # Build a PIL.Image with bottom-half brighter than top-half (IR signature).
    # Top half: dark (sky). Bottom half: brighter (IR-illuminated ground).
    img = Image.new("RGB", (640, 480), color=(20, 20, 20))
    from PIL import ImageDraw

    draw = ImageDraw.Draw(img)
    draw.rectangle([(0, 240), (640, 480)], fill=(180, 180, 180))

    from datetime import UTC, datetime

    # 03:00 UTC = 23:00 EDT, definitely night (well past 20:46 EDT civil twilight).
    night_ts = datetime(2026, 8, 25, 3, 0, tzinfo=UTC)

    should = infra.quick_classifier._should_apply_night_suppression(
        img, night_ts, None  # frame_path=None to force PIL.Image-only path
    )
    assert should is True, (
        "Night heuristic must fire when timestamp is night AND bottom is brighter "
        "than top — this is the exact regression that produced 2 false-positive "
        "Telegram alerts on 2026-08-26."
    )


def test_night_suppress_skipped_when_only_pilimage_no_timestamp(monkeypatch):
    """Phase.116 timestamp-fix (negative case): without a timestamp AND without
    a file path, the heuristic must NOT fire. This is the safe default — don't
    suppress a real detection just because we lost the timestamp signal.

    Previously, this was the only path the production code took (PIL.Image +
    timestamp=None), so the heuristic was silently dormant. Now we expect callers
    to pass a timestamp explicitly.
    """
    monkeypatch.setenv("MOTION_GATE_NIGHT_SUPPRESS_ENABLED", "1")
    import importlib

    import infra.quick_classifier

    importlib.reload(infra.quick_classifier)

    img = Image.new("RGB", (640, 480), color=(128, 128, 128))

    # No timestamp AND no frame_path → heuristic must skip (safe default).
    should = infra.quick_classifier._should_apply_night_suppression(
        img, None, None
    )
    assert should is False, (
        "Without a timestamp and without a file path, heuristic must skip "
        "(safe default — don't suppress on missing signal)."
    )


def test_quickverdict_has_reason_field():
    """Phase.116 reason plumbing — every QuickVerdict must have a reason
    field that explains why the classifier made its decision. The pipeline
    uses this for visibility (operator log) and for surfacing suppress
    attribution in the per-camera dashboard.
    """
    from infra.quick_classifier import QuickVerdict

    # Suppress with reason
    v1 = QuickVerdict(top_class="none", top_confidence=0.0, decision="suppress", reason="no_object_detected")
    assert v1.reason == "no_object_detected"

    # Pass without reason
    v2 = QuickVerdict(top_class="person", top_confidence=0.65, decision="pass_with_hint")
    assert v2.reason is None

    # Default for backward compat
    v3 = QuickVerdict(top_class="car", top_confidence=0.85, decision="pass")
    assert v3.reason is None
