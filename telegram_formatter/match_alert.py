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
    build_match_message(match_result, vm2_result, camera_label, alert=None,
                        position=None) -> dict
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
    - infra.format_ts.format_local_timestamp

RELATED:
    - vehicle_matcher.match.match_vehicle
    - listener/pipeline.py stage 13 (match stage)
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from infra.format_ts import format_local_timestamp
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


def _format_score(score: float) -> str:
    """Format a score value — decimal for [0,1], one-decimal for >1."""
    if 0 <= score <= 1:
        return f"{score:.2f}"
    return f"{score:.1f}"


def _format_runner_score(score: float) -> str:
    """Format a runner-up score for display."""
    if 0 <= score <= 1:
        return f"{score:.2f}"
    return f"{score:.1f}"


def build_person_alert_body(
    match_result: dict[str, Any],
    vm2_result: dict[str, Any],
) -> str:
    """Build the matched-person body for a TG#3 match alert.

    Slimmed layout for person matches (US-059e):
        Recognized: <label>
          Cosine: <cosine_score>   (tier1: <tier1_score>)
          Runner-ups:
            #1 <id>: cosine=<cosine>, tier1=<tier1>

    Args:
        match_result: Match result dict (must have 'matched' and 'candidate').
        vm2_result: VM2 output dict (present for compatibility).

    Returns:
        Formatted body string for person matches.
    """
    cand = match_result.get("candidate") or {}
    cosine_score = match_result.get("cosine_score", 0.0)
    tier1_score = match_result.get("tier1_score", 0.0)
    runner_ups = match_result.get("runner_ups", [])

    # Support both shapes:
    #   cosine path: match_result["candidate"]["label"]
    #   tag/Jaccard path: match_result["label"] (flat dict)
    label = cand.get("label") or match_result.get("label", "?")

    lines: list[str] = [
        f"<b>Recognized:</b> {label}",
    ]
    lines.append("")
    lines.append(f"  <b>Cosine:</b> <code>{cosine_score:.4f}</code>   (tier1: <code>{tier1_score:.4f}</code>)")

    if runner_ups:
        lines.append("")
        lines.append("  <b>Runner-ups:</b>")
        for i, ru in enumerate(runner_ups[:2], 1):
            cs = ru.get("cosine_score", 0.0)
            t1 = ru.get("tier1_score", 0.0)
            lines.append(f"    #{i} <b>{ru.get('id', '?')}</b>: cosine=<code>{cs:.4f}</code>, tier1=<code>{t1:.4f}</code>")

    return "\n".join(lines)


def build_match_alert_body(
    match_result: dict[str, Any],
    vm2_result: dict[str, Any],
    score: float,
    gap: float,
    runner_ups: list[tuple[str, float]] | None = None,
) -> str:
    """Build the matched-vehicle body for a TG#3 match alert.

    Slimmed layout (US-067c — stripped debug lines):
        Match — <label>
          ID: <id>
          Label: <label>
          Owner: <owner>    (if present)
          Color: <color>    (if present)
          Make: <make>      (if present)
          Model: <model>    (if present)
          Gap: <gap>        (if gap is known and non-zero)

    Args:
        match_result: Match result dict (must have 'matched' and 'known_vehicle').
        vm2_result: VM2 output dict (present for compatibility).
        score: Float score for the matched candidate (internal scale).
        gap: Score gap to the next-best candidate.
        runner_ups: List of (kv_id, score) tuples for next-2 candidates.

    Returns:
        Formatted body string.
    """
    # FIX (US-056a port from refactor commit 0dbd258):
    # match_vehicle() returns a FLAT dict (dict(best_candidate)), NOT
    # a {known_vehicle: {...}} envelope. Older callers passed the wrapped
    # shape -- keep both for backward compatibility.
    if isinstance(match_result, dict) and "known_vehicle" in match_result:
        kv = match_result.get("known_vehicle") or {}
    else:
        kv = match_result if isinstance(match_result, dict) else {}

    label = kv.get("label", "?")
    lines: list[str] = [f"✅ Match — <b>{label}</b>"]

    parts: list[str] = [
        f"  ID: <b>{kv.get('id', '?')}</b>",
        f"  Label: <b>{kv.get('label', '?')}</b>",
    ]
    if kv.get("owner"):
        parts.append(f"  Owner: <b>{kv['owner']}</b>")
    if kv.get("color"):
        parts.append(f"  Color: <b>{kv['color']}</b>")
    if kv.get("make"):
        parts.append(f"  Make: <b>{kv['make']}</b>")
    if kv.get("model"):
        parts.append(f"  Model: <b>{kv['model']}</b>")

    lines.extend(parts)

    # Gap line (US-067c: keep gap if non-zero, drop Confidence line)
    if gap and gap > 0:
        lines.append(f"  Gap: <b>{_format_runner_score(gap)}</b>")

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
        f"Recognized: <b>{label}</b> (species: {species}, breed: {breed})",
    ]
    lines.append("")
    lines.append(f"  Cosine: <b>{cosine_score:.4f}</b>   (tier1: {tier1_score:.4f})")

    if runner_ups:
        lines.append("")
        lines.append("  <b>Runner-ups:</b>")
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
    position: int | None = None,
    mode: str | None = None,
) -> dict[str, Any]:
    """Build a TG#3 Telegram message dict for the match result.

    The caption always starts with 'Camera: <label>'. When *alert* is
    provided, 'Alert 3 of 3', 'Alert ID' and Timestamp lines follow the
    Status line, mirroring the TG#2 / TG#1 layout.

    On match, uses build_match_alert_body for the matched-vehicle details
    (label, ID, owner, color, make/model, gap).
    On no-match, uses a slimmed body with only the top runner-up (US-067c).

    When *mode* is provided, produces a compact caption format:
    - mode='person' matched: 'person: <label> (cosine=X.XX, tier1=X.XX)'
    - mode='person' no-match: 'person (unknown)'
    - mode='animal': mirrors the animal branch structure.

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
        position: TG message position (1-3), used for the 'TG X of 3' marker.
        mode: Optional mode for compact caption ('person', 'animal', etc.).

    Returns:
        Telegram-ready message dict with 'caption' and 'photos' keys.
    """
    lines: list[str] = [f"Camera: {camera_label}"]

    # Determine classification from vm2_result if not provided.
    cls = classification or vm2_result.get("class", "vehicle")
    if "matched" not in match_result:
        raise ValueError("match_result is missing required key 'matched'")
    matched = match_result["matched"]

    if matched:
        if cls == "animal":
            # Animal match: render recognized label + scores via build_animal_alert_body.
            lines.append(build_animal_alert_body(match_result, vm2_result))
        elif cls == "person":
            # Person match: render recognized label + cosine/tier1 scores.
            lines.append(build_person_alert_body(match_result, vm2_result))
        else:
            # Vehicle match: use the standard body block.
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
                # When there's only one score, gap stays 0 (no next-best to compare).

            lines.append(build_match_alert_body(match_result, vm2_result, score, gap, runner_ups))
    else:
        # No-match: person-aware status wording (US-059e).
        if cls == "person":
            lines.append("Unrecognized person")
        elif cls == "animal":
            # Animal no-match: slimmed body with top_candidates.
            if top_candidates:
                top_id, top_score = top_candidates[0]
                lines.append(f"  Runner-up: <b>{top_id}</b> ({_format_runner_score(top_score)})")
            else:
                lines.append("Unrecognized animal")
        else:
            # Vehicle no-match: slimmed body (US-067c) — header + runner-up only.
            lines.append("\u26a0\ufe0f No match (confidence below threshold)")

            # Top runner-up if available.
            if top_candidates:
                top_id, top_score = top_candidates[0]
                lines.append(f"  Runner-up: <b>{top_id}</b> ({_format_runner_score(top_score)})")

    # Compact caption mode: when mode='person' or mode='animal' is provided,
    # replace the body lines with a simplified format.
    if mode is not None:
        if mode == "person":
            if matched:
                # 'person: <label> (cosine=X.XX, tier1=X.XX)'
                label = match_result.get("candidate", {}).get(
                    "label", match_result.get("label", "?")
                )
                cosine = match_result.get("cosine_score", 0.0)
                tier1 = match_result.get("tier1_score", 0.0)
                lines[-1] = f"person: {label} (cosine={cosine:.2f}, tier1={tier1:.2f})"
            else:
                # 'person (unknown)'
                lines[-1] = "person (unknown)"
        elif mode == "animal":
            if matched:
                # Mirror the animal branch structure: 'animal: <label>'
                label = match_result.get("candidate", {}).get(
                    "label", "unknown"
                )
                lines[-1] = f"animal: {label}"
            else:
                lines[-1] = "animal (unknown)"

    # Alert metadata (mirrors TG#1 / TG#2 caption layout).
    # Alert ID lines removed per US-073g — operator doesn't need these in TG captions.
    if alert:
        lines.append("Alert 3 of 3")
        timestamp = alert.get("timestamp")
        if timestamp:
            # Wrap in <code> tags — rendered natively in HTML parse_mode
            # (switched from MarkdownV2, US-074).
            lines.append(f"Timestamp: <code>{format_local_timestamp(timestamp)}</code>")

    # TG marker (US-067c: consistent with 067a/067b).
    if position is not None:
        lines.append(f"TG {position} of 3")

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
