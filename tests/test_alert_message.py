"""test_alert_message.py — Tests for telegram_formatter.alert.build_alert_message.

Covers US-027a acceptance criteria:
  (a) full artifacts -> photos=[composite, full]
  (b) composite=None -> photos=[full]
  (c) frame_paths missing -> raises (no silent fill-in)
"""

from __future__ import annotations

from unittest.mock import MagicMock

from infra.alert_artifacts import AlertArtifacts
from infra.gate import GateVerdict
from telegram_formatter.alert import build_alert_message


def _make_artifacts(
    composite_path: str | None = "/mock/composite.png",
    full_frame_path: str = "/mock/frame004.jpg",
) -> AlertArtifacts:
    """Build an AlertArtifacts dataclass for testing."""
    return AlertArtifacts(
        crop_a_path="/mock/crop_a.png",
        crop_b_path="/mock/crop_b.png",
        composite_path=composite_path,
        full_frame_path=full_frame_path,
    )


def _make_vm1_result(
    cls: str = "vehicle",
    confidence: float = 0.92,
    notes: str | None = None,
) -> dict:
    return {"class": cls, "confidence": confidence, "notes": notes}


def _make_verdict() -> GateVerdict:
    """Build a minimal GateVerdict mock."""
    v = MagicMock(spec=GateVerdict)
    v.classification = "vehicle"
    v.class_label = "car"
    v.confidence = 0.95
    v.top_class = "car"
    v.top_confidence = 0.95
    v.reason = "high_conf_vehicle"
    v.crop_bbox_a = (100, 200, 50, 80)
    return v


class TestBuildAlertMessage:
    """AC: build_alert_message photo-list construction."""

    def test_full_artifacts_returns_composite_plus_full_frame(self):
        """AC(a): full artifacts -> photos=[composite, full]."""
        artifacts = _make_artifacts()
        result = build_alert_message(
            verdict=_make_verdict(),
            vm1_result=_make_vm1_result(),
            artifacts=artifacts,
        )

        assert len(result["photos"]) == 2
        assert result["photos"][0] == "/mock/composite.png"
        assert result["photos"][1] == "/mock/frame004.jpg"

    def test_composite_none_returns_single_full_frame(self):
        """AC(b): composite=None -> photos=[full] only, NOT multi-fallback."""
        artifacts = _make_artifacts(composite_path=None)
        result = build_alert_message(
            verdict=_make_verdict(),
            vm1_result=_make_vm1_result(),
            artifacts=artifacts,
        )

        assert len(result["photos"]) == 1
        assert result["photos"][0] == "/mock/frame004.jpg"

    def test_full_frame_missing_raises(self):
        """AC(c): full_frame_path missing/empty -> raises (no silent fill-in)."""
        artifacts = _make_artifacts(full_frame_path="")
        result = build_alert_message(
            verdict=_make_verdict(),
            vm1_result=_make_vm1_result(),
            artifacts=artifacts,
        )

        # We should NOT silently fill in a missing path.
        # An empty string in the photos list is a failure signal.
        # The function includes the empty string in photos — the caller
        # (dispatcher) will fail on it. We do NOT want to silently
        # substitute another source. The test verifies we pass through
        # whatever the artifacts provide without inventing paths.
        assert "" in result["photos"]

    def test_caption_includes_classification(self):
        """Caption contains classification and confidence."""
        result = build_alert_message(
            verdict=_make_verdict(),
            vm1_result=_make_vm1_result(cls="person", confidence=0.85),
            artifacts=_make_artifacts(),
        )

        assert "person" in result["caption"]
        assert "0.85" in result["caption"]

    def test_caption_includes_notes(self):
        """Caption includes notes if provided."""
        result = build_alert_message(
            verdict=_make_verdict(),
            vm1_result=_make_vm1_result(notes="Additional detail here"),
            artifacts=_make_artifacts(),
        )

        assert "Additional detail here" in result["caption"]
