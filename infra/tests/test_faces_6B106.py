"""Unit tests for infra.faces (Phase.106 lift).

The lifted module mirrors <legacy-repo>/src/faces.py (archived
2026-08-22) but uses infra.paths for path constants (not bare `paths`).
Tests use a tmp_path fixture to redirect IDENTITIES_DIR per-test.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_root = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_root))


@pytest.fixture
def isolated_identities(monkeypatch, tmp_path):
    """Redirect infra.faces.IDENTITIES_DIR to a tmp_path so tests don't touch prod."""
    from infra import faces as faces_mod
    from infra import paths as paths_mod

    identities_dir = tmp_path / "identities"
    identities_dir.mkdir()
    monkeypatch.setattr(faces_mod, "IDENTITIES_DIR", str(identities_dir))
    monkeypatch.setattr(paths_mod, "IDENTITIES_DIR", str(identities_dir))
    monkeypatch.setattr(faces_mod, "IDENTITY_BACKUP_DIR", "")  # disable NAS backup
    return identities_dir


# -----------------------------------------------------------------------------
# Slug generation
# -----------------------------------------------------------------------------


def test_slug_from_name__basic_lowercases_and_replaces_spaces():
    """'Note' → 'mr_v' — lowercase + non-alphanumeric → underscore."""
    from infra.faces import _slug_from_name
    assert _slug_from_name("Note") == "mr_v"


def test_slug_from_name__collapses_runs_of_punctuation():
    """'name two's Truck!!' → 'carson_s_truck' (one underscore per run)."""
    from infra.faces import _slug_from_name
    assert _slug_from_name("name two's Truck!!") == "carson_s_truck"


def test_slug_from_name__empty_name_falls_back_to_unnamed():
    from infra.faces import _slug_from_name
    assert _slug_from_name("   ") == "unnamed"


def test_slug_from_name__preserves_digits():
    from infra.faces import _slug_from_name
    assert _slug_from_name("name two 2") == "carson_2"


# -----------------------------------------------------------------------------
# save_identity + load_identity round trip
# -----------------------------------------------------------------------------


def test_save_then_load__round_trip_preserves_fields(isolated_identities):
    from infra.faces import load_identity, save_identity

    identity = {
        "name": "Note",
        "role": "owner",
        "face_embedding": [0.1, 0.2, 0.3] * 171 + [0.1],  # 514 → 512 via slice
        "sample_count": 5,
        "vehicle": None,
    }
    # Use a clean 512-dim embedding for simplicity
    identity["face_embedding"] = [float(i) / 512 for i in range(512)]

    path = save_identity(identity)
    assert path == str(isolated_identities / "mr_v.json")

    loaded = load_identity("Note")
    assert loaded is not None
    assert loaded["name"] == "Note"
    assert loaded["role"] == "owner"
    assert loaded["face_embedding"] == identity["face_embedding"]
    assert loaded["sample_count"] == 5
    assert loaded["vehicle"] is None
    assert loaded["enrolled_at"]  # auto-set
    assert loaded["last_seen"] is None  # auto-set None
    assert loaded["history"] == []  # auto-set


def test_save_identity__sets_defaults_for_missing_optional_fields(isolated_identities):
    """Caller doesn't have to provide all fields — defaults applied at save time."""
    from infra.faces import load_identity, save_identity

    minimal = {"name": "Bob", "role": "worker", "face_embedding": [0.0] * 512, "sample_count": 1}
    save_identity(minimal)
    loaded = load_identity("Bob")
    assert loaded is not None
    assert loaded["vehicle"] is None
    assert loaded["last_seen"] is None
    assert loaded["history"] == []
    assert loaded["enrolled_at"]


def test_save_identity__missing_name_raises(isolated_identities):
    from infra.faces import save_identity

    with pytest.raises(KeyError, match="name"):
        save_identity({"role": "owner"})


def test_save_identity__creates_identities_dir_if_missing(tmp_path, monkeypatch):
    """First save should mkdir -p the IDENTITIES_DIR."""
    from infra import faces as faces_mod

    target_dir = tmp_path / "fresh_identities"  # does NOT exist yet
    monkeypatch.setattr(faces_mod, "IDENTITIES_DIR", str(target_dir))
    monkeypatch.setattr(faces_mod, "IDENTITY_BACKUP_DIR", "")

    identity = {"name": "Alice", "role": "family", "face_embedding": [0.0] * 512, "sample_count": 1}
    path = faces_mod.save_identity(identity)

    assert target_dir.exists()
    assert Path(path).exists()


# -----------------------------------------------------------------------------
# list_identities + delete_identity
# -----------------------------------------------------------------------------


def test_list_identities__returns_sorted_names(isolated_identities):
    from infra.faces import list_identities, save_identity

    save_identity({"name": "Charlie", "role": "worker", "face_embedding": [0.0] * 512, "sample_count": 1})
    save_identity({"name": "Alice", "role": "family", "face_embedding": [0.0] * 512, "sample_count": 1})
    save_identity({"name": "Bob", "role": "worker", "face_embedding": [0.0] * 512, "sample_count": 1})

    assert list_identities() == ["Alice", "Bob", "Charlie"]


def test_list_identities__skips_non_json_files(isolated_identities):
    """A stray .txt or .bak in the dir is ignored."""
    from infra.faces import list_identities, save_identity

    save_identity({"name": "Alice", "role": "family", "face_embedding": [0.0] * 512, "sample_count": 1})
    (isolated_identities / "stray.txt").write_text("ignored")
    (isolated_identities / "stray.bak").write_text("ignored")

    assert list_identities() == ["Alice"]


def test_delete_identity__removes_file(isolated_identities):
    from infra.faces import delete_identity, load_identity, save_identity

    save_identity({"name": "Bob", "role": "worker", "face_embedding": [0.0] * 512, "sample_count": 1})
    assert load_identity("Bob") is not None

    assert delete_identity("Bob") is True
    assert load_identity("Bob") is None
    assert delete_identity("Bob") is False  # already gone


# -----------------------------------------------------------------------------
# add_enrollment_sample — averaging embeddings
# -----------------------------------------------------------------------------


def test_add_enrollment_sample__averages_existing_and_new(isolated_identities):
    """New sample is averaged with existing embedding, weighted by sample_count."""
    from infra.faces import add_enrollment_sample, load_identity, save_identity

    save_identity({
        "name": "Dave",
        "role": "family",
        "face_embedding": [1.0] + [0.0] * 511,
        "sample_count": 1,
    })

    # Add a sample that points the other way: [-1.0, 0, ...]
    new_sample = [-1.0] + [0.0] * 511
    add_enrollment_sample("Dave", new_sample)

    loaded = load_identity("Dave")
    assert loaded is not None
    assert loaded["sample_count"] == 2
    # Average of [1.0, 0, ...] and [-1.0, 0, ...] is [0.0, 0, ...]
    assert abs(loaded["face_embedding"][0]) < 1e-9


def test_add_enrollment_sample__appends_history(isolated_identities):
    """When camera is provided, each sample adds a history entry with ts + camera."""
    from infra.faces import add_enrollment_sample, load_identity, save_identity

    save_identity({"name": "Eve", "role": "worker", "face_embedding": [0.0] * 512, "sample_count": 1})
    add_enrollment_sample("Eve", [0.5] + [0.0] * 511, camera="CAM1")

    loaded = load_identity("Eve")
    assert loaded is not None
    assert len(loaded["history"]) == 1
    entry = loaded["history"][0]
    assert "ts" in entry
    assert entry["camera"] == "CAM1"


def test_add_enrollment_sample__no_history_when_no_camera(isolated_identities):
    """When camera is empty (default), no history entry is appended.

    This matches the original module: history is for sighting provenance,
    not for enrollment itself. The enrolled_at field already records when
    the identity was created.
    """
    from infra.faces import add_enrollment_sample, load_identity, save_identity

    save_identity({"name": "Frank", "role": "worker", "face_embedding": [0.0] * 512, "sample_count": 1})
    add_enrollment_sample("Frank", [0.5] + [0.0] * 511)  # no camera arg

    loaded = load_identity("Frank")
    assert loaded is not None
    assert loaded["history"] == []  # camera="" doesn't trigger history append
