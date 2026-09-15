"""config.py — Animal-matcher threshold configuration.

STATUS: stable
THREAD SAFETY: immutable constants — fully thread-safe.

INPUTS:
    - Env var ANIMAL_MATCH_THRESHOLD — overrides TIER2_THRESHOLD if set.

OUTPUTS:
    - TIER2_THRESHOLD: float — MegaDescriptor cosine acceptance threshold.
    - TIER1_WEIGHTS: dict — feature-scoring weights (w1 species, w2 size,
      w3 color jaccard, w4 distinctive features jaccard).

PUBLIC API:
    TIER2_THRESHOLD (float)
        Default 0.5. Overridden by ANIMAL_MATCH_THRESHOLD env var.
    TIER1_WEIGHTS (dict)
        Weight dict for Tier-1 scoring. Keys: species, size, color,
        distinctive.

DOES NOT DO:
    - Model loading or inference.
    - File I/O for threshold tuning.
    - Telegram messaging.

CALLED BY:
    - animal_matcher.match.py (cosine threshold filter).

CALLS INTO:
    - os.environ.get.

RELATED:
    - docs/animal-threshold-tuning.md (threshold tuning report).
    - scripts/tune_animal_threshold.py (threshold tuner).
"""
from __future__ import annotations

import os

# ---------------------------------------------------------------------------
# Tier-2: MegaDescriptor cosine threshold.
# ---------------------------------------------------------------------------

# Default 0.5; overridable via ANIMAL_MATCH_THRESHOLD env var.
_DEFAULT_TIER2_THRESHOLD = 0.5


def get_tier2_threshold() -> float:
    """Return the Tier-2 cosine threshold.

    Reads ANIMAL_MATCH_THRESHOLD from env; falls back to 0.5.

    Returns:
        float threshold in [0, 1].
    """
    val = os.environ.get("ANIMAL_MATCH_THRESHOLD")
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
    "species": 1.0,
    "size": 1.0,
    "color": 1.5,
    "distinctive": 1.5,
}

# Cosine weight for combined ranking (tier1 + 5.0 * cosine_score).
COSINE_RANK_WEIGHT: float = 5.0
