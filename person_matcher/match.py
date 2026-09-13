"""match.py — Pure person matcher: tag exact + Jaccard fallback.

STATUS: stable
THREAD SAFETY: thread-safe (pure functions, no shared state)

INPUTS:
    - vm2_result: dict (required) -- VM2 detail output with
      'identity_markers' (list[str]) describing the observed person
    - candidates: list[dict] (required) -- list of known-person dicts,
      each with 'name', 'tag', 'identity_markers'

OUTPUTS:
    - return dict: matched candidate dict with extra key 'matched': True,
      or {'matched': False} when no candidate satisfies either criterion

PUBLIC API:
    match_person(vm2_result, candidates) -> dict
        Try tag match (case-insensitive, whitespace-stripped),
        then fall back to Jaccard similarity on identity_markers.

DOES NOT DO:
    - File I/O or database access
    - Network calls or Telegram messaging
    - Persistence of match results

CALLED BY:
    - listener/pipeline.py stage 12 (per-class match, person branch)

CALLS INTO:
    - stdlib len(), str.lower(), str.replace(), set()

RELATED:
    - listener/pipeline.py (calls match_person at stage 12)
    - data/known/people/known_people.json (source of candidates)
"""
from __future__ import annotations

# ---------------------------------------------------------------------------
# Tag match (exact, case-insensitive, whitespace-stripped)
# ---------------------------------------------------------------------------


def _match_by_tag(sig: dict, candidates: list[dict]) -> dict | None:
    """Try tag match first.

    The first identity_marker from the signature is used as the query tag.
    It must exactly match a candidate's 'tag' (case-insensitive,
    whitespace-stripped).
    """
    markers = sig.get("identity_markers") or []
    if not markers:
        return None
    query_tag = markers[0].strip().lower().replace(" ", "")
    for candidate in candidates:
        cand_tag = candidate.get("tag")
        if cand_tag:
            norm_cand = cand_tag.strip().lower().replace(" ", "")
            if query_tag == norm_cand:
                result = dict(candidate)
                result["matched"] = True
                return result
    return None


# ---------------------------------------------------------------------------
# Jaccard fallback on identity_markers
# ---------------------------------------------------------------------------


def _match_by_jaccard(sig: dict, candidates: list[dict]) -> dict | None:
    """Fallback to identity_markers Jaccard >= 0.5."""
    query_markers = set(sig.get("identity_markers") or [])
    if not query_markers:
        return None
    for candidate in candidates:
        cand_markers = set(candidate.get("identity_markers") or [])
        if cand_markers:
            intersection = query_markers & cand_markers
            union = query_markers | cand_markers
            jaccard = len(intersection) / len(union) if union else 0.0
            if jaccard >= 0.5:
                result = dict(candidate)
                result["matched"] = True
                return result
    return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def match_person(vm2_result: dict, candidates: list[dict]) -> dict:
    """Match a VM2 result against known-person candidates.

    Matching order:
      1. **Tag match** — first identity_marker must exactly match a
         candidate's 'tag' (case-insensitive, whitespace-stripped).
      2. **Jaccard fallback** on identity_markers (threshold >= 0.5).

    Returns the matched candidate dict with 'matched': True appended, or
    {'matched': False} if no candidate satisfies any criterion.
    Returns {'matched': False} immediately if identity_markers is absent.
    """
    # Absence of identity_markers -> no match, no raise
    if not vm2_result.get("identity_markers"):
        return {"matched": False}

    # --- Fallback: tag ---
    tag_result = _match_by_tag(vm2_result, candidates)
    if tag_result:
        return tag_result

    # --- Fallback: Jaccard ---
    jaccard_result = _match_by_jaccard(vm2_result, candidates)
    if jaccard_result:
        return jaccard_result

    return {"matched": False}