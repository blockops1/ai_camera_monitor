"""person_embedder.py -- CLIP-ViT-B/32 wrapper for person re-ID.

STATUS: stable
THREAD SAFETY: thread-safe (model cached at module level via a lazy loader;
    torch models are read-only after creation, so concurrent embed calls are safe)

LOADER CHOICE: transformers (CLIP-ViT-B/32, openai)
    CLIP-ViT-B/32 from openai produces 512-dim embeddings suitable
    for re-ID at small scale.

INPUTS:
    - env PERSON_EMBEDDER_LIVE (default "0") -- bool flag, enables live
      model loading. When "0" (default), returns a deterministic 512-dim
      vector seeded by image hash for unit tests.
    - env FARMSURV_DATA_DIR (default ~/farm-surveillance-v2/data) -- directory
      where MODELS_DIR lives; model cache target.

OUTPUTS:
    - embed_person_crop(crop_path) -> np.ndarray shape (512,) float32
    - embed_person_batch(crop_paths) -> np.ndarray shape (N, 512) float32

PUBLIC API:
    load_clip() -> torch.nn.Module
        Lazy-load and cache the CLIP-ViT-B/32 model. Returns a singleton.
    embed_person_crop(crop_path: str) -> np.ndarray
        Embed a single image from file path; shape (512,) float32.
    embed_person_batch(crop_paths: list[str]) -> np.ndarray
        Embed multiple images; shape (N, 512) float32.
    _synthetic_embedding(path: str) -> np.ndarray
        Deterministic placeholder for unit tests (no model required).
        Shape (512,) float32.

DOES NOT DO:
    - Download model weights -- uses timm's built-in HuggingFace hub loading.
    - Fine-tune or train the model (pretrained weights only).
    - Match people (see infra.person_matcher).

CALLED BY:
    - infra.person_matcher.match.py: Tier-2 embedding cosine matching.
    - listener.pipeline_stage_person: person branch embedding step.

CALLS INTO:
    - transformers: CLIPModel, CLIPProcessor (from_pretrained)
    - torch: tensor creation, inference, cpu()
    - PIL.Image: image loading/conversion
    - numpy: array operations
    - hashlib: deterministic seed for synthetic embeddings
    - functools: lru_cache
    - infra.paths: MODELS_DIR (via env var FARMSURV_DATA_DIR)

RELATED:
    - https://huggingface.co/openai/clip-vit-base-patch32
    - scripts/enroll_person.py: enrollment CLI.
"""
from __future__ import annotations

import hashlib
import logging
import os
import time
from functools import lru_cache

import numpy as np
import torch
from PIL import Image
from torch import nn

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Lazy model cache -- singleton across all calls.
# ---------------------------------------------------------------------------
_model: nn.Module | None = None
_transform = None
_live_mode = False  # True only when PERSON_EMBEDDER_LIVE=1


def _get_data_dir() -> str:
    """Return the project data directory (from infra.paths or env fallback)."""
    try:
        from infra.paths import DATA_DIR
        return DATA_DIR  # type: ignore[no-any-return]
    except ImportError:
        return os.path.expanduser(os.environ.get("FARMSURV_DATA_DIR", "~/farm-surveillance-v2/data"))


def _model_dir() -> str:
    """Return the models subdirectory under the data directory."""
    return os.path.join(_get_data_dir(), "models")


def load_clip() -> nn.Module:
    """Lazy-load the CLIP-ViT-B/32 model and cache it.

    Uses timm to create the model from HuggingFace hub. The model is
    cached at module level so subsequent calls return the same instance.

    Returns:
        The loaded PyTorch model (num_classes=0, no classifier head).
    """
    global _model, _transform

    if _model is not None:
        return _model

    # Respect PERSON_EMBEDDER_LIVE env var -- unit tests skip live load.
    if os.environ.get("PERSON_EMBEDDER_LIVE", "0") != "1":
        logger.warning(
            "PERSON_EMBEDDER_LIVE not set; _synthetic_embedding() will be "
            "used for all embed_* calls."
        )
        _model = _DummyModel()  # type: ignore[assignment]
        return _model

    from transformers import CLIPModel, CLIPProcessor

    logger.info("Loading CLIP-ViT-B/32 from hf-hub:openai/clip-vit-base-patch32 ...")
    t0 = time.time()

    loaded_model: nn.Module = CLIPModel.from_pretrained("openai/clip-vit-base-patch32")
    loaded_model.eval()
    loaded_model.requires_grad_(False)

    _transform = CLIPProcessor.from_pretrained("openai/clip-vit-base-patch32")

    elapsed = time.time() - t0
    logger.info("CLIP-ViT-B/32 loaded in %.1fs (%s)", elapsed, _model_dir())

    _model = loaded_model
    _live_mode = True
    return _model


# ---------------------------------------------------------------------------
# Dummy model for synthetic/embedding tests (no weights needed).
# ---------------------------------------------------------------------------
class _DummyModel(nn.Module):  # type: ignore[no-redef]
    """A no-op nn.Module used when PERSON_EMBEDDER_LIVE=0."""

    def __init__(self) -> None:
        super().__init__()
        self.num_classes = 0

    def forward(self, x):  # type: ignore[no-untyped-def]
        # Return a fixed 512-dim tensor regardless of input shape.
        batch_size = x.shape[0]
        return torch.zeros(batch_size, 512)


@lru_cache(maxsize=128)
def embed_person_crop(crop_path: str) -> np.ndarray:
    """Embed a single person crop from file path into a 512-dim vector.

    When PERSON_EMBEDDER_LIVE=1, loads CLIP-ViT-B/32 and runs inference.
    When PERSON_EMBEDDER_LIVE=0 (default), returns a deterministic
    SHA256-seeded 512-dim vector.

    Args:
        crop_path: File path to a PNG/JPG image.

    Returns:
        np.ndarray of shape (512,) dtype float32.

    Raises:
        RuntimeError: if the model fails to load (fail-loud).
    """
    # Respect PERSON_EMBEDDER_LIVE env var -- tests skip live load.
    if os.environ.get("PERSON_EMBEDDER_LIVE", "0") != "1":
        return _synthetic_embedding(crop_path)

    try:
        model = load_clip()
        img = Image.open(crop_path).convert("RGB")
        # CLIPProcessor returns {"pixel_values": tensor}, not a raw tensor.
        inputs = _transform(images=img, return_tensors="pt")  # type: ignore[operator]
        tensor = inputs["pixel_values"]

        with torch.no_grad():
            embedding = model.get_image_features(tensor)

        return embedding.squeeze(0).cpu().numpy().astype(np.float32)
    except Exception as exc:
        raise RuntimeError(f"Failed to embed person crop {crop_path}: {exc}") from exc


def embed_person_batch(crop_paths: list[str]) -> np.ndarray:
    """Embed a batch of person crops into N x 512 vectors.

    Args:
        crop_paths: List of file paths to PNG/JPG images.

    Returns:
        np.ndarray of shape (N, 512) dtype float32.

    Raises:
        RuntimeError: if the model fails to load (fail-loud).
    """
    if os.environ.get("PERSON_EMBEDDER_LIVE", "0") != "1":
        return np.array([_synthetic_embedding(p) for p in crop_paths], dtype=np.float32)

    try:
        model = load_clip()
        imgs = [Image.open(path).convert("RGB") for path in crop_paths]
        # CLIPProcessor handles batches natively.
        inputs = _transform(images=imgs, return_tensors="pt")  # type: ignore[operator]
        batch = inputs["pixel_values"]

        with torch.no_grad():
            embeddings = model.get_image_features(batch)

        return embeddings.cpu().numpy().astype(np.float32)
    except Exception as exc:
        raise RuntimeError(f"Failed to embed person batch: {exc}") from exc


# ---------------------------------------------------------------------------
# Synthetic embedding for offline unit tests.
# ---------------------------------------------------------------------------
def _synthetic_embedding(path: str) -> np.ndarray:
    """Return a deterministic 512-dim vector seeded by the file path.

    Uses SHA256 hash of the path string, then extracts the first 512 bits
    as float32 values in [-1, 1]. This lets unit tests run without loading
    any real model weights.

    Args:
        path: File path string to hash.

    Returns:
        np.ndarray of shape (512,) dtype float32 in [-1, 1].
    """
    digest = hashlib.sha256(path.encode()).digest()
    # Expand 32-byte SHA256 into 512 bits, map to [-1, 1] as float32.
    bits = np.unpackbits(np.frombuffer(digest, dtype=np.uint8)).astype(np.float32)
    # We get 256 bits from 32 bytes; double it to reach 512.
    if len(bits) < 512:
        bits = np.concatenate([bits, bits[:512 - len(bits)]])
    vector = (bits[:512] * 2 - 1).astype(np.float32)
    return vector
