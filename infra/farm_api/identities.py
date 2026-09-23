"""farm_api.identities — Known identities resource.

STATUS: alpha - V2-068b5
THREAD SAFETY: read-only; safe for concurrent access.

INPUTS:
    - data/known/people/known_people.json (JSON list)
    - data/vehicles/known_vehicles.json (JSON object with "entries" key)
    - data/animals/known_animals.json (JSON list)

OUTPUTS:
    - PersonIdentity, VehicleIdentity, AnimalIdentity dataclasses
    - list_known_people() -> list[PersonIdentity]
    - list_known_vehicles() -> list[VehicleIdentity]
    - list_known_animals() -> list[AnimalIdentity]

DOES NOT DO:
    - Modify identity data (write via enroll scripts)
    - Connect to external services (pure filesystem reads)

CALLERS:
    - infra.mcp_server (MCP tool adapters: list_known_people,
      list_known_vehicles, list_known_animals)
    - Any code that needs a read-only view of known identities

USAGE:
    from infra.farm_api.identities import (
        list_known_people, list_known_vehicles, list_known_animals,
    )
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from infra.paths import ANIMAL_KNOWN_FILE, PERSON_KNOWN_FILE, VEHICLE_KNOWN_FILE

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PersonIdentity:
    """A known person entry.

    Attributes:
        name: Display name / label for the person.
        enrolled_at: ISO-8601 timestamp of first enrollment.
        last_seen_at: ISO-8601 timestamp of last detection (None if never seen).
        photo_count: Number of enrolled sample photos.
    """

    name: str
    enrolled_at: str
    last_seen_at: str | None
    photo_count: int


@dataclass(frozen=True)
class VehicleIdentity:
    """A known vehicle entry.

    Attributes:
        label: Display name / label for the vehicle.
        make: Make (e.g. "Chevrolet").
        model: Model (e.g. "Silverado 1500").
        color: Color string (e.g. "white").
        owner: Owner identifier (e.g. "operator").
        enrolled_at: ISO-8601 timestamp of first enrollment.
    """

    label: str
    make: str
    model: str
    color: str
    owner: str
    enrolled_at: str


@dataclass(frozen=True)
class AnimalIdentity:
    """A known animal entry.

    Attributes:
        species: Species string (e.g. "cow", "horse").
        name: Display name / label for the animal.
        enrolled_at: ISO-8601 timestamp of first enrollment.
    """

    species: str
    name: str
    enrolled_at: str


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _read_json_file(path: Path) -> Any:
    """Read and parse a JSON file, returning empty container on failure.

    Args:
        path: Path to the JSON file.

    Returns:
        Parsed JSON (list or dict), or an empty list if the file does not
        exist or is malformed.
    """
    if not path.is_file():
        log.info("identities: %s does not exist", path)
        return []
    try:
        raw = path.read_text(encoding="utf-8")
        return json.loads(raw)
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("identities: failed to read %s: %s", path, exc)
        return []


def _now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string."""
    from datetime import UTC, datetime
    return datetime.now(UTC).isoformat()


def _extract_person(entry: dict[str, Any]) -> PersonIdentity:
    """Convert a raw known_people entry dict to PersonIdentity."""
    name = entry.get("label", entry.get("id", "unknown"))
    enrolled_at = entry.get("first_enrolled_iso", "")
    # last_seen_at is not stored in the registry; infer from ID if present
    last_seen_at = entry.get("last_seen_at", None)
    # photo_count from sample_image_paths
    sample_images = entry.get("sample_image_paths", [])
    photo_count = len(sample_images) if isinstance(sample_images, list) else 0

    return PersonIdentity(
        name=name,
        enrolled_at=enrolled_at,
        last_seen_at=last_seen_at,
        photo_count=photo_count,
    )


def _extract_vehicle(entry: dict[str, Any]) -> VehicleIdentity:
    """Convert a raw known_vehicles entry dict to VehicleIdentity."""
    label = entry.get("label", entry.get("id", "unknown"))
    make = entry.get("make", "")
    model = entry.get("model", "")
    color = entry.get("color", "")
    owner = entry.get("owner", "")
    enrolled_at = entry.get("enrolled_at", "")

    return VehicleIdentity(
        label=label,
        make=make,
        model=model,
        color=color,
        owner=owner,
        enrolled_at=enrolled_at,
    )


def _extract_animal(entry: dict[str, Any]) -> AnimalIdentity:
    """Convert a raw known_animals entry dict to AnimalIdentity."""
    species = entry.get("species", "")
    name = entry.get("label", entry.get("id", "unknown"))
    enrolled_at = entry.get("first_enrolled_iso", "")

    return AnimalIdentity(
        species=species,
        name=name,
        enrolled_at=enrolled_at,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def list_known_people() -> list[PersonIdentity]:
    """Return a list of known people.

    Reads from data/known/people/known_people.json.

    Returns:
        A list of PersonIdentity objects. Empty list if the file does not
        exist or contains no entries.
    """
    raw = _read_json_file(Path(PERSON_KNOWN_FILE))

    # known_people.json is a plain JSON list
    if not isinstance(raw, list):
        log.warning("identities: known_people.json expected a list, got %s",
                    type(raw).__name__)
        return []

    return [_extract_person(entry) for entry in raw]


def list_known_vehicles() -> list[VehicleIdentity]:
    """Return a list of known vehicles.

    Reads from data/vehicles/known_vehicles.json.

    Returns:
        A list of VehicleIdentity objects. Empty list if the file does not
        exist or contains no entries.
    """
    raw = _read_json_file(Path(VEHICLE_KNOWN_FILE))

    # known_vehicles.json is an object with an "entries" key
    if isinstance(raw, dict):
        entries = raw.get("entries", [])
        if not isinstance(entries, list):
            log.warning("identities: known_vehicles.json 'entries' expected a list")
            entries = []
    elif isinstance(raw, list):
        # Defensive: some deployments may use a plain list
        entries = raw
    else:
        log.warning("identities: known_vehicles.json unexpected format")
        entries = []

    return [_extract_vehicle(entry) for entry in entries]


def list_known_animals() -> list[AnimalIdentity]:
    """Return a list of known animals.

    Reads from data/animals/known_animals.json.

    Returns:
        A list of AnimalIdentity objects. Empty list if the file does not
        exist or contains no entries.
    """
    raw = _read_json_file(Path(ANIMAL_KNOWN_FILE))

    # known_animals.json is a plain JSON list
    if not isinstance(raw, list):
        log.warning("identities: known_animals.json expected a list, got %s",
                    type(raw).__name__)
        return []

    return [_extract_animal(entry) for entry in raw]
