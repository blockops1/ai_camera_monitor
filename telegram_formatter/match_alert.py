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
    - crop_a_path: str | None (optional) -- path to crop A image
    - crop_b_path: str | None (optional) -- path to crop B image

OUTPUTS:
    - return dict: {"caption": str, "photos": list[str]}
      shaped for the Telegram client

PUBLIC API:
    build_match_message(match_result, vm2_result, camera_label, alert=None) -> dict
        Build a Telegram-ready message dict for TG#3 (match result).
    pick_alert_image_path(vm2_result, crop_a_path, crop_b_path) -> str | None
        Pure helper: choose which crop image path to use based on
        vm2_result.better_crop (crop_a / crop_b / neither -> crop_a fallback).
    send_match_alert(bot, chat_id, match_result, vm2_result,
                     crop_a_path, crop_b_path, camera_label, alert=None)
        Build the text body, send it, then send the chosen crop photo.
        send_photo is wrapped in try/except so a bad crop path
        does not block the text body.

DOES NOT DO:
    - Compute a danger or risk assessment field
    - Derive a features summary; passes through vm2 distinctive_features

CALLS INTO:
    - telegram_formatter.dispatcher: _send_message, send_photo
    - stdlib str(): path-to-string conversion

RELATED:
    - vehicle_matcher.match.match_vehicle
    - listener/pipeline.py stage 13 (match stage)
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from telegram_formatter.dispatcher import _send_message, send_photo

logger = logging.getLogger(__name__)


def pick_alert_image_path(
    vm2_result: dict[str, Any] | None,
    crop_a_path: str | None,
    crop_b_path: str | None,
) -> str | None:
    """Choose which crop image path to attach to this match alert.

    Reads vm2_result.better_crop to decide:
    - 'crop_a' -> crop_a_path
    - 'crop_b' -> crop_b_path
    - 'neither' or missing -> crop_a_path (best-effort fallback)

    Args:
        vm2_result: VM2 output dict (may be None or missing better_crop).
        crop_a_path: Path to crop A image.
        crop_b_path: Path to crop B image.

    Returns:
        The chosen image path, or None if neither path is available.
    """
    choice = (vm2_result or {}).get("better_crop")
    if choice == "crop_b":
        return crop_b_path
    return crop_a_path  # 'crop_a', 'neither', or missing -> fallback to crop_a


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


def send_match_alert(
    bot_token: str,
    chat_id: str,
    match_result: dict[str, Any],
    vm2_result: dict[str, Any],
    crop_a_path: str | None = None,
    crop_b_path: str | None = None,
    camera_label: str = "Camera",
    alert: dict[str, Any] | None = None,
    base_url: str = "https://api.telegram.org",
) -> None:
    """Send a TG#3 match alert: text body then optional photo.

    Sends the caption via sendMessage first, then attempts to send the
    chosen crop image via send_photo. The photo send is wrapped in
    try/except so a bad crop path does not block the text body.

    Args:
        bot_token: Telegram bot token.
        chat_id: Target chat ID.
        match_result: Match result dict with 'matched' key.
        vm2_result: VM2 detail output dict.
        crop_a_path: Path to crop A image.
        crop_b_path: Path to crop B image.
        camera_label: Human-readable camera name.
        alert: Optional parent alert dict for metadata.
        base_url: Telegram API base URL override.
    """
    msg = build_match_message(match_result, vm2_result, camera_label, alert)
    caption = msg["caption"]

    # Step 1: send text body.
    _client = httpx.Client(timeout=10.0)
    try:
        _send_message(_client, bot_token, chat_id, caption, base_url)
    except Exception:
        logger.exception("match_alert: sendMessage failed for %s", chat_id)
    finally:
        _client.close()

    # Step 2: pick and send photo (best-effort, never blocks text).
    image_path = pick_alert_image_path(vm2_result, crop_a_path, crop_b_path)
    if image_path is None:
        logger.warning("match_alert: no image path chosen, skipping send_photo")
        return

    try:
        send_photo(bot_token, chat_id, image_path, caption=caption, base_url=base_url)
    except Exception:
        logger.exception("match_alert: send_photo failed for %s", image_path)
