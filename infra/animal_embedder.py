"""
animal_embedder.py — MegaDescriptor-L-384 wrapper for animal individual
re-identification.

STATUS: stable
THREAD SAFETY: thread-safe (model cached at module level via a lazy loader;
    timm models are read-only after creation, so concurrent embed calls are safe)

INPUTS:
    - env ANIMAL_EMBEDDER_LIVE (default "0") — bool flag, enables live
      MegaDescriptor model loading. When "0" (default), _synthetic_embedding()
      returns a deterministic 768-dim vector seeded by image hash for unit tests.
    - env FARMSURV_DATA_DIR (default ~/farm-surveillance-v2/data) — directory
      where MODELS_DIR lives; model cache target.

OUTPUTS:
    - load_megadescriptor() -> TimmModel — lazy-loads the MegaDescriptor-L-384
      model (hf-hub:BVRA/MegaDescriptor-L-384, num_classes=0) and caches it
      under MODELS_DIR/megadescriptor-l-384. Returns the singleton on
      subsequent calls.
    - embed_image(pil_or_ndarray) -> np.ndarray shape (768,) float32 — runs
      one image through the model, returns the 768-dim embedding vector.
    - embed_batch(images) -> np.ndarray shape (N, 768) — runs a batch of
      PIL images or numpy arrays through the model.

PUBLIC API:
    load_megadescriptor() -> torch.nn.Module
        Lazy-load and cache the MegaDescriptor model. Returns a singleton.
    embed_image(pil_or_ndarray) -> np.ndarray
        Embed a single image; shape (768,) float32.
    embed_batch(images: list[Image.Image | np.ndarray]) -> np.ndarray
        Embed a batch; shape (N, 768) float32.
    _synthetic_embedding(pil_or_ndarray) -> np.ndarray
        Deterministic placeholder for unit tests (no model required).
        Shape (768,) float32.

DOES NOT DO:
    - Download model weights — uses timm's built-in HuggingFace hub loading.
    - Fine-tune or train the model (pretrained weights only).
    - Match animals (see infra.animal_matcher).

CALLED BY:
    - infra.animal_matcher.match.py: Tier-2 embedding cosine matching.
    - listener.pipeline_stage_animal: animal branch embedding step.

CALLS INTO:
    - timm: create_model, data.resolve_model_data_config, create_transform
    - torch: tensor creation, inference, cpu()
    - PIL.Image: image loading/conversion
    - numpy: array operations
    - hashlib: deterministic seed for synthetic embeddings
    - infra.paths: MODELS_DIR (via env var FARMSURV_DATA_DIR)

RELATED:
    - https://huggingface.co/BVRA/MegaDescriptor-L-384
    - infra.animal_prompt.py: VM2 structured output (Tier-1 advisory).
    - scripts/release/config.yaml: strip_paths already exclude *.pt.
"""
from __future__ import annotations

import hashlib
import logging
import os
import time

import numpy as np
import torch
from PIL import Image
from torch import nn

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Lazy model cache — singleton across all calls.
# ---------------------------------------------------------------------------
_model: nn.Module | None = None
_transform = None
_live_mode = False  # True only when ANIMAL_EMBEDDER_LIVE=1


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


def load_megadescriptor() -> nn.Module:
    """Lazy-load the MegaDescriptor-L-384 model and cache it.

    Uses timm to create the model from HuggingFace hub. The model is
    cached at module level so subsequent calls return the same instance.

    Returns:
        The loaded PyTorch model (num_classes=0, no classifier head).
    """
    global _model, _transform

    if _model is not None:
        return _model

    # Respect ANIMAL_EMBEDDER_LIVE env var — unit tests skip live load.
    if os.environ.get("ANIMAL_EMBEDDER_LIVE", "0") != "1":
        logger.warning(
            "ANIMAL_EMBEDDER_LIVE not set; _synthetic_embedding() will be "
            "used for all embed_* calls."
        )
        # Return a dummy so downstream code can still function without
        # the model being loaded (tests don't need it).
        # We set a flag to skip real inference.
        _model = _DummyModel()  # type: ignore[assignment]
        return _model

    import timm
    from timm.data import create_transform, resolve_model_data_config

    logger.info("Loading MegaDescriptor-L-384 from hf-hub:BVRA/MegaDescriptor-L-384 ...")
    t0 = time.time()

    loaded_model: nn.Module = timm.create_model(
        "hf-hub:BVRA/MegaDescriptor-L-384",
        pretrained=True,
        num_classes=0,
    )
    loaded_model.eval()
    loaded_model.requires_grad_(False)

    # Preprocessing config
    data_cfg = resolve_model_data_config(loaded_model)
    _transform = create_transform(**data_cfg)

    elapsed = time.time() - t0
    logger.info("MegaDescriptor-L-384 loaded in %.1fs (%s)", elapsed, _model_dir())

    _model = loaded_model
    _live_mode = True
    return _model


# ---------------------------------------------------------------------------
# Dummy model for synthetic/embedding tests (no weights needed).
# ---------------------------------------------------------------------------
class _DummyModel(nn.Module):  # type: ignore[no-redef]
    """A no-op nn.Module used when ANIMAL_EMBEDDER_LIVE=0."""

    def __init__(self) -> None:
        super().__init__()
        self.num_classes = 0

    def forward(self, x):  # type: ignore[no-untyped-def]
        # Return a fixed 768-dim tensor regardless of input shape.
        batch_size = x.shape[0]
        return torch.zeros(batch_size, 768)


def embed_image(pil_or_ndarray) -> np.ndarray:
    """Embed a single image into a 768-dim vector.

    Args:
        pil_or_ndarray: A PIL.Image or numpy array (H, W, 3) RGB.

    Returns:
        np.ndarray of shape (768,) dtype float32.
    """
    if not _live_mode:
        return _synthetic_embedding(pil_or_ndarray)

    model = load_megadescriptor()
    img = _to_pil(pil_or_ndarray)
    tensor = _transform(img).unsqueeze(0)  # type: ignore[operator]

    with torch.no_grad():
        embedding = model(tensor)

    # Ensure (768,) float32 regardless of device.
    return embedding.squeeze(0).cpu().numpy().astype(np.float32)


def embed_batch(images) -> np.ndarray:
    """Embed a batch of images into N x 768 vectors.

    Args:
        images: List of PIL.Image or numpy arrays (H, W, 3) RGB.

    Returns:
        np.ndarray of shape (N, 768) dtype float32.
    """
    if not _live_mode:
        return np.array([_synthetic_embedding(img) for img in images], dtype=np.float32)

    model = load_megadescriptor()

    tensors = []
    for img in images:
        pil = _to_pil(img)
        tensors.append(_transform(pil))  # type: ignore[operator]
    batch = torch.stack(tensors)

    with torch.no_grad():
        embeddings = model(batch)

    return embeddings.cpu().numpy().astype(np.float32)


# ---------------------------------------------------------------------------
# Synthetic embedding for offline unit tests.
# ---------------------------------------------------------------------------
def _synthetic_embedding(pil_or_ndarray=None) -> np.ndarray:
    """Return a deterministic 768-dim vector seeded by the image content.

    Uses MD5 hash of raw pixel bytes so the same image always produces
    the same embedding, but different images produce different vectors.
    This lets unit tests run without loading any real model weights.

    Args:
        pil_or_ndarray: A PIL.Image or numpy array (H, W, 3) RGB.
            When None, generates a random 384x384 seed for deterministic
            but content-independent embeddings.

    Returns:
        np.ndarray of shape (768,) dtype float32.
    """
    if pil_or_ndarray is None:
        # Default seed: a 384x384 gray image so the hash is always the same.
        pil_or_ndarray = Image.new("RGB", (384, 384), color=(128, 128, 128))
    img = _to_pil(pil_or_ndarray)
    # Hash raw pixel bytes for determinism.
    pixel_bytes = bytes(img.tobytes())
    digest = hashlib.md5(pixel_bytes).digest()

    # Expand 16-byte hash into 768 floats via modular indexing, then
    # normalise to unit-ish scale.
    seed = int.from_bytes(digest, byteorder="big")
    rng = np.random.RandomState(seed & 0xFFFFFFFF)
    vector = rng.randn(768).astype(np.float32)
    # Normalise to unit vector for cosine-similarity compatibility.
    norm = np.linalg.norm(vector)
    if norm > 0:
        vector = vector / norm
    return vector


# ---------------------------------------------------------------------------
# Internal helpers.
# ---------------------------------------------------------------------------
def _to_pil(img) -> Image.Image:
    """Convert PIL.Image or numpy (H,W,3) to RGB PIL.Image."""
    if isinstance(img, Image.Image):
        return img.convert("RGB")
    # numpy array — expect (H, W, 3) uint8.
    import numpy as np
    arr = np.asarray(img)
    if arr.ndim == 2:
        arr = np.stack([arr] * 3, axis=-1)
    return Image.fromarray(arr, mode="RGB")
