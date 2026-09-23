"""Tests for infra.farm_api.identities.

STATUS: V2-068b5
RUN WITH: pytest tests/test_farm_api_identities.py -v

Test strategy:
    - Create temporary JSON files under a temp data directory.
    - Set FARMSURV_DATA_DIR so infra.paths constants resolve to the temp dir.
    - Reload infra.paths and infra.farm_api.identities to pick up the new paths.
"""

import json
import os
import sys
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Test data helpers
# ---------------------------------------------------------------------------


def _write_vehicles_json(path: Path) -> None:
    """Write a known_vehicles.json with entries at the given path."""
    data = {
        "_anonymization_note": "test data",
        "entries": [
            {
                "id": "v1",
                "label": "Test Vehicle",
                "make": "Ford",
                "model": "F-150",
                "color": "blue",
                "owner": "operator",
                "enrolled_at": "2026-01-01T00:00:00+00:00",
            },
        ],
    }
    path.write_text(json.dumps(data, indent=2))


def _write_people_json(path: Path) -> None:
    """Write a known_people.json with entries at the given path."""
    data = [
        {
            "id": "p1",
            "label": "Test Person",
            "identity_markers": ["red-jacket"],
            "first_enrolled_iso": "2026-01-01T00:00:00+00:00",
            "last_seen_at": "2026-09-20T12:00:00+00:00",
            "sample_image_paths": ["/tmp/photo1.jpg", "/tmp/photo2.jpg"],
        },
    ]
    path.write_text(json.dumps(data, indent=2))


def _write_animals_json(path: Path) -> None:
    """Write a known_animals.json with entries at the given path."""
    data = [
        {
            "id": "a1",
            "label": "Bessie",
            "species": "cow",
            "breed": "Holstein",
            "first_enrolled_iso": "2026-01-01T00:00:00+00:00",
            "sample_image_paths": ["/tmp/bessie1.jpg"],
        },
    ]
    path.write_text(json.dumps(data, indent=2))


@pytest.fixture()
def _test_data_dir(tmp_path: Path):
    """Create a temporary data directory with known identity files.

    Sets FARMSURV_DATA_DIR and reloads affected modules so the API reads
    from the temp directory instead of the real one.
    """
    data_dir = tmp_path / "data"

    # Create directories
    (data_dir / "known" / "people").mkdir(parents=True)
    (data_dir / "vehicles").mkdir(parents=True)
    (data_dir / "animals").mkdir(parents=True)

    # Write test data
    _write_people_json(data_dir / "known" / "people" / "known_people.json")
    _write_vehicles_json(data_dir / "vehicles" / "known_vehicles.json")
    _write_animals_json(data_dir / "animals" / "known_animals.json")

    # Save old env
    old_data = os.environ.get("FARMSURV_DATA_DIR")

    # Set FARMSURV_DATA_DIR
    os.environ["FARMSURV_DATA_DIR"] = str(data_dir)

    # Clear cached modules
    for mod_name in list(sys.modules.keys()):
        if mod_name.startswith(("infra.paths", "infra.farm_api.identities")):
            del sys.modules[mod_name]

    yield tmp_path

    # Restore
    if old_data is not None:
        os.environ["FARMSURV_DATA_DIR"] = old_data
    else:
        os.environ.pop("FARMSURV_DATA_DIR", None)

    # Re-clear
    for mod_name in list(sys.modules.keys()):
        if mod_name.startswith(("infra.paths", "infra.farm_api.identities")):
            del sys.modules[mod_name]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestListKnownPeopleReturnsList:
    """test_list_known_people_returns_list: list_known_people() returns a list.

    Verifies:
        - Return type is list
        - Contains PersonIdentity instances
        - Fields are populated correctly from the JSON data
    """

    def test_list_known_people_returns_list(self, _test_data_dir):
        from infra.farm_api.identities import PersonIdentity, list_known_people

        result = list_known_people()

        assert isinstance(result, list)
        assert len(result) == 1
        assert isinstance(result[0], PersonIdentity)

        person = result[0]
        assert person.name == "Test Person"
        assert person.enrolled_at == "2026-01-01T00:00:00+00:00"
        assert person.last_seen_at == "2026-09-20T12:00:00+00:00"
        assert person.photo_count == 2


class TestListKnownVehiclesReturnsList:
    """test_list_known_vehicles_returns_list: list_known_vehicles() returns a list.

    Verifies:
        - Return type is list
        - Contains VehicleIdentity instances
        - Fields are populated correctly from the JSON data
    """

    def test_list_known_vehicles_returns_list(self, _test_data_dir):
        from infra.farm_api.identities import VehicleIdentity, list_known_vehicles

        result = list_known_vehicles()

        assert isinstance(result, list)
        assert len(result) == 1
        assert isinstance(result[0], VehicleIdentity)

        vehicle = result[0]
        assert vehicle.label == "Test Vehicle"
        assert vehicle.make == "Ford"
        assert vehicle.model == "F-150"
        assert vehicle.color == "blue"
        assert vehicle.owner == "operator"
        assert vehicle.enrolled_at == "2026-01-01T00:00:00+00:00"


class TestListKnownAnimalsReturnsList:
    """test_list_known_animals_returns_list: list_known_animals() returns a list.

    Verifies:
        - Return type is list
        - Contains AnimalIdentity instances
        - Fields are populated correctly from the JSON data
    """

    def test_list_known_animals_returns_list(self, _test_data_dir):
        from infra.farm_api.identities import AnimalIdentity, list_known_animals

        result = list_known_animals()

        assert isinstance(result, list)
        assert len(result) == 1
        assert isinstance(result[0], AnimalIdentity)

        animal = result[0]
        assert animal.species == "cow"
        assert animal.name == "Bessie"
        assert animal.enrolled_at == "2026-01-01T00:00:00+00:00"
