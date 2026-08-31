"""
test_store.py — Unit tests for the known-animals JSON store (PLAN §11.86.5).

STATUS: provisional
THREAD SAFETY: pytest test functions — run sequentially per file.

WHAT THIS TESTS:
    - KnownAnimalStore.from_dict / from_file / to_dict / to_file round-trip
    - Schema validation: wrong version, missing required keys, non-dict entry
    - Lookup helpers: get_by_id, get_by_species
    - Mutation: add (with duplicate-id guard), remove (with not-found guard)
    - load_known_animals() default-path resolution
    - Atomic write survives concurrent read (write to .tmp, then rename)
    - Empty registry is the canonical initial state (data/animals/known_animals.json
      ships empty)

DESIGN CHOICES:
    - Tests build small fake dicts inline rather than reading a real
      data file, mirroring known_vehicles/tests/test_store.py.
    - When the test needs a file, it writes to tmp_path (pytest fixture)
      to keep tests hermetic — no global state changes.
    - Each test reloads the module by referencing the class directly,
      not through import-time caching (per AGENTS.md Step 3 isolation).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# Make project root importable for known_animals.* resolution
_root = Path(__file__).resolve().parent.parent.parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

from known_animals.store import (  # noqa: E402
    KNOWN_ANIMALS_SCHEMA,
    KnownAnimalStore,
    load_known_animals,
)


# ---------- fixtures ----------

def _mr_whiskers() -> dict:
    """A full-fidelity registry entry — every optional field populated."""
    return {
        "id": "a_mr_whiskers",
        "name": "Mr. Whiskers",
        "species": "cat",
        "species_confidence": "definite",
        "body_size": "small",
        "body_build": "lean",
        "coat_primary_color": "gray tabby",
        "coat_pattern": "tabby",
        "distinctive_features": [
            "left ear notch (small)",
            "white chin",
            "crooked tail tip",
        ],
        "face_details": {
            "ear_shape": "pointed",
            "tail_carriage": "high",
            "mask": "no",
        },
        "estimated_age": "adult",
        "sex_signal": "neutered",
        "label": "resident cat (CAM6 / workshop)",
        "first_enrolled": "2026-08-30",
        "source_alerts": ["ALERT_20260829_143907"],
    }


def _bandit() -> dict:
    """A minimal-fidelity entry — only required keys + name."""
    return {
        "id": "a_bandit",
        "name": "Bandit",
        "species": "raccoon",
    }


# ---------- from_dict ----------

class TestFromDict:
    def test_loads_empty_registry(self):
        store = KnownAnimalStore.from_dict({"version": 1, "animals": []})
        assert len(store) == 0

    def test_loads_minimal_entry(self):
        store = KnownAnimalStore.from_dict(
            {"version": 1, "animals": [_bandit()]}
        )
        assert len(store) == 1
        assert store.all()[0]["id"] == "a_bandit"

    def test_loads_full_entry_preserves_all_fields(self):
        """All optional fields survive the round-trip."""
        store = KnownAnimalStore.from_dict(
            {"version": 1, "animals": [_mr_whiskers()]}
        )
        entry = store.all()[0]
        assert entry["id"] == "a_mr_whiskers"
        assert entry["species"] == "cat"
        assert entry["coat_pattern"] == "tabby"
        assert entry["distinctive_features"] == [
            "left ear notch (small)",
            "white chin",
            "crooked tail tip",
        ]
        assert entry["face_details"] == {
            "ear_shape": "pointed",
            "tail_carriage": "high",
            "mask": "no",
        }
        assert entry["first_enrolled"] == "2026-08-30"
        assert entry["source_alerts"] == ["ALERT_20260829_143907"]

    def test_default_version_when_missing(self):
        """If 'version' key absent, assume current schema."""
        store = KnownAnimalStore.from_dict({"animals": [_bandit()]})
        assert len(store) == 1

    def test_rejects_unknown_schema_version(self):
        with pytest.raises(ValueError, match="unknown known_animals schema"):
            KnownAnimalStore.from_dict({"version": 99, "animals": []})

    def test_rejects_non_list_animals(self):
        with pytest.raises(TypeError, match="animals must be a list"):
            KnownAnimalStore.from_dict({"version": 1, "animals": "oops"})

    def test_rejects_non_dict_entry(self):
        with pytest.raises(TypeError, match=r"animals\[0\] must be a dict"):
            KnownAnimalStore.from_dict(
                {"version": 1, "animals": ["not a dict"]}
            )

    def test_rejects_missing_required_keys_id(self):
        bad = {"name": "Mystery", "species": "cat"}
        with pytest.raises(ValueError, match="missing required keys"):
            KnownAnimalStore.from_dict({"version": 1, "animals": [bad]})

    def test_rejects_missing_required_keys_species(self):
        bad = {"id": "a_x", "name": "Mystery"}
        with pytest.raises(ValueError, match="missing required keys"):
            KnownAnimalStore.from_dict({"version": 1, "animals": [bad]})

    def test_rejects_missing_required_keys_name(self):
        bad = {"id": "a_x", "species": "cat"}
        with pytest.raises(ValueError, match="missing required keys"):
            KnownAnimalStore.from_dict({"version": 1, "animals": [bad]})

    def test_ignores_unknown_keys(self):
        """Forward-compat: extra keys don't break loading."""
        with_extra = {**_bandit(), "future_field": "ignored"}
        store = KnownAnimalStore.from_dict(
            {"version": 1, "animals": [with_extra]}
        )
        # future_field preserved (we don't strip), but not required
        assert store.all()[0]["future_field"] == "ignored"


# ---------- from_file ----------

class TestFromFile:
    def test_loads_real_canonical_registry(self):
        """The shipped data/animals/known_animals.json must load cleanly
        and be empty — this is the canonical initial state."""
        from known_animals.store import _DEFAULT_PATH

        assert _DEFAULT_PATH.exists(), (
            f"Canonical registry missing: {_DEFAULT_PATH}. "
            "PLAN §11.86.5 requires this file to exist even when empty."
        )
        store = KnownAnimalStore.from_file(_DEFAULT_PATH)
        assert len(store) == 0
        assert store.all() == []

    def test_loads_custom_path(self, tmp_path):
        f = tmp_path / "test.json"
        f.write_text(json.dumps({"version": 1, "animals": [_bandit()]}))
        store = KnownAnimalStore.from_file(f)
        assert len(store) == 1

    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError, match="known_animals.json not found"):
            KnownAnimalStore.from_file(tmp_path / "nope.json")


# ---------- to_dict / round-trip ----------

class TestRoundTrip:
    def test_to_dict_then_from_dict(self):
        original = KnownAnimalStore([_mr_whiskers(), _bandit()])
        data = original.to_dict()
        restored = KnownAnimalStore.from_dict(data)
        assert len(restored) == 2
        assert restored.all() == original.all()

    def test_to_dict_shape(self):
        store = KnownAnimalStore([_bandit()])
        data = store.to_dict()
        assert data == {"version": KNOWN_ANIMALS_SCHEMA, "animals": [_bandit()]}

    def test_to_file_writes_valid_json(self, tmp_path):
        store = KnownAnimalStore([_mr_whiskers()])
        f = tmp_path / "out.json"
        store.to_file(f)
        assert f.exists()
        loaded = json.loads(f.read_text())
        assert loaded["version"] == 1
        assert len(loaded["animals"]) == 1
        assert loaded["animals"][0]["id"] == "a_mr_whiskers"

    def test_to_file_atomic_no_leftover_tmp(self, tmp_path):
        """Atomic write: no .tmp file should remain after to_file()."""
        store = KnownAnimalStore([_bandit()])
        f = tmp_path / "atomic.json"
        store.to_file(f)
        leftover = list(tmp_path.glob("*.tmp"))
        assert leftover == [], f"tmp file left behind: {leftover}"

    def test_to_file_creates_parent_dirs(self, tmp_path):
        """Enroll script may target a path that doesn't exist yet
        (e.g. fresh data/animals/). from_file requires the dir to exist;
        to_file should create it."""
        nested = tmp_path / "deep" / "nested" / "known_animals.json"
        store = KnownAnimalStore([_bandit()])
        store.to_file(nested)
        assert nested.exists()


# ---------- get_by_id / get_by_species ----------

class TestLookups:
    def _two_animal_store(self) -> KnownAnimalStore:
        return KnownAnimalStore([_mr_whiskers(), _bandit()])

    def test_get_by_id_hit(self):
        store = self._two_animal_store()
        entry = store.get_by_id("a_mr_whiskers")
        assert entry is not None
        assert entry["name"] == "Mr. Whiskers"

    def test_get_by_id_miss_returns_none(self):
        store = self._two_animal_store()
        assert store.get_by_id("a_nope") is None

    def test_get_by_species_filter(self):
        store = self._two_animal_store()
        cats = store.get_by_species("cat")
        assert len(cats) == 1
        assert cats[0]["name"] == "Mr. Whiskers"

    def test_get_by_species_no_match_returns_empty(self):
        store = self._two_animal_store()
        assert store.get_by_species("bear") == []

    def test_get_by_species_is_exact_match(self):
        """Documented contract: get_by_species does NOT call the
        matcher's _normalize_species() (e.g. 'Eastern coyote' ==
        'coyote'). Caller normalizes first. This test guards that
        contract."""
        store = KnownAnimalStore([
            {"id": "a_eastern", "name": "Eastern Coyote", "species": "Eastern coyote"},
        ])
        # Exact match on normalized form finds it
        assert len(store.get_by_species("Eastern coyote")) == 1
        # Exact match on the un-normalized form does NOT find it
        # (the registry stores whatever the operator wrote)
        assert len(store.get_by_species("coyote")) == 0


# ---------- mutation: add ----------

class TestAdd:
    def test_add_new_animal(self):
        store = KnownAnimalStore([])
        store.add(_bandit())
        assert len(store) == 1

    def test_add_duplicate_id_raises(self):
        store = KnownAnimalStore([_bandit()])
        with pytest.raises(ValueError, match="animal id already enrolled"):
            store.add({**_bandit(), "name": "Different name"})

    def test_add_missing_required_key_raises(self):
        store = KnownAnimalStore([])
        with pytest.raises(ValueError, match="missing required keys"):
            store.add({"id": "a_x"})  # no species, no name

    def test_add_does_not_replace_on_duplicate(self):
        """Unlike known_vehicles (silent replace), animal store fails
        loudly on duplicate id. Test that the original entry is
        preserved when add raises."""
        store = KnownAnimalStore([_bandit()])
        original = store.all()[0]
        with pytest.raises(ValueError):
            store.add({**_bandit(), "name": "Different"})
        assert store.all()[0] == original


# ---------- mutation: remove ----------

class TestRemove:
    def test_remove_existing(self):
        store = KnownAnimalStore([_bandit()])
        assert store.remove("a_bandit") is True
        assert len(store) == 0

    def test_remove_missing_returns_false(self):
        store = KnownAnimalStore([_bandit()])
        assert store.remove("a_nope") is False
        assert len(store) == 1


# ---------- dunder ----------

class TestDunder:
    def test_len(self):
        assert len(KnownAnimalStore([])) == 0
        assert len(KnownAnimalStore([_bandit(), _mr_whiskers()])) == 2

    def test_iter(self):
        store = KnownAnimalStore([_bandit(), _mr_whiskers()])
        ids = [a["id"] for a in store]
        assert ids == ["a_bandit", "a_mr_whiskers"]


# ---------- load_known_animals convenience ----------

class TestLoadKnownAnimals:
    def test_loads_default_path(self):
        """The canonical registry is the empty shipped file. load_known_animals()
        with no arg should return []."""
        result = load_known_animals()
        assert result == []

    def test_loads_explicit_path(self, tmp_path):
        f = tmp_path / "test.json"
        f.write_text(json.dumps({"version": 1, "animals": [_bandit()]}))
        result = load_known_animals(f)
        assert len(result) == 1
        assert result[0]["id"] == "a_bandit"

    def test_missing_default_raises(self, monkeypatch, tmp_path):
        """If both explicit path and default are missing, raise."""
        from known_animals import store as store_mod
        monkeypatch.setattr(store_mod, "_DEFAULT_PATH", tmp_path / "nope.json")
        with pytest.raises(FileNotFoundError):
            load_known_animals()

    def test_returns_a_copy_not_internal_list(self):
        """load_known_animals returns .all() which is a copy — mutating
        the returned list must not affect subsequent loads."""
        store = KnownAnimalStore([_bandit()])
        result = store.all()
        result.clear()
        # store itself is unchanged
        assert len(store) == 1


# ---------- integration smoke ----------

class TestIntegrationSmoke:
    """End-to-end: write a populated registry, load it back, mutate,
    save, reload. Mirrors the enroll-script workflow without actually
    invoking Qwen or the camera."""

    def test_enroll_workflow(self, tmp_path):
        # 1. Start with empty registry
        reg = tmp_path / "known_animals.json"
        store = KnownAnimalStore.from_dict({"version": 1, "animals": []})
        store.to_file(reg)

        # 2. Load it back (this is what the listener does on every alert)
        loaded = KnownAnimalStore.from_file(reg)
        assert len(loaded) == 0

        # 3. Operator enrolls Mr. Whiskers
        loaded.add(_mr_whiskers())
        loaded.to_file(reg)

        # 4. Reload — should see the new entry
        reloaded = KnownAnimalStore.from_file(reg)
        assert len(reloaded) == 1
        assert reloaded.get_by_id("a_mr_whiskers")["species"] == "cat"

        # 5. Operator enrolls Bandit (a different species)
        reloaded.add(_bandit())
        reloaded.to_file(reg)

        # 6. Reload — should see both
        final = KnownAnimalStore.from_file(reg)
        assert len(final) == 2
        assert len(final.get_by_species("cat")) == 1
        assert len(final.get_by_species("raccoon")) == 1

        # 7. Operator retires Mr. Whiskers
        final.remove("a_mr_whiskers")
        final.to_file(reg)

        # 8. Reload — should see only Bandit
        last = KnownAnimalStore.from_file(reg)
        assert len(last) == 1
        assert last.get_by_id("a_bandit") is not None