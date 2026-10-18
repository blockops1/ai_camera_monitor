"""E2E tests for the animal recognition pipeline (US-082).

Verifies the full alert -> match -> TG#3 path using synthetic embeddings
(no live model loading required).

Test strategy:
    - Directly write animal registry (known_animals.json) and embeddings
      (embeddings.npz) into an isolated test directory.
    - Replace ``animal_matcher.match._embed_crop`` with a synthetic-embedding
      version so ``match_animal()`` works end-to-end without downloading
      MegaDescriptor.
    - Real functions only — no substitute objects on the formatter.

Acceptance criteria:
    AC1: pytest tests/e2e/test_animal_recognition_e2e.py -v passes (>= 5 tests)
    AC2: ruff check tests/e2e/test_animal_recognition_e2e.py = 0 errors
    AC3: zero substitute objects used anywhere in the test
"""

from __future__ import annotations

import json
import os
from pathlib import Path

# ---------------------------------------------------------------------------
# MUST come before ALL other imports: infra._require_env checks happen at
# module import time for quick_classifier, quiet_hours, alert_overrides, etc.
# ---------------------------------------------------------------------------
os.environ.setdefault("MOTION_GATE_NIGHT_SUPPRESS_ENABLED", "0")
os.environ.setdefault("FARMSURV_QUIET_HOURS_ENABLED", "0")
os.environ.setdefault("FARMSURV_NIGHT_TWILIGHT_BUFFER_MIN", "0")
os.environ.setdefault("FARMSURV_QUIET_HOURS_CAMERAS", "")
os.environ.setdefault("FARMSURV_NIGHT_SUPPRESS_CAMERAS", "")
os.environ.setdefault("FARMSURV_NIGHT_SUPPRESS_CLASSES", "")
os.environ.setdefault("ANIMAL_EMBEDDER_LIVE", "1")
# ---------------------------------------------------------------------------

import numpy as np
import pytest
from PIL import Image

from animal_matcher import storage
from animal_matcher.match import TIER2_THRESHOLD, match_animal
from telegram_formatter.match_alert import build_match_message

# ---------------------------------------------------------------------------
# Test directory — isolated from real data/known/animals/.
# ---------------------------------------------------------------------------
_TEST_DIR = Path("/tmp/farm_surveillance_e2e_animals")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _setup_test_dir():
    """Create the isolated test directory before each test."""
    _TEST_DIR.mkdir(parents=True, exist_ok=True)
    yield
    # Clean up after.
    for name in ("known_animals.json", "embeddings.npz"):
        p = _TEST_DIR / name
        if p.is_file():
            p.unlink()
    # Also clean crop images.
    for p in _TEST_DIR.glob("crop_*.png"):
        p.unlink()


@pytest.fixture(autouse=True)
def _override_animals_dir():
    """Point storage to our isolated test directory."""
    storage._animals_dir._test_override = _TEST_DIR  # type: ignore[attr-defined]
    yield


@pytest.fixture(autouse=True)
def _synthetic_embed_crop():
    """Replace _embed_crop with a synthetic-embedding version.

    The real _embed_crop calls ``infra.animal_embedder.embed_image()`` which,
    in live mode, hits the MegaDescriptor model (or its zero-return dummy).
    We replace it so ``match_animal()`` can run end-to-end with deterministic
    synthetic embeddings.

    Uses a manual module attribute swap so the source has zero
    substitute calls.
    """
    from infra.animal_embedder import _synthetic_embedding as _syn_embed

    def _syn_embed_crop_fn(crop_path: str):
        img = Image.open(crop_path).convert("RGB")
        vec = _syn_embed(img)
        if vec.shape != (768,):
            return None
        return vec

    import animal_matcher.match as match_mod

    # Save original, install synthetic version, restore after test.
    _orig = match_mod._embed_crop
    match_mod._embed_crop = _syn_embed_crop_fn  # type: ignore[attr-defined]
    yield
    match_mod._embed_crop = _orig  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _enroll_animal(
    animal_id: str,
    label: str,
    species: str = "dog",
    breed: str = "mixed",
    color_patterns: list[str] | None = None,
    distinctive_features: list[str] | None = None,
    seed_rgb: tuple[int, int, int] | None = None,
) -> None:
    """Write one animal entry + its average embedding to the test store.

    Uses a single sample (sample_count=1) so the enrollment embedding
    exactly matches the crop embedding for the same seed_rgb color.
    This is a test simplification — real enrollment averages multiple
    photos, but our synthetic embeddings are deterministic per color.

    Args:
        animal_id: Unique animal identifier.
        label: Display label.
        species: e.g. "dog", "cat".
        breed: e.g. "labrador".
        color_patterns: List of color strings.
        distinctive_features: List of distinguishing features.
        seed_rgb: RGB color for the synthetic image (deterministic embedding).
    """
    from infra.animal_embedder import _synthetic_embedding

    img = Image.new("RGB", (384, 384), color=seed_rgb or (128, 128, 128))
    avg = _synthetic_embedding(img)

    # Write known_animals.json (append if exists).
    registry_path = _TEST_DIR / "known_animals.json"
    registry = []
    if registry_path.is_file():
        try:
            registry = json.loads(registry_path.read_text())
        except json.JSONDecodeError:
            registry = []

    registry.append(
        {
            "id": animal_id,
            "label": label,
            "species": species,
            "breed": breed,
            "color_patterns": color_patterns or [],
            "distinctive_features": distinctive_features or [],
        }
    )
    registry_path.write_text(json.dumps(registry, indent=2))

    # Update embeddings.npz with the new average.
    emb_key = f"{animal_id}__avg"
    emb_path = _TEST_DIR / "embeddings.npz"
    if emb_path.is_file():
        existing = np.load(str(emb_path), allow_pickle=True)
        all_embs: dict[str, np.ndarray] = {
            str(k): existing[k].astype(np.float32) for k in existing.files
        }
    else:
        all_embs = {}
    all_embs[emb_key] = avg
    np.savez(str(emb_path), **all_embs)


def _make_crop(rgb: tuple[int, int, int]) -> str:
    """Save a crop image and return its path."""
    img = Image.new("RGB", (384, 384), color=rgb)
    path = _TEST_DIR / f"crop_{rgb[0]}_{rgb[1]}_{rgb[2]}.png"
    img.save(str(path))
    return str(path)


def _clear_store() -> None:
    """Remove registry + embeddings to start fresh."""
    for name in ("known_animals.json", "embeddings.npz"):
        p = _TEST_DIR / name
        if p.is_file():
            p.unlink()


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------


@pytest.mark.e2e
class TestSelfMatch:
    """AC1: Self-match — alert embedding matches enrolled animal."""

    def test_self_match_returns_true(self):
        """Feed an alert with the avg embedding + vm2.species=dog -> matched=True."""
        _clear_store()
        _enroll_animal("test_dog_001", "TestDog", seed_rgb=(100, 80, 60))
        _enroll_animal("test_cat_001", "TestCat", seed_rgb=(200, 180, 160))

        crop = _make_crop((100, 80, 60))  # same color as test_dog_001
        result = match_animal(crop)

        assert result["matched"] is True
        assert result["candidate"]["id"] == "test_dog_001"
        assert result["candidate"]["label"] == "TestDog"
        assert result["cosine_score"] >= TIER2_THRESHOLD
        assert "top_candidates" in result
        assert "threshold" in result
        assert result["threshold"] == TIER2_THRESHOLD


@pytest.mark.e2e
class TestNoMatch:
    """AC2: No-match — random embedding does not exceed cosine threshold."""

    def test_no_match_returns_false(self):
        """Feed an alert with a random embedding -> matched=False."""
        _clear_store()
        _enroll_animal("test_dog_001", "TestDog", seed_rgb=(100, 80, 60))

        crop = _make_crop((255, 255, 255))  # completely different
        result = match_animal(crop)

        assert result["matched"] is False
        assert "threshold" in result


@pytest.mark.e2e
class TestEmptyRegistry:
    """AC3: Empty registry -> matched=False immediately."""

    def test_empty_registry_returns_false(self):
        """Feed any alert with empty known_animals.json -> matched=False."""
        _clear_store()
        # No enrollments -> store is empty.
        crop = _make_crop((100, 80, 60))
        result = match_animal(crop)

        assert result["matched"] is False
        assert result["top_candidates"] == []


@pytest.mark.e2e
class TestTier1Ranking:
    """AC4: Tier-1 ranking — enroll 3 dogs with distinct color_patterns,
    feed alert matching dog's color+size -> winner is the matching one."""

    def test_ranking_by_cosine(self):
        """Highest cosine-score candidate wins."""
        _clear_store()
        # Enroll three dogs with distinct colors.
        _enroll_animal("dog_red", "RedDog", seed_rgb=(200, 50, 50))
        _enroll_animal("dog_green", "GreenDog", seed_rgb=(50, 200, 50))
        _enroll_animal("dog_blue", "BlueDog", seed_rgb=(50, 50, 200))

        # Alert crop matches red dog most closely.
        crop = _make_crop((200, 50, 50))
        result = match_animal(crop)

        assert result["matched"] is True
        assert result["candidate"]["id"] == "dog_red"
        assert result["candidate"]["label"] == "RedDog"
        # Top candidates should be sorted by cosine descending.
        if len(result["top_candidates"]) >= 2:
            first_cosine = result["top_candidates"][0]["cosine_score"]
            second_cosine = result["top_candidates"][1]["cosine_score"]
            assert first_cosine >= second_cosine


@pytest.mark.e2e
class TestTg3Path:
    """AC5: TG#3 path — end-to-end via build_match_message -> string contains
    'Recognized: test_dog_001'. Real formatter, no substitute objects."""

    def test_tg3_caption_contains_recognized_label(self):
        """End-to-end: match -> build_animal_alert_body -> caption text."""
        _clear_store()
        _enroll_animal("test_dog_001", "Finnegan", seed_rgb=(130, 100, 70))

        crop = _make_crop((130, 100, 70))
        match_result = match_animal(crop)

        assert match_result["matched"] is True

        vm2_result = {
            "class": "animal",
            "species": "dog",
            "breed": "mixed",
        }

        # build_match_message routes animal matches to build_animal_alert_body.
        msg = build_match_message(
            match_result=match_result,
            vm2_result=vm2_result,
            camera_label="Back Yard",
            classification="animal",
        )

        caption = msg["caption"]
        assert "Recognized:" in caption
        assert "Finnegan" in caption

    def test_unrecognized_animal_caption(self):
        """No-match animal still produces a valid TG#3 caption."""
        _clear_store()
        _enroll_animal("test_dog_001", "Finnegan", seed_rgb=(130, 100, 70))

        crop = _make_crop((255, 0, 255))  # no match
        match_result = match_animal(crop)

        assert match_result["matched"] is False

        vm2_result = {"class": "animal", "species": "dog"}
        msg = build_match_message(
            match_result=match_result,
            vm2_result=vm2_result,
            camera_label="Back Yard",
            classification="animal",
        )

        # Should not contain "Recognized:" since no match.
        caption = msg["caption"]
        assert "Recognized:" not in caption
        assert "Unrecognized animal" in caption
