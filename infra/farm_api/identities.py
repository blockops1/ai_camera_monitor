"""farm_api.identities — Known identities resource.

STATUS: alpha - V2-068b5, write path US-106a
THREAD SAFETY: read operations are read-only; safe for concurrent access.
    write operations (enroll_known_person) serialize via file I/O.

INPUTS:
    - data/known/people/known_people.json (JSON list)
    - data/vehicles/known_vehicles.json (JSON object with "entries" key)
    - data/animals/known_animals.json (JSON list)
    - data/known/people/embeddings.npz (per-image embeddings)

OUTPUTS:
    - PersonIdentity, VehicleIdentity, AnimalIdentity dataclasses
    - list_known_people() -> list[PersonIdentity]
    - list_known_vehicles() -> list[VehicleIdentity]
    - list_known_animals() -> list[AnimalIdentity]
    - enroll_known_person() -> dict (appends to registry)

DOES NOT DO:
    - Connect to external services (filesystem reads only)

CALLERS:
    - infra.mcp_server (MCP tool adapters: list_known_people,
      list_known_vehicles, list_known_animals, enroll_person)
    - infra.api_server (POST /api/v1/identities/people)
    - scripts/enroll_person.py (CLI, calls same shared function)
    - scripts/batch_enroll_recent.py (batch, calls same shared function)

USAGE:
    from infra.farm_api.identities import (
        list_known_people, enroll_known_person,
    )
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

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


# ---------------------------------------------------------------------------
# Write path: enroll_known_person
# ---------------------------------------------------------------------------

PEOPLE_DIR: Path = Path(PERSON_KNOWN_FILE).parent
EMBEDDINGS_NPZ: Path = PEOPLE_DIR / "embeddings.npz"


def _load_people_registry() -> list[dict[str, Any]]:
    """Load the known people JSON list; create file with [] if missing."""
    PEOPLE_DIR.mkdir(parents=True, exist_ok=True)
    if Path(PERSON_KNOWN_FILE).exists():
        with open(PERSON_KNOWN_FILE, "r") as f:
            data = json.load(f)
        assert isinstance(data, list), (
            f"{PERSON_KNOWN_FILE} must contain a JSON list, got {type(data).__name__}"
        )
        return data
    return []


def _save_people_registry(registry: list[dict[str, Any]]) -> None:
    """Persist the registry to known_people.json."""
    PEOPLE_DIR.mkdir(parents=True, exist_ok=True)
    with open(PERSON_KNOWN_FILE, "w") as f:
        json.dump(registry, f, indent=2, default=str)
        f.write("\n")


def _load_embeddings() -> dict[str, np.ndarray]:
    """Load existing embeddings.npz; return empty dict if missing."""
    if not EMBEDDINGS_NPZ.exists():
        return {}
    return dict(np.load(EMBEDDINGS_NPZ, allow_pickle=True))


def _save_embeddings(embeddings: dict[str, np.ndarray]) -> None:
    """Persist embeddings dict to .npz."""
    PEOPLE_DIR.mkdir(parents=True, exist_ok=True)
    if not embeddings:
        np.savez(str(EMBEDDINGS_NPZ))
        return
    np.savez(str(EMBEDDINGS_NPZ), **embeddings)  # type: ignore[arg-type]


def _embed_person_image(image_path: str, person_id: str) -> tuple[np.ndarray, str]:
    """Load an image, produce a 512-dim embedding, return (vector, key)."""
    from infra.person_embedder import embed_person_crop

    vec = embed_person_crop(image_path)
    assert vec.shape == (512,), (
        f"Expected embedding shape (512,), got {vec.shape}"
    )
    # Use the full resolved path as key to avoid filename collisions
    # when multiple crop_a.png / crop_b.png from different alerts are enrolled.
    resolved = Path(image_path).resolve()
    key = f"{person_id}__{resolved.parent.name}__{resolved.name}"
    return vec, key


def enroll_known_person(
    name: str,
    person_id: str,
    identity_markers: list[str] | None = None,
    image_paths: list[str] | None = None,
) -> dict[str, Any]:
    """Enroll a known person.

    This is the shared write-path used by the CLI, the REST API, and the MCP tool.

    Args:
        name: Display name / label for the person.
        person_id: Unique identifier string.
        identity_markers: List of identity marker tokens for matching.
        image_paths: List of local image file paths to embed.

    Returns:
        The registry entry dict that was appended.

    Raises:
        ValueError: if person_id is already enrolled or an image path is missing.
    """
    identity_markers = identity_markers or []
    image_paths = image_paths or []

    # 1. Load state
    registry = _load_people_registry()
    embeddings = _load_embeddings()

    # 2. Check for duplicate id
    existing_ids = {entry["id"] for entry in registry}
    if person_id in existing_ids:
        raise ValueError(
            f"person_id '{person_id}' already enrolled. "
            f"Existing entry: {next(e for e in registry if e['id'] == person_id)['label']}"
        )

    # 3. Embed images
    per_image_vectors: dict[str, np.ndarray] = {}
    for img_path in image_paths:
        if not os.path.isfile(img_path):
            raise ValueError(f"image path not found: {img_path}")
        vec, key = _embed_person_image(img_path, person_id)
        per_image_vectors[key] = vec

    # 4. Compute per-person average embedding
    avg_key = f"{person_id}__avg"
    if per_image_vectors:
        all_vecs = np.stack(list(per_image_vectors.values()))
        avg_vec = all_vecs.mean(axis=0).astype(np.float32)
        per_image_vectors[avg_key] = avg_vec
    else:
        avg_vec = None

    # 5. Merge into embeddings store
    embeddings.update(per_image_vectors)
    _save_embeddings(embeddings)

    # 6. Build registry entry
    entry = {
        "id": person_id,
        "label": name,
        "identity_markers": sorted(set(identity_markers)),
        "first_enrolled_iso": datetime.now(UTC).isoformat(),
        "sample_image_paths": [os.path.abspath(p) for p in image_paths],
    }
    registry.append(entry)
    _save_people_registry(registry)

    log.info("enroll_known_person: enrolled %s (id=%s, %d images)", name, person_id, len(image_paths))
    return entry
