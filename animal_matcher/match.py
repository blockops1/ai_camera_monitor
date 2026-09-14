"""match.py — Stub animal matcher placeholder (US-045a).

STATUS: stub
THREAD SAFETY: thread-safe (pure function, no shared state)

INPUTS:
    - vm2_result: dict (required) -- VM2 animal analysis output
    - candidates: list[dict] (required) -- list of known-animal dicts

OUTPUTS:
    - return dict: {'matched': False} — stub; real matching lands in US-045d

PUBLIC API:
    match_animal(vm2_result, candidates) -> dict
        Placeholder returning {'matched': False}.
        Real implementation (Tier-2 MegaDescriptor on every alert) is US-045d.

DOES NOT DO:
    - File I/O or database access
    - Network calls or Telegram messaging
    - Persistence of match results

CALLED BY:
    - listener/pipeline.py stage 12 (per-class match, animal branch)

CALLS INTO:
    - stdlib only

RELATED:
    - listener/pipeline.py (calls match_animal at stage 12)
    - US-045d: real animal body-recognition matcher (MegaDescriptor always)
"""
from __future__ import annotations


def match_animal(vm2_result: dict, candidates: list[dict]) -> dict:
    """Stub: always return no-match.

    Real matching logic (MegaDescriptor on every animal alert) is
    implemented in US-045d.

    Args:
        vm2_result: VM2 analysis output for animal classification.
        candidates: List of known-animal dicts.

    Returns:
        {'matched': False} always.
    """
    return {"matched": False}
