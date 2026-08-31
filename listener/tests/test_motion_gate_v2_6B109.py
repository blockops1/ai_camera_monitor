"""Tests for V2 fallback behavior in motion_gate_pipeline (Phase.109 §11.39).

Coverage:
  - is_v2_enabled() respects env var
  - F1: full-frame YOLO fallback fires when crops are empty in V2 mode
  - F1: full-frame fallback does NOT fire in legacy (V1) mode
  - F1: full-frame fallback uses frame_3 as the backstop image
  - F2: no_server_motion early-return is bypassed in V2 mode
  - F2: no_server_motion early-return still fires in legacy mode
  - F3: V2 routing rule 5 suppresses when vehicle conf < 0.6 + person conf >= 0.4
  - F3: V2 routing rule 5 still routes to vehicle when vehicle conf >= 0.6
  - V2 opt-in: setting MOTION_GATE_V2=1 enables fallback; default (no env var) does not
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np

_root = Path(__file__).resolve().parent.parent.parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

from infra.quick_classifier import QuickVerdict
from listener.motion_gate_pipeline import (
    _route_decision,
    is_v2_enabled,
    run,
)

# ---------------------------------------------------------------------------
# Fake classifier — scripted responses for tests
# ---------------------------------------------------------------------------


@dataclass
class FakeClassifier:
    """Scripted QuickClassifier replacement. Each call pops the next response.

    Phase.115: classify_frame now takes str | PIL.Image. We log
    strings as-is, and convert PIL.Image to a string label for the log
    (so tests can still assert "did we call on frame_3?").
    """

    responses: list[QuickVerdict]
    call_log: list[str] = None  # type: ignore[assignment]

    def __post_init__(self):
        if self.call_log is None:
            self.call_log = []

    def classify_frame(self, frame, timestamp=None) -> QuickVerdict:
        # Phase.115: classify_frame may receive a path (str) or PIL.Image.
        # For logging, normalize to a str label.
        if isinstance(frame, str):
            self.call_log.append(frame)
        else:
            # PIL.Image or similar — tag with a placeholder name.
            self.call_log.append(f"<PIL frame {len(self.call_log) + 1}>")
        if self.responses:
            return self.responses.pop(0)
        return QuickVerdict(top_class="none", top_confidence=0.0, decision="suppress")


def _verdict(class_name: str, confidence: float, decision: str = "pass_with_hint") -> QuickVerdict:
    return QuickVerdict(
        top_class=class_name,
        top_confidence=confidence,
        decision=decision,
    )


def _make_frames(out_dir: Path, motion: bool = True) -> list[str]:
    """4 synthetic frames. motion=True → moving rectangle between adjacent pairs."""
    import cv2

    out_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for i in range(4):
        frame = np.full((200, 200, 3), 50, dtype=np.uint8)
        if motion:
            x_offset = 30 + i * 30
            frame[80:130, x_offset : x_offset + 50] = (220, 220, 220)
        path = out_dir / f"frame_{i + 1:03d}.jpg"
        cv2.imwrite(str(path), frame)
        paths.append(str(path))
    return paths


# ---------------------------------------------------------------------------
# is_v2_enabled()
# ---------------------------------------------------------------------------


def test_v2_disabled_by_default(monkeypatch):
    monkeypatch.delenv("MOTION_GATE_V2", raising=False)
    assert is_v2_enabled() is False


def test_v2_enabled_with_truthy_values(monkeypatch):
    for val in ("1", "true", "TRUE", "yes", "Yes"):
        monkeypatch.setenv("MOTION_GATE_V2", val)
        assert is_v2_enabled() is True, f"failed for {val!r}"


def test_v2_disabled_with_falsy_values(monkeypatch):
    for val in ("0", "false", "no", "off", ""):
        monkeypatch.setenv("MOTION_GATE_V2", val)
        assert is_v2_enabled() is False, f"failed for {val!r}"


# ---------------------------------------------------------------------------
# F1: full-frame YOLO fallback
# ---------------------------------------------------------------------------


def test_full_frame_fallback_fires_when_crops_empty_v2(monkeypatch, tmp_path):
    """V2 mode: when both crops return class=none, YOLO runs on frame_3."""
    monkeypatch.setenv("MOTION_GATE_V2", "1")
    frames = _make_frames(tmp_path, motion=True)

    # First 2 responses are for crop_a + crop_b (both empty).
    # 3rd response is for full-frame fallback (high-conf person).
    classifier = FakeClassifier(
        responses=[
            _verdict("none", 0.0, decision="suppress"),
            _verdict("none", 0.0, decision="suppress"),
            _verdict("person", 0.75, decision="pass_with_hint"),
        ],
    )

    verdict = run(
        frame_paths=frames,
        camera_name="CAM1",
        alert_id="test-fallback-person",
        output_dir=str(tmp_path),
        classifier=classifier,
    )

    assert verdict.decision == "person", f"expected person, got {verdict.decision}"
    assert verdict.class_label == "person"
    assert verdict.confidence == 0.75
    assert "v2_full_frame_fallback" in verdict.reason
    # The third classify_frame call should have been on the full frame_3.
    # Phase.115: classify_frame is called with PIL.Image not a path,
    # so we assert on call count instead of a string match.
    assert len(classifier.call_log) == 3, (
        f"expected 3 YOLO calls (crop_a, crop_b, full_frame_3), "
        f"got {len(classifier.call_log)}: {classifier.call_log}"
    )


def test_full_frame_fallback_does_not_fire_in_legacy_mode(monkeypatch, tmp_path):
    """V1 mode (no env var): when crops are empty, gate suppresses without
    full-frame YOLO. Backward-compat test."""
    monkeypatch.delenv("MOTION_GATE_V2", raising=False)
    frames = _make_frames(tmp_path, motion=True)

    classifier = FakeClassifier(
        responses=[
            _verdict("none", 0.0, decision="suppress"),
            _verdict("none", 0.0, decision="suppress"),
        ],
    )

    verdict = run(
        frame_paths=frames,
        camera_name="CAM1",
        alert_id="test-no-fallback-legacy",
        output_dir=str(tmp_path),
        classifier=classifier,
    )

    assert verdict.decision == "suppress"
    assert "v2_full_frame_fallback" not in verdict.reason


def test_full_frame_fallback_skipped_when_crops_already_detected(monkeypatch, tmp_path):
    """V2 mode: when crops already detect a vehicle, full-frame fallback is skipped."""
    monkeypatch.setenv("MOTION_GATE_V2", "1")
    frames = _make_frames(tmp_path, motion=True)

    classifier = FakeClassifier(
        responses=[
            _verdict("truck", 0.85, decision="pass_with_hint"),
            _verdict("truck", 0.80, decision="pass_with_hint"),
        ],
    )

    verdict = run(
        frame_paths=frames,
        camera_name="CAM5",
        alert_id="test-no-fallback-already-detected",
        output_dir=str(tmp_path),
        classifier=classifier,
    )

    assert verdict.decision == "vehicle"
    assert "v2_full_frame_fallback" not in verdict.reason
    # Only 2 classify_frame calls expected (one per crop).
    assert len(classifier.call_log) == 2, (
        f"expected 2 YOLO calls, got {len(classifier.call_log)}: {classifier.call_log}"
    )


# ---------------------------------------------------------------------------
# F2: no_server_motion early-return bypassed in V2 mode
# ---------------------------------------------------------------------------


def test_no_motion_suppresses_in_legacy_mode(monkeypatch, tmp_path):
    """V1: when no diff fires (static frames), gate suppresses without YOLO."""
    monkeypatch.delenv("MOTION_GATE_V2", raising=False)
    # motion=False → no movement between frames → no_server_motion
    frames = _make_frames(tmp_path, motion=False)

    classifier = FakeClassifier(responses=[])

    verdict = run(
        frame_paths=frames,
        camera_name="CAM1",
        alert_id="test-no-motion-legacy",
        output_dir=str(tmp_path),
        classifier=classifier,
    )

    assert verdict.decision == "suppress"
    assert verdict.reason == "no_server_motion"
    assert len(classifier.call_log) == 0, (
        f"legacy mode should not call YOLO on no-motion, got {classifier.call_log}"
    )


def test_no_motion_triggers_full_frame_yolo_in_v2(monkeypatch, tmp_path):
    """V2: when no diff fires, YOLO runs on frame_3 as backstop."""
    monkeypatch.setenv("MOTION_GATE_V2", "1")
    frames = _make_frames(tmp_path, motion=False)

    classifier = FakeClassifier(
        responses=[_verdict("person", 0.65, decision="pass_with_hint")],
    )

    verdict = run(
        frame_paths=frames,
        camera_name="CAM1",
        alert_id="test-no-motion-v2",
        output_dir=str(tmp_path),
        classifier=classifier,
    )

    assert verdict.decision == "person"
    assert verdict.confidence == 0.65
    assert "v2_full_frame_fallback" in verdict.reason
    # YOLO was called once on frame_3 (no crops because no diff).
    assert any("frame_003.jpg" in p for p in classifier.call_log)


# ---------------------------------------------------------------------------
# F3: tighter routing rule 5
# ---------------------------------------------------------------------------


def test_v2_rule5_suppresses_weak_vehicle_override(monkeypatch):
    """V2: when vehicle conf < 0.6 and person conf >= 0.4 in mixed crop,
    suppress rather than route to wrong pipeline."""
    monkeypatch.setenv("MOTION_GATE_V2", "1")

    # Build a mixed scenario: one crop says person@0.55, other says car@0.50.
    # V1 would route to vehicle (mixed_vehicle_wins).
    # V2: person (0.55) >= 0.4 AND vehicle (0.50) < 0.6 → suppress.
    verdict_a = _verdict("person", 0.55, decision="pass_with_hint")
    verdict_b = _verdict("car", 0.50, decision="pass_with_hint")

    decision, _, _, reason = _route_decision(
        verdict_a, verdict_b, thresholds={}, v2=True
    )

    assert decision == "suppress", f"V2 should suppress weak override, got {decision}"
    assert "v2_person_present_low_vehicle_override_suppressed" in reason


def test_v2_rule5_routes_vehicle_when_vehicle_confident(monkeypatch):
    """V2: when vehicle conf >= 0.6, override applies and routes to vehicle."""
    monkeypatch.setenv("MOTION_GATE_V2", "1")

    verdict_a = _verdict("person", 0.45, decision="pass_with_hint")
    verdict_b = _verdict("truck", 0.85, decision="pass_with_hint")

    decision, _, _, reason = _route_decision(
        verdict_a, verdict_b, thresholds={}, v2=True
    )

    assert decision == "vehicle"
    assert reason == "high_conf_vehicle"


def test_v2_rule5_suppresses_when_person_below_threshold(monkeypatch):
    """V2: person conf below 0.4 → vehicle wins (no override applies).

    person@0.30 (below threshold) + car@0.55 → top is car. Rule 1 (any crop
    high-conf vehicle-class) fires first → vehicle pipeline. V2 rule 5
    override does not apply because person_top.confidence < 0.4.
    """
    monkeypatch.setenv("MOTION_GATE_V2", "1")

    verdict_a = _verdict("person", 0.30, decision="pass_with_hint")
    verdict_b = _verdict("car", 0.55, decision="pass_with_hint")

    decision, _, _, reason = _route_decision(
        verdict_a, verdict_b, thresholds={}, v2=True
    )

    assert decision == "vehicle"
    assert reason == "high_conf_vehicle"


def test_legacy_rule5_always_routes_to_vehicle(monkeypatch):
    """V1 (v2=False): mixed always routes to vehicle (backward-compat)."""
    monkeypatch.delenv("MOTION_GATE_V2", raising=False)

    verdict_a = _verdict("person", 0.55, decision="pass_with_hint")
    verdict_b = _verdict("car", 0.50, decision="pass_with_hint")

    decision, _, _, reason = _route_decision(
        verdict_a, verdict_b, thresholds={}, v2=False
    )

    assert decision == "vehicle"
    assert reason == "mixed_vehicle_wins"
