"""known_vehicles — JSON store of enrollments.

Reads/writes known_vehicles.json. Owns the schema. No matcher logic
in here — just persistence.

Replaces `src/vehicle_state.py`'s known-vehicles loading.
"""

from .store import (
    KNOWN_VEHICLES_SCHEMA,
    KnownVehicleStore,
    load_known_vehicles,
)

__all__ = [
    "KNOWN_VEHICLES_SCHEMA",
    "KnownVehicleStore",
    "load_known_vehicles",
]
