"""config.py — Person-matcher threshold configuration.

STATUS: stable
THREAD SAFETY: immutable constants — fully thread-safe.

INPUTS:
    - Env var PERSON_MATCH_THRESHOLD — overrides TIER2_THRESHOLD if set.

OUTPUTS:
    - TIER2_THRESHOLD: float — CLIP-ViT-B/32 cosine acceptance threshold.
    - TIER1_WEIGHTS: dict — feature-scoring weights (identity_markers Jaccard).

PUBLIC API:
    TIER2_THRESHOLD (float)
        Default 0.85. Overridden by PERSON_MATCH_THRESHOLD env var.
    TIER1_WEIGHTS (dict)
        Weight dict for Tier-1 scoring. Keys: identity_markers.
    get_tier2_threshold() -> float

DOES NOT DO:
    - Model loading or inference.
    - File I/O for threshold tuning.
    - Telegram messaging.

CALLED BY:
    - person_matcher.match.py (cosine threshold filter).

CALLS INTO:
    - os.environ.get.

RELATED:
    - docs/animal-threshold-tuning.md (threshold tuning report).
"""
from __future__ import annotations

import os

# ---------------------------------------------------------------------------
# Tier-2: CLIP-ViT-B/32 cosine threshold.
# ---------------------------------------------------------------------------

# Default 0.85; overridable via PERSON_MATCH_THRESHOLD env var.
_DEFAULT_TIER2_THRESHOLD = 0.85

# Module-level constant (matches animal_matcher pattern).
# The actual value may differ if PERSON_MATCH_THRESHOLD is set.
TIER2_THRESHOLD: float = _DEFAULT_TIER2_THRESHOLD


def get_tier2_threshold() -> float:
    """Return the Tier-2 cosine threshold.

    Reads PERSON_MATCH_THRESHOLD from env; falls back to 0.85.

    Returns:
        float threshold in [0, 1].
    """
    val = os.environ.get("PERSON_MATCH_THRESHOLD")
    if val is not None:
        try:
            return float(val)
        except ValueError:
            pass
    return float(_DEFAULT_TIER2_THRESHOLD)


# ---------------------------------------------------------------------------
# Tier-1: Feature-scoring weights.
# ---------------------------------------------------------------------------

TIER1_WEIGHTS: dict[str, float] = {
    "identity_markers": 1.0,
}

# Cosine weight for combined ranking (tier1 + 5.0 * cosine_score).
COSINE_RANK_WEIGHT: float = 5.0
