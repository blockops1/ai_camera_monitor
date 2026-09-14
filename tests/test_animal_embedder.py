"""Tests for infra/animal_embedder.py — MegaDescriptor-L-384 wrapper.

AC3: _synthetic_embedding() returns deterministic (768,) float32 vectors.
AC4: Full test suite passes (unit tests only; live integration is opt-in).
"""

from __future__ import annotations

import os

import numpy as np
import pytest
from PIL import Image


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _ensure_offline_mode(monkeypatch):
    """Force offline mode for all tests (no model download)."""
    monkeypatch.setenv("ANIMAL_EMBEDDER_LIVE", "0")
    # Reset module-level cache so each test starts clean.
    import infra.animal_embedder

    infra.animal_embedder._model = None
    infra.animal_embedder._transform = None
    yield


# ---------------------------------------------------------------------------
# AC3: _synthetic_embedding returns deterministic (768,) float32.
# ---------------------------------------------------------------------------
class TestSyntheticEmbedding:
    """AC3: _synthetic_embedding() returns deterministic (768,) float32."""

    def test_ac3_shape_and_dtype(self):
        """AC3: synthetic embedding is shape (768,) dtype float32."""
        from infra.animal_embedder import _synthetic_embedding

        img = Image.new("RGB", (384, 384), color=(128, 64, 32))
        v = _synthetic_embedding(img)

        assert isinstance(v, np.ndarray)
        assert v.shape == (768,), f"Expected shape (768,), got {v.shape}"
        assert v.dtype == np.float32, f"Expected dtype float32, got {v.dtype}"

    def test_deterministic_same_input(self):
        """Same image → same vector (deterministic seed)."""
        from infra.animal_embedder import _synthetic_embedding

        img = Image.new("RGB", (384, 384), color=(255, 0, 0))
        v1 = _synthetic_embedding(img)
        v2 = _synthetic_embedding(img)

        assert np.array_equal(v1, v2), "Same image must produce identical embeddings"

    def test_different_inputs_different_vectors(self):
        """Different images → different vectors."""
        from infra.animal_embedder import _synthetic_embedding

        img_a = Image.new("RGB", (384, 384), color=(255, 0, 0))
        img_b = Image.new("RGB", (384, 384), color=(0, 255, 0))

        v_a = _synthetic_embedding(img_a)
        v_b = _synthetic_embedding(img_b)

        assert not np.array_equal(v_a, v_b), "Different images must produce different embeddings"

    def test_numpy_input(self):
        """Synthetic embedding accepts numpy arrays."""
        from infra.animal_embedder import _synthetic_embedding

        arr = np.random.randint(0, 256, (384, 384, 3), dtype=np.uint8)
        v = _synthetic_embedding(arr)

        assert v.shape == (768,) and v.dtype == np.float32

    def test_unit_norm(self):
        """Synthetic embeddings are unit-normalised (cosine-compatible)."""
        from infra.animal_embedder import _synthetic_embedding

        img = Image.new("RGB", (384, 384), color=(100, 200, 50))
        v = _synthetic_embedding(img)

        norm = np.linalg.norm(v)
        assert abs(norm - 1.0) < 1e-6, f"Expected unit norm, got {norm}"


# ---------------------------------------------------------------------------
# AC4: load_megadescriptor returns a model-like object offline.
# ---------------------------------------------------------------------------
class TestLoadMegaDescriptor:
    """Offline: load_megadescriptor() returns a callable module."""

    def test_returns_callable(self):
        """load_megadescriptor() returns a nn.Module-like object."""
        from infra.animal_embedder import load_megadescriptor

        model = load_megadescriptor()
        assert hasattr(model, "forward")
        assert callable(model.forward)

    def test_singleton(self):
        """Second call returns the same cached object."""
        from infra.animal_embedder import load_megadescriptor

        m1 = load_megadescriptor()
        m2 = load_megadescriptor()

        assert m1 is m2, "load_megadescriptor() must return the singleton"


# ---------------------------------------------------------------------------
# embed_image / embed_batch (offline synthetic path).
# ---------------------------------------------------------------------------
class TestEmbedFunctions:
    """Offline path: embed_image and embed_batch use synthetic embeddings."""

    def test_embed_image_shape(self):
        """embed_image returns (768,) float32."""
        from infra.animal_embedder import embed_image

        img = Image.new("RGB", (384, 384), color=(50, 100, 150))
        v = embed_image(img)

        assert v.shape == (768,) and v.dtype == np.float32

    def test_embed_image_numpy(self):
        """embed_image accepts a numpy array."""
        from infra.animal_embedder import embed_image

        arr = np.zeros((384, 384, 3), dtype=np.uint8)
        v = embed_image(arr)

        assert v.shape == (768,) and v.dtype == np.float32

    def test_embed_batch_shape(self):
        """embed_batch returns (N, 768) float32."""
        from infra.animal_embedder import embed_batch

        images = [Image.new("RGB", (384, 384), color=(i, i, i)) for i in range(5)]
        batch = embed_batch(images)

        assert batch.shape == (5, 768) and batch.dtype == np.float32

    def test_embed_batch_deterministic(self):
        """Same batch → same results."""
        from infra.animal_embedder import embed_batch

        images = [Image.new("RGB", (384, 384), color=(42, 42, 42)) for _ in range(3)]

        b1 = embed_batch(images)
        b2 = embed_batch(images)

        assert np.array_equal(b1, b2), "Same inputs must produce identical batches"


# ---------------------------------------------------------------------------
# Live integration (opt-in via ANIMAL_EMBEDDER_LIVE=1).
# ---------------------------------------------------------------------------
class TestLiveIntegration:
    """Live integration tests — skipped unless ANIMAL_EMBEDDER_LIVE=1."""

    def test_live_embedding_runs(self):
        """Live: embed through real MegaDescriptor model."""
        live = os.environ.get("ANIMAL_EMBEDDER_LIVE", "0")
        if live != "1":
            pytest.skip("ANIMAL_EMBEDDER_LIVE=1 not set")

        from infra.animal_embedder import embed_image

        img = Image.new("RGB", (384, 384), color=(128, 64, 32))
        v = embed_image(img)

        assert v.shape == (768,) and v.dtype == np.float32
