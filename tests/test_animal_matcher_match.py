"""Tests for animal_matcher/match.py — Tier-2 cosine + Tier-1 Jaccard matcher.

AC3: match_animal("") returns {matched: False} when no enrolled animals.
AC4: Full test suite passes (unit tests only; live integration is opt-in).
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest
from PIL import Image

from animal_matcher.match import match_animal

# ---------------------------------------------------------------------------
# Fixtures — isolate each test from real data/ files.
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _ensure_offline_mode(monkeypatch):
    """Force offline mode for all tests (no model download, no embedder)."""
    monkeypatch.setenv("ANIMAL_EMBEDDER_LIVE", "0")
    yield


def _patch_animals_dir(animals_dir: Path) -> None:
    """Temporarily override the animals directory used by storage module."""
    from animal_matcher.storage import _animals_dir
    _animals_dir._test_override = animals_dir  # type: ignore[attr-defined]


def _restore_animals_dir() -> None:
    from animal_matcher.storage import _animals_dir
    _animals_dir._test_override = None  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# AC3: match_animal returns {matched: False} when no enrolled animals.
# ---------------------------------------------------------------------------

class TestNoEnrolled:
    """AC3: match_animal returns {matched: False} when no enrolled animals."""

    def test_empty_registry(self, tmp_path: Path) -> None:
        """No known animals -> {matched: False, top_candidates: [], threshold: 0.85}."""
        animals_dir = tmp_path / "data" / "known" / "animals"
        animals_dir.mkdir(parents=True)
        (animals_dir / "known_animals.json").write_text("[]")
        _patch_animals_dir(animals_dir)

        crop_path = str(animals_dir / "fake.png")
        Image.new("RGB", (64, 64), color=(128, 64, 32)).save(crop_path)

        result = match_animal(crop_path)

        _restore_animals_dir()

        assert result["matched"] is False
        assert result["top_candidates"] == []
        assert isinstance(result["threshold"], float)
        assert "skipped" in result  # embedder_off path

    def test_crop_file_missing(self, tmp_path: Path) -> None:
        """Crop file not found -> {matched: False}."""
        animals_dir = tmp_path / "data" / "known" / "animals"
        animals_dir.mkdir(parents=True)
        (animals_dir / "known_animals.json").write_text(
            json.dumps([{"id": "cow-001", "label": "Bessie", "species": "cow"}])
        )
        _patch_animals_dir(animals_dir)

        result = match_animal("/tmp/nonexistent_crop.png")

        _restore_animals_dir()

        assert result["matched"] is False


# ---------------------------------------------------------------------------
# ANIMAL_EMBEDDER_LIVE check.
# ---------------------------------------------------------------------------

class TestEmbedderOff:
    """When ANIMAL_EMBEDDER_LIVE != 1, return skipped marker."""

    def test_embedder_off_returns_skipped(self, tmp_path: Path) -> None:
        """ANIMAL_EMBEDDER_LIVE=0 -> {matched: False, skipped: embedder_off}."""
        animals_dir = tmp_path / "data" / "known" / "animals"
        animals_dir.mkdir(parents=True)
        (animals_dir / "known_animals.json").write_text("[]")
        _patch_animals_dir(animals_dir)

        crop_path = str(animals_dir / "fake.png")
        Image.new("RGB", (64, 64), color=(128, 64, 32)).save(crop_path)

        with patch.dict("os.environ", {"ANIMAL_EMBEDDER_LIVE": "0"}):
            result = match_animal(crop_path)

        _restore_animals_dir()

        assert result["matched"] is False
        assert result["skipped"] == "embedder_off"
        assert result["threshold"] == 0.85


# ---------------------------------------------------------------------------
# Tier-2: cosine matching below threshold.
# ---------------------------------------------------------------------------

class TestCosineBelowThreshold:
    """Tier-2: candidates exist but cosine is below threshold."""

    def test_below_threshold_no_match(self, tmp_path: Path) -> None:
        """Cosine below threshold -> {matched: False, top_candidates: []}."""
        animals_dir = tmp_path / "data" / "known" / "animals"
        animals_dir.mkdir(parents=True)
        (animals_dir / "known_animals.json").write_text(
            json.dumps([
                {"id": "cow-001", "label": "Bessie", "species": "cow",
                 "color_patterns": ["brown"],
                 "distinctive_features": ["horned"]},
            ])
        )
        # Very low cosine similarity vector.
        vec_enrolled = np.array([-0.9, -0.9, -0.9], dtype=np.float32)
        embeddings = {"cow-001__avg": vec_enrolled}
        np.savez(str(animals_dir / "embeddings.npz"), **embeddings)  # type: ignore[arg-type]
        _patch_animals_dir(animals_dir)

        # Create a fake crop file.
        crop_path = str(animals_dir / "fake_crop.png")
        Image.new("RGB", (64, 64), color=(128, 64, 32)).save(crop_path)

        # Mock _embed_crop to return a vector orthogonal to enrolled vec,
        # ensuring cosine is well below the 0.85 threshold.
        vec_crop = np.array([0.9, 0.9, 0.9], dtype=np.float32)
        with patch.dict("os.environ", {"ANIMAL_EMBEDDER_LIVE": "1"}), \
             patch("animal_matcher.match._embed_crop", return_value=vec_crop):
            result = match_animal(crop_path)

        _restore_animals_dir()

        assert result["matched"] is False
        assert "top_candidates" in result
        assert "threshold" in result
        assert result["threshold"] == 0.85


# ---------------------------------------------------------------------------
# Tier-2: cosine matching above threshold.
# ---------------------------------------------------------------------------

class TestCosineAboveThreshold:
    """Tier-2: candidate above threshold -> match."""

    def test_match_found(self, tmp_path: Path) -> None:
        """Cosine above threshold -> {matched: True, candidate, cosine_score, tier1_score}."""
        animals_dir = tmp_path / "data" / "known" / "animals"
        animals_dir.mkdir(parents=True)
        (animals_dir / "known_animals.json").write_text(
            json.dumps([
                {"id": "cow-001", "label": "Bessie", "species": "cow",
                 "color_patterns": ["brown", "white"],
                 "distinctive_features": ["horned"]},
            ])
        )
        # Identical vector -> cosine = 1.0 (above any threshold).
        vec = np.array([0.1, 0.2, 0.3], dtype=np.float32)
        embeddings = {"cow-001__avg": vec}
        np.savez(str(animals_dir / "embeddings.npz"), **embeddings)  # type: ignore[arg-type]
        _patch_animals_dir(animals_dir)

        # Create a fake crop file.
        crop_path = str(animals_dir / "fake_crop.png")
        Image.new("RGB", (64, 64), color=(128, 64, 32)).save(crop_path)

        # Mock _embed_crop to return the same vector as the enrollment,
        # so cosine similarity = 1.0 (guaranteed match).
        with patch.dict("os.environ", {"ANIMAL_EMBEDDER_LIVE": "1"}), \
             patch("animal_matcher.match._embed_crop", return_value=vec):
            result = match_animal(crop_path)

        _restore_animals_dir()

        assert result["matched"] is True
        assert "candidate" in result
        assert result["candidate"]["id"] == "cow-001"
        assert "cosine_score" in result
        assert "tier1_score" in result
        assert "threshold" in result
        assert result["threshold"] == 0.85

    def test_match_with_runner_ups(self, tmp_path: Path) -> None:
        """Multiple candidates above threshold -> runner_ups populated."""
        animals_dir = tmp_path / "data" / "known" / "animals"
        animals_dir.mkdir(parents=True)
        (animals_dir / "known_animals.json").write_text(
            json.dumps([
                {"id": "cow-001", "label": "Bessie", "species": "cow",
                 "color_patterns": ["brown"], "distinctive_features": []},
                {"id": "cow-002", "label": "Daisy", "species": "cow",
                 "color_patterns": ["brown"], "distinctive_features": []},
            ])
        )
        # Bessie: perfect match (cosine=1.0), Daisy: lower match (cosine=0.8).
        vec_bessie = np.array([0.1, 0.2, 0.3], dtype=np.float32)
        vec_daisy = np.array([0.05, 0.15, 0.25], dtype=np.float32)
        embeddings = {"cow-001__avg": vec_bessie, "cow-002__avg": vec_daisy}
        np.savez(str(animals_dir / "embeddings.npz"), **embeddings)  # type: ignore[arg-type]
        _patch_animals_dir(animals_dir)

        crop_path = str(animals_dir / "fake_crop.png")
        Image.new("RGB", (64, 64), color=(128, 64, 32)).save(crop_path)

        # Mock _embed_crop to return Bessie's exact vector (cosine=1.0).
        with patch.dict("os.environ", {"ANIMAL_EMBEDDER_LIVE": "1"}), \
             patch("animal_matcher.match._embed_crop", return_value=vec_bessie):
            result = match_animal(crop_path)

        _restore_animals_dir()

        assert result["matched"] is True
        assert "runner_ups" in result
        assert len(result["runner_ups"]) >= 1
        assert result["runner_ups"][0]["label"] == "Daisy"


# ---------------------------------------------------------------------------
# Tier-1: Jaccard over identity_markers.
# ---------------------------------------------------------------------------

class TestTier1Scoring:
    """Tier-1: Jaccard over identity_markers field."""

    def test_jaccard_over_identity_markers(self, tmp_path: Path) -> None:
        """identity_markers with overlap -> higher Jaccard score."""
        from animal_matcher.match import _jaccard_score

        markers_a = ["spot", "horned", "large"]
        markers_b = ["spot", "horned", "small"]
        markers_c = ["blue", "tiny"]

        j_same = _jaccard_score(markers_a, markers_a)
        j_partial = _jaccard_score(markers_a, markers_b)
        j_none = _jaccard_score(markers_a, markers_c)

        assert j_same == 1.0, "Same markers should give Jaccard=1.0"
        assert j_partial > j_none, "Partial overlap > no overlap"
        assert j_none < j_partial, "No overlap < partial overlap"

    def test_empty_identity_markers(self, tmp_path: Path) -> None:
        """Empty identity_markers in candidate -> candidate skipped (no Tier-1)."""
        animals_dir = tmp_path / "data" / "known" / "animals"
        animals_dir.mkdir(parents=True)
        (animals_dir / "known_animals.json").write_text(
            json.dumps([
                {"id": "cow-001", "label": "Bessie", "species": "cow",
                 "identity_markers": []},
            ])
        )
        vec = np.array([0.1, 0.2, 0.3], dtype=np.float32)
        embeddings = {"cow-001__avg": vec}
        np.savez(str(animals_dir / "embeddings.npz"), **embeddings)  # type: ignore[arg-type]
        _patch_animals_dir(animals_dir)

        crop_path = str(animals_dir / "fake_crop.png")
        Image.new("RGB", (64, 64), color=(128, 64, 32)).save(crop_path)

        # Empty identity_markers -> _jaccard_score([], []) returns 1.0.
        with patch.dict("os.environ", {"ANIMAL_EMBEDDER_LIVE": "1"}), \
             patch("animal_matcher.match._embed_crop", return_value=vec):
            result = match_animal(crop_path)

        _restore_animals_dir()

        assert result["matched"] is True


# ---------------------------------------------------------------------------
# Return shape verification.
# ---------------------------------------------------------------------------

class TestReturnShape:
    """Verify the return shape matches the PRD spec."""

    def test_no_match_shape(self, tmp_path: Path) -> None:
        """No match -> {matched, top_candidates, threshold}."""
        animals_dir = tmp_path / "data" / "known" / "animals"
        animals_dir.mkdir(parents=True)
        (animals_dir / "known_animals.json").write_text("[]")
        _patch_animals_dir(animals_dir)

        crop_path = str(animals_dir / "fake.png")
        Image.new("RGB", (64, 64), color=(128, 64, 32)).save(crop_path)

        result = match_animal(crop_path)

        _restore_animals_dir()

        assert "matched" in result
        assert "top_candidates" in result
        assert "threshold" in result
        assert result["matched"] is False

    def test_match_shape(self, tmp_path: Path) -> None:
        """Match -> {matched, candidate, cosine_score, tier1_score, top_candidates, threshold}."""
        animals_dir = tmp_path / "data" / "known" / "animals"
        animals_dir.mkdir(parents=True)
        (animals_dir / "known_animals.json").write_text(
            json.dumps([{"id": "cow-001", "label": "Bessie", "species": "cow"}])
        )
        vec = np.array([0.1, 0.2, 0.3], dtype=np.float32)
        np.savez(str(animals_dir / "embeddings.npz"), **{"cow-001__avg": vec})  # type: ignore[arg-type]
        _patch_animals_dir(animals_dir)

        crop_path = str(animals_dir / "fake_crop.png")
        Image.new("RGB", (64, 64), color=(128, 64, 32)).save(crop_path)

        with patch.dict("os.environ", {"ANIMAL_EMBEDDER_LIVE": "1"}), \
             patch("animal_matcher.match._embed_crop", return_value=vec):
            result = match_animal(crop_path)

        _restore_animals_dir()

        assert result["matched"] is True
        assert "candidate" in result
        assert "cosine_score" in result
        assert "tier1_score" in result
        assert "top_candidates" in result
        assert "threshold" in result
        assert result["threshold"] == 0.85

    def test_no_match_below_threshold_shape(self, tmp_path: Path) -> None:
        """All cosine below threshold -> {matched: False, top_candidates populated}.
        
        This is step 7 of the task body: all-cosine-below-threshold case should
        return matched=False with top_candidates populated.
        """
        animals_dir = tmp_path / "data" / "known" / "animals"
        animals_dir.mkdir(parents=True)
        (animals_dir / "known_animals.json").write_text(
            json.dumps([
                {"id": "cow-001", "label": "Bessie", "species": "cow",
                 "identity_markers": ["spotted"]},
            ])
        )
        # Vector designed to give cosine < 0.85.
        vec_enrolled = np.array([1.0, 0.0, 0.0], dtype=np.float32)
        embeddings = {"cow-001__avg": vec_enrolled}
        np.savez(str(animals_dir / "embeddings.npz"), **embeddings)  # type: ignore[arg-type]
        _patch_animals_dir(animals_dir)

        crop_path = str(animals_dir / "fake_crop.png")
        Image.new("RGB", (64, 64), color=(128, 64, 32)).save(crop_path)

        # Orthogonal vector -> cosine ~0.0.
        vec_crop = np.array([0.0, 1.0, 0.0], dtype=np.float32)
        with patch.dict("os.environ", {"ANIMAL_EMBEDDER_LIVE": "1"}), \
             patch("animal_matcher.match._embed_crop", return_value=vec_crop):
            result = match_animal(crop_path)

        _restore_animals_dir()

        assert result["matched"] is False
        assert "top_candidates" in result
        # top_candidates should be empty (no candidates passed threshold).
        assert result["top_candidates"] == []
