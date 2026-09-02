"""Signature extraction from a vision result.

A "signature" is the structured fingerprint of a vehicle that the
matcher scores against known_vehicles.json. It is derived entirely
from the vision model's output — no I/O, no business logic.

Pure function. Replaces the legacy `extract_signature` that lived
inside `src/vehicle_state.py`.
"""

from __future__ import annotations

from typing import Any, cast


def is_empty_signature(sig: object) -> bool:
    """True if signature has no usable identification data.

    A signature is "empty" when every field that contributes to
    matching is None or absent. Used by the identifier to decide
    whether to skip a crop and try the next one.

    Tolerates None and non-dict inputs (treats them as empty) so
    callers don't have to pre-check; matches the test in
    test_signature.py for "extract_signature(None) == {}".
    """
    if not isinstance(sig, dict):
        return True
    sig_dict = cast(dict[str, Any], sig)
    if not sig_dict:
        return True
    identification_fields = (
        "color", "type", "make", "model",
    )
    return all(not sig_dict.get(f) for f in identification_fields)


def extract_signature(vision_result: object) -> dict[str, Any]:
    """Pull matcher-relevant fields out of a vision_result.

    Args:
        vision_result: The full Qwen response (color, body_style_hint,
            make, model, vehicle_features{}, description, confidence,
            plus any extras Qwen returned). None, str, list, and other
            non-dict inputs are tolerated and return an empty signature.

    Returns:
        A flat dict (possibly empty) with:
          - color: str|None
          - type: str|None (canonicalized from body_style_hint)
          - make: str|None
          - model: str|None
          - vehicle_features{} keys flattened to top level
          - description: str|None (kept so the matcher can use it
            if needed; not currently scored but available)
          - confidence: float|None (Qwen's confidence, not matcher's)
        Plus ANY other keys the vision model returned, so forward-compat
        fields aren't lost.

    Notes:
        Pure function. No imports from other domain modules.
    """
    if not vision_result or not isinstance(vision_result, dict):
        return {}
    # After the isinstance guard above, vision_result is dict[str, Any].
    # Use a local so reassignment below doesn't erase the cast.
    vision_dict: dict[str, Any] = cast(dict[str, Any], vision_result)

    # Phase 6B.87.A (added 2026-08-17) — unwrap the listener's per-vehicle
    # wrap shape ({"vehicles": [_mv], "primary_vehicle_index": 0}) before
    # extracting. The listener synthesizes this wrap at the gatekeeper
    # match site (listener.py:3475) so each moving vehicle can be scored
    # individually. Pre-6B.87.A the function saw the wrap as the input
    # dict, never reached into vehicles[], and returned the wrap itself
    # as the "signature" — which has no color/type/make/model fields, so
    # match_vehicle_scored computed every score against empty fields and
    # returned 0.00 for every known vehicle. Symptom: every gatekeeper
    # match since Phase 6B.65 produced "Top 3 candidates all 0.00".
    #
    # Two input shapes are now supported:
    #   1. Wrap: { "vehicles": [_mv, ...], "primary_vehicle_index": N }
    #      -> unwrap to _mv = vehicles[primary_vehicle_index]
    #   2. Flat: { "color": ..., "make": ..., "model": ..., ... }
    #      -> use as-is (the legacy Qwen top-level shape, also the
    #         6B.66 schema path)
    _vehicles = vision_dict.get("vehicles")
    if (
        isinstance(_vehicles, list)
        and _vehicles
        and isinstance(_vehicles[0], dict)
    ):
        _primary_idx = vision_dict.get("primary_vehicle_index", 0)
        if isinstance(_primary_idx, int) and 0 <= _primary_idx < len(_vehicles):
            vision_dict = cast(dict[str, Any], _vehicles[_primary_idx])
        else:
            vision_dict = cast(dict[str, Any], _vehicles[0])

    # Start with the canonical identification fields.
    out: dict[str, Any] = {}

    color = vision_dict.get("color")
    if color and color != "null":
        out["color"] = str(color)

    # body_style_hint is canonicalized to "type" — the matcher keys
    # on "type" for body-style comparisons.
    bsh = vision_dict.get("body_style_hint")
    if bsh and bsh != "null":
        out["type"] = str(bsh)

    make = vision_dict.get("make")
    if make and make != "null":
        out["make"] = str(make)

    model = vision_dict.get("model")
    if model and model != "null":
        out["model"] = str(model)

    # Flatten vehicle_features so the matcher can score per-feature.
    feats = vision_dict.get("vehicle_features") or {}
    if isinstance(feats, dict):
        for key, val in feats.items():
            if val is None:
                continue
            if isinstance(val, str) and val.strip().lower() in ("", "null"):
                continue
            out[key] = val

    # Carry through description (kept for diagnostics, not currently
    # scored by the matcher).
    desc = vision_dict.get("description")
    if isinstance(desc, str) and desc.strip():
        out["description"] = desc.strip()

    # Carry through Qwen's confidence.
    conf = vision_dict.get("confidence")
    if isinstance(conf, (int, float)):
        out["confidence"] = float(conf)

    # Forward-compat: any other key Qwen returned, pass through
    # unless it's None or the literal string "null".
    for key, val in vision_dict.items():
        if key in out:
            continue
        if key in (
            "color", "body_style_hint", "make", "model",
            "vehicle_features", "description", "confidence",
        ):
            continue
        if val is None:
            continue
        if isinstance(val, str) and val.strip().lower() == "null":
            continue
        out[key] = val

    return out
