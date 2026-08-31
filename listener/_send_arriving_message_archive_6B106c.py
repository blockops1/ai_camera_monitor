"""
_send_arriving_message_archive_6B106c.py — Verbatim archive of dead code removed
from listener/listener.py in Phase.106 Commit 4 (2026-08-23).

This file is an IN-TREE sibling archive for the listener's dead `_send_arriving_message`
function and its surrounding 6B.110 banner comment. Per archive-first-workflow (the operator
2026-08-20: archive the OLD version FIRST, then edit), the contents below were copied
verbatim from listener/listener.py lines 1494-1597 immediately before that block was
deleted.

WHY THIS IS HERE
----------------
The function `_send_arriving_message` was DEAD CODE in the slim listener.py post-6B.105c
(2026-08-21). The live version lives at `listener/vehicle_event_pipeline.py:277`, and
the live pipeline invokes it from `vehicle_event_pipeline.py:500`. The listener.py copy
was never called by anything (verified: `grep -rn "_send_arriving_message\b" --include="*.py" .`
shows zero callers in listener.py itself, zero importers from outside).

CONTENTS
--------
Lines 1494-1511 from listener.py: the 6B.110 banner comment block documenting the
focused-pass cascade extraction (kept for archaeological context — explains WHY
`_send_arriving_message` was orphaned).

Lines 1514-1597 from listener.py: the actual `_send_arriving_message` function definition.

ROLLBACK
--------
Restoring this function requires re-adding the imports at lines 65 (`log_outbound_telegram`),
74 (`notify`), and 80 (`VEHICLE_ARRIVING_ENABLED`), then pasting the function back at
its original location (immediately before `def _process_alert(`). Easiest full rollback is
`git revert HEAD` on the cleanup commit.

ARCHIVE LOCATION
----------------
- In-tree (this file): listener/_send_arriving_message_archive_6B106c.py
- Off-tree marker:      ~/archive/farm-surveillance-listener-deadcode-2026-08-23/MANIFEST.md
- Cleanup doc:          docs/CLEANUP-2026-08-23-listener-deadcode.md
"""


# ---------------------------------------------------------------------------
# Phase.110 — Unknown-vehicle focused-pass cascade moved to vehicle_identifier/
# ---------------------------------------------------------------------------
# 3 cascade functions moved to vehicle_identifier/focused_pass.py:
#   _focused_pass_for_unknown_arrival       → run_focused_pass
#   _vehicle_send_notification              → send_vehicle_notification
#   _convert_unknown_to_known_after_focused_pass → convert_unknown_to_known
#
# Extracted 2026-08-21. Verbatim pre-slim copy archived at
# listener/_focused_pass_archive_6B110.py for rollback / archaeology.
#
# IMPORTANT: these functions are DEAD CODE in the slim listener.py post-6B.105c.
# The slim pipeline does NOT call them. They are parked here for §11.31
# (modular vehicle matcher refactor) — when §11.31 ships, the pipeline will
# import and wire them back in. Until then, the cascade runs only via the
# legacy _process_alert path (archived). See vehicle_identifier/focused_pass.py
# header for the full status note.


def _send_arriving_message(
    alert_id: str,
    camera_name: str,
    frame_path: str,
    bot_token: str,
    chat_id: str,
) -> bool:
    """
    Phase.9 message 1: instant heads-up that a vehicle is on the
    property and the system is identifying it.

    Fires ~t+2s after webhook receipt (right after frame 1 is on disk),
    with:
      - photo: frame 1 (vehicle still distant; informational only)
      - text: short — "🚗 Vehicle entering property at <camera>, identifying..."
      - alert_id: <alert_id>-arriving (suffixed so the cooldown treats it
        as independent from message 2's "-identified" alert)

    Failure-isolated: any exception is logged, never raised. Message 2
    further down the pipeline still fires regardless of message-1 outcome.

    Returns True if Telegram accepted the message (or if there were no
    creds to send with — we don't count "no creds" as a failure of
    message-1 dispatch), False if the underlying notify() returned False.

    Phase.x channel retirement: this function is a no-op when
    VEHICLE_ARRIVING_ENABLED is False. The vehicle_tracker channel
    is the sole Telegram source for vehicle events. Re-enable by
    setting FARM_VEHICLE_ARRIVING_ENABLED=1 in the env.
    """
    if not VEHICLE_ARRIVING_ENABLED:
        # Channel #1 retired. vehicle_tracker covers arrivals/departures
        # ~28s later with a photo + identified body. No-op here.
        return True
    try:
        # 2026-07-26: replaced `[ARRIVING]` placeholder tag with descriptive
        # `[INCOMING_VEHICLE]` channel label + auto-detected `[filename]`
        # script tag via _telegram_origin. the operator asked (twice) for the
        # actual script filename + a meaningful channel label.
        arriving_alert = {
            "alert_id": f"{alert_id}-arriving",
            "title": "[INCOMING_VEHICLE] Vehicle entering property",
            "description": (
                f"🚗 <b>[INCOMING_VEHICLE]</b> Vehicle entering property at "
                f"{camera_name}, identifying..."
            ),
            "threat_level": 1,
            "camera": camera_name,
            "frame_path": frame_path,
            "source": "vehicle_arriving",
        }
        sent = notify(
            alert=arriving_alert,
            bot_token=bot_token,
            chat_id=chat_id,
            cooldown_seconds=120,
            vision_result=None,
        )
        log.info(
            f"[{alert_id}] message 1 (arriving) → telegram: "
            f"camera={camera_name}, sent={sent}"
        )
        # OUTBOUND_TELEGRAM audit line. The notifier.py notify() call above
        # already emits its own [OUTBOUND_TEGRAM] tag — this one is the
        # additional Phase.9-specific record so postmortem can grep
        # "channel=vehicle_arriving" to isolate these messages.
        # Phase.36: image_paths is the photo (if any) that was sent
        # with the "vehicle entering" message. Currently the arriving
        # alert does NOT attach a photo (it's a text-only heads-up),
        # so image_paths=[].
        log_outbound_telegram(
            channel="vehicle_arriving",
            alert_id=alert_id,
            v_id="",
            event="vehicle_arriving",
            body=arriving_alert["description"],
            sent=bool(sent),
            extra=f"camera={camera_name}",
            image_paths=[],
        )
        return sent
    except Exception as err:
        log.warning(f"[{alert_id}] message 1 (arriving) failed: {err}")
        return False
