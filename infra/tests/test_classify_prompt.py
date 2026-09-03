"""Tests for infra.classify_prompt + infra.classify_schema + infra.classify_validator.

§11.115.2: shared classify prompt for Qwen call 1. Single prompt, single
schema, used by every alert regardless of class. The output drives the
class-specific Qwen call 2.

Three modules under test:
    - infra.classify_schema: the JSON schema literal + ClassLabel enum
    - infra.classify_prompt: build_classify_prompt() renders the prompt
    - infra.classify_validator: validate_classify_response() parses + validates

Tests use synthetic prompt strings; no Qwen calls. The schema and validator
are pure functions, easy to test in isolation.
"""
from __future__ import annotations

import pytest

from infra import classify_prompt, classify_schema, classify_validator

# -----------------------------------------------------------------------------
# classify_schema
# -----------------------------------------------------------------------------


class TestClassLabel:
    """ClassLabel enum — the four possible classify responses."""

    def test_four_classes(self) -> None:
        """Four values: vehicle, person, animal, other."""
        labels = {c.value for c in classify_schema.ClassLabel}
        assert labels == {"vehicle", "person", "animal", "other"}

    def test_from_string_valid(self) -> None:
        """classify_schema.ClassLabel('vehicle') works for any of the 4."""
        assert classify_schema.ClassLabel("vehicle") is classify_schema.ClassLabel.VEHICLE
        assert classify_schema.ClassLabel("person") is classify_schema.ClassLabel.PERSON
        assert classify_schema.ClassLabel("animal") is classify_schema.ClassLabel.ANIMAL
        assert classify_schema.ClassLabel("other") is classify_schema.ClassLabel.OTHER

    def test_from_string_invalid_raises(self) -> None:
        """Unknown class string raises ValueError."""
        with pytest.raises(ValueError):
            classify_schema.ClassLabel("spaceship")


class TestClassifySchemaJson:
    """CLASSIFY_SCHEMA_JSON — the JSON literal embedded in the prompt."""

    def test_is_nonempty_string(self) -> None:
        assert isinstance(classify_schema.CLASSIFY_SCHEMA_JSON, str)
        assert len(classify_schema.CLASSIFY_SCHEMA_JSON) > 50

    def test_mentions_all_four_classes(self) -> None:
        """Schema literal explicitly lists all four class values."""
        for label in ("vehicle", "person", "animal", "other"):
            assert f'"{label}"' in classify_schema.CLASSIFY_SCHEMA_JSON

    def test_includes_confidence_range(self) -> None:
        """Schema literal shows confidence is 0.0-1.0."""
        assert "0.0" in classify_schema.CLASSIFY_SCHEMA_JSON
        assert "1.0" in classify_schema.CLASSIFY_SCHEMA_JSON


# -----------------------------------------------------------------------------
# classify_prompt
# -----------------------------------------------------------------------------


class TestBuildClassifyPrompt:
    """build_classify_prompt(camera_name, captured_at) -> str"""

    def test_returns_nonempty_string(self) -> None:
        out = classify_prompt.build_classify_prompt(
            camera_name="Front Porch", captured_at="2026-09-02 12:34:56"
        )
        assert isinstance(out, str)
        assert len(out) > 100

    def test_includes_camera_name(self) -> None:
        out = classify_prompt.build_classify_prompt(
            camera_name="Back Yard Solar", captured_at="2026-09-02 12:34:56"
        )
        assert "Back Yard Solar" in out

    def test_includes_captured_at(self) -> None:
        out = classify_prompt.build_classify_prompt(
            camera_name="Front Porch", captured_at="2026-09-02T12:34:56"
        )
        assert "2026-09-02T12:34:56" in out

    def test_includes_schema_literal(self) -> None:
        """Schema embedded in prompt so Qwen emits exactly this shape."""
        out = classify_prompt.build_classify_prompt(
            camera_name="Front Porch", captured_at="2026-09-02 12:34:56"
        )
        # Should reference the JSON schema's class field
        assert "vehicle" in out
        assert "person" in out
        assert "animal" in out
        assert "other" in out

    def test_asks_for_two_images(self) -> None:
        """Prompt explicitly references the two crops Qwen is seeing."""
        out = classify_prompt.build_classify_prompt(
            camera_name="Front Porch", captured_at="2026-09-02 12:34:56"
        )
        # Prompt should make it clear Qwen sees two images
        out_lower = out.lower()
        assert (
            "two" in out_lower
            or "both" in out_lower
            or "crop_a" in out_lower
            or "crop_b" in out_lower
        )

    def test_tells_qwen_when_to_return_other(self) -> None:
        """Prompt tells Qwen to return 'other' when uncertain (no hallucination)."""
        out = classify_prompt.build_classify_prompt(
            camera_name="Front Porch", captured_at="2026-09-02 12:34:56"
        )
        out_lower = out.lower()
        # The prompt must instruct Qwen to default to 'other' on uncertainty
        assert (
            "other" in out_lower
            and ("uncertain" in out_lower or "unclear" in out_lower or "not sure" in out_lower)
        )


# -----------------------------------------------------------------------------
# classify_validator
# -----------------------------------------------------------------------------


class TestValidateClassifyResponse:
    """validate_classify_response(raw_text) -> ClassifyResult"""

    def test_valid_vehicle_response(self) -> None:
        """Qwen returns clean JSON with class=vehicle → parsed correctly."""
        raw = '{"class": "vehicle", "confidence": 0.95, "reasoning": "silver sedan"}'
        result = classify_validator.validate_classify_response(raw)
        assert result.label is classify_schema.ClassLabel.VEHICLE
        assert result.confidence == 0.95
        assert result.reasoning == "silver sedan"
        assert result.fallback_used is False

    def test_valid_person_response(self) -> None:
        raw = '{"class": "person", "confidence": 0.88, "reasoning": "walking figure"}'
        result = classify_validator.validate_classify_response(raw)
        assert result.label is classify_schema.ClassLabel.PERSON
        assert result.confidence == 0.88

    def test_valid_animal_response(self) -> None:
        raw = '{"class": "animal", "confidence": 0.7, "reasoning": "dog shape"}'
        result = classify_validator.validate_classify_response(raw)
        assert result.label is classify_schema.ClassLabel.ANIMAL

    def test_valid_other_response(self) -> None:
        raw = '{"class": "other", "confidence": 0.6, "reasoning": "tree branch moving"}'
        result = classify_validator.validate_classify_response(raw)
        assert result.label is classify_schema.ClassLabel.OTHER

    def test_invalid_class_falls_back_to_other(self) -> None:
        """Unknown class → ClassLabel.OTHER with fallback_used=True."""
        raw = '{"class": "spaceship", "confidence": 0.99, "reasoning": "flying saucer"}'
        result = classify_validator.validate_classify_response(raw)
        assert result.label is classify_schema.ClassLabel.OTHER
        assert result.fallback_used is True

    def test_missing_class_falls_back_to_other(self) -> None:
        """No `class` key at all → OTHER."""
        raw = '{"confidence": 0.5, "reasoning": "I see a thing"}'
        result = classify_validator.validate_classify_response(raw)
        assert result.label is classify_schema.ClassLabel.OTHER
        assert result.fallback_used is True

    def test_malformed_json_falls_back_to_other(self) -> None:
        """Truncated / non-JSON text → OTHER."""
        result = classify_validator.validate_classify_response(
            "I think it's a vehicle but I'm not sure"
        )
        assert result.label is classify_schema.ClassLabel.OTHER
        assert result.fallback_used is True

    def test_empty_string_falls_back_to_other(self) -> None:
        """Empty string → OTHER."""
        result = classify_validator.validate_classify_response("")
        assert result.label is classify_schema.ClassLabel.OTHER
        assert result.fallback_used is True

    def test_json_in_code_block(self) -> None:
        """Qwen often wraps responses in ```json ... ``` — strip and parse."""
        raw = (
            "```json\n"
            '{"class": "person", "confidence": 0.9, "reasoning": "face visible"}\n'
            "```"
        )
        result = classify_validator.validate_classify_response(raw)
        assert result.label is classify_schema.ClassLabel.PERSON
        assert result.fallback_used is False

    def test_confidence_clamped_to_range(self) -> None:
        """Confidence outside [0, 1] is clamped (not a fallback)."""
        raw = '{"class": "vehicle", "confidence": 1.5, "reasoning": "x"}'
        result = classify_validator.validate_classify_response(raw)
        assert result.label is classify_schema.ClassLabel.VEHICLE
        assert 0.0 <= result.confidence <= 1.0

    def test_missing_confidence_defaults_to_zero(self) -> None:
        """Missing confidence key → 0.0, no fallback."""
        raw = '{"class": "vehicle", "reasoning": "looks like one"}'
        result = classify_validator.validate_classify_response(raw)
        assert result.label is classify_schema.ClassLabel.VEHICLE
        assert result.confidence == 0.0
        assert result.fallback_used is False
