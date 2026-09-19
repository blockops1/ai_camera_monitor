"""test_unsure_prompt.py — Tests for infra.unsure_prompt module.

Covers US-060d acceptance criteria:
  AC1: subclass field exists with enum {blurry, partial, ambiguous, other}
  AC2: SCHEMA_JSON has subclass property
  AC3: module-level exports are correct
"""

from __future__ import annotations

import json

from infra.unsure_prompt import MODE_NAME, PROMPT_TEXT, SCHEMA_JSON, build_unsure_prompt


class TestSCHEMAJSON:
    """AC2: SCHEMA_JSON structure."""

    def test_schema_has_subclass_property(self):
        """AC2: SCHEMA_JSON has a 'subclass' property key."""
        assert "subclass" in SCHEMA_JSON["properties"]

    def test_subclass_has_enum(self):
        """AC1: subclass property has enum with expected values."""
        subclass = SCHEMA_JSON["properties"]["subclass"]
        assert subclass["type"] == ["string", "null"]
        assert subclass["enum"] == ["blurry", "partial", "ambiguous", "other"]

    def test_subclass_in_required(self):
        """subclass must be in the required list."""
        assert "subclass" in SCHEMA_JSON["required"]

    def test_schema_required_has_six_fields(self):
        """Exactly 6 required fields."""
        assert len(SCHEMA_JSON["required"]) == 6

    def test_schema_additional_properties_false(self):
        """No extra keys allowed."""
        assert SCHEMA_JSON["additionalProperties"] is False

    def test_all_properties_have_description(self):
        """Every property has a description field."""
        for key, prop in SCHEMA_JSON["properties"].items():
            assert "description" in prop, f"Property '{key}' missing description"


class TestPROMPT_TEXT:
    """PROMPT_TEXT includes subclass guidance."""

    def test_prompt_mentions_subclass(self):
        """PROMPT_TEXT references subclass field."""
        assert "subclass" in PROMPT_TEXT

    def test_prompt_lists_blurry(self):
        """PROMPT_TEXT mentions 'blurry' as a subclass value."""
        assert "blurry" in PROMPT_TEXT

    def test_prompt_lists_partial(self):
        """PROMPT_TEXT mentions 'partial' as a subclass value."""
        assert "partial" in PROMPT_TEXT

    def test_prompt_lists_ambiguous(self):
        """PROMPT_TEXT mentions 'ambiguous' as a subclass value."""
        assert "ambiguous" in PROMPT_TEXT

    def test_prompt_lists_other(self):
        """PROMPT_TEXT mentions 'other' as a subclass value."""
        assert "other" in PROMPT_TEXT


class TestBuildUnsurePrompt:
    """build_unsure_prompt returns PROMPT_TEXT."""

    def test_returns_prompt_text(self):
        """build_unsure_prompt() returns PROMPT_TEXT verbatim."""
        result = build_unsure_prompt()
        assert result is PROMPT_TEXT

    def test_prompt_is_nonempty(self):
        """PROMPT_TEXT is not an empty string."""
        assert len(PROMPT_TEXT) > 100


class TestMODE_NAME:
    """MODE_NAME constant."""

    def test_mode_name_value(self):
        assert MODE_NAME == "vm2_unsure"


class TestSchemaValidJSON:
    """SCHEMA_JSON is valid JSON (can round-trip)."""

    def test_schema_json_roundtrip(self):
        """SCHEMA_JSON serializes and deserializes cleanly."""
        text = json.dumps(SCHEMA_JSON)
        restored = json.loads(text)
        assert restored == SCHEMA_JSON

    def test_subclass_enum_survives_roundtrip(self):
        """Enum values survive JSON round-trip."""
        subclass = SCHEMA_JSON["properties"]["subclass"]
        text = json.dumps(subclass)
        restored = json.loads(text)
        assert restored["enum"] == ["blurry", "partial", "ambiguous", "other"]
