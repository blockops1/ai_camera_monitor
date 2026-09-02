"""
matcher_spec.py — Vehicle matcher spec (data + YAML loader).

STATUS: stable (extracted from infra/vehicle_matcher.py 2026-08-13 as part
    of the Q1 module-purity split — vehicle_matcher's 5-job violation
    was decomposed into matcher_spec + matcher_scoring + vehicle_matcher)
THREAD SAFETY: thread-safe for read-only spec interpretation

INPUTS:
    - function arg spec_path: Path | None — optional yaml path override
    - file data/vehicle_matcher_spec.yaml (optional, falls back to
      embedded DEFAULT_SPEC if absent or malformed)

OUTPUTS:
    - load_spec() -> dict — the spec, never raises on missing file

PUBLIC API:
    DEFAULT_SPEC — embedded default spec dict (Phase 6B.26a)
    load_spec(spec_path: Path | None = None) -> dict

DOES NOT DO:
    - Match signatures — infra.matcher_scoring matches once it's
      given a spec
    - Interpret passes — that was an older 6B.26a serial-gate path
      that is no longer the production matcher (see infra.matcher_scoring)
    - Persist counters — infra.matcher_telemetry owns that

WHY HERE:
    Separating the spec (data + I/O) from the scoring engine
    (functions that consume the spec) makes both sides independently
    testable. The spec dict shape is the source-of-truth contract;
    changing it requires updating only infra.matcher_scoring's
    dimension functions (which read spec keys) — no need to touch
    any orchestrator code.

CALLED BY:
    - infra.matcher_scoring: match_vehicle_scored() (via score_vehicle
      and _dim_* functions, which read spec keys)
    - infra.vehicle_matcher: score_top_n(), match_vehicle_scored() both
      call load_spec() when no spec is passed in
    - listener.listener: imports load_spec for the no-match Telegram
      path (line 3165 comment)

CALLS INTO:
    - yaml: spec parsing (PyYAML — pyyaml is a project dep)

RELATED:
    - data/vehicle_matcher_spec.yaml — the matching rules (optional;
      load_spec defaults to DEFAULT_SPEC if file absent)
    - infra.matcher_scoring — consumes this spec's keys
    - PHASE6B29-PRD-scored-matcher.md — documents every spec key
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# DEFAULT_SPEC
# ---------------------------------------------------------------------------

DEFAULT_SPEC: dict[str, Any] = {
    "version": 1,

    # TYPE_GROUP strategy — matches within a group, never across groups.
    # sedan/coupe/hatchback are SEPARATE from vehicle deliberately (the
    # 2026-07-21 lesson: better unknown than wrong).
    "type_groups": {
        "vehicle":    ["pickup", "suv", "truck", "van"],
        "sedan":      ["sedan", "coupe", "hatchback"],
        "motorcycle": ["motorcycle"],
        # Add new groups here as needed.
    },

    # Color normalization: applied to BOTH sig.color and kv.color before
    # any comparison. Each key is the canonical color; the list is
    # alternate spellings/variants that normalize to it.
    "color_normalization": {
        "blue":   ["blue", "navy", "dark blue", "dark_blue", "midnight blue", "midnight_blue"],
        "gray":   ["gray", "grey", "silver", "charcoal", "dark gray", "dark_gray"],
        "white":  ["white", "pearl", "cream"],
        "black":  ["black", "ebony"],
        "red":    ["red", "crimson", "maroon"],
        "green":  ["green", "olive", "forest"],
        "brown":  ["brown", "tan", "beige"],
    },

    # Pass definitions. Order matters — interpreter walks in spec order.
    # Pass 1.7a is a SUB-PASS of 1.7 (the soft-signal tie-break); modeled
    # here as a separate entry but its 'fires_after' makes the dependency
    # explicit.
    "passes": [
        {
            "name": "make_model",
            "order": 1,
            "fires_when": {"sig_make": "present", "sig_model": "present"},
            "scoring": [
                # Points are added cumulatively; highest-score wins.
                {"condition": "model_substring_bidirectional", "points": 3, "against": "kv.model"},
                {"condition": "model_aliases_substring",       "points": 3, "against": "kv.model_aliases"},
                {"condition": "make_exact_match",              "points": 2, "against": "kv.make"},
                {"condition": "make_in_label",                 "points": 2, "against": "kv.label"},
            ],
            "tie_break": "color_match_first",
            "no_fallthrough": True,  # The 2026-07-21 no-fallthrough rule.
        },
        {
            "name": "make_only",
            "order": 2,
            "fires_when": {"sig_make": "present", "sig_model": "absent"},
            "scoring": [
                {"condition": "make_in_label",  "points": 2},
                {"condition": "model_in_label", "points": 1},
                {"condition": "color_matches",  "points": 1},
            ],
            "tie_break": "single_top_score_required",
            "falls_through_to": "color_type",  # When multiple tie, NOT return None.
        },
        {
            "name": "color_type",
            "order": 3,
            "fires_when": {"color": "present", "vtype": "present"},
            "match_condition": {"color": "kv.color", "vtype": "kv.type"},
            "list_order_tiebreak": True,
            "returns_on_match": True,
        },
        {
            "name": "colors_alt",
            "order": 4,
            "fires_when": {"color": "present", "vtype": "present"},
            "match_condition": {"vtype": "kv.type", "color_in": "kv.colors_alt"},
            "list_order_tiebreak": True,
            "returns_on_match": True,
        },
        {
            "name": "type_group_flex",
            "order": 5,
            "fires_when": {"color": "present", "vtype": "present"},
            "requires_group": "vehicle",
            "match_condition": {
                "group_eq": "TYPE_GROUP[vtype]",
                "color_in": ["kv.color", "kv.colors_alt"],
            },
            "on_multiple_candidates": "tiebreak_or_pick_first",
            "tiebreak_pass": "vehicle_features_tiebreak",
            "returns_on_match": True,
        },
        {
            "name": "vehicle_features_tiebreak",
            "order": 5.5,
            "fires_after": "type_group_flex",
            "scoring": [
                {"condition": "ev_wheel_signature",    "points": 1},
                {"condition": "ev_front_grille_blank", "points": 1},
                {"condition": "ev_rear_light_bar",     "points": 1},
            ],
            "conservative": "single_higher_score_required",
            "returns_on_match": True,
        },
        {
            "name": "body_style_flex",
            "order": 6,
            "fires_when": {
                "color": "present",
                "vtype": "in_kv.body_style_aliases",
                "kv.body_style_flex": True,
            },
            "match_condition": {
                "color_in": ["kv.color", "kv.colors_alt"],
                "vtype_in": "kv.body_style_aliases",
            },
            "conservative": {
                "requires_opt_in": "kv.body_style_flex",
                "requires_aliases_present": "kv.body_style_aliases",
                "on_multiple": "return_none",
            },
            "returns_on_match": True,
        },
        {
            "name": "type_only",
            "order": 7,
            "fires_when": {"vtype": "present", "sig_color": "unknown_or_null"},
            "match_condition": {"vtype": "kv.type", "kv_color": "null_or_unknown"},
            "list_order_tiebreak": True,
            "returns_on_match": True,
        },
    ],

    # Cross-cutting guardrails (declared once, not per-pass).
    "guards": {
        "no_fallthrough_after_refined_identify": True,
    },
}


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------


def load_spec(spec_path: Path | None = None) -> dict[str, Any]:
    """Load the matcher spec from YAML, or return DEFAULT_SPEC.

    Args:
        spec_path: Optional path to a YAML spec file. If None, looks for
            `data/vehicle_matcher_spec.yaml` relative to the project root.
            If the file is missing, returns DEFAULT_SPEC.

    Returns:
        dict — the spec. Always returns a dict (never raises on missing file).
    """
    if spec_path is None:
        # Default path: data/vehicle_matcher_spec.yaml relative to PROJECT_ROOT.
        # We import infra.paths here (not at top) to avoid a circular import;
        # infra.paths is allowed to load normally because it has no matcher
        # dependencies.
        from infra.paths import PROJECT_ROOT
        spec_path = Path(PROJECT_ROOT) / "data" / "vehicle_matcher_spec.yaml"

    if not spec_path.exists():
        log.info("spec_not_found, using DEFAULT_SPEC path=%s", spec_path)
        return DEFAULT_SPEC

    try:
        import yaml  # PyYAML is already in requirements.
    except ImportError:
        log.warning("yaml_unavailable, using DEFAULT_SPEC path=%s", spec_path)
        return DEFAULT_SPEC

    try:
        with open(spec_path) as f:
            loaded = yaml.safe_load(f)
        if not isinstance(loaded, dict):
            log.warning("spec_invalid_type, using DEFAULT_SPEC path=%s type=%s",
                        spec_path, type(loaded).__name__)
            return DEFAULT_SPEC
        return loaded
    except (yaml.YAMLError, OSError) as exc:
        log.warning("spec_load_failed, using DEFAULT_SPEC path=%s err=%r",
                    spec_path, exc)
        return DEFAULT_SPEC


__all__ = [
    "DEFAULT_SPEC",
    "load_spec",
]
