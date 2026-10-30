"""test_confidence_rendering.py -- parametric tests for VM1 confidence word display.

VM1 confidence is a categorical enum [definite, likely, unsure] per V2-060.
The alert caption line must render the enum word directly (no %, no "confidence"
prefix).

Covers AC7: parametric test with three fixtures asserting exact caption format.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

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


def _make_verdict(
    classification: str = "vehicle",
    bbox: tuple[int, int, int, int] | None = (100, 200, 50, 80),
) -> MagicMock:
    """Build a GateVerdict mock with a subject bbox."""
    v = MagicMock(spec=GateVerdict)
    v.classification = classification
    v.class_label = classification
    v.confidence = 0.95
    v.top_class = classification
    v.top_confidence = 0.95
    v.reason = "high_conf_vehicle"
    v.crop_bbox_a = bbox
    return v


class TestConfidenceRendering:
    """AC7: VM1 confidence renders as plain enum word, no %, no 'confidence' prefix."""

    @pytest.mark.parametrize("conf_value", ["definite", "likely", "unsure"])
    def test_confidence_word_rendered_without_percent(self, conf_value: str):
        """Each enum value renders as the word alone, not 'value%'."""
        result = build_alert_message(
            verdict=_make_verdict(classification="vehicle"),
            vm1_result={"class": "vehicle", "confidence": conf_value},
            artifacts=_make_artifacts(),
            camera_label="Front Gate",
            position=1,
            alert=None,
        )
        caption = result["caption"]
        # The enum word appears in the caption.
        assert conf_value in caption
        # No percent sign anywhere in caption.
        assert "%" not in caption
        # No "confidence" prefix before the word.
        lines = [l for l in caption.split("\n") if l]
        class_line = lines[1]  # line 2: emoji class word
        assert f"confidence {conf_value}" not in class_line

    def test_confidence_line_format_definite(self):
        """definite -> 'vehicle definite' on the class line."""
        result = build_alert_message(
            verdict=_make_verdict(classification="vehicle"),
            vm1_result={"class": "vehicle", "confidence": "definite"},
            artifacts=_make_artifacts(),
            camera_label="Front Gate",
            position=1,
            alert=None,
        )
        caption = result["caption"]
        lines = [l for l in caption.split("\n") if l]
        # Line 2: emoji + class + bullet + confidence word
        assert "vehicle" in lines[1]
        assert "definite" in lines[1]
        assert "%" not in lines[1]
        assert "confidence " not in lines[1]

    def test_confidence_line_format_likely(self):
        """likely -> 'vehicle likely' on the class line."""
        result = build_alert_message(
            verdict=_make_verdict(classification="person"),
            vm1_result={"class": "person", "confidence": "likely"},
            artifacts=_make_artifacts(),
            camera_label="Front Gate",
            position=1,
            alert=None,
        )
        caption = result["caption"]
        lines = [l for l in caption.split("\n") if l]
        assert "person" in lines[1]
        assert "likely" in lines[1]
        assert "confidence " not in lines[1]

    def test_confidence_line_format_unsure(self):
        """unsure -> 'animal unsure' on the class line."""
        result = build_alert_message(
            verdict=_make_verdict(classification="animal"),
            vm1_result={"class": "animal", "confidence": "unsure"},
            artifacts=_make_artifacts(),
            camera_label="Front Gate",
            position=1,
            alert=None,
        )
        caption = result["caption"]
        lines = [l for l in caption.split("\n") if l]
        assert "animal" in lines[1]
        assert "unsure" in lines[1]
        assert "confidence " not in lines[1]

    def test_header_uses_em_dash(self):
        """TG#1 header uses Unicode em-dash U+2014, not ASCII '--'."""
        result = build_alert_message(
            verdict=_make_verdict(classification="vehicle"),
            vm1_result={"class": "vehicle", "confidence": "definite"},
            artifacts=_make_artifacts(),
            camera_label="Front Gate",
            position=1,
            alert={"id": "evt-test-001", "timestamp": "2026-09-23T10:00:00Z"},
        )
        caption = result["caption"]
        lines = [l for l in caption.split("\n") if l]
        # Line 1: camera header
        assert "\u2014" in lines[0]
        assert "--" not in lines[0]
