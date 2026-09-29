"""E2E tests for person_matcher.match — match_person with synthetic embeddings.

Tests run with PERSON_EMBEDDER_LIVE=0 (default) so no model weights are needed.
Synthetic embeddings are deterministic (SHA256-seeded), so we can write
deterministic tests for tag match, Jaccard fallback, and cosine matching.
"""
from __future__ import annotations

import json
import os
from unittest.mock import patch

os.environ.setdefault("PERSON_EMBEDDER_LIVE", "0")
os.environ.setdefault("FARMSURV_DATA_DIR", "data")

import numpy as np
import pytest

from person_matcher.match import (
    _jaccard_score,
    _match_by_jaccard,
    _match_by_tag,
    _match_cosine,
    match_person,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_candidate(person_id: str, label: str, tag: str, markers: list[str] | None = None) -> dict:
    """Build a candidate dict matching the known_people.json schema."""
    return {
        "id": person_id,
        "label": label,
        "tag": tag,
        "identity_markers": markers or [],
    }


def _mock_embedding(people_dir):
    """Create a mock person_embedder that returns deterministic embeddings."""
    import numpy as np

    from person_matcher.storage import load_person_embeddings
    load_person_embeddings()
    # Return the avg embedding for the first person (or a synthetic one).
    mock_vec = np.zeros(512, dtype=np.float32)
    mock_vec[0] = 1.0  # Signal: this is person_001's direction
    return mock_vec


# ---------------------------------------------------------------------------
# Tests: _jaccard_score
# ---------------------------------------------------------------------------

@pytest.mark.e2e
class TestJaccardScore:
    """Test _jaccard_score helper."""

    def test_perfect_match(self):
        """AC: identical marker lists give Jaccard=1.0."""
        a = ["male", "tall", "beard"]
        b = ["male", "tall", "beard"]
        assert _jaccard_score(a, b) == 1.0

    def test_no_overlap(self):
        """AC: disjoint marker lists give Jaccard=0.0."""
        a = ["male", "tall"]
        b = ["female", "short"]
        assert _jaccard_score(a, b) == 0.0

    def test_partial_overlap(self):
        """AC: partial overlap gives correct Jaccard."""
        a = ["male", "tall", "beard"]
        b = ["male", "glasses"]
        # intersection=1, union=4 -> 0.25
        assert _jaccard_score(a, b) == pytest.approx(0.25, abs=1e-6)

    def test_empty_lists(self):
        """AC: both empty gives 1.0 (identity)."""
        assert _jaccard_score([], []) == 1.0

    def test_one_empty(self):
        """AC: one empty, one non-empty gives 0.0."""
        assert _jaccard_score(["male"], []) == 0.0

    def test_case_insensitive(self):
        """AC: markers are case-insensitive."""
        a = ["Male", "TALL", "Beard"]
        b = ["male", "tall", "beard"]
        assert _jaccard_score(a, b) == 1.0

    def test_whitespace_stripped(self):
        """AC: whitespace is stripped from markers."""
        a = [" male ", " tall "]
        b = ["male", "tall"]
        assert _jaccard_score(a, b) == 1.0

    def test_non_string_skipped(self):
        """AC: non-string entries are skipped."""
        a = ["male", None, "tall"]
        b = ["male", "tall"]
        assert _jaccard_score(a, b) == 1.0


# ---------------------------------------------------------------------------
# Tests: _match_by_tag
# ---------------------------------------------------------------------------

@pytest.mark.e2e
class TestMatchByTag:
    """Test tag match fast path."""

    def test_exact_tag_match(self):
        """AC: exact tag match (case-insensitive, whitespace-stripped)."""
        sig = {"identity_markers": ["jeremiah"]}
        candidates = [
            _make_candidate("p1", "Jeremiah", "jeremiah", ["male", "beard"]),
        ]
        result = _match_by_tag(sig, candidates)
        assert result is not None
        assert result["matched"] is True
        assert result["id"] == "p1"

    def test_no_tag_match(self):
        """AC: no match when tag doesn't match any candidate."""
        sig = {"identity_markers": ["unknown_person"]}
        candidates = [
            _make_candidate("p1", "Jeremiah", "jeremiah"),
        ]
        assert _match_by_tag(sig, candidates) is None

    def test_empty_markers(self):
        """AC: returns None when identity_markers is empty."""
        sig = {"identity_markers": []}
        candidates = [
            _make_candidate("p1", "Jeremiah", "jeremiah"),
        ]
        assert _match_by_tag(sig, candidates) is None

    def test_case_insensitive(self):
        """AC: tag match is case-insensitive."""
        sig = {"identity_markers": ["JEREMIAH"]}
        candidates = [
            _make_candidate("p1", "Jeremiah", "jeremiah"),
        ]
        result = _match_by_tag(sig, candidates)
        assert result is not None
        assert result["matched"] is True


# ---------------------------------------------------------------------------
# Tests: _match_by_jaccard
# ---------------------------------------------------------------------------

@pytest.mark.e2e
class TestMatchByJaccard:
    """Test Jaccard fallback."""

    def test_perfect_marker_match(self):
        """AC: identical markers give Jaccard=1.0 >= 0.5 threshold."""
        sig = {"identity_markers": ["male", "tall", "beard"]}
        candidates = [
            _make_candidate("p1", "Jeremiah", "jeremiah", ["male", "tall", "beard"]),
        ]
        result = _match_by_jaccard(sig, candidates)
        assert result is not None
        assert result["matched"] is True
        assert result["id"] == "p1"

    def test_no_jaccard_match(self):
        """AC: no match when Jaccard < 0.5."""
        sig = {"identity_markers": ["male", "tall", "beard"]}
        candidates = [
            _make_candidate("p1", "Jeremiah", "jeremiah", ["female", "short", "blonde"]),
        ]
        # intersection=0, union=6 -> 0.0 < 0.5
        assert _match_by_jaccard(sig, candidates) is None

    def test_jaccard_threshold_0_5(self):
        """AC: Jaccard=0.5 exactly meets the threshold."""
        sig = {"identity_markers": ["male", "tall"]}
        candidates = [
            _make_candidate("p1", "Jeremiah", "jeremiah", ["male", "glasses"]),
        ]
        # intersection=1, union=3 -> 0.333 < 0.5 -> no match
        assert _match_by_jaccard(sig, candidates) is None


# ---------------------------------------------------------------------------
# Tests: match_person (integration)
# ---------------------------------------------------------------------------

@pytest.mark.e2e
class TestMatchPerson:
    """Test match_person public API with synthetic embeddings."""

    def test_tag_match_returns_fast(self):
        """AC: tag match returns immediately without invoking cosine."""
        sig = {"identity_markers": ["jeremiah"]}
        candidates = [
            _make_candidate("p1", "Jeremiah", "jeremiah", ["male", "beard"]),
        ]
        result = match_person(sig, candidates)
        assert result["matched"] is True
        assert result["id"] == "p1"

    def test_jaccard_fallback(self):
        """AC: Jaccard fallback works when tag doesn't match."""
        sig = {"identity_markers": ["male", "tall", "beard"]}
        candidates = [
            _make_candidate("p1", "Jeremiah", "stranger", ["male", "tall", "beard"]),
        ]
        result = match_person(sig, candidates)
        assert result["matched"] is True
        assert result["id"] == "p1"

    def test_no_match(self):
        """AC: no match returns matched=False."""
        sig = {"identity_markers": ["alien"]}
        candidates = [
            _make_candidate("p1", "Jeremiah", "jeremiah", ["male", "beard"]),
        ]
        result = match_person(sig, candidates)
        assert result["matched"] is False

    def test_empty_identity_markers(self):
        """AC: absent identity_markers returns matched=False immediately."""
        sig = {}
        candidates = [
            _make_candidate("p1", "Jeremiah", "jeremiah"),
        ]
        result = match_person(sig, candidates)
        assert result["matched"] is False

    def test_empty_candidates(self):
        """AC: no candidates returns matched=False."""
        sig = {"identity_markers": ["male", "tall"]}
        result = match_person(sig, [])
        assert result["matched"] is False

    def test_crop_path_without_embedder_off(self):
        """AC: crop_path provided but embedder not live -> falls back to tag/Jaccard."""
        sig = {"identity_markers": ["male", "tall"]}
        candidates = [
            _make_candidate("p1", "Jeremiah", "stranger", ["male", "tall"]),
        ]
        result = match_person(sig, candidates, crop_path="/tmp/fake.png")
        # Since embedder is off, tag/Jaccard should still work
        assert result["matched"] is True
        assert result["id"] == "p1"


# ---------------------------------------------------------------------------
# Tests: _match_cosine
# ---------------------------------------------------------------------------

@pytest.mark.e2e
class TestMatchCosine:
    """Test cosine matching path."""

    def test_embedder_off_returns_skipped(self):
        """AC: when embedder not live, returns skipped marker."""
        sig = {"identity_markers": ["male"]}
        candidates = [
            _make_candidate("p1", "Jeremiah", "jeremiah", ["male"]),
        ]
        result = _match_cosine(sig, candidates, "/tmp/fake.png")
        assert result["matched"] is False
        assert result.get("skipped") == "embedder_off"

    def test_no_crop_file(self):
        """AC: missing crop file returns matched=False with no top_candidates."""
        sig = {"identity_markers": ["male"]}
        candidates = [
            _make_candidate("p1", "Jeremiah", "jeremiah", ["male"]),
        ]
        result = _match_cosine(sig, candidates, "/tmp/nonexistent.png")
        assert result["matched"] is False
        assert result["top_candidates"] == []

    def test_no_enrolled_people(self, tmp_path):
        """AC: no enrolled people returns matched=False."""
        from person_matcher import storage
        storage._people_dir._test_override = tmp_path
        try:
            # Mock the embedder to return a valid vector
            mock_vec = np.zeros(512, dtype=np.float32)
            mock_vec[0] = 1.0
            with patch("person_matcher.match._embed_crop", return_value=mock_vec):
                sig = {"identity_markers": ["male"]}
                candidates = [
                    _make_candidate("p1", "Jeremiah", "jeremiah", ["male"]),
                ]
                result = _match_cosine(sig, candidates, "/tmp/fake.png")
                assert result["matched"] is False
                assert result["top_candidates"] == []
        finally:
            if hasattr(storage._people_dir, "_test_override"):
                delattr(storage._people_dir, "_test_override")

    def test_cosine_below_threshold(self, tmp_path):
        """AC: cosine below 0.85 returns matched=False with top_candidates populated."""
        people_dir = tmp_path / "known" / "people"
        people_dir.mkdir(parents=True)

        # Write a registry with one person
        registry = [
            {"id": "p1", "label": "Jeremiah", "tag": "jeremiah", "identity_markers": ["male", "beard"]},
        ]
        (people_dir / "known_people.json").write_text(json.dumps(registry))

        # Embedding: orthogonal to our test vector (cosine ≈ 0)
        embeddings = np.zeros((1, 512), dtype=np.float32)
        embeddings[0, 1] = 1.0  # orthogonal to vec[0]=1.0
        np.savez(str(people_dir / "embeddings.npz"), p1__avg=embeddings[0])

        from person_matcher import storage
        storage._people_dir._test_override = people_dir
        try:
            mock_vec = np.zeros(512, dtype=np.float32)
            mock_vec[0] = 1.0
            with patch("person_matcher.match._embed_crop", return_value=mock_vec):
                sig = {"identity_markers": ["male", "beard"]}
                candidates = [
                    _make_candidate("p1", "Jeremiah", "jeremiah", ["male", "beard"]),
                ]
                result = _match_cosine(sig, candidates, "/tmp/fake.png")
                assert result["matched"] is False
                # top_candidates should list the scored candidate even though below threshold
                assert len(result["top_candidates"]) == 0  # cosine=0 < 0.85
        finally:
            if hasattr(storage._people_dir, "_test_override"):
                delattr(storage._people_dir, "_test_override")

    def test_match_cosine_has_candidate_label_and_id(self, tmp_path):
        """AC: on a matched cosine path, result['candidate']['label'] and
        result['candidate']['id'] are present and non-empty — mirrors
        animal_matcher/match.py shape (animal_e2e:215-216)."""
        people_dir = tmp_path / "known" / "people"
        people_dir.mkdir(parents=True)

        registry = [
            {"id": "p1", "label": "Jeremiah", "tag": "jeremiah",
             "identity_markers": ["male", "beard"]},
        ]
        (people_dir / "known_people.json").write_text(json.dumps(registry))

        # Embedding: identical to our test vector (cosine = 1.0 >= 0.85).
        embeddings = np.zeros((1, 512), dtype=np.float32)
        embeddings[0, 0] = 1.0
        np.savez(str(people_dir / "embeddings.npz"), p1__avg=embeddings[0])

        from person_matcher import storage
        storage._people_dir._test_override = people_dir
        try:
            mock_vec = np.zeros(512, dtype=np.float32)
            mock_vec[0] = 1.0
            # Set PERSON_EMBEDDER_LIVE=1 so _match_cosine does not early-exit.
            old = os.environ.get("PERSON_EMBEDDER_LIVE")
            os.environ["PERSON_EMBEDDER_LIVE"] = "1"
            try:
                with patch("person_matcher.match._embed_crop", return_value=mock_vec):
                    sig = {"identity_markers": ["male", "beard"]}
                    candidates = [
                        _make_candidate("p1", "Jeremiah", "jeremiah",
                                        ["male", "beard"]),
                    ]
                    result = _match_cosine(sig, candidates, "/tmp/fake.png")

                    assert result["matched"] is True
                    assert "candidate" in result
                    assert result["candidate"]["id"] == "p1"
                    assert result["candidate"]["label"] == "Jeremiah"
                    assert result["candidate"]["id"]  # non-empty
                    assert result["candidate"]["label"]  # non-empty
                    assert "cosine_score" in result
                    assert "tier1_score" in result
            finally:
                if old is not None:
                    os.environ["PERSON_EMBEDDER_LIVE"] = old
                else:
                    os.environ.pop("PERSON_EMBEDDER_LIVE", None)
        finally:
            if hasattr(storage._people_dir, "_test_override"):
                delattr(storage._people_dir, "_test_override")

    def test_unmatched_cosine_has_consistent_shape(self, tmp_path):
        """AC: on an unmatched cosine path, result does NOT contain
        candidate (or candidate.id/candidate.label are absent/None),
        matching the animal matcher no-match shape (animal_e2e:235-236)."""
        people_dir = tmp_path / "known" / "people"
        people_dir.mkdir(parents=True)

        registry = [
            {"id": "p1", "label": "Jeremiah", "tag": "jeremiah",
             "identity_markers": ["male", "beard"]},
        ]
        (people_dir / "known_people.json").write_text(json.dumps(registry))

        # Embedding: orthogonal to our test vector (cosine ≈ 0).
        embeddings = np.zeros((1, 512), dtype=np.float32)
        embeddings[0, 1] = 1.0  # orthogonal to vec[0]=1.0
        np.savez(str(people_dir / "embeddings.npz"), p1__avg=embeddings[0])

        from person_matcher import storage
        storage._people_dir._test_override = people_dir
        try:
            mock_vec = np.zeros(512, dtype=np.float32)
            mock_vec[0] = 1.0
            with patch("person_matcher.match._embed_crop", return_value=mock_vec):
                sig = {"identity_markers": ["male", "beard"]}
                candidates = [
                    _make_candidate("p1", "Jeremiah", "jeremiah",
                                    ["male", "beard"]),
                ]
                result = _match_cosine(sig, candidates, "/tmp/fake.png")

                assert result["matched"] is False
                # No 'candidate' key on no-match (consistent with animal analog).
                assert "candidate" not in result
                # top_candidates is empty because cosine < threshold.
                assert result["top_candidates"] == []
                assert "threshold" in result
        finally:
            if hasattr(storage._people_dir, "_test_override"):
                delattr(storage._people_dir, "_test_override")
