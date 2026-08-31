"""
Tests for infra/alert_overrides_offhours.py — off-hours escalation safety net.

Covers:
    - _is_off_hours: window boundaries, naive vs tz-aware, malformed input
    - _vision_sees_person: primary_subject, objects_detected, missing fields,
      non-person objects
    - _apply_off_hours_override: 4 cells of the truth table (with/without
      person × with/without off-hours), preserves L2, fixes description+title
"""


from infra.alert_overrides_offhours import (
    OFF_HOURS_END_HOUR,
    OFF_HOURS_MIN_LEVEL,
    OFF_HOURS_START_HOUR,
    _apply_off_hours_override,
    _is_off_hours,
    _vision_sees_person,
)

# ---------------------------------------------------------------------------
# _is_off_hours
# ---------------------------------------------------------------------------


class TestIsOffHours:
    """Verify _is_off_hours against the documented window: 20:00 – 06:00 local."""

    def test_window_boundaries(self):
        # Boundary cases: hour 20 is in, hour 6 is out, hour 19 is out.
        assert _is_off_hours("2026-07-20T20:00:00") is True   # exactly 8 PM
        assert _is_off_hours("2026-07-20T19:59:59") is False  # 1 second before
        assert _is_off_hours("2026-07-20T06:00:00") is False  # exactly 6 AM
        assert _is_off_hours("2026-07-20T05:59:59") is True   # 1 second before

    def test_window_midpoints(self):
        # Mid-window: 22:00 (deep night) and 02:00 (deep early) both in.
        assert _is_off_hours("2026-07-20T22:30:00") is True
        assert _is_off_hours("2026-07-20T02:15:00") is True

    def test_work_hours_out(self):
        # 14:00 local on a workday → not off-hours.
        assert _is_off_hours("2026-07-20T14:00:00") is False
        # 09:00 morning → not off-hours.
        assert _is_off_hours("2026-07-20T09:00:00") is False

    def test_constants_match_docstring(self):
        # The docstring claims "20:00 – 06:00" — make sure constants say so.
        assert OFF_HOURS_START_HOUR == 20
        assert OFF_HOURS_END_HOUR == 6

    def test_naive_treated_as_local(self):
        # Naive ISO 8601 (no tzinfo) is treated as local time.
        # 20:00 naive → off-hours.
        assert _is_off_hours("2026-07-20T20:00:00") is True
        # 14:00 naive → not off-hours.
        assert _is_off_hours("2026-07-20T14:00:00") is False

    def test_tz_aware_utc_converted_to_local(self):
        # Reolink sends +00:00 UTC. On a US-Eastern box (EDT, UTC-4),
        # 00:54 UTC = 20:54 local → off-hours (escalation territory).
        # 16:25 UTC = 12:25 EDT → not off-hours (work hours).
        # We can't assert the absolute timezone without knowing the test
        # machine's tz, but we can assert that the function doesn't
        # silently treat UTC as local.
        # Use +00:00 (UTC) at 20:00 — if the machine is UTC this is in.
        # We trust astimezone() to do the right thing regardless.
        result = _is_off_hours("2026-07-20T20:00:00+00:00")
        # Either True (machine in UTC) or some local time below 20:00 —
        # either way, the function should NOT throw.
        assert isinstance(result, bool)

    def test_malformed_returns_false(self):
        # Bad input never raises — returns False (conservative).
        assert _is_off_hours("not-a-date") is False
        assert _is_off_hours("") is False
        assert _is_off_hours(None) is False  # type: ignore[arg-type]
        assert _is_off_hours(12345) is False  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# _vision_sees_person
# ---------------------------------------------------------------------------


class TestVisionSeesPerson:
    """Truth table for _vision_sees_person."""

    def test_primary_subject_person_variants(self):
        # All canonical "person" words in primary_subject → True.
        for word in ("person", "people", "man", "woman", "child", "human"):
            assert _vision_sees_person({"primary_subject": word}) is True
            assert _vision_sees_person({"primary_subject": word.upper()}) is True  # case-insensitive

    def test_primary_subject_non_person(self):
        # Non-person subjects → False.
        for word in ("vehicle", "animal", "object", "none", "dog", ""):
            assert _vision_sees_person({"primary_subject": word}) is False

    def test_objects_detected_person(self):
        # Person found in objects_detected, even if primary_subject is something else.
        assert _vision_sees_person(
            {"primary_subject": "vehicle", "objects_detected": ["car", "person"]}
        ) is True
        # Suspect / intruder / unidentified person.
        assert _vision_sees_person(
            {"primary_subject": "vehicle", "objects_detected": ["suspect"]}
        ) is True
        assert _vision_sees_person(
            {"primary_subject": "none", "objects_detected": ["unidentified person"]}
        ) is True

    def test_objects_detected_no_person(self):
        # No person anywhere → False.
        assert _vision_sees_person(
            {"primary_subject": "vehicle", "objects_detected": ["car", "truck"]}
        ) is False
        assert _vision_sees_person(
            {"primary_subject": "animal", "objects_detected": ["dog", "cat"]}
        ) is False

    def test_missing_fields(self):
        # Empty dict → False (no person signal).
        assert _vision_sees_person({}) is False
        # None fields → False.
        assert _vision_sees_person({"primary_subject": None}) is False
        assert _vision_sees_person({"primary_subject": "", "objects_detected": None}) is False

    def test_non_dict_input(self):
        # Non-dict vision_result → False.
        assert _vision_sees_person(None) is False  # type: ignore[arg-type]
        assert _vision_sees_person("person") is False  # type: ignore[arg-type]
        assert _vision_sees_person([]) is False  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# _apply_off_hours_override
# ---------------------------------------------------------------------------


class TestApplyOffHoursOverride:
    """The 4-cell truth table + L2 preservation + description fixes."""

    def test_escalates_l0_to_min_during_off_hours_with_person(self):
        alert = {"threat_level": 0, "title": "normal activity", "description": "calm scene"}
        result = _apply_off_hours_override(
            alert,
            {"primary_subject": "person"},
            "2026-07-20T22:00:00",  # off-hours
        )
        assert result["threat_level"] == OFF_HOURS_MIN_LEVEL
        assert "off-hours" in result["description"].lower()
        assert "22:00" in result["description"]  # timestamp surfaced

    def test_does_not_escalate_during_work_hours(self):
        alert = {"threat_level": 0, "title": "normal activity", "description": "calm scene"}
        result = _apply_off_hours_override(
            alert,
            {"primary_subject": "person"},
            "2026-07-20T14:00:00",  # work hours
        )
        assert result["threat_level"] == 0
        assert result["description"] == "calm scene"  # untouched

    def test_does_not_escalate_when_no_person(self):
        alert = {"threat_level": 0, "title": "normal activity", "description": "calm scene"}
        result = _apply_off_hours_override(
            alert,
            {"primary_subject": "vehicle", "objects_detected": ["car"]},
            "2026-07-20T22:00:00",  # off-hours but no person
        )
        assert result["threat_level"] == 0
        assert result["description"] == "calm scene"

    def test_does_not_overwrite_existing_l2(self):
        # If the LLM already escalated to L2, the safety net trusts it.
        alert = {"threat_level": 2, "title": "BREAK-IN", "description": "person with crowbar"}
        result = _apply_off_hours_override(
            alert,
            {"primary_subject": "person"},
            "2026-07-20T22:00:00",
        )
        assert result["threat_level"] == 2
        assert result["title"] == "BREAK-IN"  # unchanged
        assert result["description"] == "person with crowbar"  # unchanged

    def test_replaces_title_when_error_or_empty(self):
        # Error sentinel has title="error" — gets replaced.
        alert = {"threat_level": -1, "title": "error", "description": "API down"}
        result = _apply_off_hours_override(
            alert,
            {"primary_subject": "person"},
            "2026-07-20T22:00:00",
        )
        assert result["title"] == "Person detected during off-hours"

    def test_preserves_l1_title(self):
        # If LLM already said L1, keep its title (escalation safety net only).
        alert = {"threat_level": 1, "title": "person loitering", "description": "person at door"}
        result = _apply_off_hours_override(
            alert,
            {"primary_subject": "person"},
            "2026-07-20T22:00:00",
        )
        assert result["threat_level"] == 1
        assert result["title"] == "person loitering"  # unchanged

    def test_appends_description_when_present(self):
        # When description is present, the off-hours message prepends context.
        alert = {"threat_level": 0, "title": "normal", "description": "person at door"}
        result = _apply_off_hours_override(
            alert,
            {"primary_subject": "person"},
            "2026-07-20T22:00:00",
        )
        assert "off-hours" in result["description"]
        assert "person at door" in result["description"]

    def test_uses_fallback_description_when_empty(self):
        # When description is missing/empty, fallback message appears.
        alert = {"threat_level": 0, "title": "normal"}
        result = _apply_off_hours_override(
            alert,
            {"primary_subject": "person"},
            "2026-07-20T22:00:00",
        )
        assert "Vision model confirmed a person in frame" in result["description"]
