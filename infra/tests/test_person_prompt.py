"""Tests for infra.person_prompt — §11.115.4 person call-2 prompt.

Schema per maintainer 2026-09-02 PM:
  - drop `face_bbox` (was root cause of Bug C — bbox hallucination)
  - drop `face_visible: bool` (was root cause of Bug C — false positives)
  - add `better_crop: enum["crop_a", "crop_b", "neither"]`
  - keep `attributes` + `signature`

Prompt phrasing per maintainer 2026-09-02 PM:
  "If a face is visible, which of the two images shows it better?
   Only return crop_a/crop_b if you can clearly see a face.
   If no face is visible, or you are uncertain, return neither."

This module is a renamable drop-in for infra.person_prompt_template.
For the first §11.115 commit, we create this new module with the new
schema, then in a follow-up commit remove the old one.
"""
from __future__ import annotations

from infra import person_prompt

# -----------------------------------------------------------------------------
# Schema tests
# -----------------------------------------------------------------------------


class TestPersonSchemaJson:
    """PERSON_SCHEMA_JSON contains better_crop; does NOT contain face_bbox."""

    def test_schema_is_nonempty_string(self) -> None:
        assert isinstance(person_prompt.PERSON_SCHEMA_JSON, str)
        assert len(person_prompt.PERSON_SCHEMA_JSON) > 50

    def test_schema_has_better_crop_enum(self) -> None:
        """Schema literal lists better_crop with the 3 enum values."""
        assert "better_crop" in person_prompt.PERSON_SCHEMA_JSON
        assert '"crop_a"' in person_prompt.PERSON_SCHEMA_JSON
        assert '"crop_b"' in person_prompt.PERSON_SCHEMA_JSON
        assert '"neither"' in person_prompt.PERSON_SCHEMA_JSON

    def test_schema_has_attributes(self) -> None:
        """Schema keeps `attributes` for tier-3 matching."""
        assert '"attributes"' in person_prompt.PERSON_SCHEMA_JSON

    def test_schema_has_signature(self) -> None:
        """Schema keeps `signature` field (stable identity)."""
        assert '"signature"' in person_prompt.PERSON_SCHEMA_JSON

    def test_schema_does_not_have_face_bbox(self) -> None:
        """Bug C root cause — face_bbox removed entirely."""
        assert "face_bbox" not in person_prompt.PERSON_SCHEMA_JSON

    def test_schema_does_not_have_face_visible(self) -> None:
        """Bug C root cause — face_visible: bool removed entirely."""
        assert "face_visible" not in person_prompt.PERSON_SCHEMA_JSON


class TestBuildPersonPrompt:
    """build_person_prompt() renders the §11.115 prompt text."""

    def test_returns_nonempty_string(self) -> None:
        out = person_prompt.build_person_prompt(
            camera_name="Front Porch", captured_at="2026-09-02 12:34:56"
        )
        assert isinstance(out, str)
        assert len(out) > 200

    def test_includes_camera_name(self) -> None:
        out = person_prompt.build_person_prompt(
            camera_name="Back Yard Solar", captured_at="t"
        )
        assert "Back Yard Solar" in out

    def test_includes_captured_at(self) -> None:
        out = person_prompt.build_person_prompt(
            camera_name="x", captured_at="2026-09-02T12:34:56"
        )
        assert "2026-09-02T12:34:56" in out

    def test_prompt_embeds_schema(self) -> None:
        """The rendered prompt contains the schema JSON literal."""
        out = person_prompt.build_person_prompt(
            camera_name="x", captured_at="t"
        )
        assert "better_crop" in out
        assert "crop_a" in out
        assert "crop_b" in out
        assert "neither" in out

    def test_prompt_biases_toward_neither(self) -> None:
        """maintainer: prompt must bias Qwen toward 'neither' on uncertainty."""
        out = person_prompt.build_person_prompt(
            camera_name="x", captured_at="t"
        )
        out_lower = out.lower()
        assert "neither" in out_lower
        assert (
            "uncertain" in out_lower
            or "unclear" in out_lower
            or "not sure" in out_lower
        )

    def test_prompt_does_not_request_face_bbox(self) -> None:
        """Prompt must NOT instruct Qwen to output face_bbox."""
        out = person_prompt.build_person_prompt(
            camera_name="x", captured_at="t"
        )
        out_lower = out.lower()
        # Old phrasing mentioned 'face_bbox' / 'pixel coords' — gone.
        assert "face_bbox" not in out_lower

    def test_prompt_asks_about_two_images(self) -> None:
        """Prompt makes it clear Qwen is seeing two crops."""
        out = person_prompt.build_person_prompt(
            camera_name="x", captured_at="t"
        )
        out_lower = out.lower()
        assert "two" in out_lower or "both" in out_lower
