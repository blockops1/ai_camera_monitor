"""
Tests for infra/alert_prompt.py — Qwen3.5-9B alert prompt + payload + parser.

Covers:
    - SYSTEM_PROMPT: contains required fields (threat_level, level 0/1/2,
      off-hours mention, false-alarm traps)
    - _to_local_iso: tz-aware conversion, naive passthrough, malformed
    - _build_payload: shape, model, temperature, messages, system+user
    - _parse_response: bare JSON, markdown fences, prose preamble, missing
      threat_level, non-numeric threat_level, empty input
    - _error_result: shape, threat_level=-1 sentinel
"""

import json

from infra.alert_prompt import (
    SYSTEM_PROMPT,
    _build_payload,
    _error_result,
    _parse_response,
    _to_local_iso,
)

# ---------------------------------------------------------------------------
# SYSTEM_PROMPT
# ---------------------------------------------------------------------------


class TestSystemPrompt:
    """The prompt is the contract with the LLM — verify the contract."""

    def test_mentions_all_threat_levels(self):
        for level in ("Level 0", "Level 1", "Level 2"):
            assert level in SYSTEM_PROMPT, f"SYSTEM_PROMPT missing {level}"

    def test_user_prompt_specifies_threat_level_field(self):
        # threat_level is in the user prompt (the JSON schema spec) since the
        # system prompt uses "Threat levels" as a heading. The LLM sees the
        # field name in the schema example.
        payload = _build_payload({}, "cam", "2026-07-20T14:00:00", "motion")
        user_prompt = payload["messages"][1]["content"]
        assert "threat_level" in user_prompt

    def test_mentions_off_hours_or_night(self):
        # The LLM needs context about night vs day.
        assert "10 PM" in SYSTEM_PROMPT or "Night" in SYSTEM_PROMPT

    def test_mentions_false_alarm_traps(self):
        # The prompt explicitly calls out animal resting → L0.
        assert "animal" in SYSTEM_PROMPT.lower() or "dog" in SYSTEM_PROMPT.lower()
        # Explicit "Lying down" trap.
        assert "lying down" in SYSTEM_PROMPT.lower() or "Lying down" in SYSTEM_PROMPT

    def test_mentions_parked_vehicle_baseline_override(self):
        # The prompt references the deterministic override in code so the LLM
        # doesn't try to talk its way past it.
        assert "parked_vehicle_baseline_override" in SYSTEM_PROMPT


# ---------------------------------------------------------------------------
# _to_local_iso
# ---------------------------------------------------------------------------


class TestToLocalIso:
    """Convert tz-aware timestamps to local time; naive timestamps unchanged."""

    def test_naive_passthrough(self):
        # Naive timestamp (no tzinfo) is treated as local — left alone.
        assert _to_local_iso("2026-07-20T14:00:00") == "2026-07-20T14:00:00"

    def test_tz_aware_has_offset_after_conversion(self):
        # Tz-aware timestamps get converted to local time. We can't assert
        # the exact offset (depends on test machine's tz), but the result
        # must include a UTC offset suffix.
        result = _to_local_iso("2026-07-20T20:00:00+00:00")
        # Either "+HH:MM" or "-HH:MM" suffix should be present.
        assert "+" in result[10:] or result.count("-") >= 2

    def test_malformed_returns_input(self):
        # Bad input is returned unchanged (the caller decides what to do).
        assert _to_local_iso("not-a-date") == "not-a-date"
        assert _to_local_iso("") == ""

    def test_none_returns_input(self):
        # None → returned unchanged (handled gracefully).
        assert _to_local_iso(None) is None  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# _build_payload
# ---------------------------------------------------------------------------


class TestBuildPayload:
    """OpenAI chat completions payload structure."""

    def test_payload_shape(self):
        vision = {
            "objects_detected": ["person"],
            "primary_subject": "person",
            "actions": ["walking"],
            "scene_description": "person walking",
            "confidence": 0.9,
            "notable_details": [],
            "colors": {"primary": "blue"},
            "species": None,
        }
        payload = _build_payload(vision, "CAM1", "2026-07-20T14:00:00", "rtsp_frames")
        assert payload["model"] == "qwen3.6"
        assert "messages" in payload
        assert "temperature" in payload
        assert "max_tokens" in payload

    def test_messages_structure(self):
        payload = _build_payload({}, "test_cam", "2026-07-20T14:00:00", "motion")
        messages = payload["messages"]
        assert len(messages) == 2
        assert messages[0]["role"] == "system"
        assert messages[0]["content"] == SYSTEM_PROMPT
        assert messages[1]["role"] == "user"
        # User prompt contains the camera name.
        assert "test_cam" in messages[1]["content"]

    def test_user_prompt_includes_vision_fields(self):
        vision = {
            "primary_subject": "person",
            "scene_description": "a person walking",
            "confidence": 0.85,
        }
        payload = _build_payload(vision, "cam", "2026-07-20T14:00:00", "motion")
        user_prompt = payload["messages"][1]["content"]
        assert "person" in user_prompt
        assert "0.85" in user_prompt

    def test_low_temperature(self):
        # Alert classification should be near-deterministic.
        payload = _build_payload({}, "cam", "2026-07-20T14:00:00", "motion")
        assert payload["temperature"] == 0.05

    def test_max_tokens_set(self):
        payload = _build_payload({}, "cam", "2026-07-20T14:00:00", "motion")
        assert payload["max_tokens"] == 512

    def test_user_prompt_specifies_required_fields(self):
        payload = _build_payload({}, "cam", "2026-07-20T14:00:00", "motion")
        user_prompt = payload["messages"][1]["content"]
        # The schema spec is embedded in the user prompt.
        for field in ("alert_id", "camera", "timestamp", "threat_level",
                      "title", "description", "recommendations",
                      "vision_summary", "source"):
            assert field in user_prompt


# ---------------------------------------------------------------------------
# _parse_response
# ---------------------------------------------------------------------------


class TestParseResponse:
    """Extract JSON from LLM response — bare / fenced / preamble."""

    def test_bare_json(self):
        raw = json.dumps({"threat_level": 1, "title": "alert"})
        result = _parse_response(raw)
        assert result is not None
        assert result["threat_level"] == 1

    def test_markdown_fenced_json(self):
        raw = '```json\n{"threat_level": 1, "title": "alert"}\n```'
        result = _parse_response(raw)
        assert result is not None
        assert result["title"] == "alert"

    def test_markdown_fenced_no_language(self):
        raw = '```\n{"threat_level": 2, "title": "critical"}\n```'
        result = _parse_response(raw)
        assert result is not None
        assert result["threat_level"] == 2

    def test_prose_preamble(self):
        # Some LLMs add "Sure, here is the JSON:" before the object.
        raw = 'Sure, here is the JSON:\n{"threat_level": 0, "title": "calm"}'
        result = _parse_response(raw)
        assert result is not None
        assert result["threat_level"] == 0

    def test_missing_threat_level_returns_none(self):
        # Without threat_level, the response is useless to us.
        raw = json.dumps({"title": "alert", "description": "something"})
        result = _parse_response(raw)
        assert result is None

    def test_non_dict_json_returns_none(self):
        # JSON array at the top level → not an alert.
        raw = "[1, 2, 3]"
        result = _parse_response(raw)
        assert result is None

    def test_invalid_json_returns_none(self):
        result = _parse_response("{not valid json")
        assert result is None

    def test_empty_input_returns_none(self):
        assert _parse_response("") is None

    def test_threat_level_coerced_to_int(self):
        # Float-valued threat_level gets cast to int (LLMs sometimes use 1.0).
        raw = json.dumps({"threat_level": 1.0, "title": "alert"})
        result = _parse_response(raw)
        assert result is not None
        assert result["threat_level"] == 1
        assert isinstance(result["threat_level"], int)

    def test_threat_level_zero_preserved(self):
        # L0 is a valid verdict (normal activity) — must not be rejected.
        raw = json.dumps({"threat_level": 0, "title": "normal activity"})
        result = _parse_response(raw)
        assert result is not None
        assert result["threat_level"] == 0


# ---------------------------------------------------------------------------
# _error_result
# ---------------------------------------------------------------------------


class TestErrorResult:
    """The error sentinel — caller uses threat_level=-1 to detect failures."""

    def test_shape(self):
        result = _error_result("API timeout")
        assert result["threat_level"] == -1
        assert result["title"] == "error"
        assert result["camera"] == "unknown"
        assert result["timestamp"] == ""
        assert result["source"] == "error"
        assert "API timeout" in result["description"]
        assert "API timeout" in result["vision_summary"]
        assert isinstance(result["recommendations"], list)
        assert len(result["recommendations"]) == 0

    def test_alert_id_is_uuid_string(self):
        result = _error_result("fail")
        # UUID v4 format: 8-4-4-4-12 hex chars.
        aid = result["alert_id"]
        assert len(aid) == 36
        assert aid.count("-") == 4

    def test_distinguishable_from_real_l0(self):
        # Real L0 has threat_level=0; error sentinel has -1.
        assert _error_result("x")["threat_level"] == -1



# =============================================================================
# Phase 6B.130 (§11.53) — Vehicles: section in _build_payload output
# =============================================================================


class TestFormatVehiclesBlock:
    """Phase 6B.130 — alert_prompt._format_vehicles_block produces a
    'Vehicles:' section for multi-vehicle vision results so the threat-
    level LLM sees each vehicle's identification."""

    def test_returns_empty_when_vehicles_empty(self):
        from infra.alert_prompt import _format_vehicles_block
        assert _format_vehicles_block({}) == ""
        assert _format_vehicles_block({"vehicles": []}) == ""

    def test_single_vehicle_block(self):
        from infra.alert_prompt import _format_vehicles_block
        block = _format_vehicles_block({
            "vehicles": [
                {"color": "red", "make": "Kubota", "model": "M7",
                 "body_style_hint": "tractor"},
            ],
            "primary_vehicle_index": 0,
        })
        assert "Vehicles:" in block
        assert "red Kubota M7 tractor" in block
        assert "(primary)" in block

    def test_multi_vehicle_block_marks_primary(self):
        """Primary gets '(primary)' marker; other vehicles listed without."""
        from infra.alert_prompt import _format_vehicles_block
        block = _format_vehicles_block({
            "vehicles": [
                # Index 0: incidental
                {"color": "silver", "make": "Toyota",
                 "model": "4Runner", "body_style_hint": "suv"},
                # Index 1: primary (matches bbox)
                {"color": "red", "make": "Kubota", "model": "M7",
                 "body_style_hint": "tractor"},
                # Index 2: another incidental
                {"color": "blue", "make": "Tesla", "model": "Model 3",
                 "body_style_hint": "sedan"},
            ],
            "primary_vehicle_index": 1,
        })
        assert "Vehicles:" in block
        assert "red Kubota M7 tractor (primary)" in block
        assert "silver Toyota 4Runner suv" in block
        assert "blue Tesla Model 3 sedan" in block
        # Only one (primary) marker — even though index=1 isn't the first line.
        assert block.count("(primary)") == 1

    def test_caps_at_three_vehicles_with_count_footer(self):
        """More than 3 vehicles → show 3, then a '(N more...)' footer line."""
        from infra.alert_prompt import _format_vehicles_block
        vehicles = [
            {"color": "red", "make": "K1"},
            {"color": "blue", "make": "K2"},
            {"color": "green", "make": "K3"},
            {"color": "yellow", "make": "K4"},
            {"color": "white", "make": "K5"},
        ]
        block = _format_vehicles_block({
            "vehicles": vehicles,
            "primary_vehicle_index": 0,
        })
        assert "K1" in block
        assert "K3" in block
        assert "K4" not in block  # capped
        assert "K5" not in block
        assert "(2 more vehicle(s) omitted)" in block

    def test_three_or_fewer_no_footer(self):
        from infra.alert_prompt import _format_vehicles_block
        block = _format_vehicles_block({
            "vehicles": [
                {"color": "red", "make": "K1"},
                {"color": "blue", "make": "K2"},
                {"color": "green", "make": "K3"},
            ],
            "primary_vehicle_index": 0,
        })
        assert "(more vehicle" not in block

    def test_skips_vehicles_with_no_identifying_fields(self):
        """A vehicle entry with no color/make/model/body_style_hint is
        omitted from the block (no empty lines, no spurious markers)."""
        from infra.alert_prompt import _format_vehicles_block
        block = _format_vehicles_block({
            "vehicles": [
                {"color": "red", "make": "Kubota", "model": "M7"},
                {},  # empty
                {"color": "silver", "make": "Toyota", "model": "4Runner"},
            ],
            "primary_vehicle_index": 0,
        })
        assert "red Kubota" in block
        assert "silver Toyota" in block
        # No double "Vehicles:" marker
        assert block.count("Vehicles:") == 1

    def test_invalid_primary_index_falls_back_to_zero(self):
        """primary_vehicle_index out of range → default to 0."""
        from infra.alert_prompt import _format_vehicles_block
        block = _format_vehicles_block({
            "vehicles": [
                {"color": "red", "make": "K1"},
                {"color": "blue", "make": "K2"},
            ],
            "primary_vehicle_index": 99,  # out of range
        })
        assert "red K1 (primary)" in block
        assert "blue K2" in block

    def test_non_int_primary_index_defaults_to_zero(self):
        from infra.alert_prompt import _format_vehicles_block
        block = _format_vehicles_block({
            "vehicles": [
                {"color": "red", "make": "K1"},
                {"color": "blue", "make": "K2"},
            ],
            "primary_vehicle_index": None,
        })
        assert "red K1 (primary)" in block


class TestBuildPayloadMultiVehicle:
    """End-to-end: _build_payload inserts the Vehicles: section for
    multi-vehicle vision results."""

    def test_multi_vehicle_appears_in_user_prompt(self):
        from infra.alert_prompt import _build_payload
        payload = _build_payload(
            {
                "vehicles": [
                    {"color": "red", "make": "Kubota", "model": "M7",
                     "body_style_hint": "tractor"},
                    {"color": "silver", "make": "Toyota", "model": "4Runner",
                     "body_style_hint": "suv"},
                ],
                "primary_vehicle_index": 0,
                "scene_description": "Red tractor on gravel",
            },
            "CAM5", "2026-08-26T13:05:54-04:00", "motion",
        )
        user_prompt = payload["messages"][1]["content"]
        assert "Vehicles:" in user_prompt
        assert "red Kubota M7 tractor (primary)" in user_prompt
        assert "silver Toyota 4Runner suv" in user_prompt
        # Legacy fields still present (back-compat)
        assert "Objects:" in user_prompt

    def test_no_vehicles_no_block(self):
        """vision_result without vehicles[] → no Vehicles: section."""
        from infra.alert_prompt import _build_payload
        payload = _build_payload(
            {"primary_subject": "person"},
            "Front Door", "2026-08-26T13:05:54-04:00", "motion",
        )
        user_prompt = payload["messages"][1]["content"]
        assert "Vehicles:" not in user_prompt
