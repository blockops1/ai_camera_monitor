"""Match Telegram body + sender (Phase 6B.112).

The third Telegram sent when the matcher says YES (Telegram #3a in the
lead motion message stack).

Body contents:
  - Header: camera name + match verdict
  - Matched vehicle: id, label, owner, score, gap
  - Per-dimension breakdowns: every scored dimension with its score
  - Optional second Telegram vs first (the Motion Telegram was already
    sent, so we don't repeat Qwen's full output here)

Pure function (build_match_telegram_body). Sender function
(send_match_alert) does the I/O — renders the 3-crop vertical composite
photo and sends via Telegram.
"""

from __future__ import annotations

import logging

log = logging.getLogger("telegram_formatter.match_telegram")

from dataclasses import dataclass

from infra.cameras import code_for, display_name_for

from vehicle_matcher import (
    MatchVerdict,  # 6B.90 package-form (per farm-vision skill)
)


@dataclass(frozen=True)
class MatchTelegramInput:
    """Inputs for the Match Telegram body.

    Attributes:
        camera_name:      Camera identifier — either a code (e.g. "CAM1") or a
                          friendly name (e.g. "<FRIENDLY_NAME>"). The header
                          resolves it through infra.cameras.display_name_for
                          so the body reflects the registry's canonical
                          display name (Phase 6B.167 §13.5 Commit 13).
        captured_at_iso:  ISO-8601 timestamp string
        verdict:          The MatchVerdict from the matcher.
        match_threshold:  The confidence threshold used (for transparency).
        gap_threshold:    The gap threshold used (for transparency).
        alert_id:         Optional alert identifier.
    """
    camera_name: str
    captured_at_iso: str
    verdict: MatchVerdict
    match_threshold: float
    gap_threshold: float
    alert_id: str | None = None


def _format_kv_known(verdict: MatchVerdict) -> str:
    """Format the matched vehicle's identity."""
    kv = verdict.known_vehicle
    parts: list[str] = []
    pid = kv.get("id", "?")
    label = kv.get("label", "?")
    owner = kv.get("owner")
    parts.append(f"  ID: {pid}")
    parts.append(f"  Label: {label}")
    if owner:
        parts.append(f"  Owner: {owner}")
    color = kv.get("color")
    if color:
        parts.append(f"  Color: {color}")
    make = kv.get("make")
    model = kv.get("model")
    if make or model:
        parts.append(f"  Make/Model: {make or '?'} {model or ''}".rstrip())
    body = kv.get("type")
    if body:
        parts.append(f"  Body: {body}")
    return "\n".join(parts)


def _format_breakdowns(verdict: MatchVerdict) -> str:
    """Format the per-dimension score breakdown."""
    lines: list[str] = []
    lines.append("Dimension scores:")
    for dim, score in sorted(verdict.breakdowns.items()):
        # Render as percentage for readability.
        lines.append(f"  {dim}: {score:.2f}")
    return "\n".join(lines)


def _format_other_candidates(verdict: MatchVerdict) -> str:
    """Format the runner-up candidates (for transparency)."""
    if len(verdict.all_scores) <= 1:
        return ""
    lines: list[str] = []
    lines.append("Runner-up candidates:")
    for kv_id, score in verdict.all_scores[1:]:
        lines.append(f"  {kv_id}: {score:.2f}")
    return "\n".join(lines)


def build_match_telegram_body(input: MatchTelegramInput) -> str:
    """Build the Match Telegram body.

    Args:
        input: MatchTelegramInput with the match verdict.

    Returns:
        The Telegram body as a string. No I/O.

    Phase 6B.121 (2026-08-22) — slim body per maintainer OOB:
        "I would prefer a less busy message, maybe the old title,
        what it matched to, the confidence, and the two runner-up"

    Layout (slim):
        <header>            ← ✅ Match — <camera>
        <blank>
        Matched: <label>
        <known vehicle primary fields — ID/Owner/Color/Make-Model>
        <blank>
        Confidence: <score>   (gap: <gap>)
        <blank>
        Runner-ups:
          #1 <kv>: <score>
          #2 <kv>: <score>
        <blank>
        <captured_at>        (omitted if empty)

    Removed from the previous (busy) version:
        - Per-dimension score breakdown
        - Thresholds line
        - Matched: <id> (<label>) — replaced with just the human label

    Reasoning: the breakdown + thresholds were debug info. With the
    slim layout, the user gets the match at a glance and can still
    see who the next-best guesses were (useful when the match is
    low-confidence and they're scanning quickly).

    Phase 6B.114 (2026-08-25): Removed [alert_id] prefix from header
    and removed the captured_at_iso from the header (was interrupting
    the word-flow). The captured_at webhook time is now a footer
    line at the end of the body — event time, not send time, per
    maintainer OOB correction.
    """
    v = input.verdict
    lines: list[str] = []

    # Header (kept slim — Phase 6B.114).
    # Phase 6B.167 §13.5 (Commit 13): resolve the camera identifier via
    # the registry so the body reflects the canonical display name. The
    # caller may pass either a code (CAM1/FRONT) or a name; display_name_for
    # returns the spec.name. Falls back to the input string when the
    # identifier isn't in the registry (test fixtures, legacy callers).
    lines.append(f"✅ Match — {display_name_for(input.camera_name)}")

    lines.append("")
    # Slimmed: just the human label, not (id) + (label) duplicated.
    label = v.known_vehicle.get("label", "?")
    lines.append(f"Matched: {label}")

    lines.append("")
    lines.append(_format_kv_known_slim(v))

    lines.append("")
    lines.append(f"Confidence: {v.score:.2f}  (gap: {v.gap:.2f})")

    if len(v.all_scores) > 1:
        lines.append("")
        lines.append("Runner-ups:")
        # Top 2 runner-ups (skip the matched one at index 0).
        for kv_id, score in v.all_scores[1:3]:
            lines.append(f"  #{kv_id}: {score:.2f}")

    # Footer — webhook event time (Phase 6B.114).
    if input.captured_at_iso:
        lines.append("")
        lines.append(input.captured_at_iso)

    return "\n".join(lines)


def _format_kv_known_slim(verdict: MatchVerdict) -> str:
    """Slim known-vehicle details for the match body (Phase 6B.121).

    Returns a single-line summary: "ID/Owner/Color/Make-Model".
    Drops:
        - Standalone ID line (redundant with the label above)
        - Body type line (already implied by the label)
        - Standalone Label line (now in the "Matched:" line)
    """
    kv = verdict.known_vehicle
    parts: list[str] = []
    owner = kv.get("owner")
    color = kv.get("color")
    make = kv.get("make")
    model = kv.get("model")

    if owner:
        parts.append(f"Owner: {owner}")
    if color:
        parts.append(f"Color: {color}")
    if make or model:
        make_model = f"{make or '?'} {model or ''}".rstrip()
        parts.append(f"Make/Model: {make_model}")

    return "  ".join(parts) if parts else ""


# ============================================================================
# Phase 6B.112 — Send + render. Body builders above are pure functions;
# the helpers below do I/O. TG#3 in the lead motion message stack — fires
# AFTER TG#1 (arriving) and TG#2 (vehicle in motion + composite
# motion-trail).
#
# Phase 6B.142 (2026-08-27, maintainer OOB): TG#3 is now send_message(body)
# + send_photo_group(tight_crops, caption="") — no composite image.
# The vertical-composite helper _concat_crops_vertical was removed.
# ============================================================================


def send_match_alert(
    alert_id: str,
    camera_name: str,
    match_telegram_input: MatchTelegramInput,
    crop_paths: list[str],
    bot_token: str,
    chat_id: str,
    captured_at: str,
) -> bool:
    """TG#3a — Send the match Telegram (vehicle matched a known_vehicles entry).

    Phase 6B.142 (2026-08-27, maintainer OOB): body via send_message, then
    the two tight crops (crop_a_path, crop_b_path) as a 2-image media
    group via send_photo_group (no caption). Replaces the prior vertical
    composite match_crops.jpg approach.

    Body: build_match_telegram_body(input) — see above.
    Photo: 2-image Telegram album of the tight crops Qwen3-VL identified
    the vehicle from. No album if crop_paths is empty (text-only fallback).

    Args:
        alert_id: For log lines.
        camera_name: For audit log.
        match_telegram_input: Already-built MatchTelegramInput.
        crop_paths: List of 2 tight crop JPEG paths (crop_a, crop_b).
        bot_token, chat_id: Telegram creds.
        captured_at: ISO timestamp (for log only — body already has it).

    Returns:
        True if Telegram send succeeded (with or without photo).
        False if creds missing or send failed.
    """
    from infra.audit_telegram import log_outbound_telegram
    from infra.send_telegram import send_message, send_photo_group

    if not bot_token or not chat_id:
        log.warning(
            f"[{alert_id}] match_alert NOT sent (no telegram creds)"
        )
        return False

    body = build_match_telegram_body(match_telegram_input)

    # Phase 6B.142 (2026-08-27, maintainer OOB): send the body as a text
    # message, then send the two tight crops as a 2-image media group
    # (no caption — body already went via send_message). This replaces
    # the prior behavior of building a vertical match_crops.jpg composite
    # and sending it as a single photo with caption. maintainer specifically
    # wanted both crops visible — the composite had them stacked at
    # 293x120 px each, which obscured identifying features (wheels,
    # grille, plate). Two separate images let him swipe to compare
    # angles.
    v_id = match_telegram_input.verdict.known_vehicle.get("id", "")
    sent = bool(send_message(
        bot_token, chat_id, body,
        alert_id=alert_id,
        channel="gatekeeper_match",
        event="vehicle_matched",
        v_id=v_id,
    ))

    if crop_paths:
        # Tight crops (crop_a_path, crop_b_path) — same files Qwen
        # identified the vehicle from. Telegram album, no caption.
        try:
            send_photo_group(
                bot_token, chat_id, crop_paths, caption="",
                alert_id=alert_id,
                channel="gatekeeper_match",
                event="vehicle_matched",
                v_id=v_id,
            )
        except Exception as e:  # noqa: BLE001
            # Album is best-effort — body already sent. Log and continue.
            log.warning(f"[{alert_id}] match_alert album send failed: {e!r}")

    log.info(
        f"[{alert_id}] match_alert: "
        f"kv={match_telegram_input.verdict.known_vehicle.get('id')} "
        f"score={match_telegram_input.verdict.score:.2f} "
        f"sent={sent} crops={len(crop_paths)}"
    )
    log_outbound_telegram(
        channel="gatekeeper_match",
        alert_id=alert_id,
        v_id=v_id,
        event="vehicle_matched",
        body=body,
        sent=bool(sent),
        extra=f"camera_code={code_for(camera_name)} "
              f"score={match_telegram_input.verdict.score:.2f} "
              f"gap={match_telegram_input.verdict.gap:.2f} "
              f"crop_count={len(crop_paths)}",
        image_paths=list(crop_paths),
    )
    return bool(sent)


def send_no_match_alert(
    alert_id: str,
    camera_name: str,
    no_match_telegram_input,  # no_match_telegram.NoMatchTelegramInput
    crop_paths: list[str],
    bot_token: str,
    chat_id: str,
    captured_at: str,
) -> bool:
    """TG#3b — Send the no-match Telegram (vehicle didn't match anything).

    Phase 6B.142 (2026-08-27, maintainer OOB): mirror send_match_alert —
    body via send_message, then 2-image album of tight crops (no
    caption). No more match_crops.jpg composite.

    Body: build_no_match_telegram_body(input) — see no_match_telegram.py.
    Photo: 2-image Telegram album of the tight crops Qwen identified.
    """
    from infra.audit_telegram import log_outbound_telegram
    from infra.send_telegram import send_message, send_photo_group
    from telegram_formatter.no_match_telegram import (
        build_no_match_telegram_body,
    )

    if not bot_token or not chat_id:
        log.warning(
            f"[{alert_id}] no_match_alert NOT sent (no telegram creds)"
        )
        return False

    body = build_no_match_telegram_body(no_match_telegram_input)

    # Phase 6B.142 (2026-08-27, maintainer OOB): mirror send_match_alert —
    # text body via send_message, then 2-image album of tight crops
    # (no caption). No more match_crops.jpg composite.
    sent = bool(send_message(
        bot_token, chat_id, body,
        alert_id=alert_id,
        channel="gatekeeper_no_match",
        event="vehicle_no_match",
    ))

    if crop_paths:
        try:
            send_photo_group(
                bot_token, chat_id, crop_paths, caption="",
                alert_id=alert_id,
                channel="gatekeeper_no_match",
                event="vehicle_no_match",
            )
        except Exception as e:  # noqa: BLE001
            log.warning(f"[{alert_id}] no_match_alert album send failed: {e!r}")

    log.info(
        f"[{alert_id}] no_match_alert: "
        f"reason={no_match_telegram_input.no_match.reason} "
        f"sent={sent} crops={len(crop_paths)}"
    )
    log_outbound_telegram(
        channel="gatekeeper_no_match",
        alert_id=alert_id,
        v_id="",
        event="vehicle_no_match",
        body=body,
        sent=bool(sent),
        extra=f"camera_code={code_for(camera_name)} "
              f"reason={no_match_telegram_input.no_match.reason} "
              f"crop_count={len(crop_paths)}",
        image_paths=list(crop_paths),
    )
    return bool(sent)
