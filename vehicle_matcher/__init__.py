"""vehicle_matcher — signature + known vehicles → match verdict.

Pure functions. No vision, no Telegram, no I/O.
"""

from .matcher import (
    MatchVerdict,
    NoMatch,
    match_signature,
    score_top_n,
)
from .scoring import (
    DEFAULT_CONFIDENCE_THRESHOLD,
    DEFAULT_GAP_THRESHOLD,
    ScoringSpec,
    score_color,
    score_feature,
    score_features,
    score_make,
    score_model,
    score_signature_against_known,
    score_type,
)

__all__ = [
    "DEFAULT_CONFIDENCE_THRESHOLD",
    "DEFAULT_GAP_THRESHOLD",
    "MatchVerdict",
    "NoMatch",
    "ScoringSpec",
    "match_signature",
    "score_color",
    "score_feature",
    "score_features",
    "score_make",
    "score_model",
    "score_signature_against_known",
    "score_top_n",
    "score_type",
]
