"""E2E tests for infra.person_embedder -- CLIP-ViT-B/32 wrapper (synthetic mode).

Tests run with PERSON_EMBEDDER_LIVE=0 (default) so no model weights are needed.
"""
from __future__ import annotations

import os
from unittest.mock import patch

# Set required env vars BEFORE importing infra (infra/__init__.py imports
# modules that call _require_env at module level).
os.environ["PERSON_EMBEDDER_LIVE"] = "0"
os.environ.setdefault("MOTION_GATE_NIGHT_SUPPRESS_ENABLED", "0")
os.environ.setdefault("FARMSURV_DATA_DIR", "data")

import numpy as np
import pytest

from infra.person_embedder import (
    _synthetic_embedding,
    embed_person_batch,
    embed_person_crop,
)


class TestSyntheticEmbeddingDeterministic:
    """Test synthetic embedding is deterministic."""

    def test_same_path_produces_same_vector(self):
        """AC: deterministic -- same path always returns the same vector."""
        v1 = _synthetic_embedding("/tmp/test.png")
        v2 = _synthetic_embedding("/tmp/test.png")
        assert np.array_equal(v1, v2)

    def test_different_paths_produce_different_vectors(self):
        """Different paths produce different embeddings."""
        v1 = _synthetic_embedding("/tmp/test_a.png")
        v2 = _synthetic_embedding("/tmp/test_b.png")
        assert not np.array_equal(v1, v2)


class TestSyntheticEmbeddingShape:
    """Test synthetic embedding shape and dtype."""

    def test_shape_is_512(self):
        """AC: embed_person_crop returns shape (512,)."""
        result = embed_person_crop("/tmp/x.png")
        assert result.shape == (512,)

    def test_dtype_is_float32(self):
        """AC: embed_person_crop returns dtype float32."""
        result = embed_person_crop("/tmp/x.png")
        assert result.dtype == np.float32

    def test_values_in_range(self):
        """AC: synthetic values are in [-1, 1]."""
        result = embed_person_crop("/tmp/x.png")
        assert np.all(result >= -1.0)
        assert np.all(result <= 1.0)


class TestEmbedBatch:
    """Test embed_person_batch returns correct shape."""

    def test_batch_shape(self):
        """AC: embed_person_batch returns shape (N, 512)."""
        paths = ["/tmp/a.png", "/tmp/b.png", "/tmp/c.png"]
        result = embed_person_batch(paths)
        assert result.shape == (3, 512)

    def test_batch_dtype(self):
        """AC: embed_person_batch returns dtype float32."""
        paths = ["/tmp/a.png", "/tmp/b.png"]
        result = embed_person_batch(paths)
        assert result.dtype == np.float32

    def test_batch_deterministic(self):
        """AC: batch results are deterministic."""
        paths = ["/tmp/a.png", "/tmp/b.png"]
        v1 = embed_person_batch(paths)
        v2 = embed_person_batch(paths)
        assert np.array_equal(v1, v2)


class TestFailLoud:
    """Test that live mode raises RuntimeError on failure."""

    def test_live_mode_raises_on_load_failure(self):
        """AC: when the model fails, embed_person_crop raises RuntimeError."""
        # Force live mode, then monkeypatch load_clip to raise.
        old_env = os.environ.get("PERSON_EMBEDDER_LIVE")
        try:
            os.environ["PERSON_EMBEDDER_LIVE"] = "1"
            with patch("infra.person_embedder.load_clip", side_effect=RuntimeError("model fail")), \
                 pytest.raises(RuntimeError, match="Failed to embed person crop"):
                embed_person_crop("/tmp/fail.png")
        finally:
            if old_env is not None:
                os.environ["PERSON_EMBEDDER_LIVE"] = old_env
            else:
                os.environ.pop("PERSON_EMBEDDER_LIVE", None)
