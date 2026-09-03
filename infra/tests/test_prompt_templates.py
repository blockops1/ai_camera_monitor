"""
Tests for infra/prompt_templates.py — Qwen3-VL prompt text + JSON schemas.

Covers:
    - select_prompt_template: dispatch by event_hint and mode
      (legacy / moving / static / crop / combined / auto)
    - _build_event_hint_block: mapping + motion guidance for vehicle
    - _build_messages: returns OpenAI-compatible shape with text + image parts
    - JSON schemas: presence + required fields + vehicle_features structure
"""

import pytest

from infra.prompt_templates import (
    PROMPT_TEMPLATE,
    VEHICLE_CLASSIFY_PROMPT,
    VEHICLE_CLASSIFY_SCHEMA,
    VEHICLE_CROP_PROMPT_TEMPLATE,
    VEHICLE_CROP_SCHEMA_JSON,
    VEHICLE_STATIC_PROMPT_TEMPLATE,
    VISION_SCHEMA_JSON,
    _build_event_hint_block,
    select_prompt_template,
)

# =============================================================================
# select_prompt_template — non-vehicle path
# =============================================================================


class TestSelectPromptTemplateNonVehicle:
    """event_hint != 'vehicle' should always use the legacy PROMPT_TEMPLATE."""

    def test_person_event_uses_legacy(self):
        out = select_prompt_template(
            event_hint="person", n_frames=6,
            camera_name="Front Door",
        )
        assert "Analyze these frames" in out
        assert "security camera analyst" in out

    def test_motion_event_uses_legacy(self):
        out = select_prompt_template(event_hint="motion", n_frames=6)
        assert "security camera analyst" in out

    def test_animal_event_uses_legacy(self):
        out = select_prompt_template(event_hint="animal", n_frames=3)
        assert "security camera analyst" in out

    def test_no_event_uses_legacy(self):
        out = select_prompt_template(event_hint=None, n_frames=6)
        assert "security camera analyst" in out

    def test_camera_name_substituted(self):
        out = select_prompt_template(
            event_hint=None, n_frames=1, camera_name="Solar Camera",
        )
        assert "Solar Camera" in out

    def test_event_hint_block_empty_when_no_hint(self):
        out = select_prompt_template(event_hint=None, n_frames=1)
        assert "Trigger context:" not in out

    def test_event_hint_block_substituted(self):
        block = "Trigger context: vehicle"
        out = select_prompt_template(
            event_hint=None, n_frames=1,
            event_hint_block=block,
        )
        assert block in out


# =============================================================================
# select_prompt_template — vehicle path (mode dispatch)
# =============================================================================


class TestSelectPromptTemplateVehicleMode:
    """event_hint == 'vehicle' should respect the mode parameter."""

    def test_mode_crop_always_uses_crop_template(self):
        # Phase.74 — crop short-circuit BEFORE event_hint check.
        # mode="crop" → VEHICLE_CROP_PROMPT_TEMPLATE regardless of hint.
        out = select_prompt_template(
            event_hint="vehicle", n_frames=1, mode="crop",
        )
        assert "cropped bbox of the detection zone" in out.lower()
        # The crop template must NOT include motion/face fields.
        assert "moving_vehicle_indices" not in out
        assert "face_visibility" not in out

    def test_mode_crop_ignores_event_hint_none(self):
        # Even with event_hint=None, mode="crop" returns the crop template.
        # This is the 6B.74 fix — vehicle_identifier passes event_hint=None
        # but mode="crop", expecting the crop template.
        out = select_prompt_template(event_hint=None, n_frames=1, mode="crop")
        assert "cropped bbox of the detection zone" in out.lower()

    def test_mode_moving_raises_in_6b78(self):
        # Phase.78: VEHICLE_MOTION_PROMPT_TEMPLATE removed because
        # motion is owned by infra.motion_detector. mode="moving"
        # raises ValueError directing callers to mode="static".
        # (Phase.78: replaced the old "uses motion template" test.)
        with pytest.raises(ValueError, match="mode='moving' is no longer supported"):
            select_prompt_template(
                event_hint="vehicle", n_frames=6, mode="moving",
            )

    def test_mode_static_uses_static_template(self):
        out = select_prompt_template(
            event_hint="vehicle", n_frames=1, mode="static",
        )
        assert "single-frame vehicle analysis" in out
        assert "no motion signal" in out

    def test_mode_combined_raises_in_6b78(self):
        # Phase.78: VEHICLE_COMBINED_PROMPT_TEMPLATE removed.
        with pytest.raises(ValueError, match="mode='combined' is no longer supported"):
            select_prompt_template(
                event_hint="vehicle", n_frames=6, mode="combined",
            )

    def test_mode_combined_renders_placeholder_values(self):
        # Phase.78: combined template removed. Test that mode="static"
        # (the new multi-frame default) renders placeholder values
        # correctly.
        out = select_prompt_template(
            event_hint="vehicle", n_frames=4, interval_sec=3, mode="static",
            camera_name="CAM5",
        )
        # static template doesn't render n_frames/interval_sec text;
        # it asserts on camera_name + captured_at instead.
        assert "CAM5" in out

    def test_mode_moving_renders_placeholder_values(self):
        # Phase.78: motion template removed. Test that mode="static"
        # renders camera_name correctly.
        out = select_prompt_template(
            event_hint="vehicle", n_frames=2, interval_sec=4, mode="static",
            camera_name="Gatekeeper",
        )
        assert "Gatekeeper" in out


# =============================================================================
# select_prompt_template — person mode (Phase.106)
# =============================================================================


class TestSelectPromptTemplatePersonMode:
    """Phase.106: mode='person' routes to the new person template.

    Routing priority (mirrors the 'crop' short-circuit):
      - mode="person" wins regardless of event_hint value
      - event_hint=None + mode="person" → person template (NOT legacy PROMPT_TEMPLATE)
      - event_hint="person" + mode="person" → person template (consistent)
    """

    def test_mode_person_returns_person_template(self):
        out = select_prompt_template(
            event_hint=None, n_frames=2, mode="person",
            camera_name="CAM1",
            captured_at="2026-08-22 10:00:00 EDT",
        )
        # Person template is identifiable by its header
        assert 'Person-event analysis (Phase.106)' in out
        # It must render the camera name
        assert "CAM1" in out
        # And the captured-at timestamp
        assert "2026-08-22 10:00:00 EDT" in out

    def test_mode_person_with_event_hint_person(self):
        # event_hint="person" is fine — it doesn't block routing
        out = select_prompt_template(
            event_hint="person", n_frames=2, mode="person",
            camera_name="CAM1",
        )
        assert 'Person-event analysis (Phase.106)' in out

    def test_mode_person_with_event_hint_vehicle_still_routes_to_person(self):
        # mode="person" wins over event_hint (mirrors crop short-circuit at
        # 6B.74). Defensive: even if a caller mis-tags a person event as
        # vehicle, mode="person" still gives them the right template.
        out = select_prompt_template(
            event_hint="vehicle", n_frames=2, mode="person",
            camera_name="CAM1",
        )
        assert 'Person-event analysis (Phase.106)' in out
        # And it is NOT the vehicle static template
        assert "single-frame vehicle analysis" not in out

    def test_mode_person_includes_clothing_upper_enum(self):
        # Schema must be embedded verbatim — pins the design decision
        # that clothing_upper.color uses the enum (Note's spec).
        from infra.person_prompt_template import PERSON_SCHEMA_JSON

        out = select_prompt_template(
            event_hint=None, n_frames=2, mode="person",
            camera_name="CAM1",
        )
        assert PERSON_SCHEMA_JSON in out

    def test_mode_person_pixel_coord_space_for_face_bbox(self):
        # Critical: the prompt prose MUST tell Qwen that face_bbox is in
        # PIXEL coords (not normalized 0-1) so the downstream ArcFace
        # cropper can scale back to original frame size.
        out = select_prompt_template(
            event_hint=None, n_frames=2, mode="person",
            camera_name="CAM1",
        )
        assert "PIXEL" in out.upper()

    def test_mode_person_renders_no_unresolved_placeholders(self):
        out = select_prompt_template(
            event_hint=None, n_frames=2, interval_sec=5, mode="person",
            camera_name="CAM1",
            captured_at="2026-08-22 10:00:00 EDT",
            event_hint_block="(hint: person detected)",
        )
        import re
        leftover = re.findall(r"\{[a-z_][a-z_0-9]*\}", out)
        assert leftover == [], f"unresolved placeholders: {leftover}"

    def test_mode_person_renders_event_hint_block(self):
        # event_hint_block should appear in the output when non-empty.
        out = select_prompt_template(
            event_hint=None, n_frames=2, mode="person",
            camera_name="CAM1",
            event_hint_block="camera_on_device_classified_person=true",
        )
        assert "camera_on_device_classified_person=true" in out


# =============================================================================
# select_prompt_template — auto-dispatch
# =============================================================================


class TestSelectPromptTemplateAutoDispatch:
    """mode='auto' (default) picks crop/static based on n_frames.

    Phase.78 (2026-08-14): removed the moving/combined split. Two
    prompts, two paths: 1 frame = crop, n>=2 frames = static.
    """

    def test_auto_one_frame_picks_crop(self):
        # 1 frame → crop template (tight bbox identification).
        out = select_prompt_template(
            event_hint="vehicle", n_frames=1, mode="auto",
        )
        assert "cropped bbox of the detection zone" in out.lower()

    def test_auto_many_frames_default_picks_static(self):
        # 6 frames, no env var → static template (multi-frame ID).
        out = select_prompt_template(
            event_hint="vehicle", n_frames=6, mode="auto",
        )
        # static template doesn't have moving/combined vocabulary.
        assert "Triggered on VEHICLE motion" not in out
        # and identifies the camera in the rendered prompt.
        assert "Camera" in out

    def test_auto_zero_frames_clamps_to_one(self):
        # n=0 would cause division-by-zero or bad math → clamps to 1.
        # n=1 → crop template.
        out = select_prompt_template(
            event_hint="vehicle", n_frames=0, mode="auto",
        )
        assert "cropped bbox of the detection zone" in out.lower()

    def test_auto_negative_frames_clamps_to_one(self):
        out = select_prompt_template(
            event_hint="vehicle", n_frames=-5, mode="auto",
        )
        assert "cropped bbox of the detection zone" in out.lower()


# =============================================================================
# select_prompt_template — captured_at handling
# =============================================================================


class TestSelectPromptTemplateCapturedAt:
    """captured_at is the date anchor that prevents Qwen hallucinating dates."""

    def test_captured_at_rendered_in_motion_template(self):
        # Phase.78: motion template removed. Test that captured_at
        # is rendered for static (the new multi-frame default).
        out = select_prompt_template(
            event_hint="vehicle", n_frames=6, mode="static",
            captured_at="2026-07-27T14:30:00Z",
        )
        assert "2026-07-27T14:30:00Z" in out

    def test_captured_at_rendered_in_static_template(self):
        out = select_prompt_template(
            event_hint="vehicle", n_frames=1, mode="static",
            captured_at="2026-07-27T14:30:00Z",
        )
        assert "2026-07-27T14:30:00Z" in out

    def test_captured_at_rendered_in_combined_template(self):
        # Phase.78: combined template removed. The new multi-frame
        # default IS static — captured_at must still render there.
        out = select_prompt_template(
            event_hint="vehicle", n_frames=6, mode="static",
            captured_at="2026-07-27T14:30:00Z",
        )
        assert "2026-07-27T14:30:00Z" in out

    def test_captured_at_none_replaced_with_unknown(self):
        # Phase.78: motion template removed. Test "unknown"
        # fallback on static instead.
        out = select_prompt_template(
            event_hint="vehicle", n_frames=6, mode="static",
            captured_at=None,
        )
        assert "unknown" in out

    def test_captured_at_empty_string_replaced_with_unknown(self):
        # Phase.78: motion template removed. Test "unknown"
        # fallback on static instead.
        out = select_prompt_template(
            event_hint="vehicle", n_frames=6, mode="static",
            captured_at="",
        )
        assert "unknown" in out


# =============================================================================
# _build_event_hint_block
# =============================================================================


class TestBuildEventHintBlock:
    """Render camera-side AI classification as a 1-3 sentence prompt block."""

    def test_none_returns_empty(self):
        assert _build_event_hint_block(None) == ""

    def test_empty_string_returns_empty(self):
        assert _build_event_hint_block("") == ""

    def test_vehicle_includes_motion_guidance(self):
        block = _build_event_hint_block("vehicle")
        assert "vehicle in motion" in block
        # The motion-guidance clause for vehicle triggers.
        assert "ARRIVING" in block
        assert "PARKED" in block
        assert "IN TRANSIT" in block

    def test_person_no_motion_guidance(self):
        block = _build_event_hint_block("person")
        assert "person" in block
        # People don't get the motion guidance — that's vehicle-only.
        assert "ARRIVING" not in block

    def test_people_normalized_to_person(self):
        # Reolink uses PEOPLE; map to singular.
        block = _build_event_hint_block("people")
        assert "person" in block
        # Should NOT say "people in motion".
        assert "people in motion" not in block

    def test_motion_no_motion_guidance(self):
        block = _build_event_hint_block("motion")
        assert "motion" in block
        assert "ARRIVING" not in block

    def test_animal_no_motion_guidance(self):
        block = _build_event_hint_block("animal")
        assert "animal" in block
        assert "ARRIVING" not in block

    def test_unknown_falls_through_to_unknown_trigger(self):
        block = _build_event_hint_block("unknown")
        assert "unknown trigger" in block

    def test_arbitrary_hint_uses_raw_value(self):
        # If Reolink sends something not in the mapping, use it verbatim.
        block = _build_event_hint_block("package")
        assert "package" in block


# =============================================================================
# JSON schemas — presence and shape
# =============================================================================


class TestVisionSchemas:
    """Schemas are owned by prompt_templates.py because they're tightly coupled
    to the prompt text. Adding a field to a schema should require editing the
    matching prompt template."""

    def test_vision_schema_json_has_required_fields(self):
        required = VISION_SCHEMA_JSON["required"]
        # Top-level fields every vision response must have.
        # Phase.78 (2026-08-14): vehicle_motion and
        # moving_vehicle_indices removed — motion is owned by
        # infra.motion_detector, not by Qwen.
        for field in [
            "objects_detected", "primary_subject", "actions",
            "scene_description", "confidence", "notable_details",
            "colors", "species", "vehicles", "primary_vehicle_index",
            "face_visibility", "best_frame_index",
        ]:
            assert field in required, f"missing required field: {field}"
        # The removed motion fields must NOT be required any more.
        assert "vehicle_motion" not in required
        assert "moving_vehicle_indices" not in required

    def test_vision_schema_colors_has_required_subfields(self):
        required = VISION_SCHEMA_JSON["properties"]["colors"]["required"]
        for field in ["vehicle", "clothing_primary", "clothing_secondary", "other"]:
            assert field in required

    def test_vehicle_crop_schema_includes_identification_fields(self):
        # Phase.74 — the crop prompt asks for make/model/vehicle_features
        # at the TOP level. The schema must allow these (additionalProperties
        # must NOT block them).
        required = VEHICLE_CROP_SCHEMA_JSON["required"]
        for field in ["color", "body_style_hint", "make", "model",
                      "vehicle_features", "confidence"]:
            assert field in required, f"crop schema missing required: {field}"

    def test_vehicle_crop_schema_features_includes_6b48_fields(self):
        # Phase.48 — bigger, more permanent distinguishing features.
        # Phase.129b (§11.52) — vehicle_features now lives inside
        # vehicles[].items.properties (multi-vehicle schema).
        features = VEHICLE_CROP_SCHEMA_JSON["properties"]["vehicles"]["items"]["properties"]["vehicle_features"]
        required = features["required"]
        for field in [
            "wheel_style", "wheel_arch", "wheel_color",
            "roofline_style", "front_grille_style",
            "headlight_signature", "rear_lights_signature",
            "tailgate_type", "badge_text_readable",
            "window_tint", "cab_marker_lights", "bed_cover",
        ]:
            assert field in required, f"crop features missing: {field}"

    def test_vehicle_classify_schema_includes_all_required(self):
        required = VEHICLE_CLASSIFY_SCHEMA["required"]
        for field in [
            "make", "model", "body_style", "trim_level", "year_range",
            "visible_modifications", "distinctive_features", "confidence",
        ]:
            assert field in required, f"classify schema missing: {field}"

    def test_schemas_are_strict(self):
        # All schemas should be strict (additionalProperties=false) so Qwen
        # can't sneak in fields the downstream code doesn't expect.
        assert VISION_SCHEMA_JSON["additionalProperties"] is False
        assert VEHICLE_CROP_SCHEMA_JSON["additionalProperties"] is False
        assert VEHICLE_CLASSIFY_SCHEMA["additionalProperties"] is False

    def test_cab_marker_lights_accepts_string_and_boolean(self):
        # Phase.66 — Qwen has been returning "false" (string) even when
        # the schema said boolean. The crop schema must accept both.
        # Phase.129b (§11.52) — vehicle_features moved into
        # vehicles[].items.properties (multi-vehicle schema).
        features = VEHICLE_CROP_SCHEMA_JSON["properties"]["vehicles"]["items"]["properties"]["vehicle_features"]
        cml_type = features["properties"]["cab_marker_lights"]["type"]
        assert "boolean" in cml_type
        assert "string" in cml_type
        assert "null" in cml_type

    def test_window_tint_enum_includes_factory_privacy(self):
        # 6B.48 — factory_privacy added for the rear-only factory glass
        # common on SUVs.
        # Phase.129b (§11.52) — vehicle_features moved into
        # vehicles[].items.properties (multi-vehicle schema).
        features = VEHICLE_CROP_SCHEMA_JSON["properties"]["vehicles"]["items"]["properties"]["vehicle_features"]
        wt_type = features["properties"]["window_tint"]
        if "enum" in wt_type:
            # If enum is set, factory_privacy must be there.
            assert "factory_privacy" in wt_type["enum"] or None in wt_type["enum"]


# =============================================================================
# Prompt templates — presence and uniqueness
# =============================================================================


class TestPromptTemplatePresence:
    """3 prompt templates exist (legacy + 2 vehicle variants).

    Phase.78 (2026-08-14): 5 → 3 templates. VEHICLE_MOTION_PROMPT_TEMPLATE
    and VEHICLE_COMBINED_PROMPT_TEMPLATE removed because motion is owned
    by infra.motion_detector.
    """

    def test_all_templates_present(self):
        for tpl in [
            PROMPT_TEMPLATE,
            VEHICLE_STATIC_PROMPT_TEMPLATE,
            VEHICLE_CROP_PROMPT_TEMPLATE,
            VEHICLE_CLASSIFY_PROMPT,
        ]:
            assert isinstance(tpl, str)
            assert len(tpl) > 100

    def test_motion_templates_removed(self):
        # Phase.78 regression: the deleted template names must NOT
        # exist as attributes on the module. If someone reintroduces
        # them, this test catches it.
        import infra.prompt_templates as pt
        assert not hasattr(pt, "VEHICLE_MOTION_PROMPT_TEMPLATE")
        assert not hasattr(pt, "VEHICLE_COMBINED_PROMPT_TEMPLATE")

    def test_templates_are_distinct(self):
        # Crop template is the only one without motion fields.
        # Static template doesn't need motion_justification either
        # (motion is owned by infra.motion_detector in 6B.78).
        assert "moving_vehicle_indices" not in VEHICLE_STATIC_PROMPT_TEMPLATE
        assert "moving_vehicle_indices" not in VEHICLE_CROP_PROMPT_TEMPLATE
        assert "motion_justification" not in VEHICLE_STATIC_PROMPT_TEMPLATE
        # Crop asks for make/model at top level.
        assert "make" in VEHICLE_CROP_PROMPT_TEMPLATE

    def test_legacy_prompt_uses_double_brace_escaping(self):
        # The legacy PROMPT_TEMPLATE uses {{ }} for JSON braces because
        # .format() would consume single braces. select_prompt_template
        # uses str.replace() instead.
        assert "{{" in PROMPT_TEMPLATE
        assert "}}" in PROMPT_TEMPLATE


# =============================================================================
# §11.115.22: pairwise-diff prompt (3-image vehicle identification)
# =============================================================================


class TestPairwiseDiffPrompt:
    """When the Qwen payload is [diff, crop_a, crop_b], the prompt must
    name the three images and instruct Qwen to use the diff to pick the
    moving subject. Without this, alert 9275a92b hallucinated 3 vehicles
    from 1 moving tractor past 2 parked vehicles.

    Phase.78 (2026-08-14) replaced motion-bias prompts with static. Now
    §11.115.22 re-introduces motion-aware framing for n_frames >= 3:
    VEHICLE_DIFF_STATIC_PROMPT_TEMPLATE is selected by select_prompt_template
    when mode="static" and n_frames >= 3.
    """

    def test_diff_static_template_exists(self):
        import infra.prompt_templates as pt

        assert hasattr(pt, "VEHICLE_DIFF_STATIC_PROMPT_TEMPLATE")
        tpl = pt.VEHICLE_DIFF_STATIC_PROMPT_TEMPLATE
        assert isinstance(tpl, str)
        assert len(tpl) > 100

    def test_diff_static_template_names_three_images(self):
        import infra.prompt_templates as pt

        tpl = pt.VEHICLE_DIFF_STATIC_PROMPT_TEMPLATE
        # Names the 3 inputs so Qwen can refer to them by role.
        # Allow either ordinal ("1.", "2.", "3.") or descriptive labels.
        lower = tpl.lower()
        assert "diff" in lower
        assert "streak crop a" in lower or "image 1" in lower
        assert "streak crop b" in lower or "image 3" in lower
        assert "pairwise" in lower

    def test_diff_static_template_instructs_moving_subject_only(self):
        """Critical §11.115.22 directive: identify the moving subject, ignore parked."""
        import infra.prompt_templates as pt

        tpl = pt.VEHICLE_DIFF_STATIC_PROMPT_TEMPLATE
        lower = tpl.lower()
        # Phrases that make "moving vs parked" unambiguous.
        assert "moving" in lower
        # Either "ignore" or "parked" or "stationary" — Qwen must be told
        # not to enumerate parked vehicles.
        assert any(
            phrase in lower for phrase in ("ignore", "parked", "stationary")
        )

    def test_select_prompt_template_uses_diff_static_for_n3(self):
        """select_prompt_template(event_hint='vehicle', n_frames=3) returns
        the diff-aware template, not the legacy static one."""
        from infra.prompt_templates import (
            VEHICLE_DIFF_STATIC_PROMPT_TEMPLATE,
            select_prompt_template,
        )

        rendered = select_prompt_template(
            event_hint="vehicle",
            n_frames=3,
            camera_name="Outside Front Solar",
            captured_at="2026-09-03 14:07",
        )
        # The diff template uses lowercase markers like "moving subject"
        # that the legacy static template does not contain.
        assert "moving subject" in rendered.lower()
        # Sanity: must be the diff template, not a copy of the static one.
        assert (
            rendered
            == VEHICLE_DIFF_STATIC_PROMPT_TEMPLATE.replace(
                "{camera_name}", "Outside Front Solar"
            )
            .replace("{captured_at}", "2026-09-03 14:07")
            .replace("{event_hint_block}", "")
        )

    def test_select_prompt_template_uses_legacy_static_for_n2(self):
        """Backward compat: n_frames=2 still uses the legacy static template."""
        from infra.prompt_templates import (
            VEHICLE_STATIC_PROMPT_TEMPLATE,
            select_prompt_template,
        )

        rendered = select_prompt_template(
            event_hint="vehicle",
            n_frames=2,
            camera_name="x",
            captured_at="t",
        )
        # Rendered must equal the legacy static template after placeholder
        # substitution.
        assert (
            rendered
            == VEHICLE_STATIC_PROMPT_TEMPLATE.replace("{camera_name}", "x")
            .replace("{captured_at}", "t")
            .replace("{event_hint_block}", "")
        )


class TestBuildMessagesMIME:
    """§11.115.22 — pairwise_diff.png must be sent as image/png, not image/jpeg.

    Pairwise differentials are PNG (lossless diff signal, §11.88). Crops are
    JPEG. llama-server tolerates the wrong MIME in practice but the OpenAI
    data-URL spec is strict — and we've been silently mislabeling PNGs as
    JPEGs since pairwise_diff entered the system. Detect by extension.
    """

    def _write(self, path: str, suffix: str, body: bytes = b"\x89PNG\r\n\x1a\n") -> None:
        import os
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(body)

    def test_png_path_sends_as_image_png(self, tmp_path):
        from infra.prompt_templates import _build_messages

        png_path = str(tmp_path / "pairwise_diff.png")
        self._write(png_path, ".png")
        msgs = _build_messages(
            frame_paths=[png_path],
            camera_name="x",
            event_hint="vehicle",
        )
        image_parts = [
            c for c in msgs[0]["content"] if c.get("type") == "image_url"  # type: ignore[index]
        ]
        assert len(image_parts) == 1, f"expected 1 image part, got {len(image_parts)}"
        url = image_parts[0]["image_url"]["url"]
        assert url.startswith("data:image/png;base64,"), (
            f"PNG must use image/png MIME, got {url[:60]!r}"
        )

    def test_jpeg_path_sends_as_image_jpeg(self, tmp_path):
        from infra.prompt_templates import _build_messages

        jpeg_path = str(tmp_path / "crop_a.jpg")
        self._write(jpeg_path, ".jpg", body=b"\xff\xd8\xff\xe0")
        msgs = _build_messages(
            frame_paths=[jpeg_path],
            camera_name="x",
            event_hint="vehicle",
        )
        image_parts = [
            c for c in msgs[0]["content"] if c.get("type") == "image_url"  # type: ignore[index]
        ]
        assert len(image_parts) == 1
        url = image_parts[0]["image_url"]["url"]
        assert url.startswith("data:image/jpeg;base64,"), (
            f"JPEG must use image/jpeg MIME, got {url[:60]!r}"
        )

    def test_three_image_payload_preserves_order_and_mime(self, tmp_path):
        """[diff, crop_a, crop_b] must produce 3 image parts in order with right MIME."""
        from infra.prompt_templates import _build_messages

        png = str(tmp_path / "pairwise_diff.png")
        jpg_a = str(tmp_path / "crop_a.jpg")
        jpg_b = str(tmp_path / "crop_b.jpg")
        self._write(png, ".png")
        self._write(jpg_a, ".jpg", body=b"\xff\xd8\xff\xe0")
        self._write(jpg_b, ".jpg", body=b"\xff\xd8\xff\xe0")

        msgs = _build_messages(
            frame_paths=[png, jpg_a, jpg_b],
            camera_name="x",
            event_hint="vehicle",
        )
        content = msgs[0]["content"]  # type: ignore[index]
        image_parts = [c for c in content if c.get("type") == "image_url"]
        assert len(image_parts) == 3
        mimes = [p["image_url"]["url"].split(";")[0] for p in image_parts]
        assert mimes == ["data:image/png", "data:image/jpeg", "data:image/jpeg"], (
            f"expected [png, jpeg, jpeg], got {mimes}"
        )
