"""vehicle_matcher — pure vehicle matching (plate + features Jaccard).

No file I/O. No database. No Telegram.
"""

from .match import match_vehicle

__all__ = ["match_vehicle"]
