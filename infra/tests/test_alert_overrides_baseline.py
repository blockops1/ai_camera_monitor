"""
Tests for infra/alert_overrides_baseline.py — 4 baseline-noise suppressions.

Covers:
    - _vision_signals_distant_vehicle: vehicle words + keyword matching
    - _vision_returns_none: truth table for primary_subject + objects
    - All 4 baseline overrides:
        - parked_vehicle_baseline
        - distant_vehicle_baseline
        - static_object_baseline
        - vision_none_baseline
    - _apply_baseline_overrides: orchestration order, idempotency
"""

import pytest

from infra.alert_overrides_baseline import (
    _apply_baseline_overrides,
    _apply_distant_vehicle_baseline_override,
    _apply_parked_vehicle_baseline_override,
    _apply_static_object_baseline_override,
    _apply_vision_none_baseline_override,
    _get_distant_vehicle_cameras,
    _get_parked_vehicle_cameras,
    _get_static_object_cameras,
    _get_vision_none_cameras,
    _vision_returns_none,
    _vision_signals_distant_vehicle,
)

# ---------------------------------------------------------------------------
# _vision_signals_distant_vehicle
# ---------------------------------------------------------------------------


class TestVisionSignalsDistantVehicle:
    """Match vehicle words + distance keywords in scene_description."""

    def test_vehicle_in_objects_with_ambiguous_scene(self):
        for kw in ("faint", "indistinct", "distant", "reflection", "across the road",
                   "off-property", "barely visible", "light source"):
            result = _vision_signals_distant_vehicle(
                {
                    "objects_detected": ["vehicle"],
                    "scene_description": f"vehicle {kw} in frame",
                }
            )
            assert result is True, f"keyword {kw!r} should match"

    def test_vehicle_in_primary_subject_with_ambiguous_scene(self):
        result = _vision_signals_distant_vehicle(
            {
                "primary_subject": "car",
                "scene_description": "faint reflection of headlights",
            }
        )
        assert result is True

    def test_vehicle_no_ambiguity_keyword(self):
        # Clear vehicle, no distance words → not distant.
        result = _vision_signals_distant_vehicle(
            {
                "objects_detected": ["car"],
                "scene_description": "person standing next to a parked truck",
            }
        )
        assert result is False

    def test_no_vehicle_at_all(self):
        # No vehicle signal → not distant (that's vision_none territory).
        result = _vision_signals_distant_vehicle(
            {
                "primary_subject": "person",
                "objects_detected": ["person", "dog"],
                "scene_description": "person walking, faint light in background",
            }
        )
        assert result is False

    def test_non_dict_input(self):
        assert _vision_signals_distant_vehicle(None) is False  # type: ignore[arg-type]
        assert _vision_signals_distant_vehicle({}) is False


# ---------------------------------------------------------------------------
# _vision_returns_none
# ---------------------------------------------------------------------------


class TestVisionReturnsNone:
    """Truth table documented at alert_overrides_baseline.py:163-180."""

    def test_primary_subject_none_with_empty_objects(self):
        # Vision saw literally nothing.
        assert _vision_returns_none({"primary_subject": "none", "objects_detected": []}) is True

    def test_primary_subject_empty_with_empty_objects(self):
        assert _vision_returns_none({"primary_subject": "", "objects_detected": []}) is True

    def test_primary_subject_none_but_objects_has_person(self):
        # objects_detected had a person → vision DID see something.
        assert _vision_returns_none(
            {"primary_subject": "none", "objects_detected": ["person"]}
        ) is False

    def test_primary_subject_none_but_objects_has_vehicle(self):
        assert _vision_returns_none(
            {"primary_subject": "none", "objects_detected": ["car"]}
        ) is False

    def test_primary_subject_real_value(self):
        # Anything that's not 'none' or empty → vision saw something.
        for word in ("person", "vehicle", "animal", "object", "dog"):
            assert _vision_returns_none({"primary_subject": word}) is False

    def test_missing_dict_input_treated_as_saw_nothing(self):
        # Missing/malformed vision == treat as "saw nothing"
        # (conservative — let the override suppress fabricated L1s).
        assert _vision_returns_none(None) is True  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# _apply_parked_vehicle_baseline_override
# ---------------------------------------------------------------------------


class TestParkedVehicleBaseline:
    """Downgrade L1 → L0 when parked camera sees no person."""

    def test_downgrades_l1_when_no_person_off_hours(self):
        # Use a camera known to be in the parked-vehicle set
        # (loaded from config/alert_overrides.json at import time).
        camera = next(iter(_get_parked_vehicle_cameras()), "")
        if not camera:
            pytest.skip("No parked_vehicle_baseline cameras in config")
        alert = {"threat_level": 1, "title": "parked car alert"}
        result = _apply_parked_vehicle_baseline_override(
            alert,
            {"primary_subject": "vehicle", "objects_detected": ["car"]},
            camera,
            "2026-07-20T22:00:00",  # off-hours
        )
        assert result["threat_level"] == 0
        assert result["suppressed_by"] == "parked_vehicle_baseline_override"

    def test_does_not_downgrade_when_person_present(self):
        camera = next(iter(_get_parked_vehicle_cameras()), None)
        if not camera:
            pytest.skip("No parked_vehicle_baseline cameras in config")
        alert = {"threat_level": 1, "title": "person at car"}
        result = _apply_parked_vehicle_baseline_override(
            alert,
            {"primary_subject": "person", "objects_detected": ["person"]},
            camera,
            "2026-07-20T22:00:00",
        )
        assert result["threat_level"] == 1
        assert "suppressed_by" not in result

    def test_does_not_downgrade_l2(self):
        # L2 critical threats always pass through.
        camera = next(iter(_get_parked_vehicle_cameras()), None)
        if not camera:
            pytest.skip("No parked_vehicle_baseline cameras in config")
        alert = {"threat_level": 2, "title": "BREAK-IN"}
        result = _apply_parked_vehicle_baseline_override(
            alert,
            {"primary_subject": "vehicle", "objects_detected": ["car"]},
            camera,
            "2026-07-20T22:00:00",
        )
        assert result["threat_level"] == 2
        assert "suppressed_by" not in result

    def test_does_not_apply_during_work_hours(self):
        camera = next(iter(_get_parked_vehicle_cameras()), None)
        if not camera:
            pytest.skip("No parked_vehicle_baseline cameras in config")
        alert = {"threat_level": 1, "title": "parked car alert"}
        result = _apply_parked_vehicle_baseline_override(
            alert,
            {"primary_subject": "vehicle", "objects_detected": ["car"]},
            camera,
            "2026-07-20T14:00:00",  # work hours
        )
        assert result["threat_level"] == 1

    def test_does_not_apply_outside_camera_set(self):
        alert = {"threat_level": 1, "title": "alert"}
        result = _apply_parked_vehicle_baseline_override(
            alert,
            {"primary_subject": "vehicle", "objects_detected": ["car"]},
            "CAM1",  # NOT in parked-vehicle set
            "2026-07-20T22:00:00",
        )
        assert result["threat_level"] == 1

    def test_does_not_apply_without_timestamp(self):
        # Conservative — better to err on signal than suppress without proof.
        camera = next(iter(_get_parked_vehicle_cameras()), None)
        if not camera:
            pytest.skip("No parked_vehicle_baseline cameras in config")
        alert = {"threat_level": 1, "title": "alert"}
        result = _apply_parked_vehicle_baseline_override(
            alert,
            {"primary_subject": "vehicle", "objects_detected": ["car"]},
            camera,
            None,
        )
        assert result["threat_level"] == 1


# ---------------------------------------------------------------------------
# _apply_distant_vehicle_baseline_override
# ---------------------------------------------------------------------------


class TestDistantVehicleBaseline:
    """Downgrade L1 → L0 when distant/ambiguous vehicle signal + no person."""

    def test_downgrades_l1_with_distant_keyword(self):
        camera = next(iter(_get_distant_vehicle_cameras()), None)
        if not camera:
            pytest.skip("No distant_vehicle_baseline cameras in config")
        alert = {"threat_level": 1, "title": "faint vehicle"}
        result = _apply_distant_vehicle_baseline_override(
            alert,
            {
                "primary_subject": "vehicle",
                "objects_detected": ["car"],
                "scene_description": "faint reflection across the road",
            },
            camera,
            "2026-07-20T22:00:00",
        )
        assert result["threat_level"] == 0
        assert result["suppressed_by"] == "distant_vehicle_baseline_override"

    def test_does_not_downgrade_clear_vehicle(self):
        camera = next(iter(_get_distant_vehicle_cameras()), None)
        if not camera:
            pytest.skip("No distant_vehicle_baseline cameras in config")
        alert = {"threat_level": 1, "title": "vehicle in driveway"}
        result = _apply_distant_vehicle_baseline_override(
            alert,
            {
                "primary_subject": "vehicle",
                "objects_detected": ["car"],
                "scene_description": "vehicle parked in driveway",  # no ambiguity keywords
            },
            camera,
            "2026-07-20T22:00:00",
        )
        assert result["threat_level"] == 1

    def test_does_not_downgrade_when_person_present(self):
        camera = next(iter(_get_distant_vehicle_cameras()), None)
        if not camera:
            pytest.skip("No distant_vehicle_baseline cameras in config")
        alert = {"threat_level": 1, "title": "person at vehicle"}
        result = _apply_distant_vehicle_baseline_override(
            alert,
            {
                "primary_subject": "person",
                "objects_detected": ["person", "car"],
                "scene_description": "faint reflection",
            },
            camera,
            "2026-07-20T22:00:00",
        )
        assert result["threat_level"] == 1


# ---------------------------------------------------------------------------
# _apply_static_object_baseline_override
# ---------------------------------------------------------------------------


class TestStaticObjectBaseline:
    """Downgrade L1 → L0 on cameras with static environmental noise + no person."""

    def test_downgrades_l1_with_no_person(self):
        camera = next(iter(_get_static_object_cameras()), None)
        if not camera:
            pytest.skip("No static_object_baseline cameras in config")
        alert = {"threat_level": 1, "title": "object on ground"}
        result = _apply_static_object_baseline_override(
            alert,
            {"primary_subject": "object", "objects_detected": ["tarp", "debris"]},
            camera,
            "2026-07-20T22:00:00",
        )
        assert result["threat_level"] == 0
        assert result["suppressed_by"] == "static_object_baseline_override"

    def test_does_not_downgrade_when_person_present(self):
        camera = next(iter(_get_static_object_cameras()), None)
        if not camera:
            pytest.skip("No static_object_baseline cameras in config")
        alert = {"threat_level": 1, "title": "person at door"}
        result = _apply_static_object_baseline_override(
            alert,
            {"primary_subject": "person", "objects_detected": ["person", "tarp"]},
            camera,
            "2026-07-20T22:00:00",
        )
        assert result["threat_level"] == 1


# ---------------------------------------------------------------------------
# _apply_vision_none_baseline_override
# ---------------------------------------------------------------------------


class TestVisionNoneBaseline:
    """Downgrade L1 → L0 when vision returned nothing identifiable."""

    def test_downgrades_l1_when_vision_none(self):
        camera = next(iter(_get_vision_none_cameras()), None)
        if not camera:
            pytest.skip("No vision_none_baseline cameras in config")
        alert = {"threat_level": 1, "title": "Nighttime Activity at Door"}
        result = _apply_vision_none_baseline_override(
            alert,
            {"primary_subject": "none", "objects_detected": []},
            camera,
            "2026-07-20T22:00:00",
        )
        assert result["threat_level"] == 0
        assert result["suppressed_by"] == "vision_none_baseline_override"

    def test_does_not_downgrade_when_vision_saw_person(self):
        camera = next(iter(_get_vision_none_cameras()), None)
        if not camera:
            pytest.skip("No vision_none_baseline cameras in config")
        alert = {"threat_level": 1, "title": "person at door"}
        result = _apply_vision_none_baseline_override(
            alert,
            {"primary_subject": "none", "objects_detected": ["person"]},  # contradiction
            camera,
            "2026-07-20T22:00:00",
        )
        assert result["threat_level"] == 1

    def test_does_not_apply_during_work_hours(self):
        camera = next(iter(_get_vision_none_cameras()), None)
        if not camera:
            pytest.skip("No vision_none_baseline cameras in config")
        alert = {"threat_level": 1, "title": "alert"}
        result = _apply_vision_none_baseline_override(
            alert,
            {"primary_subject": "none", "objects_detected": []},
            camera,
            "2026-07-20T14:00:00",  # work hours
        )
        assert result["threat_level"] == 1


# ---------------------------------------------------------------------------
# _apply_baseline_overrides (orchestrator)
# ---------------------------------------------------------------------------


class TestApplyBaselineOverrides:
    """Verify order: parked → distant → static → vision_none. Idempotent."""

    def test_idempotent_after_first_demote(self):
        # Once an alert is downgraded to L0, subsequent overrides skip.
        # Test that the suppressed_by marker reflects the FIRST rule that fired.
        camera = "CAM1"  # in static_object AND vision_none sets
        alert = {"threat_level": 1, "title": "tarp on ground"}
        result = _apply_baseline_overrides(
            alert,
            {"primary_subject": "object", "objects_detected": ["tarp"]},
            camera,
            "2026-07-20T22:00:00",
        )
        assert result["threat_level"] == 0
        # Order: parked_vehicle (skipped — not parked), distant (skipped — not vehicle),
        # static_object (FIRES first), vision_none (would skip because primary is 'object').
        assert result["suppressed_by"] == "static_object_baseline_override"

    def test_no_override_when_l2(self):
        # L2 passes through everything.
        camera = "CAM1"
        alert = {"threat_level": 2, "title": "BREAK-IN"}
        result = _apply_baseline_overrides(
            alert,
            {"primary_subject": "object", "objects_detected": ["tarp"]},
            camera,
            "2026-07-20T22:00:00",
        )
        assert result["threat_level"] == 2
        assert "suppressed_by" not in result

    def test_no_override_when_camera_not_in_any_set(self):
        alert = {"threat_level": 1, "title": "alert"}
        result = _apply_baseline_overrides(
            alert,
            {"primary_subject": "vehicle", "objects_detected": ["car"]},
            "Inside Some Workshop",  # not in any baseline set
            "2026-07-20T22:00:00",
        )
        assert result["threat_level"] == 1
        assert "suppressed_by" not in result

    def test_distant_fires_before_static(self):
        # On a camera that's in BOTH distant and static sets, the first matching
        # rule should win (distant is checked first per the docstring).
        # Find a camera in both sets — or skip if none.
        distant_cams = _get_distant_vehicle_cameras()
        static_cams = _get_static_object_cameras()
        overlap = distant_cams & static_cams
        if not overlap:
            pytest.skip("No camera in both distant_vehicle + static_object sets")
        camera = next(iter(overlap))
        alert = {"threat_level": 1, "title": "faint vehicle"}
        result = _apply_baseline_overrides(
            alert,
            {
                "primary_subject": "vehicle",
                "objects_detected": ["car"],
                "scene_description": "faint reflection across the road",
            },
            camera,
            "2026-07-20T22:00:00",
        )
        assert result["threat_level"] == 0
        assert result["suppressed_by"] == "distant_vehicle_baseline_override"

    def test_no_override_without_timestamp(self):
        # Conservative — skip all overrides without a timestamp.
        alert = {"threat_level": 1, "title": "alert"}
        result = _apply_baseline_overrides(
            alert,
            {"primary_subject": "vehicle", "objects_detected": ["car"]},
            "CAM1",
            None,
        )
        assert result["threat_level"] == 1
        assert "suppressed_by" not in result


# ---------------------------------------------------------------------------
# Camera set getters
# ---------------------------------------------------------------------------


class TestCameraSetGetters:
    """Getters return frozenset; sets may be empty depending on config."""

    def test_parked_cameras_returns_frozenset(self):
        result = _get_parked_vehicle_cameras()
        assert isinstance(result, frozenset)

    def test_distant_cameras_returns_frozenset(self):
        result = _get_distant_vehicle_cameras()
        assert isinstance(result, frozenset)

    def test_static_cameras_returns_frozenset(self):
        result = _get_static_object_cameras()
        assert isinstance(result, frozenset)

    def test_vision_none_cameras_returns_frozenset(self):
        result = _get_vision_none_cameras()
        assert isinstance(result, frozenset)
