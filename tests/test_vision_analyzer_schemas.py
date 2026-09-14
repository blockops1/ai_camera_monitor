"""
test_vision_analyzer_schemas.py — Tests for VM2 prompt schema correctness.

Tests that vehicle and animal SCHEMA_JSON include the better_crop field
(US-044a) with the correct enum, required status, and additionalProperties.
"""

from infra.animal_prompt import SCHEMA_JSON as ANIMAL_SCHEMA
from infra.person_prompt import SCHEMA_JSON as PERSON_SCHEMA
from infra.vehicle_prompt import SCHEMA_JSON as VEHICLE_SCHEMA


class TestVehicleBetterCrop:
    """US-044a: vehicle SCHEMA_JSON must have better_crop field."""

    def test_vehicle_better_crop_in_properties(self):
        """vehicle SCHEMA_JSON declares 'better_crop' in properties."""
        assert "better_crop" in VEHICLE_SCHEMA["properties"]

    def test_vehicle_better_crop_enum_values(self):
        """vehicle better_crop enum == ['crop_a', 'crop_b', 'neither']."""
        enum = VEHICLE_SCHEMA["properties"]["better_crop"]["enum"]
        assert enum == ["crop_a", "crop_b", "neither"]

    def test_vehicle_better_crop_is_required(self):
        """'better_crop' is listed in vehicle SCHEMA_JSON 'required'."""
        assert "better_crop" in VEHICLE_SCHEMA["required"]

    def test_vehicle_additionalProperties_false(self):
        """Vehicle schema still has additionalProperties=False."""
        assert VEHICLE_SCHEMA.get("additionalProperties") is False

    def test_vehicle_better_crop_is_string_type(self):
        """vehicle better_crop type is 'string'."""
        assert VEHICLE_SCHEMA["properties"]["better_crop"]["type"] == "string"


class TestAnimalBetterCrop:
    """US-044a: animal SCHEMA_JSON must have better_crop field."""

    def test_animal_better_crop_in_properties(self):
        """animal SCHEMA_JSON declares 'better_crop' in properties."""
        assert "better_crop" in ANIMAL_SCHEMA["properties"]

    def test_animal_better_crop_enum_values(self):
        """animal better_crop enum == ['crop_a', 'crop_b', 'neither']."""
        enum = ANIMAL_SCHEMA["properties"]["better_crop"]["enum"]
        assert enum == ["crop_a", "crop_b", "neither"]

    def test_animal_better_crop_is_required(self):
        """'better_crop' is listed in animal SCHEMA_JSON 'required'."""
        assert "better_crop" in ANIMAL_SCHEMA["required"]

    def test_animal_additionalProperties_false(self):
        """Animal schema still has additionalProperties=False."""
        assert ANIMAL_SCHEMA.get("additionalProperties") is False

    def test_animal_better_crop_is_string_type(self):
        """animal better_crop type is 'string'."""
        assert ANIMAL_SCHEMA["properties"]["better_crop"]["type"] == "string"


class TestPersonBetterCrop:
    """Baseline: person SCHEMA_JSON already had better_crop (no regression)."""

    def test_person_better_crop_in_properties(self):
        """person SCHEMA_JSON declares 'better_crop' in properties."""
        assert "better_crop" in PERSON_SCHEMA["properties"]

    def test_person_better_crop_enum_values(self):
        """person better_crop enum == ['crop_a', 'crop_b', 'neither']."""
        enum = PERSON_SCHEMA["properties"]["better_crop"]["enum"]
        assert enum == ["crop_a", "crop_b", "neither"]

    def test_person_better_crop_is_required(self):
        """'better_crop' is listed in person SCHEMA_JSON 'required'."""
        assert "better_crop" in PERSON_SCHEMA["required"]
