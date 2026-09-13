"""person_matcher — pure person matching (tag + features Jaccard).

No file I/O. No database. No Telegram.
"""

from .match import match_person

__all__ = ["match_person"]
