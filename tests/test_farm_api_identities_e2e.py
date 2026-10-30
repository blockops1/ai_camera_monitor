"""test_farm_api_identities_e2e — E2E tests for farm_api identities endpoint.

E2E strategy:
    - Create a real data/ tree with known_people.json, known_vehicles.json,
      and known_animals.json in a temp directory.
    - Set FARMSURV_DATA_DIR so infra.paths constants resolve to the temp dir.
    - Reload affected modules to pick up the new paths.
    - Import the API module and exercise list_known_people(),
      list_known_vehicles(), list_known_animals() against real file I/O.

Bug classes covered:
    - Identity records not loaded from JSON files.
    - Photo count not computed correctly.
    - Make/model/color fields missing from VehicleIdentity.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Test data helpers
# ---------------------------------------------------------------------------

def _write_vehicles_json(path: Path) -> None:
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


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------

@pytest.fixture()
def _identities_e2e_env(tmp_path: Path):
    """Create a temp data directory with identity JSON files."""
    data_dir = tmp_path / "data"
    (data_dir / "known" / "people").mkdir(parents=True)
    (data_dir / "vehicles").mkdir(parents=True)
    (data_dir / "animals").mkdir(parents=True)

    _write_people_json(data_dir / "known" / "people" / "known_people.json")
    _write_vehicles_json(data_dir / "vehicles" / "known_vehicles.json")
    _write_animals_json(data_dir / "animals" / "known_animals.json")

    old_data = os.environ.get("FARMSURV_DATA_DIR")
    os.environ["FARMSURV_DATA_DIR"] = str(data_dir)

    for mod_name in list(sys.modules.keys()):
        if mod_name.startswith(("infra.paths", "infra.farm_api.identities")):
            del sys.modules[mod_name]

    yield tmp_path

    if old_data is not None:
        os.environ["FARMSURV_DATA_DIR"] = old_data
    else:
        os.environ.pop("FARMSURV_DATA_DIR", None)
    for mod_name in list(sys.modules.keys()):
        if mod_name.startswith(("infra.paths", "infra.farm_api.identities")):
            del sys.modules[mod_name]


# ---------------------------------------------------------------------------
# E2E tests
# ---------------------------------------------------------------------------

class TestListKnownPeopleE2E:
    """E2E: list_known_people() loads from real JSON file."""

    def test_returns_list_of_person_identity(self, _identities_e2e_env):
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


class TestListKnownVehiclesE2E:
    """E2E: list_known_vehicles() loads from real JSON file."""

    def test_returns_list_of_vehicle_identity(self, _identities_e2e_env):
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


class TestListKnownAnimalsE2E:
    """E2E: list_known_animals() loads from real JSON file."""

    def test_returns_list_of_animal_identity(self, _identities_e2e_env):
        from infra.farm_api.identities import AnimalIdentity, list_known_animals

        result = list_known_animals()
        assert isinstance(result, list)
        assert len(result) == 1
        assert isinstance(result[0], AnimalIdentity)

        animal = result[0]
        assert animal.species == "cow"
        assert animal.name == "Bessie"
        assert animal.enrolled_at == "2026-01-01T00:00:00+00:00"


class TestListKnownE2E:
    """E2E: All three identity types return consistent list types."""

    def test_all_return_lists(self, _identities_e2e_env):
        from infra.farm_api.identities import (
            AnimalIdentity,
            PersonIdentity,
            VehicleIdentity,
            list_known_animals,
            list_known_people,
            list_known_vehicles,
        )

        people = list_known_people()
        vehicles = list_known_vehicles()
        animals = list_known_animals()

        assert isinstance(people, list)
        assert isinstance(vehicles, list)
        assert isinstance(animals, list)
        assert all(isinstance(p, PersonIdentity) for p in people)
        assert all(isinstance(v, VehicleIdentity) for v in vehicles)
        assert all(isinstance(a, AnimalIdentity) for a in animals)
