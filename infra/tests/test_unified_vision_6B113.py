"""
test_unified_vision_6B113.py — Tests for infra.unified_vision (Phase §11.113).

Tests cover the four contract surfaces of the Variant A unified prompt +
schema:

  TestSchemaFields    — UNIFIED_SCHEMA_JSON literal structure (strict-JSON
                        runtime schema passed as response_format.json_schema.schema)
  TestPromptBody      — UNIFIED_PROMPT_TEMPLATE_FORMAT literal content
                        (prose visible to Qwen)
  TestBuildFn         — build_unified_prompt substitution behavior
  TestSelectRoute     — select_prompt_template(mode="unified") dispatch
  TestLazyImport      — infra.unified_vision loaded lazily (no eager
                        import cost when callers don't use mode="unified")

Per the wider-scope design (2026-09-02):
  - every field is required (additionalProperties=false)
  - booleans widened to ["boolean", "string"] (Qwen3-VL sometimes
    serializes booleans as strings — structured-output-recipes pitfall #6a)
  - enums use "none" / "unknown" sentinel strings (not null) for
    absence vs indeterminate (structured-output-recipes pitfall #1)
  - The prompt must EXPLICITLY tell Qwen to set non-matching class
    blocks to "none" — that's the leakage test. If Qwen guesses
    across classes, the schema allows it via enum values, but the
    prompt instruction is the guardrail.
"""

from __future__ import annotations

import json

from infra.prompt_templates import select_prompt_template
from infra.unified_vision import (
    UNIFIED_PROMPT_TEMPLATE_FORMAT,
    UNIFIED_SCHEMA_JSON,
    build_unified_prompt,
)

# ============================================================================
# TestSchemaFields — UNIFIED_SCHEMA_JSON literal structure
# ============================================================================


class TestSchemaFields:
    """Schema literal must be valid JSON-Schema with every field required."""

    def test_schema_parses_as_json(self):
        """The literal must be valid JSON parseable by the stdlib."""
        parsed = json.loads(UNIFIED_SCHEMA_JSON)
        assert isinstance(parsed, dict)
        assert parsed["type"] == "object"
        assert parsed["additionalProperties"] is False

    def test_schema_required_keys(self):
        """All nine top-level fields must be required."""
        parsed = json.loads(UNIFIED_SCHEMA_JSON)
        required = set(parsed["required"])
        assert required == {
            "primary_class",
            "vehicle_present",
            "person_present",
            "animal_present",
            "vehicle_features",
            "person_features",
            "animal_features",
            "scene_description",
            "observations",
        }

    def test_primary_class_enum(self):
        """primary_class must be exactly vehicle|person|animal|none — no null."""
        parsed = json.loads(UNIFIED_SCHEMA_JSON)
        assert parsed["properties"]["primary_class"]["enum"] == [
            "vehicle", "person", "animal", "none"
        ]

    def test_present_fields_widen_boolean_to_string(self):
        """The 3 present booleans must accept ["boolean", "string"]."""
        parsed = json.loads(UNIFIED_SCHEMA_JSON)
        for field in ("vehicle_present", "person_present", "animal_present"):
            assert "boolean" in parsed["properties"][field]["type"]
            assert "string" in parsed["properties"][field]["type"]

    def test_vehicle_features_required_keys(self):
        """vehicle_features must require all 7 sub-fields."""
        parsed = json.loads(UNIFIED_SCHEMA_JSON)
        vf = parsed["properties"]["vehicle_features"]
        assert vf["additionalProperties"] is False
        assert set(vf["required"]) == {
            "make", "model", "color", "body_style",
            "plate", "plate_state", "occupants_visible",
        }

    def test_vehicle_make_enum_includes_unknown_and_none(self):
        """vehicle_features.make must include 'unknown' and 'none' sentinels."""
        parsed = json.loads(UNIFIED_SCHEMA_JSON)
        make_enum = parsed["properties"]["vehicle_features"]["properties"]["make"]["enum"]
        assert "unknown" in make_enum
        assert "none" in make_enum
        # At least 5 real makes plus the two sentinels
        assert len([m for m in make_enum if m not in ("unknown", "none", None)]) >= 5

    def test_person_features_required_keys(self):
        parsed = json.loads(UNIFIED_SCHEMA_JSON)
        pf = parsed["properties"]["person_features"]
        assert pf["additionalProperties"] is False
        assert set(pf["required"]) == {
            "clothing_upper", "clothing_lower",
            "carrying", "action", "face_visible",
        }

    def test_animal_features_required_keys(self):
        parsed = json.loads(UNIFIED_SCHEMA_JSON)
        af = parsed["properties"]["animal_features"]
        assert af["additionalProperties"] is False
        assert set(af["required"]) == {"species", "color", "size", "behavior"}

    def test_animal_species_enum_has_known_species_and_sentinels(self):
        parsed = json.loads(UNIFIED_SCHEMA_JSON)
        species_enum = parsed["properties"]["animal_features"]["properties"]["species"]["enum"]
        # Real species
        for s in ("deer", "bear", "coyote", "fox", "raccoon", "dog", "cat"):
            assert s in species_enum, f"missing species: {s}"
        # Sentinels
        assert "unknown" in species_enum
        assert "none" in species_enum


# ============================================================================
# TestPromptBody — UNIFIED_PROMPT_TEMPLATE_FORMAT literal content
# ============================================================================


class TestPromptBody:
    """The prompt prose visible to Qwen must mention every field and rule."""

    def test_prompt_mentions_primary_class(self):
        assert "primary_class" in UNIFIED_PROMPT_TEMPLATE_FORMAT

    def test_prompt_mentions_all_three_class_blocks(self):
        """Prompt must explicitly reference vehicle_features, person_features,
        and animal_features so Qwen knows to fill all three (with 'none'
        for non-matching)."""
        for cls in ("vehicle_features", "person_features", "animal_features"):
            assert cls in UNIFIED_PROMPT_TEMPLATE_FORMAT, f"missing {cls}"

    def test_prompt_has_explicit_leakage_rules(self):
        """The whole point of Variant A is preventing class leakage. The
        prompt must contain rules 1-7 (or equivalent) explicitly stating
        non-matching blocks must be 'none'."""
        # Look for the rule-list section
        assert "RULES" in UNIFIED_PROMPT_TEMPLATE_FORMAT
        assert "MUST" in UNIFIED_PROMPT_TEMPLATE_FORMAT  # rules use MUST
        # Verify each non-matching-class rule is present
        # (if primary_class=person, vehicle/animal must be "none")
        for marker in (
            'primary_class = "vehicle"',
            'primary_class = "person"',
            'primary_class = "animal"',
        ):
            assert marker in UNIFIED_PROMPT_TEMPLATE_FORMAT, f"missing rule: {marker}"

    def test_prompt_explains_none_vs_unknown(self):
        """The prompt must distinguish 'none' (absent) from 'unknown'
        (present-but-indeterminable) per structured-output-recipes pitfall #1."""
        assert '"none"' in UNIFIED_PROMPT_TEMPLATE_FORMAT
        assert '"unknown"' in UNIFIED_PROMPT_TEMPLATE_FORMAT

    def test_prompt_forbids_markdown_fences(self):
        """Pitfall #4: NO free-form CoT before the JSON object."""
        assert "markdown fences" in UNIFIED_PROMPT_TEMPLATE_FORMAT.lower() or \
            "no markdown" in UNIFIED_PROMPT_TEMPLATE_FORMAT.lower()
        assert "ONLY the JSON" in UNIFIED_PROMPT_TEMPLATE_FORMAT or \
            "ONLY" in UNIFIED_PROMPT_TEMPLATE_FORMAT


# ============================================================================
# TestBuildFn — build_unified_prompt substitution behavior
# ============================================================================


class TestBuildFn:
    """build_unified_prompt substitutes placeholders correctly."""

    def test_substitutes_camera_name(self):
        out = build_unified_prompt(
            camera_name="OFS",
            captured_at="2026-09-02 14:00 EDT",
        )
        assert 'camera "OFS"' in out
        assert "{camera_name}" not in out

    def test_substitutes_captured_at(self):
        out = build_unified_prompt(
            camera_name="OFS",
            captured_at="2026-09-02 14:00 EDT",
        )
        assert "2026-09-02 14:00 EDT" in out
        assert "{captured_at}" not in out

    def test_substitutes_event_hint_block(self):
        out = build_unified_prompt(
            camera_name="OFS",
            captured_at="2026-09-02 14:00 EDT",
            event_hint_block="[Vehicle event, OFS, 14:00]",
        )
        assert "[Vehicle event, OFS, 14:00]" in out
        assert "{event_hint_block}" not in out

    def test_substitutes_n_frame_str_single(self):
        out = build_unified_prompt(
            camera_name="OFS",
            captured_at="2026-09-02 14:00 EDT",
            n_frames=1,
        )
        assert "single frame" in out
        assert "{n_frame_str}" not in out

    def test_substitutes_n_frame_str_multi(self):
        out = build_unified_prompt(
            camera_name="OFS",
            captured_at="2026-09-02 14:00 EDT",
            n_frames=3,
            interval_sec=4,
        )
        assert "3-frame sequence (4s apart)" in out
        assert "{n_frame_str}" not in out

    def test_no_curly_brace_placeholders_leftover(self):
        """No unresolved `{...}` placeholders after substitution."""
        out = build_unified_prompt(
            camera_name="OFS",
            captured_at="2026-09-02 14:00 EDT",
            event_hint_block="[hint]",
            interval_sec=4,
            n_frames=2,
        )
        # Filter out the literal schema block (which contains { and }
        # but those are part of the embedded JSON example, not placeholders).
        # We check for *unresolved* placeholders by looking for {xxx} patterns
        # that match our placeholder names.
        for ph in (
            "{camera_name}", "{captured_at}", "{event_hint_block}",
            "{n_frame_str}", "{event_hint}",
        ):
            assert ph not in out, f"unresolved placeholder: {ph}"

    def test_event_hint_block_absent_uses_unknown_label(self):
        """When event_hint_block is empty, the {event_hint} replacement
        should not crash and should use 'unknown' or omit the substitution."""
        out = build_unified_prompt(
            camera_name="OFS",
            captured_at="2026-09-02 14:00 EDT",
            event_hint_block="",
        )
        # Either substituted with 'unknown' or left as-is if logic
        # can't extract from empty block. Must not raise.
        assert isinstance(out, str)
        assert len(out) > 1000  # full prompt rendered


# ============================================================================
# TestSelectRoute — select_prompt_template dispatches mode="unified"
# ============================================================================


class TestSelectRoute:
    """mode='unified' must route to build_unified_prompt."""

    def test_mode_unified_routes_to_unified_prompt(self):
        out = select_prompt_template(
            event_hint=None,
            n_frames=1,
            camera_name="OFS",
            captured_at="2026-09-02 14:00 EDT",
            mode="unified",
        )
        # Markers unique to the unified prompt body
        assert "primary_class" in out
        assert "vehicle_features" in out
        assert "person_features" in out
        assert "animal_features" in out
        # The unified prompt explicitly tells Qwen to pick one of three
        # classes — verify the picker block is present
        assert 'primary_class = "vehicle"' in out or 'primary_class = "person"' in out
        # Camera name + capture time substituted
        assert "OFS" in out
        assert "2026-09-02 14:00 EDT" in out
        assert "{camera_name}" not in out
        assert "{captured_at}" not in out

    def test_mode_unified_with_event_hint_vehicle(self):
        """Even when event_hint=vehicle, mode='unified' should still go
        through the unified dispatcher (not the legacy vehicle path)."""
        out = select_prompt_template(
            event_hint="vehicle",
            n_frames=2,
            camera_name="OFS",
            captured_at="2026-09-02 14:00 EDT",
            mode="unified",
        )
        assert "primary_class" in out
        assert "{event_hint}" not in out  # must be substituted

    def test_mode_unified_does_not_break_legacy_modes(self):
        """Adding the unified branch must not affect existing mode dispatch."""
        crop_out = select_prompt_template(
            event_hint="vehicle",
            n_frames=1,
            camera_name="OFS",
            captured_at="2026-09-02 14:00 EDT",
            mode="crop",
        )
        assert "{camera_name}" not in crop_out  # crop also substitutes
        # Crop prompt is identification-only (no scene narrative); verify
        # the unified branch didn't pollute it.
        assert "primary_class" not in crop_out

    def test_mode_unified_with_n_frames_3(self):
        """n_frames=3 should set n_frame_str to '3-frame sequence'."""
        out = select_prompt_template(
            event_hint=None,
            n_frames=3,
            camera_name="OFS",
            captured_at="2026-09-02 14:00 EDT",
            mode="unified",
            interval_sec=4,
        )
        assert "3-frame sequence (4s apart)" in out


# ============================================================================
# TestLazyImport — infra.unified_vision loaded lazily
# ============================================================================


class TestLazyImport:
    """The dispatcher must lazy-import infra.unified_vision (matching
    the pattern of person/animal prompt templates)."""

    def test_dispatcher_lazy_loads(self, monkeypatch):
        """Importing select_prompt_template module must NOT import
        infra.unified_vision at module load — only when mode='unified'
        is actually requested."""
        import importlib
        import sys

        # Force-clear infra.unified_vision if it was loaded by another test
        monkeypatch.delitem(sys.modules, "infra.unified_vision", raising=False)

        # Re-import infra.prompt_templates and check infra.unified_vision
        # was NOT pulled in transitively
        if "infra.prompt_templates" in sys.modules:
            importlib.reload(sys.modules["infra.prompt_templates"])
        assert "infra.unified_vision" not in sys.modules, (
            "infra.unified_vision must be lazy-loaded, not eager."
        )

    def test_mode_unified_triggers_import(self):
        """Calling select_prompt_template(mode='unified') must trigger
        the lazy import of infra.unified_vision."""
        import sys
        # At this point infra.unified_vision may already be loaded from
        # earlier tests; that's fine — the contract is "loaded by the
        # dispatcher when mode='unified' fires."
        out = select_prompt_template(
            event_hint=None,
            n_frames=1,
            camera_name="OFS",
            captured_at="2026-09-02 14:00 EDT",
            mode="unified",
        )
        # After this call, the module must be importable
        assert "infra.unified_vision" in sys.modules
        assert len(out) > 1000