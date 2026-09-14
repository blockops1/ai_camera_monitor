"""match.py — Pure vehicle matcher: scored path + plate/Jaccard fallback.

STATUS: stable
THREAD SAFETY: thread-safe (pure functions, no shared state)

INPUTS:
    - vm2_result: dict (required) -- VM2 analysis output with keys
      'make', 'model', 'color', 'body_style_hint', and optionally
      'vehicle_features' (dict with sub-fields)
    - candidates: list[dict] (required) -- list of known-vehicle dicts,
      each with the same shape as vm2_result plus 'id', 'label', 'owner'

OUTPUTS:
    - return dict: matched candidate dict with extra key 'matched: True',
      or {'matched': False} when no candidate satisfies either criterion

PUBLIC API:
    match_vehicle(vm2_result, candidates) -> dict
        Try scored matching (make+model+color+type), then fall back to
        license-plate match and Jaccard feature similarity.

DOES NOT DO:
    - File I/O or database access
    - Network calls or Telegram messaging
    - Persistence of match results

CALLED BY:
    - listener/pipeline.py stage 9 (match stage, vehicle only)

CALLS INTO:
    - stdlib len(), str.lower(), str.replace(), os.environ.get()

RELATED:
    - listener/pipeline.py (calls match_vehicle at stage 9)
    - data/vehicles/known_vehicles.json (source of candidates)
"""

from __future__ import annotations

import logging
import os

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_MATCH_MIN_SCORE = 3.0


def _get_min_score() -> float:
    """Read MATCH_MIN_SCORE from env; default 3.0."""
    val = os.environ.get("MATCH_MIN_SCORE")
    if val is not None:
        try:
            return float(val)
        except ValueError:
            pass
    return _MATCH_MIN_SCORE


# ---------------------------------------------------------------------------
# Color normalization
# ---------------------------------------------------------------------------

_COLOR_NORMALIZATION: dict[str, list[str]] = {
    "blue": ["blue", "navy", "dark blue", "dark_blue",
             "midnight blue", "midnight_blue"],
    "gray": ["gray", "grey", "silver", "charcoal",
             "dark gray", "dark_gray"],
    "white": ["white", "pearl", "cream"],
    "black": ["black", "ebony"],
    "red": ["red", "crimson", "maroon"],
    "green": ["green", "olive", "forest"],
    "brown": ["brown", "tan", "beige"],
}

# Reverse map: variant -> canonical
_NORMALIZATION_REVERSE: dict[str, str] = {}
for _canonical, _variants in _COLOR_NORMALIZATION.items():
    for _v in _variants:
        _NORMALIZATION_REVERSE[_v] = _canonical


def _normalize_color(color: str | None) -> str | None:
    """Normalize a color string to its canonical form.

    Returns None if the input is None or not recognized.
    """
    if color is None:
        return None
    key = color.strip().lower()
    return _NORMALIZATION_REVERSE.get(key)


# ---------------------------------------------------------------------------
# Spec-driven scoring (Pass 1: make+model; Pass 1.7a: feature tie-breaks)
# ---------------------------------------------------------------------------

# Tie-break feature weights - each matching feature adds +1.
_FEATURE_WEIGHTS: list[str] = [
    "wheel_style",
    "wheel_arch",
    "wheel_color",
    "front_grille_style",
    "rear_lights_signature",
]


def _score_candidate(
    sig: dict,
    kv: dict,
    ev: dict | None,
) -> tuple[float, bool]:
    """Compute a v1-style score for a single known-vehicle candidate.

    Scoring follows the matcher_spec passes:
      Pass 1 (make_model, order=1): make+model primary (+3 for model, +2 for
          make_exact_match or make_in_label).  Stops on match if
          no_fallthrough=True.
      Pass 1.7a (vehicle_features_tiebreak, order=5.5): wheel_style,
          wheel_arch, wheel_color, front_grille_style, rear_lights_signature
          (+1 each).
      Pass 3-5 (color_type, colors_alt, type_group_flex): color + body_style
          fallback (returns on match, score=1.0).

    Returns (score, is_exact) where is_exact is True when a zero-score
    "returns_on_match" pass fires (color_type, colors_alt, type_group_flex).
    """
    score: float = 0.0

    sig_make = sig.get("make")
    sig_model = sig.get("model")
    sig_color = sig.get("color")
    sig_body = sig.get("body_style_hint")
    sig_ev = sig.get("vehicle_features") or {}
    kv_make = kv.get("make")
    kv_model = kv.get("model")
    kv_label = kv.get("label", "")
    kv_model_aliases = kv.get("model_aliases") or []
    kv_color = kv.get("color")
    kv_colors_alt = kv.get("colors_alt") or []
    kv_type = kv.get("type")
    kv_body_style_aliases = kv.get("body_style_aliases") or []
    kv_body_style_flex = bool(kv.get("body_style_flex"))

    # --- Pass 1: make_model ---
    if sig_make and sig_model:
        model_scored = False

        # model_substring_bidirectional: check if sig_model is a substring of
        # kv_model or vice-versa (at least 3 chars).
        if _models_overlap(sig_model, kv_model):
            score += 3
            model_scored = True

        # model_aliases_substring
        for alias in kv_model_aliases:
            if _models_overlap(sig_model, alias):
                score += 3
                model_scored = True
                break

        # make_exact_match
        if not model_scored and _make_match(sig_make, kv_make):
            score += 2
            model_scored = True

        # make_in_label
        if not model_scored and _make_in_label(sig_make, kv_label):
            score += 2

        # tie-break: color_match_first
        nc = _normalize_color(sig_color)
        if nc and kv_color and nc == _normalize_color(kv_color):
            score += 1

        if model_scored:
            # After make+model, check feature tie-breaks (Pass 1.7a)
            score += _feature_tiebreak_score(sig_ev, kv)
            return score, False

    # --- Pass 2: make_only (model absent) ---
    if sig_make and not sig_model:
        if _make_in_label(sig_make, kv_label):
            score += 2
        if kv_model and sig_make.lower() in str(kv_model).lower():
            score += 1
        nc = _normalize_color(sig_color)
        if nc and kv_color and nc == _normalize_color(kv_color):
            score += 1
        if score > 0:
            return score, False

    # --- Pass 3: color_type ---
    if (sig_color and sig_body and kv_color and kv_type
            and _normalize_color(sig_color) == _normalize_color(kv_color)
            and sig_body.lower() == kv_type.lower()):
        return 1.0, True

    # --- Pass 4: colors_alt ---
    if sig_color and sig_body and kv_type and kv_color:
        nc = _normalize_color(sig_color)
        if sig_body.lower() == kv_type.lower():
            alt_colors = [str(kv_color).lower()] + [
                c.lower() for c in kv_colors_alt]
            if (str(sig_color).lower() in alt_colors
                    or (nc and nc in alt_colors)):
                return 1.0, True

    # --- Pass 5: type_group_flex ---
    if sig_color and sig_body and kv_type and kv_color:
        nc = _normalize_color(sig_color)
        if sig_body.lower() == kv_type.lower():
            alt_colors = [str(kv_color).lower()] + [
                c.lower() for c in kv_colors_alt]
            if (str(sig_color).lower() in alt_colors
                    or (nc and nc in alt_colors)):
                # After type_group_flex, apply feature tiebreaks
                fb = _feature_tiebreak_score(sig_ev, kv)
                if fb > 0:
                    return 1.0 + fb, True
                return 1.0, True

    # --- Pass 6: body_style_flex ---
    if sig_color and sig_body and kv_body_style_flex and kv_body_style_aliases:
        alt_colors = [str(kv_color or "")] + [c.lower() for c in kv_colors_alt]
        if (sig_body.lower() in [b.lower() for b in kv_body_style_aliases]
                and (str(sig_color).lower() in alt_colors
                     or (_normalize_color(sig_color)
                         and _normalize_color(sig_color) in alt_colors))):
            # Conservative: requires higher score (we have none)
            pass

    # --- Pass 7: type_only (no color) ---
    if (sig_body and not sig_color and kv_type
            and sig_body.lower() == kv_type.lower()):
        return 1.0, True

    return score, False


def _models_overlap(sig_model: str, kv_model: str | None) -> bool:
    """Check if two model strings overlap in both directions (>=3 chars)."""
    if kv_model is None:
        return False
    sig_lower = sig_model.strip().lower()
    kv_lower = str(kv_model).strip().lower()
    if len(sig_lower) < 3 or len(kv_lower) < 3:
        return False
    return sig_lower in kv_lower or kv_lower in sig_lower


def _make_match(sig_make: str, kv_make: str | None) -> bool:
    """Check if signature make matches known-vehicle make (exact)."""
    if not kv_make:
        return False
    return sig_make.strip().lower() == str(kv_make).strip().lower()


def _make_in_label(sig_make: str, kv_label: str) -> bool:
    """Check if signature make appears in the known-vehicle label."""
    if not sig_make or not kv_label:
        return False
    return sig_make.strip().lower() in kv_label.lower()


def _feature_tiebreak_score(sig_ev: dict, kv: dict) -> float:
    """Score feature tie-breaks (Pass 1.7a).

    Checks vehicle_features sub-fields from the signature against the
    known-vehicle's vehicle_features. Each matching field adds +1.

    Fields checked: wheel_style, wheel_arch, wheel_color,
    front_grille_style, rear_lights_signature.
    """
    if not sig_ev:
        return 0.0

    kv_ev = kv.get("vehicle_features") or {}
    score = 0.0

    for field in _FEATURE_WEIGHTS:
        sig_val = sig_ev.get(field)
        kv_val = kv_ev.get(field)
        if sig_val is None or kv_val is None:
            continue
        if str(sig_val).strip().lower() == str(kv_val).strip().lower():
            score += 1

    return score


# ---------------------------------------------------------------------------
# Plate match (exact, case-insensitive, whitespace-stripped)
# ---------------------------------------------------------------------------


def _match_by_plate(sig: dict, candidates: list[dict]) -> dict | None:
    """Try license-plate match first."""
    query_plate = sig.get("license_plate")
    if not query_plate:
        return None
    norm_query = query_plate.strip().lower().replace(" ", "").replace("-", "")
    for candidate in candidates:
        cand_plate = candidate.get("license_plate")
        if cand_plate:
            norm_cand = cand_plate.strip().lower().replace(" ", "").replace("-", "")
            if norm_query == norm_cand:
                result = dict(candidate)
                result["matched"] = True
                return result
    return None


# ---------------------------------------------------------------------------
# Jaccard fallback on distinctive_features
# ---------------------------------------------------------------------------


def _match_by_jaccard(sig: dict, candidates: list[dict]) -> dict | None:
    """Fallback to distinctive_features Jaccard >= 0.5."""
    query_features = set(sig.get("distinctive_features") or [])
    if not query_features:
        return None
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
    return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def match_vehicle(vm2_result: dict, candidates: list[dict]) -> dict:
    """Match a VM2 result against known-vehicle candidates.

    Matching order:
      1. **Scored path** (v1-style): computes make+model+color+type score.
         Returns highest-scoring candidate if score >= MATCH_MIN_SCORE.
      2. **License plate** (exact, case-insensitive, whitespace-stripped).
      3. **Jaccard fallback** on distinctive_features (threshold >= 0.5).

    Steps 2 and 3 are LAST-RESORT FALLBACK when the scored path yields
    nothing above the threshold.

    Returns the matched candidate dict with 'matched': True appended, or
    {'matched': False} if no candidate satisfies any criterion.
    """
    min_score = _get_min_score()

    # --- Scored path (Pass 1 make+model, Pass 1.7a features, Pass 3-5) ---
    best_score = -1.0
    best_candidate = None
    all_scores: list[tuple[str, float]] = []

    for candidate in candidates:
        kv = candidate
        ev = kv.get("vehicle_features") or {}

        score, _ = _score_candidate(vm2_result, kv, ev)
        cid = kv.get("id", str(candidate))
        all_scores.append((cid, score))
        if score > best_score:
            best_score = score
            best_candidate = candidate

    if best_candidate is not None and best_score >= min_score:
        result = dict(best_candidate)
        result["matched"] = True
        result["score"] = best_score
        result["all_scores"] = all_scores
        return result

    # --- Fallback: license plate ---
    plate_result = _match_by_plate(vm2_result, candidates)
    if plate_result:
        plate_result["score"] = plate_result.get("score", 0.0)
        plate_result["all_scores"] = all_scores
        return plate_result

    # --- Fallback: Jaccard ---
    jaccard_result = _match_by_jaccard(vm2_result, candidates)
    if jaccard_result:
        jaccard_result["score"] = jaccard_result.get("score", 0.0)
        jaccard_result["all_scores"] = all_scores
        return jaccard_result

    return {"matched": False}


def score_top_n(
    vm2_result: dict,
    known_vehicles: list[dict],
    n: int = 3,
) -> list[tuple[str, float]]:
    """Return top-N (kv_id, score) tuples sorted desc, regardless of threshold.

    Computes the v1-style _score_candidate score for every known vehicle
    and returns the *n* highest-scoring candidates as ``(id, score)``
    tuples, sorted in descending order by score.

    This is used for no-match alerts so the operator can see which known
    vehicles were closest matches even though none crossed the threshold.

    Args:
        vm2_result: VM2 analysis output (make, model, color, etc.).
        known_vehicles: List of known-vehicle dicts.
        n: Number of top candidates to return (default 3).

    Returns:
        List of (kv_id, score) tuples, length <= n.
    """
    results: list[tuple[str, float]] = []
    for kv in known_vehicles:
        ev = kv.get("vehicle_features") or {}
        score, _ = _score_candidate(vm2_result, kv, ev)
        cid = kv.get("id", str(kv))
        results.append((cid, score))
    results.sort(key=lambda x: x[1], reverse=True)
    return results[:n]
