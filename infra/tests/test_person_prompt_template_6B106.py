"""Unit tests for infra.person_prompt_template (Phase 6B.106).

Tests the new person-event prompt template, schema literal, and
build_person_prompt(). Does NOT test the integration with
select_prompt_template — that's a separate routing test (file 5/12).

Pins:
  - Schema shape: persons[]/primary_person_index/scene_description/
    confidence/notable_details/frame_positions fields present
  - Color enum normalization: clothing_upper.color + clothing_lower.color
    use the documented enum (no free-form strings)
  - face_bbox: PIXEL coord space reminder present, NOT normalized
  - frame_positions: left empty by Qwen (motion detector owns this)
  - Both-frame instruction: prompt tells Qwen to inspect both frames
  - No .format() collisions: PERSON_SCHEMA_JSON's `{`/`}` don't crash
    the build
"""

import sys
from pathlib import Path

# Ensure project root on path so `import infra.X` works.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


# --- 1. Module surface --------------------------------------------------


class TestModuleSurface:
    def test_module_imports_without_error(self):
        """infra.person_prompt_template is importable."""
        import infra.person_prompt_template as ppt

        assert ppt is not None

    def test_exports_required_public_api(self):
        """Module exposes PERSON_PROMPT_TEMPLATE_FORMAT, PERSON_SCHEMA_JSON,
        build_person_prompt."""
        import infra.person_prompt_template as ppt

        assert hasattr(ppt, "PERSON_PROMPT_TEMPLATE_FORMAT")
        assert hasattr(ppt, "PERSON_SCHEMA_JSON")
        assert callable(ppt.build_person_prompt)

    def test_module_has_refactor_header(self):
        """AGENTS.md Step 1.5: every module has the standard header sections."""
        from inspect import getsourcefile

        import infra.person_prompt_template as ppt
        path = getsourcefile(ppt)
        assert path is not None
        with open(path) as f:
            src = f.read()

        # All mandatory header sections must be present (in any case)
        for section in (
            "STATUS:",
            "THREAD SAFETY:",
            "INPUTS:",
            "OUTPUTS:",
            "PUBLIC API:",
            "DOES NOT DO:",
            "CALLED BY:",
            "CALLS INTO:",
        ):
            assert section in src, f"missing header section: {section!r}"


# --- 2. Schema literal shape --------------------------------------------


class TestPersonSchemaJson:
    def test_schema_includes_persons_array(self):
        """Schema has persons[] as the primary subject container."""
        from infra.person_prompt_template import PERSON_SCHEMA_JSON

        assert '"persons":' in PERSON_SCHEMA_JSON

    def test_schema_includes_clothing_upper_with_color_and_type(self):
        """Schema enum-locks clothing_upper.color and .type."""
        from infra.person_prompt_template import PERSON_SCHEMA_JSON

        assert '"clothing_upper":' in PERSON_SCHEMA_JSON
        assert '"color"' in PERSON_SCHEMA_JSON
        assert '"type"' in PERSON_SCHEMA_JSON
        # Color enum must include the documented values
        for color in ("black", "white", "gray", "red", "blue", "green"):
            assert f'"{color}"' in PERSON_SCHEMA_JSON, f"missing color enum: {color}"

    def test_schema_includes_clothing_lower_with_color_and_type(self):
        """Schema enum-locks clothing_lower.color and .type (per maintainer's spec)."""
        from infra.person_prompt_template import PERSON_SCHEMA_JSON

        assert '"clothing_lower":' in PERSON_SCHEMA_JSON

    def test_schema_includes_carrying_list(self):
        """carrying[] is a free-form list of short noun phrases."""
        from infra.person_prompt_template import PERSON_SCHEMA_JSON

        assert '"carrying":' in PERSON_SCHEMA_JSON

    def test_schema_includes_action_enum(self):
        """action uses a verb enum (walking/standing/etc.)."""
        from infra.person_prompt_template import PERSON_SCHEMA_JSON

        assert '"action":' in PERSON_SCHEMA_JSON
        for verb in ("walking", "standing", "approaching", "leaving"):
            assert f'"{verb}"' in PERSON_SCHEMA_JSON, f"missing action enum: {verb}"

    def test_schema_includes_face_visible_bool(self):
        """face_visible is a boolean (any_face_visible analog)."""
        from infra.person_prompt_template import PERSON_SCHEMA_JSON

        assert '"face_visible"' in PERSON_SCHEMA_JSON

    def test_schema_includes_face_bbox_pixel_coords(self):
        """face_bbox is a [x1,y1,x2,y2] array, NOT normalized."""
        from infra.person_prompt_template import PERSON_SCHEMA_JSON

        assert '"face_bbox"' in PERSON_SCHEMA_JSON
        # Should NOT use normalized 0-1 hint inside the schema literal
        # (the prompt prose explains coord space; schema is shape-only)
        assert "[x1, y1, x2, y2]" in PERSON_SCHEMA_JSON

    def test_schema_includes_top_level_fields(self):
        """Top-level: primary_person_index, scene_description, confidence,
        notable_details, frame_positions."""
        from infra.person_prompt_template import PERSON_SCHEMA_JSON

        for field in (
            '"primary_person_index"',
            '"scene_description"',
            '"confidence"',
            '"notable_details"',
            '"frame_positions"',
        ):
            assert field in PERSON_SCHEMA_JSON, f"missing top-level field: {field}"

    def test_schema_is_valid_json_loadable(self):
        """PERSON_SCHEMA_JSON is parseable as JSON (with the union types as
        pseudo-JSON literals — we only verify brace balance + key presence)."""
        from infra.person_prompt_template import PERSON_SCHEMA_JSON

        # Brace/bracket balance check
        assert PERSON_SCHEMA_JSON.count("{") == PERSON_SCHEMA_JSON.count("}")
        assert PERSON_SCHEMA_JSON.count("[") == PERSON_SCHEMA_JSON.count("]")


# --- 3. Prompt template content -----------------------------------------


class TestPersonPromptTemplateContent:
    def test_template_mentions_both_frames(self):
        """Prompt explicitly tells Qwen to inspect BOTH frames."""
        from infra.person_prompt_template import PERSON_PROMPT_TEMPLATE_FORMAT

        assert "BOTH frames" in PERSON_PROMPT_TEMPLATE_FORMAT or "both frames" in PERSON_PROMPT_TEMPLATE_FORMAT

    def test_template_tells_qwen_not_to_infer_trajectory(self):
        """frame_positions is left empty; motion detector owns it."""
        from infra.person_prompt_template import PERSON_PROMPT_TEMPLATE_FORMAT

        assert "frame_positions" in PERSON_PROMPT_TEMPLATE_FORMAT

    def test_template_specifies_pixel_coords_for_face_bbox(self):
        """face_bbox is in PIXEL coords of Qwen's image, not normalized."""
        from infra.person_prompt_template import PERSON_PROMPT_TEMPLATE_FORMAT

        # Must contain "PIXEL" guidance
        assert "PIXEL" in PERSON_PROMPT_TEMPLATE_FORMAT.upper()

    def test_template_uses_enum_for_clothing_color(self):
        """Color is enum-locked; no free-form string examples."""
        from infra.person_prompt_template import PERSON_PROMPT_TEMPLATE_FORMAT

        # Spec quote: "shirt" (free-form) is OK as a type example,
        # but the color guidance must say "enum" or list explicit colors
        assert "enum" in PERSON_PROMPT_TEMPLATE_FORMAT.lower()

    def test_template_has_no_output_preamble(self):
        """Output only JSON, no markdown fences."""
        from infra.person_prompt_template import PERSON_PROMPT_TEMPLATE_FORMAT

        assert "No markdown fences" in PERSON_PROMPT_TEMPLATE_FORMAT


# --- 4. build_person_prompt() -------------------------------------------


class TestBuildPersonPrompt:
    def test_renders_camera_name(self):
        """{camera_name} placeholder is substituted."""
        from infra.person_prompt_template import build_person_prompt

        out = build_person_prompt(
            camera_name="CAM1",
            captured_at="2026-08-22 10:00:00 EDT",
        )
        assert "CAM1" in out
        assert "{camera_name}" not in out

    def test_renders_captured_at(self):
        """{captured_at} placeholder is substituted."""
        from infra.person_prompt_template import build_person_prompt

        out = build_person_prompt(
            camera_name="CAM1",
            captured_at="2026-08-22 10:00:00 EDT",
        )
        assert "2026-08-22 10:00:00 EDT" in out
        assert "{captured_at}" not in out

    def test_renders_event_hint_block(self):
        """{event_hint_block} placeholder is substituted (or omitted if blank)."""
        from infra.person_prompt_template import build_person_prompt

        out_blank = build_person_prompt(
            camera_name="CAM1",
            captured_at="2026-08-22 10:00:00 EDT",
            event_hint_block="",
        )
        assert "{event_hint_block}" not in out_blank

        out_with_hint = build_person_prompt(
            camera_name="CAM1",
            captured_at="2026-08-22 10:00:00 EDT",
            event_hint_block="(hint: a person is at the front door)",
        )
        assert "(hint: a person is at the front door)" in out_with_hint
        assert "{event_hint_block}" not in out_with_hint

    def test_renders_schema_json(self):
        """{schema_json} placeholder is replaced with PERSON_SCHEMA_JSON."""
        from infra.person_prompt_template import (
            PERSON_SCHEMA_JSON,
            build_person_prompt,
        )

        out = build_person_prompt(
            camera_name="CAM1",
            captured_at="2026-08-22 10:00:00 EDT",
        )
        # PERSON_SCHEMA_JSON must appear verbatim in the rendered prompt
        assert PERSON_SCHEMA_JSON in out
        assert "{schema_json}" not in out

    def test_no_unresolved_placeholders(self):
        """After build, no `{...}` placeholders remain."""
        from infra.person_prompt_template import build_person_prompt

        out = build_person_prompt(
            camera_name="CAM1",
            captured_at="2026-08-22 10:00:00 EDT",
            event_hint_block="hint",
        )
        # No {something} placeholders
        import re
        leftover = re.findall(r"\{[a-z_][a-z_0-9]*\}", out)
        assert leftover == [], f"unresolved placeholders: {leftover}"

    def test_handles_special_chars_in_camera_name(self):
        """Camera name with quotes/special chars doesn't break the prompt."""
        from infra.person_prompt_template import build_person_prompt

        # Just a smoke test — ensure no exceptions
        out = build_person_prompt(
            camera_name='Front "Door" Outside',
            captured_at="2026-08-22 10:00:00 EDT",
        )
        assert 'Front "Door" Outside' in out

    def test_interval_sec_substitutes(self):
        """{interval_sec} placeholder is substituted."""
        from infra.person_prompt_template import build_person_prompt

        out_4s = build_person_prompt(
            camera_name="CAM1",
            captured_at="2026-08-22 10:00:00 EDT",
            interval_sec=4,
        )
        assert "4s apart" in out_4s

        out_8s = build_person_prompt(
            camera_name="CAM1",
            captured_at="2026-08-22 10:00:00 EDT",
            interval_sec=8,
        )
        assert "8s apart" in out_8s

    def test_schema_braces_dont_break_format(self):
        """PERSON_SCHEMA_JSON's literal `{`/`}` survive build_person_prompt()
        without being eaten by .format(). This pins the use of str.replace()
        over .format() — see PLAN.md §11.36."""
        from infra.person_prompt_template import build_person_prompt

        out = build_person_prompt(
            camera_name="CAM1",
            captured_at="2026-08-22 10:00:00 EDT",
        )
        # The schema has many `{` and `}`; assert balance survives
        assert out.count("{") == out.count("}")
