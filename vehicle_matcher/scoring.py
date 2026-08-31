"""Per-dimension scoring functions for vehicle matching.

A signature is scored against a known_vehicle by summing per-dimension
scores. Each dimension is a pure function: takes (signature_value,
known_value), returns a float.

Pure functions. No I/O. No imports from other domain modules.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Default thresholds. Tunable per-deployment via ScoringSpec.
DEFAULT_CONFIDENCE_THRESHOLD: float = 0.6
DEFAULT_GAP_THRESHOLD: float = 0.15


@dataclass(frozen=True)
class ScoringSpec:
    """Scoring specification for one matcher run.

    Attributes:
        confidence_threshold: Minimum total score to accept a match.
        gap_threshold: Minimum gap between best and second-best match.
            If best < second + gap, no match is returned (ambiguous).
        weights: Per-dimension weight overrides. Keys are dimension
            names; values are floats. None uses the default weight.
    """
    confidence_threshold: float = DEFAULT_CONFIDENCE_THRESHOLD
    gap_threshold: float = DEFAULT_GAP_THRESHOLD
    weights: dict[str, float] = field(default_factory=dict)


# --- Equality-style dimension scorers ---------------------------------------


def _normalize(value: Any) -> str | None:
    """Normalize a value for comparison. None and 'null' → None."""
    if value is None:
        return None
    if isinstance(value, bool):
        # bool is a subclass of int in Python — normalize to "true"/"false"
        # before the int branch fires.
        return "true" if value else "false"
    if isinstance(value, str):
        s = value.strip()
        if s == "" or s.lower() == "null":
            return None
        return s.lower()
    if isinstance(value, (int, float)):
        return str(value)
    return str(value).lower()


def score_color(sig_value: Any, known_value: Any) -> float:
    """Color match: 1.0 exact, 0.0 different, 0.5 either missing."""
    sig = _normalize(sig_value)
    known = _normalize(known_value)
    if sig is None or known is None:
        return 0.5
    if sig == known:
        return 1.0
    return 0.0


def score_make(sig_value: Any, known_value: Any) -> float:
    """Make match: 1.0 exact, 0.0 different, 0.5 either missing."""
    sig = _normalize(sig_value)
    known = _normalize(known_value)
    if sig is None or known is None:
        return 0.5
    if sig == known:
        return 1.0
    return 0.0


def score_type(sig_value: Any, known_value: Any) -> float:
    """Body-style match: 1.0 exact, 0.0 different, 0.5 either missing.

    Body style is a coarse category. Exact match required.
    """
    sig = _normalize(sig_value)
    known = _normalize(known_value)
    if sig is None or known is None:
        return 0.5
    if sig == known:
        return 1.0
    return 0.0


def score_model(sig_value: Any, known_value: Any) -> float:
    """Model match: 1.0 exact, 0.8 substring (e.g. 'F-150' in 'F-150 XLT'),
    0.0 different, 0.5 either missing."""
    sig = _normalize(sig_value)
    known = _normalize(known_value)
    if sig is None or known is None:
        return 0.5
    if sig == known:
        return 1.0
    if sig in known or known in sig:
        return 0.8
    return 0.0


# --- Feature scoring --------------------------------------------------------


def score_feature(
    feature_name: str,
    sig_value: Any,
    known_value: Any,
) -> float:
    """Per-feature match score.

    Default: 1.0 exact match, 0.0 mismatch, 0.5 either missing.

    Some features have bespoke logic (see below).

    Phase.84 (2026-08-16) — Note absence-evidence fix
    (d99a38e6). cab_marker_lights=False in BOTH sig and kv is no
    longer treated as a positive match (was 1.0). Same for bed_cover
    ='none' in both, window_tint='unknown' in both, etc. Sharing a
    default value is the absence of evidence on both sides, not a
    signal of sameness. One side absent and the other present now
    also returns 0.0 instead of "match on absence" — a real absence
    against a real presence is informative, not a match.

    Implementation note: the absence check must come BEFORE the
    equality check, otherwise False == False ("false" == "false")
    would fire a positive match.
    """
    sig = _normalize(sig_value)
    known = _normalize(known_value)

    # Both missing → no signal either way.
    if sig is None and known is None:
        return 0.5
    # One missing → can't verify; neutral score.
    if sig is None or known is None:
        return 0.5
    # Specific feature rules: gate cab_marker_lights + bed_cover on
    # positive presence FIRST (Phase.84 absence-evidence fix).
    if feature_name in ("cab_marker_lights", "bed_cover"):
        absent_values = {"false", "none", "", "null"}
        sig_absent = sig in absent_values
        known_absent = known in absent_values
        if sig_absent or known_absent:
            return 0.0
    # Both present and not in the absence-feature set: exact match.
    if sig == known:
        return 1.0
    return 0.0


def score_features(
    signature: dict[str, Any],
    known_vehicle_features: dict[str, Any],
    feature_keys: list[str] | None = None,
) -> dict[str, float]:
    """Score every overlapping feature between signature and known.

    Args:
        signature: The extracted signature dict.
        known_vehicle_features: The known vehicle's features dict
            (keyed by feature name, e.g. from
            known_vehicles["vehicle_features"]).
        feature_keys: Optional list of feature names to score. None
            means score all keys present in either signature or known.

    Returns:
        Dict mapping feature_name → score (0.0..1.0).
    """
    if not known_vehicle_features:
        return {}

    if feature_keys is None:
        feature_keys = sorted(set(signature) | set(known_vehicle_features))

    out: dict[str, float] = {}
    for key in feature_keys:
        # Only score features the known vehicle cares about.
        if key in known_vehicle_features:
            sig_val = signature.get(key)
            known_val = known_vehicle_features[key]
            out[key] = score_feature(key, sig_val, known_val)
    return out


# --- Aggregate scoring ------------------------------------------------------


def score_signature_against_known(
    signature: dict[str, Any],
    known_vehicle: dict[str, Any],
    spec: ScoringSpec | None = None,
) -> tuple[float, dict[str, float]]:
    """Total score for one signature against one known vehicle.

    Args:
        signature: The signature dict (output of extract_signature).
        known_vehicle: One known vehicle dict from known_vehicles.json.
            Must have keys: id, label, color, type, make, model,
            vehicle_features{}.
        spec: ScoringSpec with weights/thresholds. None = defaults.

    Returns:
        Tuple of (total_score, dimension_breakdowns):
          - total_score: sum of weighted per-dimension scores.
          - breakdowns: {dim_name: score} for every scored dimension.

    Pure function.
    """
    if spec is None:
        spec = ScoringSpec()

    breakdowns: dict[str, float] = {}

    # Identity dimensions.
    breakdowns["color"] = score_color(
        signature.get("color"),
        known_vehicle.get("color"),
    )
    breakdowns["type"] = score_type(
        signature.get("type"),
        known_vehicle.get("type"),
    )
    breakdowns["make"] = score_make(
        signature.get("make"),
        known_vehicle.get("make"),
    )
    breakdowns["model"] = score_model(
        signature.get("model"),
        known_vehicle.get("model"),
    )

    # Feature dimensions.
    feat_scores = score_features(
        signature,
        known_vehicle.get("vehicle_features", {}) or {},
    )
    breakdowns.update(feat_scores)

    # Apply per-dimension weights from spec.
    weighted_total = 0.0
    for dim, score in breakdowns.items():
        weight = spec.weights.get(dim, 1.0)
        weighted_total += score * weight

    return weighted_total, breakdowns
