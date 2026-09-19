"""Tests for animal_matcher/match.py — Tier-2 cosine + Tier-1 ranking matcher.

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
        """No known animals → {matched: False, top_candidates: [], threshold: 0.5}."""
        animals_dir = tmp_path / "data" / "known" / "animals"
        animals_dir.mkdir(parents=True)
        (animals_dir / "known_animals.json").write_text("[]")
        _patch_animals_dir(animals_dir)

        vm2_result = {"crop_a_path": "/tmp/fake_crop.png", "class": "cow"}
        result = match_animal(vm2_result, [])

        _restore_animals_dir()

        assert result["matched"] is False
        assert result["top_candidates"] == []
        assert isinstance(result["threshold"], float)

    def test_crop_file_missing(self, tmp_path: Path) -> None:
        """Crop file not found → {matched: False}."""
        animals_dir = tmp_path / "data" / "known" / "animals"
        animals_dir.mkdir(parents=True)
        (animals_dir / "known_animals.json").write_text(
            json.dumps([{"id": "cow-001", "label": "Bessie", "species": "cow"}])
        )
        _patch_animals_dir(animals_dir)

        vm2_result = {"crop_a_path": "/tmp/nonexistent_crop.png", "class": "cow"}
        result = match_animal(vm2_result, [])

        _restore_animals_dir()

        assert result["matched"] is False


# ---------------------------------------------------------------------------
# Tier-2: cosine matching below threshold.
# ---------------------------------------------------------------------------

class TestCosineBelowThreshold:
    """Tier-2: candidates exist but cosine is below threshold."""

    def test_below_threshold_no_match(self, tmp_path: Path) -> None:
        """Cosine below threshold → {matched: False, top_candidates: []}."""
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

        vm2_result = {"crop_a_path": crop_path, "class": "cow"}

        # Mock _embed_crop to return a vector orthogonal to enrolled vec,
        # ensuring cosine is well below the 0.5 threshold.
        vec_crop = np.array([0.9, 0.9, 0.9], dtype=np.float32)
        with patch("animal_matcher.match._embed_crop", return_value=vec_crop):
            result = match_animal(vm2_result, [])

        _restore_animals_dir()

        assert result["matched"] is False
        assert "top_candidates" in result
        assert "threshold" in result


# ---------------------------------------------------------------------------
# Tier-2: cosine matching above threshold.
# ---------------------------------------------------------------------------

class TestCosineAboveThreshold:
    """Tier-2: candidate above threshold → match."""

    def test_match_found(self, tmp_path: Path) -> None:
        """Cosine above threshold → {matched: True, candidate, cosine_score, tier1_score}."""
        animals_dir = tmp_path / "data" / "known" / "animals"
        animals_dir.mkdir(parents=True)
        (animals_dir / "known_animals.json").write_text(
            json.dumps([
                {"id": "cow-001", "label": "Bessie", "species": "cow",
                 "color_patterns": ["brown", "white"],
                 "distinctive_features": ["horned"]},
            ])
        )
        # Identical vector → cosine = 1.0 (above any threshold).
        vec = np.array([0.1, 0.2, 0.3], dtype=np.float32)
        embeddings = {"cow-001__avg": vec}
        np.savez(str(animals_dir / "embeddings.npz"), **embeddings)  # type: ignore[arg-type]
        _patch_animals_dir(animals_dir)

        # Create a fake crop file.
        crop_path = str(animals_dir / "fake_crop.png")
        Image.new("RGB", (64, 64), color=(128, 64, 32)).save(crop_path)

        vm2_result = {"crop_a_path": crop_path, "class": "cow",
                       "colors": ["brown"], "distinctive_features": ["horned"]}

        # Mock _embed_crop to return the same vector as the enrollment,
        # so cosine similarity = 1.0 (guaranteed match).
        with patch("animal_matcher.match._embed_crop", return_value=vec):
            result = match_animal(vm2_result, [])

        _restore_animals_dir()

        assert result["matched"] is True
        assert "candidate" in result
        assert result["candidate"]["id"] == "cow-001"
        assert "cosine_score" in result
        assert "tier1_score" in result
        assert "threshold" in result

    def test_match_with_runner_ups(self, tmp_path: Path) -> None:
        """Multiple candidates above threshold → runner_ups populated."""
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

        vm2_result = {"crop_a_path": crop_path, "class": "cow"}

        # Mock _embed_crop to return Bessie's exact vector (cosine=1.0).
        with patch("animal_matcher.match._embed_crop", return_value=vec_bessie):
            result = match_animal(vm2_result, [])

        _restore_animals_dir()

        assert result["matched"] is True
        assert "runner_ups" in result
        assert len(result["runner_ups"]) >= 1
        assert result["runner_ups"][0]["label"] == "Daisy"


# ---------------------------------------------------------------------------
# Tier-1: feature scoring.
# ---------------------------------------------------------------------------

class TestTier1Scoring:
    """Tier-1: species, color, distinctive features scoring."""

    def test_species_match_boosts_score(self, tmp_path: Path) -> None:
        """VM2 class='cow' + candidate species='cow' → species score added."""
        from animal_matcher.match import _tier1_score

        candidate = {"id": "cow-001", "label": "Bessie", "species": "cow"}
        vm2_with_class = {"class": "cow", "crop_a_path": "/tmp/x.png"}
        vm2_no_class = {"crop_a_path": "/tmp/x.png"}

        score_with = _tier1_score(candidate, vm2_with_class)
        score_without = _tier1_score(candidate, vm2_no_class)

        # Species match should add weight (1.0).
        assert score_with > score_without, "Species match should boost score"


# ---------------------------------------------------------------------------
# Return shape verification.
# ---------------------------------------------------------------------------

class TestReturnShape:
    """Verify the return shape matches the PRD spec."""

    def test_no_match_shape(self, tmp_path: Path) -> None:
        """No match → {matched, top_candidates, threshold}."""
        animals_dir = tmp_path / "data" / "known" / "animals"
        animals_dir.mkdir(parents=True)
        (animals_dir / "known_animals.json").write_text("[]")
        _patch_animals_dir(animals_dir)

        result = match_animal({"crop_a_path": "/tmp/x.png", "class": "cow"}, [])

        _restore_animals_dir()

        assert "matched" in result
        assert "top_candidates" in result
        assert "threshold" in result
        assert result["matched"] is False

    def test_match_shape(self, tmp_path: Path) -> None:
        """Match → {matched, candidate, cosine_score, tier1_score, top_candidates, threshold}."""
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

        vm2_result = {"crop_a_path": crop_path, "class": "cow",
                       "colors": ["brown"], "distinctive_features": ["horned"]}

        # Mock _embed_crop to return the same vector as the enrollment.
        with patch("animal_matcher.match._embed_crop", return_value=vec):
            result = match_animal(vm2_result, [])

        _restore_animals_dir()

        assert result["matched"] is True
        assert "candidate" in result
        assert "cosine_score" in result
        assert "tier1_score" in result
        assert "top_candidates" in result
        assert "threshold" in result
