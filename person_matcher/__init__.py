"""person_matcher — pure person matching (tag + CLIP cosine + Jaccard).

No file I/O at module level. No database. No Telegram.
"""

from .config import TIER1_WEIGHTS, TIER2_THRESHOLD, get_tier2_threshold
from .match import match_person
from .storage import cosine_sim, load_known_people, load_person_embeddings

__all__ = [
    "TIER1_WEIGHTS",
    "TIER2_THRESHOLD",
    "cosine_sim",
    "get_tier2_threshold",
    "load_known_people",
    "load_person_embeddings",
    "match_person",
]
