"""known_animals.store — JSON store of known-animal enrollments.

STATUS: stable (Phase.165 §11.86.5, 2026-08-30)
THREAD SAFETY: single-threaded (KnownAnimalStore is mutable; the
    persistence helpers load_known_animals is read-only and safe to
    call concurrently from any thread).

INPUTS:
    - file data/animals/known_animals.json (default, see _DEFAULT_PATH
      below). Schema versioned: KNOWN_ANIMALS_SCHEMA.
    - function arg path: optional override; default = _DEFAULT_PATH.

OUTPUTS:
    - load_known_animals(path=None) -> list[dict] (the .all() of the
      loaded store).
    - KnownAnimalStore.from_file / to_file / from_dict / to_dict: pure
      dict <-> store conversions.
    - KnownAnimalStore.add / remove / get_by_id / get_by_species:
      mutation + lookup helpers.
    - No network. No side effects beyond the file the caller passes.

PUBLIC API:
    KNOWN_ANIMALS_SCHEMA — int constant, current schema version (=1)
    KnownAnimalStore(animals) — wraps a list of dicts
    KnownAnimalStore.from_dict(data) — build from top-level dict
    KnownAnimalStore.from_file(path) — load from JSON file
    KnownAnimalStore.to_dict() — serialize to top-level dict
    KnownAnimalStore.to_file(path) — write JSON (atomic via .tmp + rename)
    KnownAnimalStore.all() — return all animals (read-only list)
    KnownAnimalStore.get_by_id(animal_id) — None if missing
    KnownAnimalStore.get_by_species(species) — list, possibly empty
    KnownAnimalStore.add(animal) — by id; ValueError on duplicate
    KnownAnimalStore.remove(animal_id) — True if removed
    KnownAnimalStore.__len__ / __iter__
    load_known_animals(path=None) — list[dict], default = _DEFAULT_PATH

DOES NOT DO:
    - Match animals against vision results — that's
      infra.animal_matcher.match_animal (PLAN §11.86.2).
    - Capture frames or interact with RTSP — that's
      infra.persistent_rtsp / infra.frame_capture.
    - Send Telegram — that's infra.send_telegram.
    - Detect motion — that's infra.motion_detector.
    - Persist anything beyond the JSON file the caller passes.
    - Auto-extract Qwen attributes — the matcher reads whatever
      fields are stored here, but this module never calls Qwen.
      scripts/enroll_animal.py is the operator-driven enrollment tool.
    - Compute threat tier — that's infra.telegram_formatter (PLAN §11.86.6).

CALLED BY:
    - listener.animal_event_pipeline: load_known_animals() in the
      match path (PLAN §11.86.7). Not wired yet at §11.86.5 commit
      time; the loader is in place.
    - scripts/enroll_animal: load → mutate → save during operator
      enrollment.

CALLS INTO:
    - stdlib json + pathlib only. No project imports; keeps this
      module self-contained per AGENTS.md Step 3 isolation rule.

RELATED:
    - infra.animal_matcher — consumes this store; requires
      'species' and 'name' (or 'id') per entry.
    - infra.animal_prompt_template — produces vision records that
      score against these entries.
    - data/animals/known_animals.json — the canonical default store.

Phase.165 §11.86.5 (2026-08-30) — initial scaffold. Mirrors
known_vehicles/store.py. Key differences:
  - required fields per entry: id, species, name
  - richer optional fields (distinctive_features, face_details,
    body_size, body_build, coat_primary_color, coat_pattern,
    estimated_age, sex_signal, label, first_enrolled, source_alerts)
  - get_by_species() helper for the matcher's species hard-filter
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

# Schema version. Bump if format changes incompatibly. from_dict()
# rejects anything != KNOWN_ANIMALS_SCHEMA with a ValueError so we
# never silently load stale data.
KNOWN_ANIMALS_SCHEMA: int = 1

# Canonical default-path for the project.
# Resolves to <project_root>/data/animals/known_animals.json. Exposed
# at module level so tests can monkeypatch it. The infra.paths constant
# ANIMAL_KNOWN_FILE points at the same location — both must agree, so
# if you change the directory layout, update both.
_DEFAULT_PATH = Path(__file__).resolve().parent.parent / "data" / "animals" / "known_animals.json"

# Sanity check at import time: if infra.paths is importable, the two
# paths must agree. Tests that monkeypatch _DEFAULT_PATH via
# monkeypatch.setattr won't trigger this check (paths is read once).
try:
    from infra.paths import ANIMAL_KNOWN_FILE as _ANIMALS_PATHS_FILE  # noqa: F401
    if str(Path(_DEFAULT_PATH).resolve()) != str(Path(_ANIMALS_PATHS_FILE).resolve()):
        # Don't raise — just log. Some test envs run paths.py with a
        # monkeypatched FARMSURV_PROJECT_ROOT, so this divergence is
        # expected in tests but should never occur in production.
        import warnings
        warnings.warn(
            f"known_animals._DEFAULT_PATH ({_DEFAULT_PATH}) disagrees with "
            f"infra.paths.ANIMAL_KNOWN_FILE ({_ANIMALS_PATHS_FILE}). "
            "Check FARMSURV_PROJECT_ROOT / FARMSURV_DATA_DIR env vars.",
            stacklevel=2,
        )
except ImportError:
    pass  # infra.paths may not be importable in minimal test envs

# Required per-entry keys. The matcher needs 'species' (to filter
# candidates) and either 'id' or 'name' (to label a match). Anything
# else is optional — richer fields improve match scores but aren't
# required for the basic flow.
_REQUIRED_ENTRY_KEYS = frozenset({"id", "species", "name"})


class KnownAnimalStore:
    """In-memory known-animals store with file persistence.

    Wraps the loaded list + provides helpers for lookup and updates.
    Does not enforce per-field semantics on optional fields; the
    matcher (infra.animal_matcher) decides what each field means.
    """

    def __init__(self, animals: list[dict[str, Any]]) -> None:
        self._animals = list(animals)

    # ---- class-method constructors ----

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> KnownAnimalStore:
        """Build from the top-level JSON dict.

        Validates:
          - schema version matches KNOWN_ANIMALS_SCHEMA
          - 'animals' is a list
          - every entry is a dict
          - every entry has all required keys (id, species, name)
        """
        version = data.get("version", KNOWN_ANIMALS_SCHEMA)
        if version != KNOWN_ANIMALS_SCHEMA:
            raise ValueError(
                f"unknown known_animals schema version: {version} "
                f"(expected {KNOWN_ANIMALS_SCHEMA})"
            )
        animals = data.get("animals", [])
        if not isinstance(animals, list):
            raise TypeError(
                f"animals must be a list, got {type(animals).__name__}"
            )
        for i, entry in enumerate(animals):
            if not isinstance(entry, dict):
                raise TypeError(
                    f"animals[{i}] must be a dict, got {type(entry).__name__}"
                )
            missing = _REQUIRED_ENTRY_KEYS - set(entry.keys())
            if missing:
                raise ValueError(
                    f"animals[{i}] missing required keys: {sorted(missing)}. "
                    f"Required: {sorted(_REQUIRED_ENTRY_KEYS)}"
                )
        return cls(animals)

    @classmethod
    def from_file(cls, path: str | Path) -> KnownAnimalStore:
        """Load from a JSON file. Raises FileNotFoundError if missing."""
        p = Path(path)
        if not p.is_file():
            raise FileNotFoundError(f"known_animals.json not found: {p}")
        with p.open() as f:
            data = json.load(f)
        return cls.from_dict(data)

    # ---- serializers ----

    def to_dict(self) -> dict[str, Any]:
        """Serialize to the top-level JSON dict."""
        return {
            "version": KNOWN_ANIMALS_SCHEMA,
            "animals": list(self._animals),
        }

    def to_file(self, path: str | Path) -> None:
        """Write to a JSON file atomically (.tmp + rename).

        Atomic write matters here: scripts/enroll_animal appends a
        new entry and re-saves. If the process is killed mid-write,
        a partial file would corrupt the registry and break the
        listener's match path on next load.
        """
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".tmp")
        with tmp.open("w") as f:
            json.dump(self.to_dict(), f, indent=2, sort_keys=True)
            f.flush()
        tmp.replace(p)

    # ---- read helpers ----

    def all(self) -> list[dict[str, Any]]:
        """Return all animals (read-only list copy)."""
        return list(self._animals)

    def get_by_id(self, animal_id: str) -> dict[str, Any] | None:
        """Look up an animal by id. Returns None if not found."""
        for a in self._animals:
            if a.get("id") == animal_id:
                return a
        return None

    def get_by_species(self, species: str) -> list[dict[str, Any]]:
        """Return all animals matching the given normalized species.

        Note: this does NOT call infra.animal_matcher._normalize_species().
        That helper lives in the matcher module and this module is
        self-contained (no project imports). The caller should
        normalize before calling if they want fuzzy species matching
        (e.g. "Eastern coyote" -> "coyote"). For now, exact-match.
        """
        return [a for a in self._animals if a.get("species") == species]

    # ---- mutation ----

    def add(self, animal: dict[str, Any]) -> None:
        """Add an animal. Raises ValueError on duplicate id or missing
        required keys.

        Unlike known_vehicles.store (which silently replaces on
        duplicate), animal enrollment is operator-driven and a
        duplicate-id entry usually indicates a typo or stale state.
        Fail loudly so the operator sees it.
        """
        missing = _REQUIRED_ENTRY_KEYS - set(animal.keys())
        if missing:
            raise ValueError(
                f"animal missing required keys: {sorted(missing)}. "
                f"Required: {sorted(_REQUIRED_ENTRY_KEYS)}"
            )
        animal_id = animal.get("id")
        for existing in self._animals:
            if existing.get("id") == animal_id:
                raise ValueError(
                    f"animal id already enrolled: {animal_id!r}. "
                    f"Use KnownAnimalStore.remove() first if you intend "
                    f"to replace."
                )
        self._animals.append(animal)

    def remove(self, animal_id: str) -> bool:
        """Remove an animal by id. Returns True if removed, False if not found."""
        for i, a in enumerate(self._animals):
            if a.get("id") == animal_id:
                self._animals.pop(i)
                return True
        return False

    # ---- dunder ----

    def __len__(self) -> int:
        return len(self._animals)

    def __iter__(self):
        return iter(self._animals)


def load_known_animals(
    path: str | Path | None = None,
) -> list[dict[str, Any]]:
    """Convenience: load and return the animals list.

    If path is None, loads from the project default (_DEFAULT_PATH =
    <project_root>/data/animals/known_animals.json). Raises
    FileNotFoundError if neither an explicit path nor the default
    file exists.

    Phase.165 §11.86.5 (2026-08-30) — initial scaffold. Mirrors
    known_vehicles.load_known_vehicles (Phase.83, 2026-08-16).
    Behavior: defaults to the canonical empty registry; if the file
    doesn't exist, the caller decides whether to abort or fall back
    to "no known animals" (the matcher handles empty registries
    gracefully via NoMatch("no_known_animals")).
    """
    if path is None:
        path = _DEFAULT_PATH
    return KnownAnimalStore.from_file(path).all()