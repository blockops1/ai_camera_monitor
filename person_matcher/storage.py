"""storage.py — Read/write person registry + embeddings for Tier-2 cosine matching.

STATUS: stable
THREAD SAFETY: thread-safe (pure functions, no shared mutable state)

INPUTS:
    - data/known/people/known_people.json — registry of enrolled people
    - data/known/people/embeddings.npz — per-person average embeddings

OUTPUTS:
    - load_known_people() -> list[dict] — person registry entries
    - load_person_embeddings() -> dict[str, np.ndarray] — person_id -> avg embedding
    - cosine_sim(a, b) -> float — cosine similarity between two vectors

PUBLIC API:
    load_known_people() -> list[dict]
        Load the person registry from known_people.json.
    load_person_embeddings() -> dict[str, np.ndarray]
        Load average embeddings from embeddings.npz. Returns empty dict on miss.
    cosine_sim(a: np.ndarray, b: np.ndarray) -> float
        Cosine similarity between two vectors. Returns float in [-1, 1].

DOES NOT DO:
    - Model loading or inference.
    - Image processing.
    - Telegram messaging.

CALLED BY:
    - person_matcher.match.py: Tier-2 cosine matching logic.

CALLS INTO:
    - json: read known_people.json.
    - numpy: load .npz, linear algebra for cosine sim.
    - infra.paths: DATA_DIR (fallback to env).

RELATED:
    - scripts/enroll_person.py: writes known_people.json + embeddings.npz.
    - person_matcher/config.py: TIER2_THRESHOLD, TIER1_WEIGHTS.
"""
from __future__ import annotations

import json
import logging
import os
import pickle
from pathlib import Path

import numpy as np

logger = logging.getLogger(__name__)


def _people_dir() -> Path:
    """Return the data/known/people/ directory."""
    # Test override: used by unit tests to isolate from real data/.
    if hasattr(_people_dir, "_test_override"):
        return _people_dir._test_override  # type: ignore[return-value]
    try:
        from infra.paths import DATA_DIR
        return Path(DATA_DIR) / "known" / "people"  # type: ignore[no-any-return]
    except ImportError:
        return Path(os.path.expanduser(
            os.environ.get("FARMSURV_DATA_DIR", "~/farm-surveillance-v2/data")
        )) / "known" / "people"


def load_known_people() -> list[dict]:
    """Load the person registry from known_people.json.

    Returns an empty list if the file is missing or malformed.

    Returns:
        List of person entry dicts (same schema as enroll_person registry).
    """
    p = _people_dir() / "known_people.json"
    if not p.is_file():
        return []
    try:
        data = json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return []
    if not isinstance(data, list):
        return []
    return data


def load_person_embeddings() -> dict[str, np.ndarray]:
    """Load average embeddings from embeddings.npz.

    Keys are person_id strings; values are 512-dim float32 vectors.
    Returns empty dict if the file is missing.

    Returns:
        Dict mapping person_id -> avg embedding vector.
    """
    p = _people_dir() / "embeddings.npz"
    if not p.is_file():
        return {}
    try:
        loaded = np.load(str(p), allow_pickle=True)
        return {str(k): loaded[k].astype(np.float32) for k in loaded.files}  # type: ignore[return-value]
    except (OSError, ValueError, pickle.UnpicklingError):
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
