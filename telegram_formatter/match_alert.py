"""match_alert.py — Build TG#3 Telegram message dict after vehicle matching.

STATUS: stable
THREAD SAFETY: thread-safe (pure function, no shared state)

INPUTS:
    - match_result: dict (required) -- match output with 'matched' key
      (bool) and, when matched, plate + features from the candidate
    - vm2_result: dict (required) -- VM2 detail output with
      'license_plate' (str | None) and 'distinctive_features' (list[str])

OUTPUTS:
    - return dict: {"caption": str, "photos": list[str]}
      shaped for the Telegram client

PUBLIC API:
    build_match_message(match_result, vm2_result) -> dict
        Build a Telegram-ready message dict for TG#3 (match result).

DOES NOT DO:
    - Send the Telegram message (pipeline.py handles transport)
    - Compute a danger or risk assessment field
    - Derive a features summary; passes through vm2 distinctive_features

CALLS INTO:
    - stdlib str(): path-to-string conversion

RELATED:
    - vehicle_matcher.match.match_vehicle
    - listener/pipeline.py stage 9 (match stage)
"""

from __future__ import annotations

from typing import Any


def build_match_message(
    match_result: dict[str, Any],
    vm2_result: dict[str, Any],
) -> dict[str, Any]:
    """Build a TG#3 Telegram message dict for the match result."""
    lines: list[str] = []

    if match_result.get("matched"):
        plate = vm2_result.get("license_plate")
        if plate:
            lines.append(f"Recognized: {plate}")
        lines.append("Status: recognized vehicle")
    else:
        lines.append("Status: unrecognized vehicle")

    features = vm2_result.get("distinctive_features")
    if features:
        lines.append(f"Distinctive features: {', '.join(features)}")

    return {
        "caption": "\n".join(lines),
        "photos": [],
    }
