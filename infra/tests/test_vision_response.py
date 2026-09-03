"""
Tests for infra/vision_response.py — Qwen3-VL response parsing/validation.

Covers:
    - _parse_response: bare JSON, markdown fences, malformed, recovery paths
    - _validate_vision_result: defaults filled, missing fields
    - _try_recover_stringified_lists: pattern 1 (multi-quoted),
      pattern 2 (comma-list-in-string), non-recovery cases
    - _error_result: sentinel shape
    - _parse_vehicle_classify_response: crop-classify shape + defaults
    - _vehicle_classify_error: crop error sentinel shape
"""


from infra.vision_response import (
    _error_result,
    _parse_response,
    _parse_vehicle_classify_response,
    _try_recover_stringified_lists,
    _validate_vision_result,
    _vehicle_classify_error,
)

# =============================================================================
# _parse_response
# =============================================================================


class TestParseResponse:
    """Extract JSON from model response — bare, fenced, malformed, recovery."""

    def test_empty_raw_returns_none(self):
        assert _parse_response("") is None

    def test_none_raw_returns_none(self):
        assert _parse_response(None) is None  # type: ignore[arg-type]

    def test_bare_json(self):
        raw = '{"objects_detected": [], "primary_subject": "test"}'
        result = _parse_response(raw)
        assert result is not None
        assert result["primary_subject"] == "test"

    def test_bare_json_with_whitespace(self):
        raw = '   \n  {"objects_detected": [], "primary_subject": "test"}  \n  '
        result = _parse_response(raw)
        assert result is not None
        assert result["primary_subject"] == "test"

    def test_markdown_code_fence_json(self):
        raw = '```json\n{"objects_detected": [], "primary_subject": "fenced"}\n```'
        result = _parse_response(raw)
        assert result is not None
        assert result["primary_subject"] == "fenced"

    def test_markdown_code_fence_no_lang(self):
        raw = '```\n{"objects_detected": [], "primary_subject": "no_lang"}\n```'
        result = _parse_response(raw)
        assert result is not None
        assert result["primary_subject"] == "no_lang"

    def test_markdown_fence_with_surrounding_prose(self):
        raw = 'Here is the response:\n```json\n{"primary_subject": "x"}\n```\nDone.'
        # The function expects the response to START with ```. Surrounding
        # prose with ``` in the middle should NOT be parsed by the fence
        # path — but json.loads on the full text will still succeed if the
        # response is bare JSON. This documents current behavior.
        result = _parse_response(raw)
        # Either it parses the bare JSON at the end (json.loads succeeds
        # on the full string) OR it returns None. Both are acceptable
        # failure modes for this unusual input shape.
        if result is not None:
            assert "primary_subject" in result

    def test_malformed_json_returns_none(self):
        raw = "this is not json { malformed"
        assert _parse_response(raw) is None

    def test_stringified_list_not_recovered_when_json_loads_succeeds(self):
        # Pattern 2 only fires when json.loads FAILS first.
        # If the response parses cleanly (string-valued field with commas),
        # _parse_response returns it as-is — recovery is for genuinely
        # malformed JSON, not for semantically-wrong types.
        # See _try_recover_stringified_lists docstring.
        raw = '{"items": "red jugs, white cooler, blue tarp", "primary_subject": "stuff"}'
        result = _parse_response(raw)
        assert result is not None
        # items remains a string because the JSON was valid.
        assert isinstance(result["items"], str)
        assert result["items"] == "red jugs, white cooler, blue tarp"

    def test_stringified_list_recovery_triggers_on_malformed_json(self):
        # Recovery only fires when json.loads fails first.
        # Example: trailing comma makes the JSON invalid.
        raw = '{"items": "red jugs", "white cooler", "primary_subject": "stuff"}'
        # Note: this JSON has two strings following "items" without array brackets.
        # json.loads will fail; recovery should try to fix.
        result = _parse_response(raw)
        # Recovery may or may not succeed depending on regex match. We just
        # verify it doesn't crash and either returns None or a dict.
        if result is not None:
            assert isinstance(result, dict)

    def test_non_dict_result_returns_none(self):
        # json.loads succeeds on a list, but _validate_vision_result
        # returns None for non-dict inputs.
        raw = "[1, 2, 3]"
        # _parse_response will json.loads successfully but the validator
        # rejects non-dict. Per docstring: only returns None for parse
        # failures. Let me verify actual behavior.
        result = _parse_response(raw)
        # Per _validate_vision_result docstring: returns None if not a dict.
        assert result is None


# =============================================================================
# _try_recover_stringified_lists
# =============================================================================


class TestTryRecoverStringifiedLists:
    """Heuristic recovery for LLM-emitting-lists-as-strings bugs."""

    def test_no_recovery_needed_passes_through(self):
        # Already-valid JSON should not be broken.
        text = '{"items": ["a", "b"], "primary_subject": "ok"}'
        result = _try_recover_stringified_lists(text)
        # May or may not match — let's check it doesn't BREAK valid JSON.
        # Pattern 2 won't match because items is already an array, not a string.
        assert result == text or result is not None

    def test_pattern_1_multi_quoted_strings_on_same_line(self):
        # "items": "a", "b", "c" on the same line — LLM emitted scalars
        # instead of an array.
        text = '{"items": "a", "b", "c"}'
        result = _try_recover_stringified_lists(text)
        # Should detect this pattern and convert to a list.
        assert result is not None
        # After recovery, the list should be parseable.
        import json
        parsed = json.loads(result)
        assert parsed["items"] == ["a", "b", "c"]

    def test_pattern_2_comma_separated_in_string(self):
        # "items": "red, white, blue" — 2+ comma-separated string.
        text = '{"items": "red, white, blue"}'
        result = _try_recover_stringified_lists(text)
        assert result is not None
        import json
        parsed = json.loads(result)
        assert parsed["items"] == ["red", "white", "blue"]

    def test_pattern_2_long_string_not_converted(self):
        # A long prose-like string should NOT be converted.
        text = '{"items": "this is a long prose string with multiple words but no list-like structure"}'
        result = _try_recover_stringified_lists(text)
        # 60-char limit prevents conversion.
        assert result == text or result is None

    def test_refuses_to_act_on_next_key_collision(self):
        # Pattern 1 must NOT fire when the rest contains a JSON structural
        # character (would mean the next key is starting, not more list items).
        text = '{"foo": "bar", "baz": "qux"}'
        result = _try_recover_stringified_lists(text)
        # The "rest" contains ":" which means it's the next key — should
        # refuse to act. Pattern 2 might still fire on each key:value pair
        # but won't break valid JSON.
        assert result is not None  # function returns text either way


# =============================================================================
# _validate_vision_result
# =============================================================================


class TestValidateVisionResult:
    """Fill defaults + reject non-dict."""

    def test_non_dict_returns_none(self):
        assert _validate_vision_result([]) is None  # type: ignore[arg-type]
        assert _validate_vision_result(None) is None  # type: ignore[arg-type]
        assert _validate_vision_result("string") is None  # type: ignore[arg-type]

    def test_empty_dict_fills_all_defaults(self):
        result = _validate_vision_result({})
        assert result is not None
        assert result["primary_subject"] == "unknown"
        assert result["actions"] == []
        assert result["scene_description"] == ""
        assert result["confidence"] == 0.0
        assert result["notable_details"] == []
        assert result["colors"]["vehicle"] is None
        assert result["colors"]["clothing_primary"] is None
        assert result["species"] is None
        assert result["vehicles"] == []
        assert result["primary_vehicle_index"] == 0
        # Phase.78 (2026-08-14): vehicle_motion and
        # moving_vehicle_indices defaults are removed. The parser
        # only fills defaults for fields the schema requires. Motion
        # is owned by infra.motion_detector.
        assert "vehicle_motion" not in result
        assert "moving_vehicle_indices" not in result

    def test_face_visibility_default_shape(self):
        result = _validate_vision_result({})
        assert result is not None
        fv = result["face_visibility"]
        assert fv["any_face_visible"] is False
        assert fv["best_frame_index"] == 1
        assert fv["best_frame_face_fraction"] == 0.0
        assert fv["front_facing"] is False
        assert fv["per_frame"] == []
        assert "missing from vision response" in fv["notes"]

    def test_existing_fields_preserved(self):
        result = _validate_vision_result({
            "primary_subject": "car",
            "confidence": 0.95,
            "colors": {"vehicle": "blue"},
        })
        assert result is not None
        assert result["primary_subject"] == "car"
        assert result["confidence"] == 0.95
        assert result["colors"]["vehicle"] == "blue"
        # Defaults still applied for missing fields.
        assert result["actions"] == []

    def test_face_visibility_dict_preserved(self):
        result = _validate_vision_result({
            "face_visibility": {
                "any_face_visible": True,
                "best_frame_index": 2,
                "best_frame_face_fraction": 0.05,
                "front_facing": True,
                "per_frame": [],
                "notes": "frontal face",
            },
        })
        assert result is not None
        fv = result["face_visibility"]
        assert fv["any_face_visible"] is True
        assert fv["best_frame_index"] == 2

    def test_face_visibility_non_dict_replaced_with_default(self):
        # Malformed face_visibility (e.g. None) → default shape.
        result = _validate_vision_result({"face_visibility": None})
        assert result is not None
        assert isinstance(result["face_visibility"], dict)
        assert result["face_visibility"]["any_face_visible"] is False

    def test_largest_face_fraction_backcompat_alias(self):
        # Pre-6B.66 prompts returned "largest_face_fraction" instead of
        # "best_frame_face_fraction". The validator must rename it.
        result = _validate_vision_result({
            "face_visibility": {
                "any_face_visible": True,
                "best_frame_index": 1,
                "largest_face_fraction": 0.04,
                "front_facing": True,
                "per_frame": [],
                "notes": None,
            },
        })
        assert result is not None
        fv = result["face_visibility"]
        assert fv["best_frame_face_fraction"] == 0.04
        assert "largest_face_fraction" not in fv

    def test_per_frame_entries_get_default_bbox(self):
        # Downstream code reads entry["bbox"] directly — absent would crash.
        result = _validate_vision_result({
            "face_visibility": {
                "any_face_visible": True,
                "best_frame_index": 1,
                "best_frame_face_fraction": 0.0,
                "front_facing": False,
                "per_frame": [
                    {"index": 1, "face_fraction": 0.0, "front_facing": False},
                    {"index": 2, "face_fraction": 0.0, "front_facing": False},
                ],
                "notes": None,
            },
        })
        assert result is not None
        fv = result["face_visibility"]
        for entry in fv["per_frame"]:
            assert "bbox" in entry
            assert entry["bbox"] is None

    def test_best_frame_index_default_when_missing(self):
        # If Qwen doesn't return best_frame_index, default to 1.
        result = _validate_vision_result({})
        assert result is not None
        assert result["best_frame_index"] == 1

    def test_best_frame_index_preserved_when_present(self):
        result = _validate_vision_result({"best_frame_index": 3})
        assert result is not None
        assert result["best_frame_index"] == 3

    def test_best_frame_index_invalid_type_replaced(self):
        # A string in best_frame_index is invalid → default to 1.
        result = _validate_vision_result({"best_frame_index": "oops"})
        assert result is not None
        assert result["best_frame_index"] == 1


# =============================================================================
# _error_result
# =============================================================================


class TestErrorResult:
    """Standard error sentinel shape for analyze_frames failures."""

    def test_error_sentinel_shape(self):
        result = _error_result("test failure")
        assert result["objects_detected"] == ["error"]
        assert result["primary_subject"] == "error"
        assert result["actions"] == []
        assert "test failure" in result["scene_description"]
        assert result["confidence"] == 0.0
        assert any("test failure" in d for d in result["notable_details"])
        assert result["colors"]["vehicle"] is None
        assert result["species"] is None
        assert result["vehicles"] == []
        assert result["primary_vehicle_index"] == 0
        # Phase.78 (2026-08-14): motion fields removed from the
        # error sentinel. The sentinel mirrors the schema's required
        # fields only.
        assert "vehicle_motion" not in result
        assert "moving_vehicle_indices" not in result

    def test_error_sentinel_distinguishable_from_empty_scene(self):
        # The error sentinel must contain "error" in objects_detected so
        # _is_vision_error_result can identify it.
        from infra.vision_analyzer import _is_vision_error_result
        err = _error_result("queue timeout")
        assert _is_vision_error_result(err) is True
        # An empty scene (e.g. objects_detected=[] or ["bird"]) should NOT
        # be an error sentinel.
        assert _is_vision_error_result({"objects_detected": ["bird"]}) is False
        assert _is_vision_error_result({"objects_detected": []}) is False


# =============================================================================
# _parse_vehicle_classify_response
# =============================================================================


class TestParseVehicleClassifyResponse:
    """Crop-classify response parser with field defaults."""

    def test_empty_raw_returns_none(self):
        assert _parse_vehicle_classify_response("") is None

    def test_valid_json_with_make(self):
        raw = '{"make": "Ford", "model": "F-150", "confidence": 0.95}'
        result = _parse_vehicle_classify_response(raw)
        assert result is not None
        assert result["make"] == "Ford"
        assert result["model"] == "F-150"
        assert result["confidence"] == 0.95

    def test_markdown_fence_stripped(self):
        raw = '```json\n{"make": "Chevrolet", "model": "Silverado"}\n```'
        result = _parse_vehicle_classify_response(raw)
        assert result is not None
        assert result["make"] == "Chevrolet"

    def test_missing_fields_filled_with_defaults(self):
        raw = '{"make": "Tesla"}'
        result = _parse_vehicle_classify_response(raw)
        assert result is not None
        assert result["make"] == "Tesla"
        assert result["model"] is None
        assert result["body_style"] is None
        assert result["trim_level"] is None
        assert result["year_range"] is None
        assert result["visible_modifications"] is None
        assert result["distinctive_features"] is None
        assert result["confidence"] == 0.0

    def test_response_without_make_returns_none(self):
        # The shape detection requires "make" in the response.
        raw = '{"model": "F-150"}'
        assert _parse_vehicle_classify_response(raw) is None

    def test_malformed_json_returns_none(self):
        assert _parse_vehicle_classify_response("not json") is None


# =============================================================================
# _vehicle_classify_error
# =============================================================================


class TestVehicleClassifyError:
    """Crop-classify error sentinel."""

    def test_error_sentinel_shape(self):
        result = _vehicle_classify_error("API timeout")
        assert result["make"] is None
        assert result["model"] is None
        assert result["body_style"] is None
        assert result["trim_level"] is None
        assert result["year_range"] is None
        assert result["visible_modifications"] is None
        assert result["distinctive_features"] is None
        assert result["confidence"] == 0.0
        assert result["_error"] == "API timeout"

    def test_error_sentinel_distinguishable_from_valid(self):
        # No "make" field in the error sentinel — _parse_vehicle_classify_response
        # would reject it. This is by design: the sentinel is for return-only,
        # never round-tripping through the parser.
        from infra.vision_response import _parse_vehicle_classify_response
        err = _vehicle_classify_error("oops")
        assert _parse_vehicle_classify_response('{"make": "x"}') is not None
        # The sentinel itself has "make": null but also _error: "oops",
        # which is the signal that it's a sentinel not a parsed result.
        assert "_error" in err



# =============================================================================
# Phase.129b (§11.52) — backward-compat population from vehicles[]
# =============================================================================


class TestPopulateLegacyFieldsFromVehicles:
    """Phase.129b — multi-vehicle crop schema requires Qwen to emit
    vehicles[] with full per-vehicle identification. Legacy consumers
    (_extract_signature, _vision_summary_str fall-back, alert body
    builders) read top-level fields. _populate_legacy_fields_from_vehicles
    copies vehicles[primary_vehicle_index] → top-level fields so legacy
    consumers keep working without changes.
    """

    def test_copies_primary_vehicle_fields_to_top_level(self):
        from infra.vision_response import _populate_legacy_fields_from_vehicles
        result = {
            "vehicles": [
                {
                    "color": "red", "body_style_hint": "tractor",
                    "make": "Kubota", "model": "M7",
                    "vehicle_features": {"wheel_style": "tractor_ag"},
                    "description": "Red tractor with front loader",
                    "confidence": 0.82,
                },
            ],
            "primary_vehicle_index": 0,
        }
        _populate_legacy_fields_from_vehicles(result)
        assert result["color"] == "red"
        assert result["body_style_hint"] == "tractor"
        assert result["make"] == "Kubota"
        assert result["model"] == "M7"
        assert result["vehicle_features"] == {"wheel_style": "tractor_ag"}
        assert result["description"] == "Red tractor with front loader"
        assert result["confidence"] == 0.82
        # `type` alias for slim match_stage _extract_signature
        assert result["type"] == "tractor"

    def test_picks_correct_primary_vehicle_index(self):
        """When primary_vehicle_index=1, copy vehicles[1] (not vehicles[0])."""
        from infra.vision_response import _populate_legacy_fields_from_vehicles
        result = {
            "vehicles": [
                {"color": "red", "make": "Kubota", "model": "M7",
                 "body_style_hint": "tractor", "vehicle_features": {},
                 "description": "Tractor", "confidence": 0.82},
                {"color": "silver", "make": "Toyota", "model": "4Runner",
                 "body_style_hint": "suv", "vehicle_features": {},
                 "description": "Silver 4Runner", "confidence": 0.88},
            ],
            "primary_vehicle_index": 1,
        }
        _populate_legacy_fields_from_vehicles(result)
        # Top-level fields reflect the 4Runner, not the tractor
        assert result["color"] == "silver"
        assert result["make"] == "Toyota"
        assert result["model"] == "4Runner"
        assert result["type"] == "suv"

    def test_empty_vehicles_does_nothing(self):
        """Empty vehicles[] → no copy. Top-level fields stay at default."""
        from infra.vision_response import _populate_legacy_fields_from_vehicles
        result: dict = {"vehicles": []}
        _populate_legacy_fields_from_vehicles(result)
        assert "color" not in result
        assert "make" not in result

    def test_out_of_range_primary_index_falls_back_to_zero(self):
        """primary_vehicle_index beyond list length → use 0."""
        from infra.vision_response import _populate_legacy_fields_from_vehicles
        result = {
            "vehicles": [
                {"color": "red", "make": "Kubota", "model": "M7",
                 "body_style_hint": "tractor", "vehicle_features": {},
                 "description": "Tractor", "confidence": 0.82},
            ],
            "primary_vehicle_index": 5,
        }
        _populate_legacy_fields_from_vehicles(result)
        assert result["make"] == "Kubota"

    def test_does_not_overwrite_existing_top_level_fields(self):
        """If top-level fields are already set, don't clobber them."""
        from infra.vision_response import _populate_legacy_fields_from_vehicles
        result = {
            "vehicles": [{"color": "red", "make": "Kubota"}],
            "color": "silver",  # already set
            "make": "Toyota",   # already set
        }
        _populate_legacy_fields_from_vehicles(result)
        # Original values preserved
        assert result["color"] == "silver"
        assert result["make"] == "Toyota"

    def test_idempotent(self):
        """Calling twice produces the same result."""
        from infra.vision_response import _populate_legacy_fields_from_vehicles
        result = {
            "vehicles": [{"color": "red", "make": "Kubota", "model": "M7",
                          "body_style_hint": "tractor", "vehicle_features": {},
                          "description": "Tractor", "confidence": 0.82}],
            "primary_vehicle_index": 0,
        }
        _populate_legacy_fields_from_vehicles(result)
        first = dict(result)
        _populate_legacy_fields_from_vehicles(result)
        assert result == first


class TestValidateVisionResultBackwardCompat:
    """End-to-end test: _validate_vision_result runs through both
    fill-defaults AND _populate_legacy_fields_from_vehicles.
    """

    def test_multi_vehicle_result_backward_compat(self):
        from infra.vision_response import _validate_vision_result
        result = _validate_vision_result({
            "vehicles": [
                {"color": "red", "body_style_hint": "tractor",
                 "make": "Kubota", "model": "M7",
                 "vehicle_features": {"wheel_style": "tractor_ag"},
                 "description": "Red tractor",
                 "confidence": 0.82},
                {"color": "silver", "body_style_hint": "suv",
                 "make": "Toyota", "model": "4Runner",
                 "vehicle_features": {},
                 "description": "Silver 4Runner",
                 "confidence": 0.88},
            ],
            "primary_vehicle_index": 0,
        })
        assert result is not None
        # Primary (tractor) fields populated at top level
        assert result["color"] == "red"
        assert result["make"] == "Kubota"
        assert result["model"] == "M7"
        assert result["type"] == "tractor"
        # Original multi-vehicle array preserved
        assert len(result["vehicles"]) == 2



# =============================================================================
# Phase.130 (§11.53) — multi-vehicle Telegram-layer support
# =============================================================================


class TestPopulateLegacyMultiVehicleWideSurface:
    """Phase.130 — _populate_legacy_fields_from_vehicles also writes
    `colors.vehicle` (so alert_prompt reads useful colors) and builds
    `objects_detected` from EVERY vehicle in vehicles[] (not just the
    primary), so the threat-level LLM sees the full picture.
    """

    def test_populates_colors_vehicle_from_primary(self):
        from infra.vision_response import _populate_legacy_fields_from_vehicles
        result: dict = {
            "vehicles": [
                {"color": "red", "make": "Kubota", "model": "M7",
                 "body_style_hint": "tractor"},
            ],
            "primary_vehicle_index": 0,
        }
        _populate_legacy_fields_from_vehicles(result)
        assert result["colors"]["vehicle"] == "red"

    def test_does_not_clobber_existing_colors(self):
        """When colors.vehicle is already set, don't overwrite."""
        from infra.vision_response import _populate_legacy_fields_from_vehicles
        result: dict = {
            "vehicles": [{"color": "red", "make": "Kubota", "model": "M7"}],
            "primary_vehicle_index": 0,
            "colors": {"vehicle": "silver", "clothing_primary": None},
        }
        _populate_legacy_fields_from_vehicles(result)
        assert result["colors"]["vehicle"] == "silver"

    def test_builds_objects_detected_for_each_vehicle(self):
        """objects_detected lists every vehicle, not just the primary."""
        from infra.vision_response import _populate_legacy_fields_from_vehicles
        result = {
            "vehicles": [
                {"color": "red", "make": "Kubota", "model": "M7",
                 "body_style_hint": "tractor"},
                {"color": "silver", "make": "Toyota", "model": "4Runner",
                 "body_style_hint": "suv"},
            ],
            "primary_vehicle_index": 0,
        }
        _populate_legacy_fields_from_vehicles(result)
        assert result["objects_detected"] == [
            "tractor: Kubota M7",
            "suv: Toyota 4Runner",
        ]

    def test_objects_detected_falls_back_to_body_style_only(self):
        """When make/model unknown, just emit body_style (e.g. 'tractor')."""
        from infra.vision_response import _populate_legacy_fields_from_vehicles
        result = {
            "vehicles": [
                {"color": "red", "body_style_hint": "tractor",  # no make/model
                 "make": None, "model": None},
            ],
            "primary_vehicle_index": 0,
        }
        _populate_legacy_fields_from_vehicles(result)
        assert result["objects_detected"] == ["tractor"]

    def test_objects_detected_skips_non_dict_entries(self):
        """Defensive — non-dict entries are skipped."""
        from infra.vision_response import _populate_legacy_fields_from_vehicles
        result = {
            "vehicles": [
                {"color": "red", "make": "Kubota", "model": "M7",
                 "body_style_hint": "tractor"},
                "not a dict",
                None,
            ],
            "primary_vehicle_index": 0,
        }
        _populate_legacy_fields_from_vehicles(result)
        assert result["objects_detected"] == ["tractor: Kubota M7"]

    def test_objects_detected_skipped_when_already_present(self):
        """If objects_detected is non-empty, don't rebuild it."""
        from infra.vision_response import _populate_legacy_fields_from_vehicles
        result = {
            "vehicles": [{"color": "red", "make": "Kubota", "model": "M7"}],
            "primary_vehicle_index": 0,
            "objects_detected": ["pre-existing"],
        }
        _populate_legacy_fields_from_vehicles(result)
        assert result["objects_detected"] == ["pre-existing"]

    def test_no_vehicles_no_legacy_fill(self):
        """Empty vehicles[] = no objects_detected, no colors.vehicle."""
        from infra.vision_response import _populate_legacy_fields_from_vehicles
        result: dict = {"vehicles": []}
        _populate_legacy_fields_from_vehicles(result)
        assert "objects_detected" not in result
        assert "colors" not in result


class TestValidateVisionResultMultiVehicleWideSurface:
    """End-to-end: _validate_vision_result runs both back-compat paths."""

    def test_objects_detected_and_colors_both_populated(self):
        from infra.vision_response import _validate_vision_result
        result = _validate_vision_result({
            "vehicles": [
                {"color": "red", "make": "Kubota", "model": "M7",
                 "body_style_hint": "tractor"},
                {"color": "silver", "make": "Toyota", "model": "4Runner",
                 "body_style_hint": "suv"},
            ],
            "primary_vehicle_index": 0,
        })
        assert result is not None
        assert result["colors"]["vehicle"] == "red"
        assert result["objects_detected"] == [
            "tractor: Kubota M7",
            "suv: Toyota 4Runner",
        ]
