"""Tests for infra.vehicle_prompt — §11.115.4 vehicle call-2 prompt consolidation."""
from __future__ import annotations

from infra import vehicle_prompt


class TestVehicleSchemaJson:
    def test_schema_is_nonempty_string(self) -> None:
        assert isinstance(vehicle_prompt.VEHICLE_SCHEMA_JSON, str)
        assert len(vehicle_prompt.VEHICLE_SCHEMA_JSON) > 50

    def test_schema_has_vehicles_array(self) -> None:
        """vehicles[] array is the top-level structure."""
        assert '"vehicles"' in vehicle_prompt.VEHICLE_SCHEMA_JSON

    def test_schema_has_make_field(self) -> None:
        assert '"make"' in vehicle_prompt.VEHICLE_SCHEMA_JSON

    def test_schema_has_color_field(self) -> None:
        assert '"color"' in vehicle_prompt.VEHICLE_SCHEMA_JSON

    def test_schema_has_primary_vehicle_index(self) -> None:
        """Mirrors primary_vehicle_index pattern from existing code."""
        assert '"primary_vehicle_index"' in vehicle_prompt.VEHICLE_SCHEMA_JSON

    def test_schema_does_not_have_face_fields(self) -> None:
        """Vehicles don't have face recognition routing."""
        assert "better_crop" not in vehicle_prompt.VEHICLE_SCHEMA_JSON
        assert "face_bbox" not in vehicle_prompt.VEHICLE_SCHEMA_JSON
        assert "face_visible" not in vehicle_prompt.VEHICLE_SCHEMA_JSON


class TestBuildVehiclePrompt:
    def test_returns_nonempty_string(self) -> None:
        out = vehicle_prompt.build_vehicle_prompt(
            camera_name="Driveway", captured_at="t"
        )
        assert isinstance(out, str)
        assert len(out) > 100

    def test_includes_camera_name(self) -> None:
        out = vehicle_prompt.build_vehicle_prompt(
            camera_name="Driveway East", captured_at="t"
        )
        assert "Driveway East" in out

    def test_includes_captured_at(self) -> None:
        out = vehicle_prompt.build_vehicle_prompt(
            camera_name="x", captured_at="2026-09-02T12:34:56"
        )
        assert "2026-09-02T12:34:56" in out

    def test_template_format_constant_matches_legacy(self) -> None:
        """VEHICLE_PROMPT_TEMPLATE_FORMAT is a re-export of the legacy template."""
        from infra.prompt_templates import VEHICLE_CROP_PROMPT_TEMPLATE

        assert vehicle_prompt.VEHICLE_PROMPT_TEMPLATE_FORMAT == VEHICLE_CROP_PROMPT_TEMPLATE
