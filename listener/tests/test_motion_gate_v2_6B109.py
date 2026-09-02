"""Tests for V2 fallback behavior in motion_gate_pipeline (Phase 6B.109 §11.39).

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

    Phase 6B.115: classify_frame now takes str | PIL.Image. We log
    strings as-is, and convert PIL.Image to a string label for the log
    (so tests can still assert "did we call on frame_3?").
    """

    responses: list[QuickVerdict]
    call_log: list[str] = None  # type: ignore[assignment]

    def __post_init__(self):
        if self.call_log is None:
            self.call_log = []

    def classify_frame(self, frame, timestamp=None) -> QuickVerdict:
        # Phase 6B.115: classify_frame may receive a path (str) or PIL.Image.
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
# F1: REMOVED in Phase 6B.171 STRICT commit. V2's full-frame YOLO
# fallback is gone — both V1 and V2 suppress when crops are empty.
# test_full_frame_fallback_fires_when_crops_empty_v2 is gone (was here).
# test_full_frame_fallback_does_not_fire_in_legacy_mode and
# test_full_frame_fallback_skipped_when_crops_already_detected below
# still assert "v2_full_frame_fallback" NOT in reason — that string is
# never added now, so the assertions still hold.
# ---------------------------------------------------------------------------


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
# F2: no_server_motion — both V1 and V2 suppress with no fallback
# (Phase 6B.171 STRICT commit removed V2's full-frame YOLO backstop)
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


def test_no_motion_suppresses_in_v2_no_fallback(monkeypatch, tmp_path):
    """V2 (Phase 6B.171 STRICT): when no diff fires, gate suppresses — no
    V2 full-frame YOLO backstop. maintainer 2026-09-01: 'I don't want a V2
    mode fall back if the differential boxes show that there's no actual
    differential motion.'
    """
    monkeypatch.setenv("MOTION_GATE_V2", "1")
    frames = _make_frames(tmp_path, motion=False)

    classifier = FakeClassifier(responses=[])

    verdict = run(
        frame_paths=frames,
        camera_name="CAM1",
        alert_id="test-no-motion-v2",
        output_dir=str(tmp_path),
        classifier=classifier,
    )

    assert verdict.decision == "suppress"
    assert verdict.reason == "no_server_motion"
    # YOLO was NEVER called — V2 backstop is removed in STRICT commit.
    assert len(classifier.call_log) == 0, (
        f"V2 should not fall back to YOLO on no-motion, got {classifier.call_log}"
    )


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
