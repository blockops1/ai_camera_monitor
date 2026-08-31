"""known_animals — JSON store of animal enrollments.

Reads/writes known_animals.json. Owns the schema. No matcher logic
in here — just persistence.

Mirrors the known_vehicles module pattern (Phase.83, 2026-08-16).
Adds the new dimensions (species, distinctive_features array,
face_details dict) that the wider-scope vision schema (§11.86.2)
introduced in Phase.165.

Replaces the gap left when Phase 6A retired the legacy animal
tracker (no replacement was needed for animals at the time; this
module ships the missing registry).
"""

from .store import (
    KNOWN_ANIMALS_SCHEMA,
    KnownAnimalStore,
    load_known_animals,
)

__all__ = [
    "KNOWN_ANIMALS_SCHEMA",
    "KnownAnimalStore",
    "load_known_animals",
]