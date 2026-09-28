"""E2E tests for person_matcher.storage — load_known_people, load_person_embeddings, cosine_sim.

Tests run with PERSON_EMBEDDER_LIVE=0 (default) so no model weights are needed.
Mirror the animal_matcher storage tests (V2-080 audit: unit tests for storage layer).
"""
from __future__ import annotations

import json
import os

os.environ.setdefault("PERSON_EMBEDDER_LIVE", "0")
os.environ.setdefault("FARMSURV_DATA_DIR", "data")

import numpy as np
import pytest

from person_matcher.storage import cosine_sim, load_known_people, load_person_embeddings

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

@pytest.fixture()
def fake_registry(tmp_path):
    """Create a fake person registry and set _test_override."""
    people_dir = tmp_path / "known" / "people"
    people_dir.mkdir(parents=True)

    registry = [
        {"id": "person_001", "label": "Jeremiah", "tag": "jeremiah", "identity_markers": ["male", "tall", "beard"]},
        {"id": "person_002", "label": "Operator", "tag": "operator", "identity_markers": ["male", "medium_height", "glasses"]},
    ]
    (people_dir / "known_people.json").write_text(json.dumps(registry))

    # Create embeddings: person_001__avg, person_002__avg
    embeddings = np.array([
        [1.0, 0.0, 0.0, 0.0, 0.0],
        [0.0, 1.0, 0.0, 0.0, 0.0],
    ], dtype=np.float32)
    np.savez(str(people_dir / "embeddings.npz"), person_001__avg=embeddings[0], person_002__avg=embeddings[1])

    return people_dir


@pytest.fixture()
def set_people_dir_override(fake_registry):
    """Apply the test override to the module."""
    from person_matcher import storage
    storage._people_dir._test_override = fake_registry
    yield fake_registry
    # Cleanup
    if hasattr(storage._people_dir, "_test_override"):
        delattr(storage._people_dir, "_test_override")


# ---------------------------------------------------------------------------
# Tests: load_known_people
# ---------------------------------------------------------------------------

@pytest.mark.e2e
class TestLoadKnownPeople:
    """Test load_known_people function."""

    def test_loads_registry(self, set_people_dir_override):
        """AC: load_known_people returns the registry entries."""
        result = load_known_people()
        assert len(result) == 2
        assert result[0]["id"] == "person_001"
        assert result[0]["label"] == "Jeremiah"
        assert result[1]["id"] == "person_002"
        assert result[1]["label"] == "Operator"

    def test_returns_empty_when_no_file(self, tmp_path):
        """AC: returns [] when known_people.json is missing."""
        from person_matcher import storage
        storage._people_dir._test_override = tmp_path
        try:
            result = load_known_people()
            assert result == []
        finally:
            if hasattr(storage._people_dir, "_test_override"):
                delattr(storage._people_dir, "_test_override")

    def test_returns_empty_when_malformed_json(self, tmp_path):
        """AC: returns [] when JSON is malformed."""
        people_dir = tmp_path / "known" / "people"
        people_dir.mkdir(parents=True)
        (people_dir / "known_people.json").write_text("not valid json{{{")
        from person_matcher import storage
        storage._people_dir._test_override = people_dir
        try:
            result = load_known_people()
            assert result == []
        finally:
            if hasattr(storage._people_dir, "_test_override"):
                delattr(storage._people_dir, "_test_override")

    def test_returns_empty_when_not_list(self, tmp_path):
        """AC: returns [] when JSON is not a list."""
        people_dir = tmp_path / "known" / "people"
        people_dir.mkdir(parents=True)
        (people_dir / "known_people.json").write_text('{"key": "value"}')
        from person_matcher import storage
        storage._people_dir._test_override = people_dir
        try:
            result = load_known_people()
            assert result == []
        finally:
            if hasattr(storage._people_dir, "_test_override"):
                delattr(storage._people_dir, "_test_override")


# ---------------------------------------------------------------------------
# Tests: load_person_embeddings
# ---------------------------------------------------------------------------

@pytest.mark.e2e
class TestLoadPersonEmbeddings:
    """Test load_person_embeddings function."""

    def test_loads_embeddings(self, set_people_dir_override):
        """AC: load_person_embeddings returns dict with correct keys and shapes."""
        result = load_person_embeddings()
        assert len(result) == 2
        assert "person_001__avg" in result
        assert "person_002__avg" in result
        assert result["person_001__avg"].shape == (5,)
        assert result["person_001__avg"].dtype == np.float32

    def test_returns_empty_when_no_file(self, tmp_path):
        """AC: returns {} when embeddings.npz is missing."""
        from person_matcher import storage
        storage._people_dir._test_override = tmp_path
        try:
            result = load_person_embeddings()
            assert result == {}
        finally:
            if hasattr(storage._people_dir, "_test_override"):
                delattr(storage._people_dir, "_test_override")

    def test_returns_empty_when_malformed_npz(self, tmp_path):
        """AC: returns {} when embeddings.npz is corrupted."""
        people_dir = tmp_path / "known" / "people"
        people_dir.mkdir(parents=True)
        (people_dir / "embeddings.npz").write_bytes(b"not a npz file")
        from person_matcher import storage
        storage._people_dir._test_override = people_dir
        try:
            result = load_person_embeddings()
            assert result == {}
        finally:
            if hasattr(storage._people_dir, "_test_override"):
                delattr(storage._people_dir, "_test_override")


# ---------------------------------------------------------------------------
# Tests: cosine_sim
# ---------------------------------------------------------------------------

class TestCosineSim:
    """Test cosine_sim function."""

    def test_perfect_match(self):
        """AC: identical vectors give cosine=1.0."""
        a = np.array([1.0, 0.0, 0.0], dtype=np.float32)
        b = np.array([1.0, 0.0, 0.0], dtype=np.float32)
        assert cosine_sim(a, b) == pytest.approx(1.0, abs=1e-6)

    def test_opposite_vectors(self):
        """AC: opposite vectors give cosine=-1.0."""
        a = np.array([1.0, 0.0, 0.0], dtype=np.float32)
        b = np.array([-1.0, 0.0, 0.0], dtype=np.float32)
        assert cosine_sim(a, b) == pytest.approx(-1.0, abs=1e-6)

    def test_orthogonal_vectors(self):
        """AC: orthogonal vectors give cosine=0.0."""
        a = np.array([1.0, 0.0, 0.0], dtype=np.float32)
        b = np.array([0.0, 1.0, 0.0], dtype=np.float32)
        assert cosine_sim(a, b) == pytest.approx(0.0, abs=1e-6)

    def test_zero_vector_returns_zero(self):
        """AC: zero vector returns 0.0."""
        a = np.array([0.0, 0.0, 0.0], dtype=np.float32)
        b = np.array([1.0, 0.0, 0.0], dtype=np.float32)
        assert cosine_sim(a, b) == 0.0

    def test_scaled_vectors(self):
        """AC: scaled identical vectors give cosine=1.0."""
        a = np.array([2.0, 4.0, 6.0], dtype=np.float32)
        b = np.array([1.0, 2.0, 3.0], dtype=np.float32)
        assert cosine_sim(a, b) == pytest.approx(1.0, abs=1e-6)
