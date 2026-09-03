"""
test_animal_prompt_template_6B165_3.py — Tests for the wider-scope
animal prompt template (Phase.165 §11.86.3, revised 2026-08-29).

Mirrors the structure of infra.tests.test_person_prompt_template
(per-module test convention). Each test class groups assertions about
one contract surface:

  TestSchemaFields — the JSON schema literal ANIMAL_SCHEMA_JSON
  TestPromptBody    — the rendered prompt string's content
  TestBuildFn       — build_animal_prompt() substitution behavior
  TestSelectRoute   — select_prompt_template(mode="animal") dispatch

Per the wider-scope design (2026-08-29):
  - species is FREE-FORM (no enum constraint)
  - species_confidence is a separate field (definite/likely/unsure)
  - distinctive_features is an ARRAY (not a single string)
  - face_details is a NESTED OBJECT
  - YOLO hint is passed but Qwen is told to OVERRIDE it
  - The prompt must let Qwen distinguish individual animals of the
    same species (coyote A vs coyote B).

All assertions reference the literal template + schema constants so
any drift in the prompt body is caught immediately.
"""


from infra.animal_prompt_template import (
    ANIMAL_PROMPT_TEMPLATE_FORMAT,
    ANIMAL_SCHEMA_JSON,
    build_animal_prompt,
)
from infra.prompt_templates import select_prompt_template

# ============================================================================
# TestSchemaFields — ANIMAL_SCHEMA_JSON literal structure
# ============================================================================


class TestSchemaFields:
    """Schema literal must reflect the wider-scope design."""

    def test_schema_is_a_nonempty_string(self):
        """The schema literal must be a non-empty string embedded in the prompt.

        It is read by Qwen as text, not parsed by us — so we don't
        require it to be strict JSON (newlines and pipe-enum
        placeholders are allowed). The only requirement is that it
        contains all the field names Qwen must emit.
        """
        assert isinstance(ANIMAL_SCHEMA_JSON, str)
        assert len(ANIMAL_SCHEMA_JSON) > 1000
        # Top-level keys must be present
        assert '"animals"' in ANIMAL_SCHEMA_JSON
        assert '"primary_animal_index"' in ANIMAL_SCHEMA_JSON
        assert '"confidence"' in ANIMAL_SCHEMA_JSON

    def test_schema_species_is_free_form_string(self):
        """species field is NOT an enum — it must be a free-form string.

        The wider-scope design (2026-08-29) requires that Qwen can
        emit any species name (coyote, wolf, fox, fisher, raccoon,
        deer, wild turkey, etc.) and that hybrids are acceptable.
        """
        # Schema field is "species": "<free-form example string>" | null
        # Verify the field exists and that "species" is not followed by
        # a pipe-delimited enum like the legacy first-draft
        # ("species": "dog" | "cat" | ...).
        schema = ANIMAL_SCHEMA_JSON
        assert '"species"' in schema
        # Should NOT contain a species enum like "dog" | "cat" |
        # "horse" | "sheep" | "cow" | "bear" | "bird" as the only
        # allowed values.
        legacy_enum = '"dog" | "cat" | "horse" | "sheep" | "cow" | "bear" | "bird"'
        assert legacy_enum not in schema, (
            "schema still has the legacy 7-class YOLO species enum; "
            "Note 2026-08-29 wants free-form species"
        )
        # Should contain "free-form species name" guidance instead.
        assert "free-form species name" in schema, (
            "schema should explicitly tell Qwen species is free-form"
        )

    def test_schema_has_species_confidence_field(self):
        """species_confidence enum: definite | likely | unsure | null.

        The matcher raises its threshold to 0.65 when Qwen reports
        "unsure", so this field is load-bearing for downstream logic.
        """
        schema = ANIMAL_SCHEMA_JSON
        assert '"species_confidence"' in schema
        # All three confidence values present
        assert '"definite"' in schema
        assert '"likely"' in schema
        assert '"unsure"' in schema

    def test_schema_distinctive_features_is_an_array(self):
        """distinctive_features is an ARRAY, not a single string.

        Note 2026-08-29: "the distinguishing characteristics result
        should be enough that it could identify different coyotes from
        one another, for example." A single-string field can't
        distinguish individuals; an array of features can.
        """
        schema = ANIMAL_SCHEMA_JSON
        assert '"distinctive_features"' in schema
        # Look for the array syntax right after the field name
        # e.g. '"distinctive_features": [\n'
        idx = schema.index('"distinctive_features"')
        # Next 60 chars should show '[' not '"'
        after = schema[idx:idx+60]
        assert "[" in after.split(":")[1][:50], (
            "distinctive_features must be an array, not a single string"
        )
        # The old 'distinctive_markings' (singular string) field is removed.
        assert '"distinctive_markings"' not in schema, (
            "legacy singular-string field distinctive_markings must be "
            "removed; the wider-scope schema uses distinctive_features[]"
        )

    def test_schema_face_details_is_a_nested_object(self):
        """face_details is a nested object with ear_shape, tail_carriage, mask.

        These three sub-fields are the textbook coyote-vs-wolf-vs-fox
        discriminators when the body is too small/blurry to see.
        """
        schema = ANIMAL_SCHEMA_JSON
        assert '"face_details"' in schema
        # Look for the inner braces after face_details
        idx = schema.index('"face_details"')
        after = schema[idx:idx+400]
        assert '"ear_shape"' in after
        assert '"tail_carriage"' in after
        assert '"mask"' in after
        # Each should be an enum
        assert '"pointed"' in after
        assert '"floppy"' in after
        assert '"curled"' in after

    def test_schema_has_body_build_and_coat_pattern(self):
        """body_build and coat_pattern are new fields in wider-scope schema.

        body_build separates lean/stocky/athletic/compact from
        body_size — useful for distinguishing individual coyotes.
        coat_pattern separates solid/bi-color/tri-color/tabby from
        coat_primary_color.
        """
        schema = ANIMAL_SCHEMA_JSON
        assert '"body_build"' in schema
        assert '"lean"' in schema
        assert '"stocky"' in schema
        assert '"athletic"' in schema
        assert '"compact"' in schema
        assert '"coat_pattern"' in schema
        assert '"bi-color"' in schema
        assert '"tri-color"' in schema

    def test_schema_has_estimated_age_and_sex_signal(self):
        """estimated_age (juvenile/adult/senior) and sex_signal
        (male/female/neutered) are present.
        """
        schema = ANIMAL_SCHEMA_JSON
        assert '"estimated_age"' in schema
        assert '"juvenile"' in schema
        assert '"adult"' in schema
        assert '"senior"' in schema
        assert '"sex_signal"' in schema
        assert '"neutered"' in schema

    def test_schema_coat_primary_color_matches_clothing_enum(self):
        """coat_primary_color uses the same 13-color enum as person clothing.

        Lets animal_matcher reuse person_matcher's _normalize_color +
        _color_similarity helpers directly.
        """
        schema = ANIMAL_SCHEMA_JSON
        for color in [
            "black", "white", "gray", "silver", "red", "blue", "green",
            "yellow", "brown", "orange", "pink", "purple",
        ]:
            assert f'"{color}"' in schema, (
                f"coat_primary_color enum missing {color}"
            )

    def test_schema_has_behavior_field_for_threat_classifier(self):
        """behavior field preserved for downstream threat classification
        (§11.86.6 — "approaching door" vs "passing through" is a key
        signal but doesn't belong in matching).
        """
        schema = ANIMAL_SCHEMA_JSON
        assert '"behavior"' in schema
        assert "free-form short verb" in schema

    def test_schema_does_not_have_legacy_breed_field(self):
        """The legacy 'breed' field is removed in the wider-scope schema.

        Note's pivot (2026-08-29) treats free-form species as the
        primary determinant — breed is too narrow for wild canids
        (mixed coywolves, feral cats) and the matcher's distinctive_
        features[] Jaccard does the individual-identification job.
        """
        schema = ANIMAL_SCHEMA_JSON
        # 'breed' should not appear as a top-level field
        # (it may still appear in the prompt body as guidance).
        # Look for it as a schema field:
        assert '"breed"' not in schema, (
            "legacy breed field must be removed from the schema; "
            "wider-scope uses distinctive_features[] + face_details{}"
        )

    def test_schema_does_not_have_face_or_clothing_fields(self):
        """Animal events are orthogonal to person events.

        The animal pipeline should NOT request face_bbox or clothing
        fields — those belong to PERSON_PROMPT_TEMPLATE_FORMAT.
        """
        schema = ANIMAL_SCHEMA_JSON
        assert '"face_bbox"' not in schema
        assert '"clothing_upper"' not in schema
        assert '"clothing_lower"' not in schema

    def test_schema_has_primary_animal_index(self):
        """primary_animal_index is required for downstream matcher dispatch."""
        schema = ANIMAL_SCHEMA_JSON
        assert '"primary_animal_index"' in schema
        assert '"confidence"' in schema
        assert '"notable_details"' in schema
        assert '"frame_positions"' in schema


# ============================================================================
# TestPromptBody — ANIMAL_PROMPT_TEMPLATE_FORMAT body content
# ============================================================================


class TestPromptBody:
    """The rendered prompt's body must instruct Qwen correctly."""

    def test_prompt_body_overrides_yolo_hint(self):
        """Prompt must explicitly tell Qwen its species call OVERRIDES YOLO.

        Note 2026-08-29: "vision model is smarter than Yolo" —
        Qwen should not be anchored to YOLO's class label.
        """
        rendered = build_animal_prompt(
            camera_name="OFS",
            captured_at="2026-08-30 14:00 EDT",
            event_hint_block="[Animal event]",
            species_hint="dog",
        )
        assert "OVERRIDES the YOLO hint" in rendered, (
            "prompt body must contain the OVERRIDE language; "
            "Qwen shouldn't be anchored to YOLO's class"
        )

    def test_prompt_body_mentions_yolo_override_examples(self):
        """Body gives Qwen specific override examples (dog→coyote, bear→raccoon)."""
        rendered = build_animal_prompt(
            camera_name="OFS",
            captured_at="2026-08-30 14:00 EDT",
            event_hint_block="[Animal event]",
            species_hint="dog",
        )
        assert "coyote" in rendered
        assert "raccoon" in rendered
        # Trust your eyes over the gate
        assert "Trust your eyes" in rendered

    def test_prompt_body_species_is_free_form(self):
        """Prompt tells Qwen species is free-form with example wild canids."""
        rendered = build_animal_prompt(
            camera_name="OFS",
            captured_at="2026-08-30 14:00 EDT",
            event_hint_block="[Animal event]",
            species_hint="bird",
        )
        # Free-form guidance present
        assert "FREE-FORM species name" in rendered
        # Example species from the wider-scope design
        for example in [
            "coyote", "Eastern coyote", "wolf", "red fox", "fisher",
            "raccoon", "deer", "wild turkey", "red-tailed hawk",
            "bobcat", "coydog",
        ]:
            assert example in rendered, (
                f"prompt body should mention '{example}' as a "
                f"free-form species example"
            )

    def test_prompt_body_distinctive_features_is_array(self):
        """Prompt must tell Qwen distinctive_features is an ARRAY."""
        rendered = build_animal_prompt(
            camera_name="OFS",
            captured_at="2026-08-30 14:00 EDT",
            event_hint_block="[Animal event]",
            species_hint="dog",
        )
        assert "distinctive_features" in rendered
        # The body must explicitly call it an ARRAY
        idx = rendered.index("distinctive_features")
        after = rendered[idx:idx+200]
        assert "ARRAY" in after, (
            "prompt body must say distinctive_features is an ARRAY "
            "(the legacy field was a single string and confused Qwen)"
        )

    def test_prompt_body_distinctive_features_examples_for_individual_id(self):
        """Prompt gives Qwen example features for distinguishing individuals.

        Note 2026-08-29: "the distinguishing characteristics result
        should be enough that it could identify different coyotes from
        one another."
        """
        rendered = build_animal_prompt(
            camera_name="OFS",
            captured_at="2026-08-30 14:00 EDT",
            event_hint_block="[Animal event]",
            species_hint="dog",
        )
        # Coyote individual-id examples
        assert "left ear notched" in rendered
        assert "white-tipped tail" in rendered
        # Dog examples
        assert "blue collar" in rendered or "torn left ear" in rendered
        # The 'different coyotes' framing
        assert "individual" in rendered.lower() or "different coyotes" in rendered

    def test_prompt_body_species_confidence_hedge(self):
        """Prompt must instruct Qwen to hedge with species_confidence."""
        rendered = build_animal_prompt(
            camera_name="OFS",
            captured_at="2026-08-30 14:00 EDT",
            event_hint_block="[Animal event]",
            species_hint="dog",
        )
        assert "species_confidence" in rendered
        # The body must tell Qwen 'unsure' raises the matcher threshold
        # (so Qwen uses it honestly, not as a default).
        idx = rendered.index("species_confidence")
        after = rendered[idx:idx+400].lower()
        assert "matcher" in after or "threshold" in after, (
            "prompt body must mention that 'unsure' raises the "
            "matcher threshold so Qwen uses it honestly"
        )

    def test_prompt_body_face_details_canid_discriminators(self):
        """Prompt tells Qwen about ear_shape/tail_carriage wild canid discriminators."""
        rendered = build_animal_prompt(
            camera_name="OFS",
            captured_at="2026-08-30 14:00 EDT",
            event_hint_block="[Animal event]",
            species_hint="dog",
        )
        assert "ear_shape" in rendered
        assert "tail_carriage" in rendered
        # Wild canid framing
        idx = rendered.index("face_details")
        after = rendered[idx:idx+600].lower()
        assert "coyote" in after and "wolf" in after and "fox" in after, (
            "face_details body must explain coyote/wolf/fox discriminators"
        )

    def test_prompt_body_uses_null_not_unknown(self):
        """Prompt follows person-prompt convention: null not 'unknown'."""
        rendered = build_animal_prompt(
            camera_name="OFS",
            captured_at="2026-08-30 14:00 EDT",
            event_hint_block="[Animal event]",
            species_hint="dog",
        )
        assert "return null" in rendered.lower()
        assert 'not "unknown"' in rendered or "not 'unknown'" in rendered

    def test_prompt_body_has_decision_rule_for_no_animal(self):
        """If no animal is visible, return animals=[] with high confidence."""
        rendered = build_animal_prompt(
            camera_name="OFS",
            captured_at="2026-08-30 14:00 EDT",
            event_hint_block="[Animal event]",
            species_hint="dog",
        )
        assert "no animal is visible" in rendered.lower() or \
               "no animal visible" in rendered.lower()
        assert '"animals": []' in rendered

    def test_prompt_body_requests_json_only_no_markdown(self):
        """Prompt asks for JSON only — no preamble, no markdown fences."""
        rendered = build_animal_prompt(
            camera_name="OFS",
            captured_at="2026-08-30 14:00 EDT",
            event_hint_block="[Animal event]",
            species_hint="dog",
        )
        assert "Output ONLY the JSON object" in rendered
        assert "No markdown fences" in rendered or "no markdown" in rendered.lower()


# ============================================================================
# TestBuildFn — build_animal_prompt() substitution behavior
# ============================================================================


class TestBuildFn:
    """The build function correctly substitutes all placeholders."""

    def test_substitutes_camera_name(self):
        out = build_animal_prompt(
            camera_name="OFS",
            captured_at="2026-08-30 14:00 EDT",
        )
        assert 'Camera "OFS"' in out
        assert "{camera_name}" not in out

    def test_substitutes_captured_at(self):
        out = build_animal_prompt(
            camera_name="OFS",
            captured_at="2026-08-30 14:00 EDT",
        )
        assert "2026-08-30 14:00 EDT" in out
        assert "{captured_at}" not in out

    def test_substitutes_event_hint_block(self):
        out = build_animal_prompt(
            camera_name="OFS",
            captured_at="2026-08-30 14:00 EDT",
            event_hint_block="[Animal event, OFS, 14:00]",
        )
        assert "[Animal event, OFS, 14:00]" in out
        assert "{event_hint_block}" not in out

    def test_substitutes_schema_json(self):
        out = build_animal_prompt(
            camera_name="OFS",
            captured_at="2026-08-30 14:00 EDT",
        )
        assert ANIMAL_SCHEMA_JSON in out
        assert "{schema_json}" not in out

    def test_substitutes_interval_sec(self):
        out = build_animal_prompt(
            camera_name="OFS",
            captured_at="2026-08-30 14:00 EDT",
            interval_sec=7,
        )
        assert "7s apart" in out
        assert "{interval_sec}" not in out

    def test_substitutes_species_hint(self):
        """YOLO's class label is rendered into the prompt as context."""
        out = build_animal_prompt(
            camera_name="OFS",
            captured_at="2026-08-30 14:00 EDT",
            species_hint="bear",
        )
        assert 'class hint: "bear"' in out
        assert "{species_hint}" not in out

    def test_species_hint_default_is_unknown(self):
        """Default species_hint is 'unknown' so callers without YOLO work."""
        out = build_animal_prompt(
            camera_name="OFS",
            captured_at="2026-08-30 14:00 EDT",
        )
        assert 'class hint: "unknown"' in out

    def test_no_curly_brace_placeholders_leftover(self):
        """No unresolved `{...}` placeholders remain after substitution."""
        out = build_animal_prompt(
            camera_name="OFS",
            captured_at="2026-08-30 14:00 EDT",
            event_hint_block="[hint]",
            interval_sec=4,
            species_hint="dog",
        )
        # Filter out the literal JSON braces in ANIMAL_SCHEMA_JSON
        # (these are content, not placeholders).
        # We only care that none of the placeholder names survived.
        for placeholder in [
            "{camera_name}", "{captured_at}", "{event_hint_block}",
            "{interval_sec}", "{species_hint}", "{schema_json}",
        ]:
            assert placeholder not in out, (
                f"placeholder {placeholder} leaked into output"
            )

    def test_custom_event_hint_block(self):
        """An empty event_hint_block still substitutes cleanly."""
        out = build_animal_prompt(
            camera_name="OFS",
            captured_at="2026-08-30 14:00 EDT",
            event_hint_block="",
        )
        # The empty string is rendered; the schema still follows.
        assert ANIMAL_SCHEMA_JSON in out


# ============================================================================
# TestSelectRoute — select_prompt_template(mode="animal") dispatch
# ============================================================================


class TestSelectRoute:
    """The select_prompt_template dispatcher routes mode='animal' correctly."""

    def test_mode_animal_returns_animal_template(self):
        out = select_prompt_template(
            event_hint=None,
            n_frames=2,
            camera_name="OFS",
            captured_at="2026-08-30 14:00 EDT",
            event_hint_block="[Animal event, OFS]",
            mode="animal",
            species_hint="dog",
        )
        # Should be the animal prompt (with YOLO override language)
        assert "OVERRIDES the YOLO hint" in out
        assert "FREE-FORM species name" in out
        # And NOT the legacy/person/vehicle prompts
        assert "make" not in out.lower() or "make sure" in out.lower()
        assert "clothing_upper" not in out

    def test_mode_animal_ignores_event_hint(self):
        """mode='animal' short-circuits BEFORE the event_hint vehicle guard."""
        out = select_prompt_template(
            event_hint="vehicle",  # legacy vehicle hint shouldn't matter
            n_frames=2,
            camera_name="OFS",
            captured_at="2026-08-30 14:00 EDT",
            mode="animal",
            species_hint="dog",
        )
        # Should still be the animal prompt, not the vehicle prompt
        assert "OVERRIDES the YOLO hint" in out
        assert "Animal-event analysis" in out

    def test_mode_animal_forwards_species_hint(self):
        """species_hint kwarg is passed through to build_animal_prompt."""
        out = select_prompt_template(
            event_hint=None,
            n_frames=2,
            camera_name="OFS",
            captured_at="2026-08-30 14:00 EDT",
            mode="animal",
            species_hint="horse",
        )
        assert 'class hint: "horse"' in out

    def test_mode_animal_default_species_hint_is_unknown(self):
        """When species_hint is not passed, default is 'unknown'."""
        out = select_prompt_template(
            event_hint=None,
            n_frames=2,
            camera_name="OFS",
            captured_at="2026-08-30 14:00 EDT",
            mode="animal",
        )
        assert 'class hint: "unknown"' in out

    def test_mode_animal_with_no_event_hint_renders(self):
        """Animal mode works with event_hint=None (animal pipeline passes that)."""
        out = select_prompt_template(
            event_hint=None,
            n_frames=2,
            camera_name="OFS",
            captured_at="2026-08-30 14:00 EDT",
            mode="animal",
            species_hint="dog",
        )
        assert "Animal-event analysis" in out

    def test_mode_animal_with_event_hint_block(self):
        """event_hint_block is rendered into the prompt body."""
        out = select_prompt_template(
            event_hint=None,
            n_frames=2,
            camera_name="OFS",
            captured_at="2026-08-30 14:00 EDT",
            event_hint_block="[Animal event, OFS, 14:00]",
            mode="animal",
            species_hint="dog",
        )
        assert "[Animal event, OFS, 14:00]" in out

    def test_mode_person_still_routes_to_person(self):
        """The person short-circuit is preserved (no regression)."""
        out = select_prompt_template(
            event_hint=None,
            n_frames=2,
            camera_name="FDO",
            captured_at="2026-08-30 14:00 EDT",
            mode="person",
        )
        # Person template's distinctive marker (clothing_upper)
        assert "clothing_upper" in out

    def test_mode_crop_still_routes_to_vehicle_crop(self):
        """The crop short-circuit is preserved (no regression)."""
        out = select_prompt_template(
            event_hint=None,
            n_frames=2,
            camera_name="OFS",
            captured_at="2026-08-30 14:00 EDT",
            mode="crop",
        )
        # Vehicle crop template's distinctive marker
        assert "make" in out.lower()
        assert "model" in out.lower()

    def test_mode_auto_single_frame_still_crop(self):
        """Auto-dispatch: 1 frame → crop, no regression."""
        out = select_prompt_template(
            event_hint="vehicle",
            n_frames=1,
            camera_name="OFS",
            captured_at="2026-08-30 14:00 EDT",
        )
        # 1 frame auto → crop → vehicle identification prompt
        assert "make" in out.lower()
        assert "model" in out.lower()


# ============================================================================
# TestLazyImport — module isolation
# ============================================================================


class TestLazyImport:
    """animal_prompt_template is lazy-imported to keep prompt_templates light."""

    def test_animal_prompt_template_importable(self):
        from infra.animal_prompt_template import (
            ANIMAL_SCHEMA_JSON,
            build_animal_prompt,
        )
        assert callable(build_animal_prompt)
        assert isinstance(ANIMAL_SCHEMA_JSON, str)
        assert isinstance(ANIMAL_PROMPT_TEMPLATE_FORMAT, str)

    def test_animal_prompt_template_exports(self):
        """__all__ exposes the three public symbols."""
        import infra.animal_prompt_template as m
        assert "ANIMAL_PROMPT_TEMPLATE_FORMAT" in m.__all__
        assert "ANIMAL_SCHEMA_JSON" in m.__all__
        assert "build_animal_prompt" in m.__all__
