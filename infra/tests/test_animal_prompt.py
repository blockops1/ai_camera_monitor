"""Tests for infra.animal_prompt — §11.115.4 animal call-2 prompt.

Per §11.115 design: animal call-2 schema = {species, breed, size, ...}.
No face-crop logic (animals don't have face-recognition routing).

Mirrors the structure of test_person_prompt.py.
"""
from __future__ import annotations

from infra import animal_prompt


class TestAnimalSchemaJson:
    def test_schema_is_nonempty_string(self) -> None:
        assert isinstance(animal_prompt.ANIMAL_SCHEMA_JSON, str)
        assert len(animal_prompt.ANIMAL_SCHEMA_JSON) > 50

    def test_schema_has_species(self) -> None:
        assert '"species"' in animal_prompt.ANIMAL_SCHEMA_JSON

    def test_schema_has_breed(self) -> None:
        """Breed is optional (mixed breeds, wildlife) but in schema."""
        assert '"breed"' in animal_prompt.ANIMAL_SCHEMA_JSON

    def test_schema_has_size(self) -> None:
        """Size bucket for tier-3 matching."""
        assert '"size"' in animal_prompt.ANIMAL_SCHEMA_JSON

    def test_schema_has_confidence(self) -> None:
        assert '"confidence"' in animal_prompt.ANIMAL_SCHEMA_JSON

    def test_schema_does_not_have_face_fields(self) -> None:
        """Animals don't go through face recognition."""
        assert "better_crop" not in animal_prompt.ANIMAL_SCHEMA_JSON
        assert "face_bbox" not in animal_prompt.ANIMAL_SCHEMA_JSON
        assert "face_visible" not in animal_prompt.ANIMAL_SCHEMA_JSON


class TestBuildAnimalPrompt:
    def test_returns_nonempty_string(self) -> None:
        out = animal_prompt.build_animal_prompt(
            camera_name="Front Porch", captured_at="t"
        )
        assert isinstance(out, str)
        assert len(out) > 100

    def test_includes_camera_name(self) -> None:
        out = animal_prompt.build_animal_prompt(
            camera_name="Back Yard Solar", captured_at="t"
        )
        assert "Back Yard Solar" in out

    def test_includes_captured_at(self) -> None:
        out = animal_prompt.build_animal_prompt(
            camera_name="x", captured_at="2026-09-02T12:34:56"
        )
        assert "2026-09-02T12:34:56" in out

    def test_prompt_embeds_schema(self) -> None:
        out = animal_prompt.build_animal_prompt(
            camera_name="x", captured_at="t"
        )
        assert "species" in out
        assert "breed" in out
        assert "size" in out

    def test_prompt_asks_about_two_images(self) -> None:
        out = animal_prompt.build_animal_prompt(
            camera_name="x", captured_at="t"
        )
        out_lower = out.lower()
        assert "two" in out_lower or "both" in out_lower

    def test_prompt_tells_qwen_to_return_unknown_when_unsure(self) -> None:
        """Mirrors the `neither` bias from person prompt."""
        out = animal_prompt.build_animal_prompt(
            camera_name="x", captured_at="t"
        )
        out_lower = out.lower()
        assert (
            "uncertain" in out_lower
            or "unclear" in out_lower
            or "unknown" in out_lower
            or "not sure" in out_lower
        )
