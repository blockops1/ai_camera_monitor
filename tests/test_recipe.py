"""Tests for apply_recipe + verify_recipe -- mock CLI, no real hardware."""

from __future__ import annotations

import textwrap
from pathlib import Path
from unittest.mock import MagicMock, patch

from infra.reolink import (
    DetectionConfig,
    ReolinkCommandError,
    _parse_recipe_toml,
    apply_recipe,
    verify_recipe,
)

# ---------------------------------------------------------------------------
# _parse_recipe_toml
# ---------------------------------------------------------------------------


class TestParseRecipeToml:
    """TOML parser returns {camera: {subcmd: {field: value}}}."""

    def test_parse_person_front(self, tmp_path: Path):
        toml_text = textwrap.dedent("""
            [detect_ai.person.front]
            stay_time = 2
            sensitivity = 50
        """)
        recipe = tmp_path / "recipe.toml"
        recipe.write_text(toml_text)
        result = _parse_recipe_toml(str(recipe))

        assert result == {
            "front": {
                "detect_ai.person": {"stay_time": 2, "sensitivity": 50},
            }
        }

    def test_parse_multi_camera(self, tmp_path: Path):
        toml_text = textwrap.dedent("""
            [detect_ai.person.front]
            stay_time = 2
            sensitivity = 50
            [detect_ai.person.back]
            stay_time = 3
            sensitivity = 60
        """)
        recipe = tmp_path / "recipe.toml"
        recipe.write_text(toml_text)
        result = _parse_recipe_toml(str(recipe))

        assert result["front"]["detect_ai.person"] == {"stay_time": 2, "sensitivity": 50}
        assert result["back"]["detect_ai.person"] == {"stay_time": 3, "sensitivity": 60}

    def test_parse_all_subcommands(self, tmp_path: Path):
        toml_text = textwrap.dedent("""
            [detect_ai.person.front]
            stay_time = 2
            sensitivity = 50
            [detect_ai.vehicle.front]
            stay_time = 2
            sensitivity = 50
            [detect_ai.dog_cat.front]
            stay_time = 2
            sensitivity = 30
        """)
        recipe = tmp_path / "recipe.toml"
        recipe.write_text(toml_text)
        result = _parse_recipe_toml(str(recipe))

        assert "front" in result
        assert "detect_ai.person" in result["front"]
        assert "detect_ai.vehicle" in result["front"]
        assert "detect_ai.dog_cat" in result["front"]


# ---------------------------------------------------------------------------
# Mock helper for apply_recipe / verify_recipe
# ---------------------------------------------------------------------------

def _make_mock_detection_config(
    stay_time: int = 0,
    sensitivity: int = 50,
) -> DetectionConfig:
    """Return a DetectionConfig with the given fields."""
    return DetectionConfig(
        enabled=True,
        sensitivity=sensitivity,
        person_stay_time=stay_time if stay_time != -1 else 0,
        raw={"sensitivity": sensitivity},
    )


# ---------------------------------------------------------------------------
# apply_recipe
# ---------------------------------------------------------------------------


class TestApplyRecipe:
    """apply_recipe calls detect_ai_apply with correct args and returns results."""

    @patch("infra.reolink.ReolinkClient")
    def test_apply_single_camera_match(self, mock_client_cls, tmp_path: Path):
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client
        mock_client.detect_ai_apply.return_value = None

        toml_text = textwrap.dedent("""
            [detect_ai.person.front]
            stay_time = 2
            sensitivity = 50
        """)
        recipe = tmp_path / "recipe.toml"
        recipe.write_text(toml_text)

        results = apply_recipe(str(recipe))

        assert len(results) == 1
        assert results[0].camera == "front"
        assert results[0].ok is True
        assert mock_client.detect_ai_apply.called
        # Check that args were passed correctly
        call_kw = mock_client.detect_ai_apply.call_args
        assert call_kw.kwargs.get("atype") == "person"
        assert call_kw.kwargs.get("camera") == "front"
        assert call_kw.kwargs.get("stay_time") == 2
        assert call_kw.kwargs.get("sensitivity") == 50

    @patch("infra.reolink.ReolinkClient")
    def test_apply_all_cameras(self, mock_client_cls, tmp_path: Path):
        """All 6 cameras in recipe -> 6 results."""
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client
        mock_client.detect_ai_apply.return_value = None

        toml_text = textwrap.dedent("""
            [detect_ai.person.front]
            stay_time = 2
            sensitivity = 50
            [detect_ai.person.back]
            stay_time = 2
            sensitivity = 50
            [detect_ai.person.outside_front_garage]
            stay_time = 2
            sensitivity = 50
            [detect_ai.person.outside_front_solar]
            stay_time = 2
            sensitivity = 50
            [detect_ai.person.outside_front_power]
            stay_time = 2
            sensitivity = 50
            [detect_ai.person.outside_back_solar]
            stay_time = 2
            sensitivity = 50
        """)
        recipe = tmp_path / "recipe.toml"
        recipe.write_text(toml_text)

        results = apply_recipe(str(recipe))
        assert len(results) == 6

    @patch("infra.reolink.ReolinkClient")
    def test_apply_camera_failure(self, mock_client_cls, tmp_path: Path):
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client
        mock_client.detect_ai_apply.side_effect = ReolinkCommandError(
            1, "", "", ["detect", "ai", "apply"]
        )

        toml_text = textwrap.dedent("""
            [detect_ai.person.front]
            stay_time = 2
            sensitivity = 50
        """)
        recipe = tmp_path / "recipe.toml"
        recipe.write_text(toml_text)

        results = apply_recipe(str(recipe))
        assert results[0].ok is False
        assert "apply failed" in results[0].diffs[0]

    @patch("infra.reolink.ReolinkClient")
    def test_apply_filter_camera(self, mock_client_cls, tmp_path: Path):
        """camera='front' skips back."""
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client
        mock_client.detect_ai_apply.return_value = None

        toml_text = textwrap.dedent("""
            [detect_ai.person.front]
            stay_time = 2
            sensitivity = 50
            [detect_ai.person.back]
            stay_time = 2
            sensitivity = 50
        """)
        recipe = tmp_path / "recipe.toml"
        recipe.write_text(toml_text)

        results = apply_recipe(str(recipe), camera="front")
        assert len(results) == 1
        assert results[0].camera == "front"

    @patch("infra.reolink.ReolinkClient")
    def test_apply_multiple_subcommands(self, mock_client_cls, tmp_path: Path):
        """Multiple subcommands per camera -> multiple detect_ai_apply calls."""
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client
        mock_client.detect_ai_apply.return_value = None

        toml_text = textwrap.dedent("""
            [detect_ai.person.front]
            stay_time = 2
            sensitivity = 50
            [detect_ai.vehicle.front]
            stay_time = 2
            sensitivity = 50
            [detect_ai.dog_cat.front]
            stay_time = 2
            sensitivity = 30
        """)
        recipe = tmp_path / "recipe.toml"
        recipe.write_text(toml_text)

        results = apply_recipe(str(recipe))
        assert len(results) == 1
        assert mock_client.detect_ai_apply.call_count == 3


# ---------------------------------------------------------------------------
# verify_recipe
# ---------------------------------------------------------------------------


class TestVerifyRecipe:
    """verify_recipe reads live state, diffs against recipe, returns match."""

    @patch("infra.reolink.ReolinkClient")
    def test_verify_match(self, mock_client_cls, tmp_path: Path):
        """Live state matches recipe -> match=True, no diffs."""
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client
        mock_client.detect_ai_get.return_value = DetectionConfig(
            enabled=True,
            sensitivity=50,
            person_stay_time=2,
            vehicle_stay_time=2,
            dog_cat_stay_time=2,
        )

        toml_text = textwrap.dedent("""
            [detect_ai.person.front]
            stay_time = 2
            sensitivity = 50
        """)
        recipe = tmp_path / "recipe.toml"
        recipe.write_text(toml_text)

        results = verify_recipe(str(recipe))

        assert len(results) == 1
        assert results[0].camera == "front"
        assert results[0].match is True
        assert results[0].ok is True
        assert results[0].diffs == []

    @patch("infra.reolink.ReolinkClient")
    def test_verify_mismatch(self, mock_client_cls, tmp_path: Path):
        """Live sensitivity=70, recipe=50 -> diff reported."""
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client
        mock_client.detect_ai_get.return_value = DetectionConfig(
            enabled=True,
            sensitivity=70,  # differs from recipe
            person_stay_time=2,
        )

        toml_text = textwrap.dedent("""
            [detect_ai.person.front]
            stay_time = 2
            sensitivity = 50
        """)
        recipe = tmp_path / "recipe.toml"
        recipe.write_text(toml_text)

        results = verify_recipe(str(recipe))

        assert results[0].match is False
        assert results[0].ok is False
        assert any("sensitivity" in d for d in results[0].diffs)

    @patch("infra.reolink.ReolinkClient")
    def test_verify_stay_time_mismatch(self, mock_client_cls, tmp_path: Path):
        """Stay_time differs from recipe."""
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client
        mock_client.detect_ai_get.return_value = DetectionConfig(
            enabled=True,
            sensitivity=50,
            person_stay_time=5,  # recipe says 2
        )

        toml_text = textwrap.dedent("""
            [detect_ai.person.front]
            stay_time = 2
            sensitivity = 50
        """)
        recipe = tmp_path / "recipe.toml"
        recipe.write_text(toml_text)

        results = verify_recipe(str(recipe))
        assert results[0].match is False
        assert any("stay_time" in d for d in results[0].diffs)

    @patch("infra.reolink.ReolinkClient")
    def test_verify_filter_camera(self, mock_client_cls, tmp_path: Path):
        """camera='back' only verifies back."""
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client
        mock_client.detect_ai_get.return_value = DetectionConfig(
            enabled=True,
            sensitivity=50,
            person_stay_time=2,
        )

        toml_text = textwrap.dedent("""
            [detect_ai.person.front]
            stay_time = 2
            sensitivity = 50
            [detect_ai.person.back]
            stay_time = 2
            sensitivity = 50
        """)
        recipe = tmp_path / "recipe.toml"
        recipe.write_text(toml_text)

        results = verify_recipe(str(recipe), camera="back")
        assert len(results) == 1
        assert results[0].camera == "back"

    @patch("infra.reolink.ReolinkClient")
    def test_verify_all_cameras(self, mock_client_cls, tmp_path: Path):
        """6 cameras in recipe -> 6 results."""
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client
        mock_client.detect_ai_get.return_value = DetectionConfig(
            enabled=True,
            sensitivity=50,
            person_stay_time=2,
            vehicle_stay_time=2,
            dog_cat_stay_time=2,
        )

        toml_text = textwrap.dedent("""
            [detect_ai.person.front]
            stay_time = 2
            sensitivity = 50
            [detect_ai.person.back]
            stay_time = 2
            sensitivity = 50
            [detect_ai.person.outside_front_garage]
            stay_time = 2
            sensitivity = 50
            [detect_ai.person.outside_front_solar]
            stay_time = 2
            sensitivity = 50
            [detect_ai.person.outside_front_power]
            stay_time = 2
            sensitivity = 50
            [detect_ai.person.outside_back_solar]
            stay_time = 2
            sensitivity = 50
        """)
        recipe = tmp_path / "recipe.toml"
        recipe.write_text(toml_text)

        results = verify_recipe(str(recipe))
        assert len(results) == 6
        assert all(r.match for r in results)

    @patch("infra.reolink.ReolinkClient")
    def test_verify_round_trip(self, mock_client_cls, tmp_path: Path):
        """apply then verify: live state changes to match recipe, verify returns match=True."""
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client

        # After apply, live state matches recipe — differentiate by atype
        def _detect_ai_get(atype=None, camera=None):
            configs = {
                "person": DetectionConfig(
                    enabled=True, sensitivity=50, person_stay_time=2),
                "vehicle": DetectionConfig(
                    enabled=True, sensitivity=50, vehicle_stay_time=2),
                "dog_cat": DetectionConfig(
                    enabled=True, sensitivity=30, dog_cat_stay_time=2),
            }
            return configs.get(atype or "person", DetectionConfig())

        mock_client.detect_ai_get.side_effect = _detect_ai_get

        toml_text = textwrap.dedent("""
            [detect_ai.person.front]
            stay_time = 2
            sensitivity = 50
            [detect_ai.vehicle.front]
            stay_time = 2
            sensitivity = 50
            [detect_ai.dog_cat.front]
            stay_time = 2
            sensitivity = 30
        """)
        recipe = tmp_path / "recipe.toml"
        recipe.write_text(toml_text)

        # Apply (succeeds)
        apply_results = apply_recipe(str(recipe))
        assert all(r.ok for r in apply_results)

        # Verify (mock returns matching state per-type)
        verify_results = verify_recipe(str(recipe))
        assert all(r.match for r in verify_results)
