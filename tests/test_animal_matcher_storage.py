"""Tests for animal_matcher/storage.py — registry + embeddings loading.

AC2: load_known, load_embeddings, cosine_sim all present and functional.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from animal_matcher.storage import cosine_sim, load_embeddings, load_known


@pytest.fixture()
def fake_animals_dir(tmp_path: Path) -> Path:
    """Create a fake data/known/animals/ directory for testing."""
    animals_dir = tmp_path / "data" / "known" / "animals"
    animals_dir.mkdir(parents=True)
    (animals_dir / "known_animals.json").write_text("[]")
    return animals_dir


def _patch_animals_dir(animals_dir: Path) -> None:
    """Temporarily override the animals directory used by storage module."""
    from animal_matcher.storage import _animals_dir
    _animals_dir._test_override = animals_dir  # type: ignore[attr-defined]


def _restore_animals_dir() -> None:
    from animal_matcher.storage import _animals_dir
    _animals_dir._test_override = None  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# AC2: load_known returns list of dicts.
# ---------------------------------------------------------------------------

class TestLoadKnown:
    """AC2: load_known() returns list of dicts."""

    def test_empty_registry(self, fake_animals_dir: Path) -> None:
        """Empty known_animals.json returns []."""
        _patch_animals_dir(fake_animals_dir)
        result = load_known()
        _restore_animals_dir()
        assert result == []

    def test_with_entries(self, fake_animals_dir: Path) -> None:
        """Registry with entries returns list of dicts."""
        _patch_animals_dir(fake_animals_dir)
        data = [
            {"id": "cow-001", "label": "Bessie", "species": "cow"},
            {"id": "dog-001", "label": "Rover", "species": "dog"},
        ]
        (fake_animals_dir / "known_animals.json").write_text(json.dumps(data))
        result = load_known()
        _restore_animals_dir()
        assert len(result) == 2
        assert result[0]["id"] == "cow-001"
        assert result[1]["label"] == "Rover"

    def test_missing_file(self, tmp_path: Path) -> None:
        """Missing known_animals.json returns []."""
        animals_dir = tmp_path / "no_such_dir" / "known" / "animals"
        animals_dir.mkdir(parents=True)
        _patch_animals_dir(animals_dir)
        result = load_known()
        _restore_animals_dir()
        assert result == []

    def test_malformed_json(self, fake_animals_dir: Path) -> None:
        """Malformed JSON returns []."""
        _patch_animals_dir(fake_animals_dir)
        (fake_animals_dir / "known_animals.json").write_text("{bad json")
        result = load_known()
        _restore_animals_dir()
        assert result == []


# ---------------------------------------------------------------------------
# AC2: load_embeddings returns dict of vectors.
# ---------------------------------------------------------------------------

class TestLoadEmbeddings:
    """AC2: load_embeddings() returns dict of vectors."""

    def test_missing_file(self, fake_animals_dir: Path) -> None:
        """Missing embeddings.npz returns {}."""
        _patch_animals_dir(fake_animals_dir)
        result = load_embeddings()
        _restore_animals_dir()
        assert result == {}

    def test_with_embeddings(self, fake_animals_dir: Path) -> None:
        """Embeddings file with vectors returns dict."""
        _patch_animals_dir(fake_animals_dir)
        embeddings = {
            "cow-001__avg": np.array([0.1, 0.2, 0.3], dtype=np.float32),
            "dog-001__avg": np.array([0.4, 0.5, 0.6], dtype=np.float32),
        }
        np.savez(str(fake_animals_dir / "embeddings.npz"), **embeddings)  # type: ignore[arg-type]
        result = load_embeddings()
        _restore_animals_dir()
        assert len(result) == 2
        assert "cow-001__avg" in result
        assert "dog-001__avg" in result
        np.testing.assert_array_almost_equal(result["cow-001__avg"], embeddings["cow-001__avg"])


# ---------------------------------------------------------------------------
# AC2: cosine_sim returns float in [-1, 1].
# ---------------------------------------------------------------------------

class TestCosineSim:
    """AC2: cosine_sim(a, b) returns float in [-1, 1]."""

    def test_identical_vectors(self) -> None:
        """Same vector → cosine = 1.0."""
        v = np.array([1.0, 2.0, 3.0], dtype=np.float32)
        sim = cosine_sim(v, v)
        assert abs(sim - 1.0) < 1e-6

    def test_orthogonal_vectors(self) -> None:
        """Orthogonal vectors → cosine = 0.0."""
        a = np.array([1.0, 0.0, 0.0], dtype=np.float32)
        b = np.array([0.0, 1.0, 0.0], dtype=np.float32)
        sim = cosine_sim(a, b)
        assert abs(sim) < 1e-6

    def test_opposite_vectors(self) -> None:
        """Opposite vectors → cosine = -1.0."""
        a = np.array([1.0, 0.0, 0.0], dtype=np.float32)
        b = np.array([-1.0, 0.0, 0.0], dtype=np.float32)
        sim = cosine_sim(a, b)
        assert abs(sim - (-1.0)) < 1e-6

    def test_scaled_vector(self) -> None:
        """Scaled vector → cosine = 1.0 (direction matters, not magnitude)."""
        a = np.array([1.0, 2.0, 3.0], dtype=np.float32)
        b = np.array([2.0, 4.0, 6.0], dtype=np.float32)
        sim = cosine_sim(a, b)
        assert abs(sim - 1.0) < 1e-6

    def test_zero_vector(self) -> None:
        """Zero vector → cosine = 0.0."""
        a = np.array([0.0, 0.0, 0.0], dtype=np.float32)
        b = np.array([1.0, 2.0, 3.0], dtype=np.float32)
        sim = cosine_sim(a, b)
        assert sim == 0.0

    def test_returns_float(self) -> None:
        """cosine_sim returns a Python float, not np.float64."""
        a = np.array([1.0, 0.0], dtype=np.float32)
        b = np.array([0.0, 1.0], dtype=np.float32)
        result = cosine_sim(a, b)
        assert isinstance(result, float)
