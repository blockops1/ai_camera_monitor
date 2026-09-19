"""storage.py — Read/write animal registry + embeddings for Tier-2 cosine matching.

STATUS: stable
THREAD SAFETY: thread-safe (pure functions, no shared mutable state)

INPUTS:
    - data/known/animals/known_animals.json — registry of enrolled animals
    - data/known/animals/embeddings.npz — per-animal average embeddings

OUTPUTS:
    - load_known() -> list[dict] — animal registry entries
    - load_embeddings() -> dict[str, np.ndarray] — animal_id -> avg embedding
    - cosine_sim(a, b) -> float — cosine similarity between two vectors

PUBLIC API:
    load_known() -> list[dict]
        Load the animal registry from known_animals.json.
    load_embeddings() -> dict[str, np.ndarray]
        Load average embeddings from embeddings.npz. Returns empty dict on miss.
    cosine_sim(a: np.ndarray, b: np.ndarray) -> float
        Cosine similarity between two vectors. Returns float in [-1, 1].

DOES NOT DO:
    - Model loading or inference.
    - Image processing.
    - Telegram messaging.

CALLED BY:
    - animal_matcher.match.py: Tier-2 cosine matching logic.

CALLS INTO:
    - json: read known_animals.json.
    - numpy: load .npz, linear algebra for cosine sim.
    - infra.paths: DATA_DIR (fallback to env).

RELATED:
    - scripts/enroll_animal.py: writes known_animals.json + embeddings.npz.
    - animal_matcher/config.py: TIER2_THRESHOLD, TIER1_WEIGHTS.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

import numpy as np

logger = logging.getLogger(__name__)


def _animals_dir() -> Path:
    """Return the data/known/animals/ directory."""
    # Test override: used by unit tests to isolate from real data/.
    if hasattr(_animals_dir, "_test_override"):
        return _animals_dir._test_override  # type: ignore[return-value]
    try:
        from infra.paths import DATA_DIR
        return Path(DATA_DIR) / "known" / "animals"  # type: ignore[no-any-return]
    except ImportError:
        return Path(os.path.expanduser(
            os.environ.get("FARMSURV_DATA_DIR", "~/farm-surveillance-v2/data")
        )) / "known" / "animals"


def load_known() -> list[dict]:
    """Load the animal registry from known_animals.json.

    Returns an empty list if the file is missing or malformed.

    Returns:
        List of animal entry dicts (same schema as enroll_animal registry).
    """
    p = _animals_dir() / "known_animals.json"
    if not p.is_file():
        return []
    try:
        data = json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return []
    if not isinstance(data, list):
        return []
    return data


def load_embeddings() -> dict[str, np.ndarray]:
    """Load average embeddings from embeddings.npz.

    Keys are animal_id strings; values are 768-dim float32 vectors.
    Returns empty dict if the file is missing.

    Returns:
        Dict mapping animal_id -> avg embedding vector.
    """
    p = _animals_dir() / "embeddings.npz"
    if not p.is_file():
        return {}
    try:
        loaded = np.load(str(p), allow_pickle=True)
        return {str(k): loaded[k].astype(np.float32) for k in loaded.files}  # type: ignore[return-value]
    except (OSError, ValueError):
        return {}


def cosine_sim(a: np.ndarray, b: np.ndarray) -> float:
    """Compute cosine similarity between two vectors.

    Args:
        a: 1-D numpy array.
        b: 1-D numpy array, same shape as a.

    Returns:
        Cosine similarity in [-1, 1]. Returns 0.0 if either vector is zero.
    """
    norm_a = np.linalg.norm(a)
    norm_b = np.linalg.norm(b)
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return float(np.dot(a, b) / (norm_a * norm_b))
