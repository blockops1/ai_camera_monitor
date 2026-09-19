"""test_alert_message.py — Tests for telegram_formatter.alert.build_alert_message.

Covers US-058a acceptance criteria:
  (a) full artifacts -> photos=[composite]
  (b) composite=None -> photos=[]
"""

from __future__ import annotations

from unittest.mock import MagicMock

from infra.alert_artifacts import AlertArtifacts
from infra.gate import GateVerdict
from telegram_formatter.alert import build_alert_message


def _make_artifacts(
    composite_path: str | None = "/mock/composite.png",
) -> AlertArtifacts:
    """Build an AlertArtifacts dataclass for testing."""
    return AlertArtifacts(
        crop_a_path="/mock/crop_a.png",
        crop_b_path="/mock/crop_b.png",
        composite_path=composite_path,
    )


def _make_vm1_result(
    cls: str = "vehicle",
    subclass: str | None = "sedan",
    confidence: str = "likely",
    notes: str | None = None,
    count: int | None = 1,
    face_visible: bool | None = False,
) -> dict:
    return {
        "class": cls,
        "subclass": subclass,
        "confidence": confidence,
        "notes": notes,
        "count": count,
        "face_visible": face_visible,
    }


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

    def test_full_artifacts_returns_composite_only(self):
        """AC(a): full artifacts -> photos=[composite]."""
        artifacts = _make_artifacts()
        result = build_alert_message(
            verdict=_make_verdict(),
            vm1_result=_make_vm1_result(),
            artifacts=artifacts,
        )

        assert len(result["photos"]) == 1
        assert result["photos"][0] == "/mock/composite.png"

    def test_composite_none_returns_empty_photos(self):
        """AC(b): composite=None -> photos=[] only."""
        artifacts = _make_artifacts(composite_path=None)
        result = build_alert_message(
            verdict=_make_verdict(),
            vm1_result=_make_vm1_result(),
            artifacts=artifacts,
        )

        assert len(result["photos"]) == 0

    def test_caption_includes_classification(self):
        """Caption contains classification and confidence."""
        result = build_alert_message(
            verdict=_make_verdict(),
            vm1_result=_make_vm1_result(cls="person", confidence="likely"),
            artifacts=_make_artifacts(),
        )

        assert "person" in result["caption"]
        assert "likely" in result["caption"]

    def test_caption_includes_notes(self):
        """Caption includes notes if provided."""
        result = build_alert_message(
            verdict=_make_verdict(),
            vm1_result=_make_vm1_result(notes="Additional detail here"),
            artifacts=_make_artifacts(),
        )

        assert "Additional detail here" in result["caption"]
