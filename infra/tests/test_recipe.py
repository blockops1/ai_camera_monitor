"""
Tests for infra/recipe.py — load + validate + resolve the motion recipe.

Phase 6B.166 §11.87.3. Covers:
  - load_recipe() against the real config/motion_recipe.json
  - validate_recipe() rejecting unknown keys, out-of-range values,
    non-int values (incl. bool which is technically int in Python),
    wrong shapes, missing fleet
  - resolve_for_camera() merging fleet + per-camera override
  - resolve_for_camera() falling through to fleet for unknown cameras
  - resolve_for_camera() accepting pre-loaded recipe to skip re-load
  - RECIPE_KEYS / FLEET_RANGES invariants

No pytest fixtures used; each test creates a small inline recipe
dict so failures are self-describing.
"""

import json
import os

import pytest

from infra.paths import MOTION_RECIPE_FILE
from infra.recipe import (
    FLEET_RANGES,
    RECIPE_KEYS,
    RecipeLoadError,
    load_recipe,
    resolve_for_camera,
    validate_recipe,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


VALID_FLEET = {
    "motion_sensitivity": 25,
    "smart_person": 50,
    "smart_vehicle": 30,
    "smart_pet": 30,
    "delay_person": 0,
    "delay_vehicle": 2,
    "delay_pet": 2,
}


def _full_recipe(fleet=None, cameras=None, comment=None):
    """Build a valid recipe dict with sensible defaults."""
    out: dict = {}
    if comment is not None:
        out["_comment"] = comment
    out["fleet"] = dict(fleet) if fleet is not None else dict(VALID_FLEET)
    if cameras is not None:
        out["cameras"] = dict(cameras)
    return out


@pytest.fixture
def recipe_file(tmp_path):
    """Write a valid recipe JSON to a temp file, yield path."""
    path = tmp_path / "motion_recipe.json"
    path.write_text(json.dumps(_full_recipe()))
    return str(path)


@pytest.fixture
def real_recipe_file():
    """Path to the actual committed config/motion_recipe.json."""
    return str(MOTION_RECIPE_FILE)


# ---------------------------------------------------------------------------
# load_recipe
# ---------------------------------------------------------------------------


class TestLoadRecipe:
    def test_load_valid_file(self, recipe_file):
        recipe = load_recipe(recipe_file)
        assert "fleet" in recipe
        assert recipe["fleet"]["motion_sensitivity"] == 25

    def test_load_real_committed_file(self, real_recipe_file):
        # The actual file in the repo must validate cleanly. This is
        # the smoke test for §11.87.3 acceptance.
        recipe = load_recipe(real_recipe_file)
        assert recipe["fleet"]["motion_sensitivity"] == 25
        assert "CAM5" in recipe["cameras"]
        assert recipe["cameras"]["CAM5"]["motion_sensitivity"] == 40
        # Other cameras fall through to fleet
        assert recipe["cameras"]["CAM3"].get("motion_sensitivity") is None

    def test_load_missing_file_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            load_recipe(str(tmp_path / "does_not_exist.json"))

    def test_load_malformed_json_raises_recipe_error(self, tmp_path):
        path = tmp_path / "broken.json"
        path.write_text("{ this is not json")
        with pytest.raises(RecipeLoadError, match="not valid JSON"):
            load_recipe(str(path))

    def test_load_validation_error_raises(self, tmp_path):
        # Out-of-range value
        path = tmp_path / "bad.json"
        path.write_text(json.dumps({"fleet": {"motion_sensitivity": 100}}))
        with pytest.raises(RecipeLoadError, match="out of range"):
            load_recipe(str(path))

    def test_load_default_uses_motion_recipe_file(self, monkeypatch):
        # When env_path is None, the loader reads from
        # infra.paths.MOTION_RECIPE_FILE. Verify by checking the
        # constant and asserting the committed file is loadable.
        from infra.paths import MOTION_RECIPE_FILE
        assert MOTION_RECIPE_FILE.endswith("config/motion_recipe.json")
        assert os.path.exists(MOTION_RECIPE_FILE)
        recipe = load_recipe()
        assert recipe["fleet"]["motion_sensitivity"] == 25


# ---------------------------------------------------------------------------
# validate_recipe
# ---------------------------------------------------------------------------


class TestValidateRecipe:
    def test_valid_full_recipe_passes(self):
        validate_recipe(_full_recipe())  # should not raise

    def test_valid_recipe_with_no_cameras(self):
        validate_recipe(_full_recipe(cameras=None))

    def test_valid_recipe_with_empty_cameras(self):
        validate_recipe(_full_recipe(cameras={}))

    def test_valid_recipe_with_empty_camera_override(self):
        validate_recipe(_full_recipe(cameras={"Some Cam": {}}))

    def test_valid_recipe_with_comment_keys(self):
        # _comment is allowed at any object level.
        recipe = _full_recipe(comment="top-level", cameras={})
        recipe["fleet"]["_comment"] = "fleet-level"
        recipe["cameras"]["Cam"] = {"_comment": "cam-level"}
        validate_recipe(recipe)

    def test_rejects_unknown_top_level_key(self):
        with pytest.raises(RecipeLoadError, match="unknown top-level keys"):
            validate_recipe({"fleet": {}, "bogus_key": 1})

    def test_rejects_missing_fleet(self):
        with pytest.raises(RecipeLoadError, match="missing required 'fleet'"):
            validate_recipe({"cameras": {}})

    def test_rejects_fleet_wrong_type(self):
        with pytest.raises(RecipeLoadError, match="fleet.*must be a JSON object"):
            validate_recipe({"fleet": "not a dict"})

    def test_rejects_cameras_wrong_type(self):
        with pytest.raises(RecipeLoadError, match="cameras.*must be a JSON object"):
            validate_recipe({"fleet": {}, "cameras": []})

    def test_rejects_unknown_fleet_key(self):
        with pytest.raises(RecipeLoadError, match="unknown recipe keys in fleet"):
            validate_recipe({"fleet": {"motion_sensitivity": 25, "bogus": 1}})

    def test_rejects_unknown_camera_key(self):
        with pytest.raises(RecipeLoadError, match="unknown recipe keys in cameras"):
            validate_recipe({
                "fleet": {},
                "cameras": {"Front": {"motion_sensitivity": 30, "bogus": 1}},
            })

    def test_rejects_non_int_value(self):
        with pytest.raises(RecipeLoadError, match="must be int"):
            validate_recipe({"fleet": {"motion_sensitivity": "25"}})

    def test_rejects_float_value(self):
        with pytest.raises(RecipeLoadError, match="must be int"):
            validate_recipe({"fleet": {"motion_sensitivity": 25.5}})

    def test_rejects_bool_value(self):
        # bool is technically int in Python but semantically wrong here.
        with pytest.raises(RecipeLoadError, match="must be int"):
            validate_recipe({"fleet": {"motion_sensitivity": True}})

    def test_rejects_motion_out_of_range_high(self):
        with pytest.raises(RecipeLoadError, match="out of range.*0..50"):
            validate_recipe({"fleet": {"motion_sensitivity": 51}})

    def test_rejects_motion_out_of_range_low(self):
        with pytest.raises(RecipeLoadError, match="out of range"):
            validate_recipe({"fleet": {"motion_sensitivity": -1}})

    def test_rejects_smart_out_of_range_high(self):
        with pytest.raises(RecipeLoadError, match="out of range.*0..100"):
            validate_recipe({"fleet": {"smart_person": 101}})

    def test_rejects_delay_out_of_range_high(self):
        with pytest.raises(RecipeLoadError, match="out of range.*0..8"):
            validate_recipe({"fleet": {"delay_person": 9}})

    def test_accepts_boundary_values(self):
        # 0 and the upper bound must be valid for every slider.
        recipe = {"fleet": {
            "motion_sensitivity": 0,   # lower bound
            "smart_person": 100,        # upper bound
            "smart_vehicle": 0,
            "smart_pet": 100,
            "delay_person": 8,          # upper bound
            "delay_vehicle": 0,
            "delay_pet": 8,
        }}
        validate_recipe(recipe)

    def test_rejects_non_dict_camera_entry(self):
        with pytest.raises(RecipeLoadError, match="must be a JSON object"):
            validate_recipe({"fleet": {}, "cameras": {"Cam": "string"}})

    def test_rejects_non_dict_top_level(self):
        with pytest.raises(RecipeLoadError, match="top-level JSON object"):
            validate_recipe([1, 2, 3])


# ---------------------------------------------------------------------------
# resolve_for_camera
# ---------------------------------------------------------------------------


class TestResolveForCamera:
    def test_known_camera_with_override_uses_override(self):
        recipe = _full_recipe(cameras={
            "Front Solar": {"motion_sensitivity": 40},
        })
        result = resolve_for_camera("Front Solar", recipe)
        assert result["motion_sensitivity"] == 40
        # Non-overridden fields fall through to fleet
        assert result["smart_person"] == 50

    def test_known_camera_with_empty_override_falls_through(self):
        recipe = _full_recipe(cameras={"Front Solar": {}})
        result = resolve_for_camera("Front Solar", recipe)
        assert result["motion_sensitivity"] == 25  # fleet

    def test_unknown_camera_returns_fleet(self):
        recipe = _full_recipe(cameras={
            "Front Solar": {"motion_sensitivity": 40},
        })
        result = resolve_for_camera("Unknown Camera", recipe)
        assert result["motion_sensitivity"] == 25  # fleet, not 40

    def test_returns_all_seven_slider_keys(self):
        recipe = _full_recipe()
        result = resolve_for_camera("Anything", recipe)
        assert set(result.keys()) == RECIPE_KEYS

    def test_no_metadata_keys_in_resolved_output(self):
        # _comment must not leak into the resolved dict.
        recipe = _full_recipe(comment="meta")
        recipe["fleet"]["_comment"] = "fleet meta"
        result = resolve_for_camera("Anything", recipe)
        assert "_comment" not in result

    def test_per_camera_partial_override(self):
        # Override only some keys; others fall through.
        recipe = _full_recipe(cameras={
            "Front Solar": {
                "motion_sensitivity": 35,
                "delay_person": 5,
            },
        })
        result = resolve_for_camera("Front Solar", recipe)
        assert result["motion_sensitivity"] == 35  # overridden
        assert result["delay_person"] == 5          # overridden
        assert result["smart_person"] == 50         # fleet
        assert result["delay_vehicle"] == 2         # fleet

    def test_malformed_camera_override_is_load_error(self):
        # resolve_for_camera does not validate; callers must run
        # validate_recipe() (or load_recipe(), which validates
        # implicitly) first. If a caller passes a malformed
        # recipe, validate_recipe() catches it before
        # resolve_for_camera is reached.
        bad_recipe = {
            "fleet": dict(VALID_FLEET),
            "cameras": {"Front": "not a dict"},  # would crash resolve
        }
        with pytest.raises(RecipeLoadError, match="must be a JSON object"):
            validate_recipe(bad_recipe)

    def test_loading_recipe_if_not_provided(self, recipe_file):
        # env_path=None, recipe=None → load_recipe() is called internally
        result = resolve_for_camera("Anything", env_path=recipe_file)
        assert result["motion_sensitivity"] == 25

    def test_real_recipe_file_resolution(self, real_recipe_file):
        # End-to-end: load + resolve against the committed file.
        result = resolve_for_camera("Outside Front Solar", env_path=real_recipe_file)
        assert result["motion_sensitivity"] == 40  # override
        assert result["smart_person"] == 50         # fleet

        result_garage = resolve_for_camera("Outside Front Garage", env_path=real_recipe_file)
        assert result_garage["motion_sensitivity"] == 25  # fleet

    def test_does_not_mutate_input_recipe(self):
        recipe = _full_recipe(cameras={"Front Solar": {"motion_sensitivity": 40}})
        snapshot = json.loads(json.dumps(recipe))  # deep copy
        _ = resolve_for_camera("Front Solar", recipe)
        assert recipe == snapshot


# ---------------------------------------------------------------------------
# RECIPE_KEYS / FLEET_RANGES invariants
# ---------------------------------------------------------------------------


class TestConstants:
    def test_recipe_keys_has_seven_sliders(self):
        assert len(RECIPE_KEYS) == 7

    def test_fleet_ranges_covers_all_recipe_keys(self):
        # Every key in RECIPE_KEYS must have a range.
        assert set(FLEET_RANGES.keys()) == RECIPE_KEYS

    def test_fleet_ranges_have_valid_bounds(self):
        for lo, hi in FLEET_RANGES.values():
            assert 0 <= lo <= hi
            assert lo < hi

    def test_motion_range_matches_reolink_firmware(self):
        # Per scripts/tune_510a_motion_sensitivity.py::_find_slider_in_group(),
        # the slider reads from 0 to 50.
        assert FLEET_RANGES["motion_sensitivity"] == (0, 50)

    def test_smart_range_matches_reolink_firmware(self):
        assert FLEET_RANGES["smart_person"] == (0, 100)
        assert FLEET_RANGES["smart_vehicle"] == (0, 100)
        assert FLEET_RANGES["smart_pet"] == (0, 100)

    def test_delay_range_matches_reolink_firmware(self):
        # 0..8 seconds (Alarm Delay slider).
        assert FLEET_RANGES["delay_person"] == (0, 8)
        assert FLEET_RANGES["delay_vehicle"] == (0, 8)
        assert FLEET_RANGES["delay_pet"] == (0, 8)
