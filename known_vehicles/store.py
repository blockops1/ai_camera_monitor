"""known_vehicles.store — JSON store of known-vehicle enrollments.

STATUS: stable (post-6B.83, 2026-08-16).
THREAD SAFETY: single-threaded (KnownVehicleStore is mutable; the
    persistence helpers load_known_vehicles is read-only and safe to
    call concurrently from any thread).

INPUTS:
    - file data/vehicles/known_vehicles.json (default, see
      _DEFAULT_PATH below). Schema versioned: KNOWN_VEHICLES_SCHEMA.
    - function arg path: optional override; default = _DEFAULT_PATH.

OUTPUTS:
    - load_known_vehicles(path=None) -> list[dict] (the .all() of the
      loaded store).
    - KnownVehicleStore.from_file / to_file / from_dict / to_dict: pure
      dict <-> store conversions.
    - KnownVehicleStore.add / remove / get_by_id: mutation helpers.
    - No network. No side effects beyond the file the caller passes.

PUBLIC API:
    KNOWN_VEHICLES_SCHEMA — int constant, current schema version
    KnownVehicleStore(vehicles) — wraps a list
    KnownVehicleStore.from_dict(data) — build from top-level dict
    KnownVehicleStore.from_file(path) — load from JSON file
    KnownVehicleStore.to_dict() — serialize
    KnownVehicleStore.to_file(path) — write JSON
    KnownVehicleStore.all() — return vehicles (copy)
    KnownVehicleStore.get_by_id(kv_id) — None if missing
    KnownVehicleStore.add(vehicle) — by id; replaces if exists
    KnownVehicleStore.remove(kv_id) — True if removed
    KnownVehicleStore.__len__ / __iter__
    load_known_vehicles(path=None) — list[dict], path defaults to
        _DEFAULT_PATH

DOES NOT DO:
    - Score vehicles — that's vehicle_matcher.scoring
    - Identify vehicles from crops — that's vehicle_identifier.identifier
    - Send Telegram — that's infra.send_telegram
    - Detect motion — that's infra.motion_detector
    - Persist anything beyond the JSON file the caller passes

CALLED BY:
    - listener.listener:load_known_vehicles() in the 2nd-Telegram
      match path (line 3411) and the cascade reclass path (line 1587)
    - Anything that needs the canonical 12-vehicle list defaults to
      _DEFAULT_PATH.

CALLS INTO:
    - stdlib json + pathlib only. No project imports; keeps this
      module self-contained per AGENTS.md Step 3 isolation rule.

RELATED:
    - infra.matcher_spec — the spec the matcher scores against
    - infra.matcher_scoring — the per-dimension scoring engine
    - vehicle_matcher.matcher — the orchestrator that wraps the scorer
    - data/vehicles/known_vehicles.json — the canonical default store

Phase 6B.83 (2026-08-16) — load_known_vehicles() now defaults to
    _DEFAULT_PATH instead of returning []. The previous contract left
    the listener's matcher running against an empty list silently;
    the no-match Telegram fired but with no candidates scored.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

# Schema version. Bump if format changes incompatibly.
KNOWN_VEHICLES_SCHEMA: int = 1

# Phase 6B.83 (2026-08-16): canonical default-path for the project.
# Resolves to <project_root>/data/vehicles/known_vehicles.json — the
# file copied from the old repo at refactor cutover time. Project
# root = parent of this file's parent (known_vehicles/store.py sits
# two levels under the project root). Exposed at module level so
# tests can monkeypatch it (see test_load_known_vehicles_*).
_DEFAULT_PATH = Path(__file__).resolve().parent.parent / "data" / "vehicles" / "known_vehicles.json"


class KnownVehicleStore:
    """In-memory known-vehicles store with file persistence.

    Wraps the loaded list + provides helpers for lookup and updates.
    Does not enforce schema; that's the caller's job (the matcher
    requires certain fields to score).
    """

    def __init__(self, vehicles: list[dict[str, Any]]) -> None:
        self._vehicles = list(vehicles)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> KnownVehicleStore:
        """Build from the top-level JSON dict."""
        version = data.get("version", KNOWN_VEHICLES_SCHEMA)
        if version != KNOWN_VEHICLES_SCHEMA:
            raise ValueError(
                f"unknown known_vehicles schema version: {version} "
                f"(expected {KNOWN_VEHICLES_SCHEMA})"
            )
        vehicles = data.get("vehicles", [])
        if not isinstance(vehicles, list):
            raise TypeError(
                f"vehicles must be a list, got {type(vehicles).__name__}"
            )
        return cls(vehicles)

    @classmethod
    def from_file(cls, path: str | Path) -> KnownVehicleStore:
        """Load from a JSON file."""
        p = Path(path)
        if not p.is_file():
            raise FileNotFoundError(f"known_vehicles.json not found: {p}")
        with p.open() as f:
            data = json.load(f)
        return cls.from_dict(data)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to the top-level JSON dict."""
        return {
            "version": KNOWN_VEHICLES_SCHEMA,
            "vehicles": list(self._vehicles),
        }

    def to_file(self, path: str | Path) -> None:
        """Write to a JSON file."""
        p = Path(path)
        with p.open("w") as f:
            json.dump(self.to_dict(), f, indent=2, sort_keys=True)

    def all(self) -> list[dict[str, Any]]:
        """Return all vehicles (read-only list)."""
        return list(self._vehicles)

    def get_by_id(self, kv_id: str) -> dict[str, Any] | None:
        """Look up a vehicle by id. Returns None if not found."""
        for v in self._vehicles:
            if v.get("id") == kv_id:
                return v
        return None

    def add(self, vehicle: dict[str, Any]) -> None:
        """Add a vehicle. If id matches an existing entry, replace it."""
        kv_id = vehicle.get("id")
        if not kv_id:
            raise ValueError("vehicle must have an 'id'")
        for i, existing in enumerate(self._vehicles):
            if existing.get("id") == kv_id:
                self._vehicles[i] = vehicle
                return
        self._vehicles.append(vehicle)

    def remove(self, kv_id: str) -> bool:
        """Remove a vehicle by id. Returns True if removed."""
        for i, v in enumerate(self._vehicles):
            if v.get("id") == kv_id:
                self._vehicles.pop(i)
                return True
        return False

    def __len__(self) -> int:
        return len(self._vehicles)

    def __iter__(self):
        return iter(self._vehicles)


def load_known_vehicles(
    path: str | Path | None = None,
) -> list[dict[str, Any]]:
    """Convenience: load and return the vehicles list.

    If path is None, loads from the project default (_DEFAULT_PATH =
    <project_root>/data/vehicles/known_vehicles.json). Raises
    FileNotFoundError if neither an explicit path nor the default
    file exists.

    Phase 6B.83 (2026-08-16) — previous contract was "path=None ->
    []", which silently left the listener's matcher running against
    zero known vehicles. Now it falls through to the canonical default
    so the match path is non-empty by default. Tests can monkeypatch
    _DEFAULT_PATH to point at tmp_path.
    """
    if path is None:
        path = _DEFAULT_PATH
    return KnownVehicleStore.from_file(path).all()
