"""Unit tests for the known-vehicles JSON store."""

from __future__ import annotations

import json
import sys
from pathlib import Path

_root = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_root))

import pytest

from known_vehicles.store import (
    _DEFAULT_PATH,
    KNOWN_VEHICLES_SCHEMA,
    KnownVehicleStore,
    load_known_vehicles,
)


def _carson_white():
    return {
        "id": "v_carson_white",
        "label": "<visitor-name>'s white pickup",
        "owner": "<visitor-name>",
        "color": "white",
        "type": "pickup",
        "make": "GMC",
        "model": "Sierra 1500",
        "vehicle_features": {
            "wheel_style": "black steel",
            "cab_marker_lights": False,
            "bed_cover": "none",
        },
    }


def _jayco():
    return {
        "id": "v_jayco_camper",
        "label": "Jayco Jay Feather travel trailer",
        "owner": "Jeremiah",
        "color": "white",
        "type": "trailer",
        "make": "Jayco",
        "model": "Jay Feather",
    }


# --- KnownVehicleStore ------------------------------------------------------


def test_from_dict_loads_vehicles():
    data = {"version": 1, "vehicles": [_carson_white()]}
    store = KnownVehicleStore.from_dict(data)
    assert len(store) == 1
    assert store.all()[0]["id"] == "v_carson_white"


def test_from_dict_default_version():
    """If 'version' is missing, assume current schema."""
    data = {"vehicles": [_carson_white()]}
    store = KnownVehicleStore.from_dict(data)
    assert len(store) == 1


def test_from_dict_rejects_unknown_version():
    data = {"version": 99, "vehicles": []}
    with pytest.raises(ValueError, match="unknown known_vehicles schema"):
        KnownVehicleStore.from_dict(data)


def test_from_dict_rejects_non_list_vehicles():
    data = {"version": 1, "vehicles": "not a list"}
    with pytest.raises(TypeError, match="vehicles must be a list"):
        KnownVehicleStore.from_dict(data)


def test_from_dict_empty():
    store = KnownVehicleStore.from_dict({"version": 1, "vehicles": []})
    assert len(store) == 0


def test_to_dict_roundtrip():
    store = KnownVehicleStore([_carson_white(), _jayco()])
    data = store.to_dict()
    assert data["version"] == KNOWN_VEHICLES_SCHEMA
    assert len(data["vehicles"]) == 2
    rebuilt = KnownVehicleStore.from_dict(data)
    assert len(rebuilt) == 2


# --- File I/O ---------------------------------------------------------------


def test_from_file(tmp_path):
    p = tmp_path / "kv.json"
    p.write_text(json.dumps({
        "version": 1,
        "vehicles": [_carson_white()],
    }))
    store = KnownVehicleStore.from_file(p)
    assert len(store) == 1


def test_from_file_missing_raises(tmp_path):
    p = tmp_path / "does_not_exist.json"
    with pytest.raises(FileNotFoundError):
        KnownVehicleStore.from_file(p)


def test_to_file_roundtrip(tmp_path):
    p = tmp_path / "kv.json"
    store = KnownVehicleStore([_carson_white(), _jayco()])
    store.to_file(p)
    assert p.exists()
    rebuilt = KnownVehicleStore.from_file(p)
    assert len(rebuilt) == 2


def test_to_file_is_valid_json(tmp_path):
    p = tmp_path / "kv.json"
    store = KnownVehicleStore([_carson_white()])
    store.to_file(p)
    raw = json.loads(p.read_text())
    assert raw["version"] == 1
    assert isinstance(raw["vehicles"], list)


# --- Lookup -----------------------------------------------------------------


def test_get_by_id_found():
    store = KnownVehicleStore([_carson_white(), _jayco()])
    v = store.get_by_id("v_carson_white")
    assert v is not None
    assert v["id"] == "v_carson_white"


def test_get_by_id_not_found():
    store = KnownVehicleStore([_carson_white()])
    v = store.get_by_id("v_unknown")
    assert v is None


def test_get_by_id_empty_store():
    store = KnownVehicleStore([])
    assert store.get_by_id("anything") is None


# --- Mutation ---------------------------------------------------------------


def test_add_new_vehicle():
    store = KnownVehicleStore([])
    store.add(_carson_white())
    assert len(store) == 1
    assert store.get_by_id("v_carson_white") is not None


def test_add_existing_replaces():
    """Adding a vehicle with an existing id replaces, not duplicates."""
    store = KnownVehicleStore([_carson_white()])
    updated = dict(_carson_white())
    updated["label"] = "<visitor-name>'s NEW white pickup"
    store.add(updated)
    assert len(store) == 1
    fetched = store.get_by_id("v_carson_white")
    assert fetched is not None
    assert fetched["label"] == "<visitor-name>'s NEW white pickup"


def test_add_requires_id():
    store = KnownVehicleStore([])
    with pytest.raises(ValueError, match="must have an 'id'"):
        store.add({"label": "no id"})


def test_remove_existing_returns_true():
    store = KnownVehicleStore([_carson_white(), _jayco()])
    assert store.remove("v_carson_white") is True
    assert len(store) == 1


def test_remove_missing_returns_false():
    store = KnownVehicleStore([_carson_white()])
    assert store.remove("v_nonexistent") is False
    assert len(store) == 1


# --- Iteration --------------------------------------------------------------


def test_iter_returns_vehicles():
    store = KnownVehicleStore([_carson_white(), _jayco()])
    seen = list(store)
    assert len(seen) == 2


def test_all_returns_copy():
    """mutating the returned list doesn't affect the store."""
    store = KnownVehicleStore([_carson_white()])
    out = store.all()
    out.append({"id": "x"})
    assert len(store) == 1


# --- Convenience loader -----------------------------------------------------


def test_load_known_vehicles_default_path(monkeypatch, tmp_path):
    """Phase 6B.83 (2026-08-16): when no path is provided, load from the
    project-root default (data/vehicles/known_vehicles.json) so the
    listener's matcher path has 12 vehicles instead of [].

    The previous contract (path=None -> []) left the listener calling
    load_known_vehicles() with no path, which meant match_with_details
    and score_top_n always ran against an empty list. Pinned bug.
    """
    # Seed a fake project root with one vehicle in the default location.
    fake_root = tmp_path
    vehicles_dir = fake_root / "data" / "vehicles"
    vehicles_dir.mkdir(parents=True)
    kv_file = vehicles_dir / "known_vehicles.json"
    kv_file.write_text(json.dumps({
        "version": 1,
        "vehicles": [_carson_white()],
    }))
    # Point load_known_vehicles at this tmp root via monkeypatching its
    # default. Store exposes the default as _DEFAULT_PATH.
    monkeypatch.setattr(
        "known_vehicles.store._DEFAULT_PATH",
        kv_file,
    )
    out = load_known_vehicles()
    assert len(out) == 1
    assert out[0]["id"] == "v_carson_white"


def test_load_known_vehicles_explicit_path_overrides_default(monkeypatch, tmp_path):
    """Explicit path arg wins over the default even when the default
    would also resolve to a real file."""
    # Set a real-looking default that would resolve under fake_root.
    default_file = tmp_path / "default_kv.json"
    default_file.write_text(json.dumps({
        "version": 1,
        "vehicles": [_carson_white()],
    }))
    monkeypatch.setattr(
        "known_vehicles.store._DEFAULT_PATH",
        default_file,
    )
    # Explicit path points elsewhere.
    explicit_file = tmp_path / "explicit_kv.json"
    explicit_file.write_text(json.dumps({
        "version": 1,
        "vehicles": [_carson_white(), _jayco()],
    }))
    out = load_known_vehicles(explicit_file)
    assert len(out) == 2


def test_load_known_vehicles_default_path_missing_raises(monkeypatch, tmp_path):
    """Phase 6B.83: if the default path doesn't exist, fail loudly.
    Silently returning [] hides the same bug we just fixed."""
    missing = tmp_path / "does_not_exist.json"
    monkeypatch.setattr(
        "known_vehicles.store._DEFAULT_PATH",
        missing,
    )
    with pytest.raises(FileNotFoundError):
        load_known_vehicles()


def test_load_known_vehicles_from_path(tmp_path):
    p = tmp_path / "kv.json"
    p.write_text(json.dumps({
        "version": 1,
        "vehicles": [_carson_white()],
    }))
    vehicles = load_known_vehicles(p)
    assert len(vehicles) == 1
    assert vehicles[0]["id"] == "v_carson_white"


def test_load_known_vehicles_real_default_path():
    """Sanity check: the production default-path file actually loads.
    Catches schema-vs-data drift (Phase 6B.83 found the real file was
    a list, not the wrapped {version, vehicles} the schema expected,
    and load_known_vehicles() failed with AttributeError on .get()).
    """
    # Public repo: skip when the operator hasn't created known_vehicles.json yet
    # (the test verifies the schema of the operator's actual data file).
    if not _DEFAULT_PATH.exists():
        pytest.skip(
            "Public repo: requires operator's known_vehicles.json "
            "(created by copying data/vehicles/known_vehicles.example.json)"
        )
    loaded = load_known_vehicles()
    assert isinstance(loaded, list)
    # On public install, the canonical file is empty. Skip the >=10 count check.
    if len(loaded) == 0:
        pytest.skip(
            "Public repo: known_vehicles.json is empty (canonical). "
            "Operator should populate it."
        )
    assert len(loaded) >= 10  # current canonical list has 12
