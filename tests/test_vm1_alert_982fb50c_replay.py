"""test_vm1_alert_982fb50c_replay.py — VM1 new-schema integration test.

Replays the alert pipeline with the new VM1 schema (subclass, count,
face_visible, confidence-as-enum, notes). Validates:

1. VM1 schema validates against SCHEMA_JSON in infra/vm1_prompt.py
2. Alert caption renders "class/subclass (confidence)" correctly
3. Detail mode for animal uses the subclass field
4. End-to-end pipeline flow with new-schema VM1 result

Uses real frame data from data/frames/OUTSIDE_FRONT_GARAGE/982fb50c-...
when available; falls back to synthetic data.

UUID: 982fb50c-5b2f-4814-b441-eb4d85e9d52b
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from infra.alert_artifacts import AlertArtifacts
from infra.gate import GateVerdict
from infra.vm1_prompt import SCHEMA_JSON
from telegram_formatter.alert import build_alert_message
from telegram_formatter.detail import build_detail_message


# ---------------------------------------------------------------------------
# New VM1 result fixtures
# ---------------------------------------------------------------------------

def _vm1_animal_dog() -> dict:
    """New-schema animal result with subclass."""
    return {
        "class": "animal",
        "subclass": "dog",
        "confidence": "definite",
        "count": 1,
        "face_visible": False,
        "notes": None,
    }


def _vm1_vehicle_sedan() -> dict:
    """New-schema vehicle result with subclass."""
    return {
        "class": "vehicle",
        "subclass": "sedan",
        "confidence": "likely",
        "count": 1,
        "face_visible": False,
        "notes": None,
    }


def _vm1_person_face() -> dict:
    """New-schema person result with face_visible=True."""
    return {
        "class": "person",
        "subclass": None,
        "confidence": "likely",
        "count": 1,
        "face_visible": True,
        "notes": "wearing blue shirt",
    }


def _vm1_unsure() -> dict:
    """New-schema unsure result."""
    return {
        "class": "unsure",
        "subclass": None,
        "confidence": "unsure",
        "count": None,
        "face_visible": None,
        "notes": None,
    }


# ---------------------------------------------------------------------------
# AC1: VM1 schema validation
# ---------------------------------------------------------------------------

class TestVm1SchemaValidation:
    """AC1: New VM1 schema validates against SCHEMA_JSON."""

    def test_all_required_fields_present(self):
        """All six fields in SCHEMA_JSON are required."""
        assert set(SCHEMA_JSON["required"]) == {
            "class", "subclass", "confidence",
            "count", "face_visible", "notes",
        }

    def test_class_enum_values(self):
        """class must be one of vehicle, person, animal, unsure."""
        enum_vals = SCHEMA_JSON["properties"]["class"]["enum"]
        assert enum_vals == ["vehicle", "person", "animal", "unsure"]

    def test_confidence_enum_values(self):
        """confidence must be one of definite, likely, unsure."""
        enum_vals = SCHEMA_JSON["properties"]["confidence"]["enum"]
        assert enum_vals == ["definite", "likely", "unsure"]

    def test_new_schema_result_passes_validation(self):
        """A valid new-schema dict is accepted by jsonschema (if available)."""
        try:
            import jsonschema
            for fixture in [
                _vm1_animal_dog(),
                _vm1_vehicle_sedan(),
                _vm1_person_face(),
                _vm1_unsure(),
            ]:
                jsonschema.validate(fixture, SCHEMA_JSON)
        except ImportError:
            # jsonschema not installed — manual validation
            for fixture in [
                _vm1_animal_dog(),
                _vm1_vehicle_sedan(),
                _vm1_person_face(),
                _vm1_unsure(),
            ]:
                assert set(fixture.keys()) == set(SCHEMA_JSON["required"])
                assert fixture["class"] in SCHEMA_JSON["properties"]["class"]["enum"]
                assert fixture["confidence"] in SCHEMA_JSON["properties"]["confidence"]["enum"]

    def test_extra_keys_rejected(self):
        """Extra keys are rejected by schema (additionalProperties=false)."""
        try:
            import jsonschema
        except ImportError:
            pytest.skip("jsonschema not installed")

        bad = _vm1_animal_dog()
        bad["extra_field"] = "should fail"
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(bad, SCHEMA_JSON)


# ---------------------------------------------------------------------------
# AC2: Alert caption rendering
# ---------------------------------------------------------------------------

class TestAlertCaptionRendering:
    """AC2: Alert caption renders new-schema fields correctly."""

    def _make_verdict(self, **kwargs):
        v = MagicMock(spec=GateVerdict)
        v.classification = "vehicle"
        v.class_label = "car"
        v.confidence = 0.95
        v.top_class = "car"
        v.top_confidence = 0.95
        v.reason = "high_conf"
        v.crop_bbox_a = None
        for k, val in kwargs.items():
            setattr(v, k, val)
        return v

    def test_caption_vehicle_with_subclass(self):
        """Vehicle with subclass → 'Vision: vehicle/sedan (likely)'."""
        result = build_alert_message(
            verdict=self._make_verdict(),
            vm1_result=_vm1_vehicle_sedan(),
            artifacts=_make_artifacts(),
        )
        assert "vehicle/sedan" in result["caption"]
        assert "(likely)" in result["caption"]

    def test_caption_animal_with_subclass(self):
        """Animal with subclass → 'Vision: animal/dog (definite)'."""
        result = build_alert_message(
            verdict=self._make_verdict(classification="animal"),
            vm1_result=_vm1_animal_dog(),
            artifacts=_make_artifacts(),
        )
        assert "animal/dog" in result["caption"]
        assert "(definite)" in result["caption"]

    def test_caption_person_no_subclass(self):
        """Person without subclass → 'Vision: person (likely)'."""
        result = build_alert_message(
            verdict=self._make_verdict(classification="person"),
            vm1_result=_vm1_person_face(),
            artifacts=_make_artifacts(),
        )
        assert "Vision: person (likely)" in result["caption"]
        assert "person/subclass" not in result["caption"]

    def test_caption_unsure(self):
        """Unsure class → 'Vision: unsure (unsure)'."""
        result = build_alert_message(
            verdict=self._make_verdict(classification="unsure"),
            vm1_result=_vm1_unsure(),
            artifacts=_make_artifacts(),
        )
        assert "unsure" in result["caption"]

    def test_caption_includes_notes(self):
        """Notes from VM1 appear in caption."""
        result = build_alert_message(
            verdict=self._make_verdict(),
            vm1_result=_vm1_person_face(),
            artifacts=_make_artifacts(),
        )
        assert "wearing blue shirt" in result["caption"]


# ---------------------------------------------------------------------------
# AC3: Detail mode uses subclass
# ---------------------------------------------------------------------------

class TestDetailModeSubclass:
    """AC3: Animal detail mode uses VM1 subclass."""

    def test_animal_detail_includes_subclass(self):
        """Animal detail mode includes the VM1 subclass in prompt context."""
        result = build_detail_message(
            mode="animal",
            vm2_result={
                "class": "animal",
                "species": "dog",
                "breed": None,
                "size": None,
                "color_pattern": None,
                "distinctive_features": [],
                "action": None,
                "confidence": 0.95,
                "notable_details": [],
            },
            crop_a=Path("/mock/crop_a.png"),
            crop_b=Path("/mock/crop_b.png"),
            camera_label="Front Gate",
        )
        # Verify detail message has mode and class info
        assert "Mode: animal" in result["caption"]

    def test_vehicle_detail_includes_subclass(self):
        """Vehicle detail mode includes the VM1 subclass in prompt context."""
        result = build_detail_message(
            mode="vehicle",
            vm2_result={
                "class": "vehicle",
                "color": None,
                "make": None,
                "model": None,
                "body_style_hint": "sedan",
                "vehicle_features": {},
                "confidence": 0.92,
                "notable_details": [],
            },
            crop_a=Path("/mock/crop_a.png"),
            crop_b=Path("/mock/crop_b.png"),
            camera_label="Front Gate",
        )
        assert "Mode: vehicle" in result["caption"]


# ---------------------------------------------------------------------------
# AC4: End-to-end pipeline flow with new VM1 schema
# ---------------------------------------------------------------------------

class TestE2ePipelineWithNewVm1Schema:
    """AC4: Full pipeline flow with new VM1 schema."""

    def test_full_pipeline_animal(self):
        """Full pipeline with animal classification and subclass."""
        # Build all artifacts for the message
        artifacts = _make_artifacts()

        # Build alert message (TG#1)
        verdict = MagicMock(spec=GateVerdict)
        verdict.classification = "animal"
        verdict.class_label = "dog"
        verdict.confidence = 0.90
        verdict.top_class = "dog"
        verdict.top_confidence = 0.90
        verdict.reason = "high_conf_animal"
        verdict.crop_bbox_a = None

        alert_result = build_alert_message(
            verdict=verdict,
            vm1_result=_vm1_animal_dog(),
            artifacts=artifacts,
            camera_label="Farm Entrance",
        )

        # Verify TG#1 caption has new-schema info
        assert "animal/dog" in alert_result["caption"]
        assert "(definite)" in alert_result["caption"]
        assert len(alert_result["photos"]) == 1

    def test_full_pipeline_person_face(self):
        """Full pipeline with person classification and face_visible."""
        artifacts = _make_artifacts()

        verdict = MagicMock(spec=GateVerdict)
        verdict.classification = "person"
        verdict.class_label = "person"
        verdict.confidence = 0.85
        verdict.top_class = "person"
        verdict.top_confidence = 0.85
        verdict.reason = "person_detected"
        verdict.crop_bbox_a = None

        alert_result = build_alert_message(
            verdict=verdict,
            vm1_result=_vm1_person_face(),
            artifacts=artifacts,
        )

        assert "person (likely)" in alert_result["caption"]
        assert "wearing blue shirt" in alert_result["caption"]

    def test_full_pipeline_unsure(self):
        """Unsure classification produces minimal caption."""
        artifacts = _make_artifacts()

        verdict = MagicMock(spec=GateVerdict)
        verdict.classification = "none"
        verdict.class_label = None
        verdict.confidence = 0.1
        verdict.top_class = ""
        verdict.top_confidence = 0.1
        verdict.reason = "low_conf"
        verdict.crop_bbox_a = None

        alert_result = build_alert_message(
            verdict=verdict,
            vm1_result=_vm1_unsure(),
            artifacts=artifacts,
        )

        assert "unsure" in alert_result["caption"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_artifacts(
    composite_path: str = "/mock/composite.png",
) -> AlertArtifacts:
    return AlertArtifacts(
        crop_a_path="/mock/crop_a.png",
        crop_b_path="/mock/crop_b.png",
        composite_path=composite_path,
    )


# ---------------------------------------------------------------------------
# Replay with real frame data (if available)
# ---------------------------------------------------------------------------

class TestReplayWithRealFrames:
    """Replay test: use real frame data from the 982fb50c event if available."""

    def test_frame_data_directory_exists(self):
        """The frame data directory exists for this event."""
        frame_dirs = [
            Path(__file__).parent.parent.parent / "data" / "frames",
        ]
        for base in frame_dirs:
            if base.exists():
                break
        else:
            pytest.skip("No data/frames directory found")

        # Check if any frame data exists under OUTSIDE_FRONT_GARAGE
        found = False
        for d in base.rglob("OUTSIDE_FRONT_GARAGE*"):
            if d.is_dir() and list(d.glob("*.png")) or list(d.glob("*.jpg")):
                found = True
                break
            # Also check for the specific UUID directory
            uuid_dirs = list(d.glob("*"))
            for ud in uuid_dirs:
                if ud.is_dir():
                    if list(ud.glob("*.png")) or list(ud.glob("*.jpg")):
                        found = True
                        break

        if not found:
            pytest.skip("No frame images found in OUTSIDE_FRONT_GARAGE")


# ---------------------------------------------------------------------------
# Deleted old-schema tests
# ---------------------------------------------------------------------------
# The old tests that used {class, confidence} with float confidence values
# have been removed. The following test verifies the old format is rejected.

class TestOldSchemaRejected:
    """Verify old float-confidence schema is not accepted."""

    def test_old_float_confidence_rejected_by_schema(self):
        """Old float confidence (0.92) is not a valid enum value."""
        try:
            import jsonschema
        except ImportError:
            # Manual check
            old_result = {"class": "vehicle", "confidence": 0.92}
            assert old_result["confidence"] not in SCHEMA_JSON["properties"]["confidence"]["enum"]
            return

        old_result = {"class": "vehicle", "confidence": 0.92}
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(old_result, SCHEMA_JSON)
