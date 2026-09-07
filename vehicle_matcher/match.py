"""match.py — Pure vehicle matcher: plate match or Jaccard fallback.

STATUS: stable
THREAD SAFETY: thread-safe (pure function, no shared state)

INPUTS:
    - vm2_result: dict (required) -- VM2 analysis output with keys
      'license_plate' (str | None) and 'distinctive_features' (list[str])
    - candidates: list[dict] (required) -- list of known-vehicle dicts,
      each with the same shape as vm2_result

OUTPUTS:
    - return dict: matched candidate dict with extra key 'matched: True',
      or {'matched': False} when no candidate satisfies either criterion

PUBLIC API:
    match_vehicle(vm2_result, candidates) -> dict
        Try license-plate match first, then Jaccard fallback on features.

DOES NOT DO:
    - File I/O or database access
    - Network calls or Telegram messaging
    - Persistence of match results

CALLED BY:
    - listener/pipeline.py stage 9 (match stage, vehicle only)

CALLS INTO:
    - stdlib len(), str.lower(), str.replace()

RELATED:
    - listener/pipeline.py (calls match_vehicle at stage 9)
    - data/vehicles/known_vehicles.json (source of candidates in future story)
"""

from __future__ import annotations


def match_vehicle(vm2_result: dict, candidates: list[dict]) -> dict:
    """Match a VM2 result against known-vehicle candidates.

    License plate match first (exact, case-insensitive, whitespace-stripped).
    If no plate match, fallback to Jaccard similarity on distinctive_features
    with threshold >= 0.5.

    Returns the matched candidate dict with 'matched': True appended, or
    {'matched': False} if nothing matches.
    """
    # --- License plate: exact, case-insensitive, whitespace-stripped ---
    query_plate = vm2_result.get("license_plate")
    if query_plate:
        norm_query = query_plate.strip().lower().replace(" ", "")
        for candidate in candidates:
            cand_plate = candidate.get("license_plate")
            if cand_plate:
                norm_cand = cand_plate.strip().lower().replace(" ", "")
                if norm_query == norm_cand:
                    result = dict(candidate)
                    result["matched"] = True
                    return result

    # --- Fallback: distinctive_features Jaccard >= 0.5 ---
    query_features = set(vm2_result.get("distinctive_features") or [])
    if query_features:
        for candidate in candidates:
            cand_features = set(candidate.get("distinctive_features") or [])
            if cand_features:
                intersection = query_features & cand_features
                union = query_features | cand_features
                jaccard = len(intersection) / len(union) if union else 0.0
                if jaccard >= 0.5:
                    result = dict(candidate)
                    result["matched"] = True
                    return result

    return {"matched": False}
