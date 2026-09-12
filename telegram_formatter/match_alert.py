"""match_alert.py — Build TG#3 Telegram message dict after vehicle matching.

STATUS: stable
THREAD SAFETY: thread-safe (pure function, no shared state)

INPUTS:
    - match_result: dict (required) -- match output with 'matched' key
      (bool) and, when matched, plate + features from the candidate
    - vm2_result: dict (required) -- VM2 detail output with
      'license_plate' (str | None) and 'distinctive_features' (list[str])
    - camera_label: str (required) -- human-readable camera name
    - alert: dict | None (optional) -- parent alert dict with 'id' and
      'timestamp' keys, for caption metadata

OUTPUTS:
    - return dict: {"caption": str, "photos": list[str]}
      shaped for the Telegram client

PUBLIC API:
    build_match_message(match_result, vm2_result, camera_label, alert=None) -> dict
        Build a Telegram-ready message dict for TG#3 (match result).

DOES NOT DO:
    - Send the Telegram message (pipeline.py handles transport)
    - Compute a danger or risk assessment field
    - Derive a features summary; passes through vm2 distinctive_features

CALLS INTO:
    - stdlib str(): path-to-string conversion

RELATED:
    - vehicle_matcher.match.match_vehicle
    - listener/pipeline.py stage 13 (match stage)
"""

from __future__ import annotations

from typing import Any


def build_match_message(
    match_result: dict[str, Any],
    vm2_result: dict[str, Any],
    camera_label: str = "Camera",
    alert: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a TG#3 Telegram message dict for the match result.

    The caption always starts with 'Camera: <label>'. When *alert* is
    provided, 'Alert 3 of 3', Alert ID and Timestamp lines follow the
    Status line, mirroring the TG#2 / TG#1 layout.
    """
    lines: list[str] = [
        f"Camera: {camera_label}",
    ]

    if match_result.get("matched"):
        plate = vm2_result.get("license_plate")
        if plate:
            lines.append(f"Recognized: {plate}")
        lines.append("Status: recognized vehicle")
    else:
        lines.append("Status: unrecognized vehicle")

    # Alert metadata (mirrors TG#1 / TG#2 caption layout).
    if alert:
        lines.append("Alert 3 of 3")
        alert_id = alert.get("id")
        if alert_id:
            lines.append(f"Alert ID: {alert_id}")
        timestamp = alert.get("timestamp")
        if timestamp:
            lines.append(f"Timestamp: {timestamp}")

    features = vm2_result.get("distinctive_features")
    if features:
        lines.append(f"Distinctive features: {', '.join(features)}")

    return {
        "caption": "\n".join(lines),
        "photos": [],
    }
