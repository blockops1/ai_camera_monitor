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
    build_match_alert_body(match_result, vm2_result, score, gap, runner_ups) -> str
        Build the matched-vehicle details block for TG#3 (label, ID,
        owner, color, make/model, confidence, gap, runner-ups).
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


def status_wording_for(classification: str, matched: bool) -> str:
    """Return 'recognized <class>' or 'unrecognized <class>' based on match.

    Args:
        classification: Gate classification (e.g. 'vehicle', 'person', 'animal').
        matched: True when the subject was matched in the knowledge base.

    Returns:
        Status wording string, e.g. 'recognized vehicle', 'unrecognized person'.
    """
    return f"{'recognized' if matched else 'unrecognized'} {classification}"


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


def build_match_alert_body(
    match_result: dict[str, Any],
    vm2_result: dict[str, Any],
    score: float,
    gap: float,
    runner_ups: list[tuple[str, float]] | None = None,
) -> str:
    """Build the matched-vehicle body for a TG#3 match alert.

    Slimmed v1 layout (Phase 6B.121):
        ✅ Match — <label>

          ID: <id>
          Label: <label>
          Owner: <owner>    (if present)
          Color: <color>    (if present)
          Make/Model: <make> <model>   (if present)
          Body: <type>      (if present)

        Confidence: <score>   (gap: <gap>)

        Runner-ups:           (if present)
          #1 <kv_id>: <score>
          #2 <kv_id>: <score>

    Args:
        match_result: Match result dict (must have 'matched' and 'known_vehicle').
        vm2_result: VM2 output dict (present for compatibility).
        score: Float score for the matched candidate (internal scale).
        gap: Score gap to the next-best candidate.
        runner_ups: List of (kv_id, score) tuples for next-2 candidates.

    Returns:
        Formatted body string.
    """
    kv = match_result.get("known_vehicle") or {}
    lines: list[str] = [f"✅ Match \u2014 {kv.get('label', '?')}"]
    lines.append("")
    parts: list[str] = [
        f"  ID: {kv.get('id', '?')}",
        f"  Label: {kv.get('label', '?')}",
    ]
    if kv.get("owner"):
        parts.append(f"  Owner: {kv['owner']}")
    if kv.get("color"):
        parts.append(f"  Color: {kv['color']}")
    if kv.get("make") or kv.get("model"):
        parts.append(f"  Make/Model: {kv.get('make', '?')} {kv.get('model', '')}".rstrip())
    if kv.get("type"):
        parts.append(f"  Body: {kv['type']}")
    lines.extend(parts)
    lines.append("")

    score_str = f"{score:.2f}" if 0 <= score <= 1 else f"{score:.1f}"
    lines.append(f"Confidence: {score_str}   (gap: {gap:.2f})")
    lines.append("")

    if runner_ups:
        lines.append("Runner-ups:")
        for i, (kid, s) in enumerate(runner_ups[:2], 1):
            s_str = f"{s:.2f}" if 0 <= s <= 1 else f"{s:.1f}"
            lines.append(f"  #{i} {kid}: {s_str}")

    return "\n".join(lines)


def build_animal_alert_body(
    match_result: dict[str, Any],
    vm2_result: dict[str, Any],
) -> str:
    """Build the matched-animal body for a TG#3 match alert.

    Slimmed layout for animal matches (US-045e):
        Recognized: <label> (species: <species>, breed: <breed>)
          Cosine: <cosine_score>   (tier1: <tier1_score>)
          Runner-ups:
            #1 <id>: cosine=<cosine>, tier1=<tier1>

    Args:
        match_result: Match result dict (must have 'matched' and 'candidate').
        vm2_result: VM2 output dict (present for compatibility).

    Returns:
        Formatted body string for animal matches.
    """
    cand = match_result.get("candidate") or {}
    cosine_score = match_result.get("cosine_score", 0.0)
    tier1_score = match_result.get("tier1_score", 0.0)
    runner_ups = match_result.get("runner_ups", [])

    label = cand.get("label", "?")
    species = cand.get("species", "unknown")
    breed = cand.get("breed", "unknown")

    lines: list[str] = [
        f"Recognized: {label} (species: {species}, breed: {breed})",
    ]
    lines.append("")
    lines.append(f"  Cosine: {cosine_score:.4f}   (tier1: {tier1_score:.4f})")

    if runner_ups:
        lines.append("")
        lines.append("  Runner-ups:")
        for i, ru in enumerate(runner_ups[:2], 1):
            cs = ru.get("cosine_score", 0.0)
            t1 = ru.get("tier1_score", 0.0)
            lines.append(f"    #{i} {ru.get('id', '?')}: cosine={cs:.4f}, tier1={t1:.4f}")

    return "\n".join(lines)


def build_match_message(
    match_result: dict[str, Any],
    vm2_result: dict[str, Any],
    camera_label: str = "Camera",
    alert: dict[str, Any] | None = None,
    classification: str | None = None,
    reason: str = "",
    top_candidates: list[tuple[str, float]] | None = None,
    match_threshold: float = 0.0,
    gap_threshold: float = 0.0,
) -> dict[str, Any]:
    """Build a TG#3 Telegram message dict for the match result.

    The caption always starts with 'Camera: <label>'. When *alert* is
    provided, 'Alert 3 of 3', 'Alert ID' and Timestamp lines follow the
    Status line, mirroring the TG#2 / TG#1 layout.

    On match, uses build_match_alert_body for the matched-vehicle details
    (label, ID, owner, color, make/model, body, confidence, gap, runner-ups).
    On no-match, uses build_no_match_alert_body (from no_match_telegram)
    which shows reason, top candidates, and thresholds.

    Args:
        match_result: Match result dict with 'matched' key.
        vm2_result: VM2 detail output.
        camera_label: Human-readable camera name.
        alert: Optional parent alert dict for metadata.
        classification: Gate classification ('vehicle', 'person', 'animal').
        reason: Reason for no-match (e.g. 'below confidence threshold').
        top_candidates: Top-N (kv_id, score) tuples for no-match display.
        match_threshold: Score threshold used for match/no-match decision.
        gap_threshold: Gap threshold used for match/no-match decision.

    Returns:
        Telegram-ready message dict with 'caption' and 'photos' keys.
    """
    from telegram_formatter.no_match_telegram import build_no_match_alert_body

    lines: list[str] = [
        f"Camera: {camera_label}",
    ]

    # Determine classification from vm2_result if not provided.
    cls = classification or vm2_result.get("class", "vehicle")
    matched = match_result.get("matched", False)

    if matched:
        if cls == "animal":
            # Animal match: render recognized label + scores via build_animal_alert_body.
            lines.append(build_animal_alert_body(match_result, vm2_result))
        else:
            # Vehicle/person match: use the standard body block.
            score = match_result.get("score", 0.0)
            all_scores = match_result.get("all_scores", [])

            # Compute gap and runner-ups from all_scores.
            gap = 0.0
            runner_ups: list[tuple[str, float]] = []
            if all_scores:
                # all_scores is [(id, score), ...]; the first is the best.
                best_val = all_scores[0][1]
                if len(all_scores) > 1:
                    gap = best_val - all_scores[1][1]
                    runner_ups = all_scores[1:]
                else:
                    gap = best_val

            lines.append(build_match_alert_body(match_result, vm2_result, score, gap, runner_ups))
    else:
        # No-match: use classification-aware wording + full no-match body.
        status = status_wording_for(cls, False)
        lines.append(f"Status: {status}")
        if top_candidates:
            lines.append(build_no_match_alert_body(
                reason=reason,
                top_candidates=top_candidates,
                match_threshold=match_threshold,
                gap_threshold=gap_threshold,
                captured_at=alert.get("timestamp", "") if alert else "",
            ))

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
