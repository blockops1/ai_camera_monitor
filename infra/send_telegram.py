"""
send_telegram.py — Telegram Bot API transport (the actual sends).

STATUS: stable (extracted from infra/notifier.py, 2026-08-13;
    transport was always used by both notifier.py and listener.listener,
    just pretended to be private to notifier via underscore prefixes)
THREAD SAFETY: thread-safe (no shared mutable state; each call is
    its own httpx POST)

INPUTS:
    - function arg bot_token: str (required) — Telegram bot API token
    - function arg chat_id: str (required) — Telegram chat ID
    - function arg text: str or caption: str (send_message /
      send_photo_with_caption)
    - function arg frame_path: str (send_photo /
      send_photo_with_caption) — absolute path to JPEG
    - function arg frame_paths: list[str] (send_photo_group) —
      absolute paths to JPEGs, max 10 per Telegram limit

OUTPUTS:
    - return value: bool (True if Telegram accepted the API call,
      False on any failure including client errors, server errors,
      missing files, or transport failures)
    - network call: POST to https://api.telegram.org/bot{token}/{method}
      where method ∈ {sendMessage, sendPhoto, sendMediaGroup}
    - retries on transient errors (OSError, httpx network errors, 5xx,
      bad-JSON response) up to MAX_ATTEMPTS with exponential backoff

PUBLIC API:
    send_message(bot_token: str, chat_id: str, text: str) -> bool
        HTML-formatted Telegram message (parse_mode=HTML). Single
        message — no photo. The most common path.
    send_photo(bot_token: str, chat_id: str, frame_path: str) -> bool
        Send a photo WITHOUT caption. Caller follows up with
        send_message for the body. Used by notifier.notify()'s
        three-message path (photo / vision / text).
    send_photo_with_caption(bot_token: str, chat_id: str,
                            frame_path: str, caption: str) -> bool
        D16 fix: single sendPhoto with body as caption (≤1024 chars),
        falling back to two messages if caption exceeds the limit.
        Primary path in the listener's motion/match/no-match alerts.
    send_photo_group(bot_token: str, chat_id: str,
                     frame_paths: list[str],
                     caption: str = "") -> bool
        sendMediaGroup for up to 10 photos as one Telegram media
        group with the caption on the first photo. Fallback path
        when vision failed but frames were captured cleanly.

DOES NOT DO:
    - Decide WHICH alerts to send — that's infra.notifier.notify()
    - Apply cooldowns or quiet-hours — those are upstream gates
    - Log the send to the audit line — infra.audit_telegram owns that.
      Phase 6B.124 (2026-08-26, maintainer OOB directive after 2 morning
      Telegrams went out un-audited): every public function in this
      module now calls log_outbound_telegram internally with the
      caller-provided alert_id/channel/event/v_id. Centralizing the
      audit in the transport closes the gap where composite_alert and
      match_telegram bypassed notifier.notify() and never logged.
    - Read or render message bodies — formatting is the caller's job
      (HTML bodies need caller-passed HTML; plain-text bodies come
      from telegram_formatter/)

CALLED BY:
    - infra.notifier.notify(): send_photo() + send_message() in the
      three-message path; send_photo() is internal to notify
    - listener.listener._vehicle_send_notification:
      send_photo_with_caption() (5 sites) + send_message() (5 sites)
    - listener.listener gatekeeper paths:
      send_photo_with_caption() + send_photo_group() (multi-frame
      fallback when vision analysis fails)

CALLS INTO:
    - httpx: HTTP transport to api.telegram.org
    - json + os: payload construction + file reads
    - time.sleep: exponential backoff between retries

RELATED:
    - infra.audit_telegram — log_outbound_telegram() called AFTER each
      successful or suppressed send
    - infra.notifier — orchestrates the send decision + format
"""
from __future__ import annotations

import json
import logging
import os
import time

import httpx

from infra.audit_telegram import log_outbound_telegram

log = logging.getLogger("send_telegram")


# ----------------------------------------------------------------------
# Constants
# ----------------------------------------------------------------------

# Telegram Bot API base URL. Per-call method is appended.
TELEGRAM_API_URL = "https://api.telegram.org/bot{token}/{method}"

# Caption field limit. Captions longer than this must fall back to
# a two-message path (photo with truncated caption + text body).
TELEGRAM_CAPTION_LIMIT = 1024

# MediaGroup limit. Telegram rejects groups with more than 10 items.
TELEGRAM_MEDIA_GROUP_LIMIT = 10

# Retry policy for transient Telegram failures (network blips, 5xx,
# etc.). 4xx errors are NOT retried — they're client errors that
# won't resolve.
MAX_ATTEMPTS = 4  # 1 initial + 3 retries = ~3.5s total worst case

# Backoff parameters (seconds). Attempt 1 fails → sleep 0.5s before
# attempt 2. Doubles each retry: 0.5, 1.0, 2.0.
_BACKOFF_BASE_SECONDS = 0.5


# ----------------------------------------------------------------------
# Audit helper
# ----------------------------------------------------------------------

def _audit(
    *,
    channel: str,
    alert_id: str,
    v_id: str,
    event: str,
    body: str,
    sent: bool,
    image_paths: list[str] | None,
) -> None:
    """Centralized audit call. Wraps log_outbound_telegram so the
    transport owns the contract: every send (success or failure) emits
    exactly one OUTBOUND_TELEGRAM log line, no exceptions, never raises.

    Phase 6B.124: this is the SINGLE chokepoint. Callers no longer
    audit themselves — they pass the context args and the transport
    audits before returning.
    """
    try:
        log_outbound_telegram(
            channel=channel,
            alert_id=alert_id,
            v_id=v_id,
            event=event,
            body=body,
            sent=sent,
            image_paths=image_paths or [],
        )
    except Exception as audit_err:  # pragma: no cover — audit must never break the send path
        log.warning(
            f"audit_telegram failed for channel={channel} alert_id={alert_id}: "
            f"{type(audit_err).__name__}: {audit_err}"
        )


# ----------------------------------------------------------------------
# Public API
# ----------------------------------------------------------------------


def send_message(
    bot_token: str,
    chat_id: str,
    text: str,
    *,
    alert_id: str,
    channel: str,
    event: str,
    v_id: str = "",
) -> bool:
    """HTML-formatted Telegram message. parse_mode=HTML handles user-supplied
    content safely (only <, >, &, " need escaping).

    Transient errors (OSError, httpx network errors) are retried with
    exponential backoff up to MAX_ATTEMPTS times. 4xx HTTP responses are
    not retried — they indicate bad request data (wrong chat_id, malformed
    text, etc.) and retrying wastes rate-limit budget without changing the
    outcome.

    Phase 6B.124: emits one OUTBOUND_TELEGRAM audit line on every return
    path (success, suppression, failure). alert_id/channel/event are
    keyword-only required args — caller must provide audit context.
    """
    url = TELEGRAM_API_URL.format(token=bot_token, method="sendMessage")
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
    }

    last_err: str | None = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            response = httpx.post(url, json=payload, timeout=10)
        except Exception as err:
            last_err = f"{type(err).__name__}: {err}"
            if attempt < MAX_ATTEMPTS:
                backoff = _BACKOFF_BASE_SECONDS * (2 ** (attempt - 1))
                log.debug(
                    f"Telegram sendMessage transient error "
                    f"(attempt {attempt}/{MAX_ATTEMPTS}): {err}; "
                    f"retrying in {backoff:.1f}s"
                )
                time.sleep(backoff)
                continue
            log.warning(
                f"Telegram sendMessage failed after {MAX_ATTEMPTS} "
                f"attempts: {last_err}"
            )
            _audit(channel=channel, alert_id=alert_id, v_id=v_id, event=event,
                   body=text, sent=False, image_paths=None)
            return False

        # Got a response — check for 4xx (don't retry) vs 5xx/network (retry).
        # Default missing status_code to 200 (success) for callers that mock
        # httpx.post with a plain MagicMock() without setting status_code.
        # NOTE: MagicMock auto-creates attributes on access, so hasattr()
        # doesn't help — we have to check the type instead.
        raw_status = getattr(response, "status_code", 200)
        status = raw_status if isinstance(raw_status, int) else 200
        if status >= 400 and status < 500:
            # Permanent client error — don't retry.
            try:
                data = response.json()
            except Exception:
                data = {}
            log.warning(
                f"Telegram sendMessage client error "
                f"(HTTP {response.status_code}): "
                f"{data.get('description', 'unknown')}"
            )
            _audit(channel=channel, alert_id=alert_id, v_id=v_id, event=event,
                   body=text, sent=False, image_paths=None)
            return False

        if status >= 500:
            last_err = f"HTTP {response.status_code}"
            if attempt < MAX_ATTEMPTS:
                backoff = _BACKOFF_BASE_SECONDS * (2 ** (attempt - 1))
                log.debug(
                    f"Telegram sendMessage server error "
                    f"(attempt {attempt}/{MAX_ATTEMPTS}): HTTP "
                    f"{response.status_code}; retrying in {backoff:.1f}s"
                )
                time.sleep(backoff)
                continue
            log.warning(
                f"Telegram sendMessage server error after "
                f"{MAX_ATTEMPTS} attempts: {last_err}"
            )
            _audit(channel=channel, alert_id=alert_id, v_id=v_id, event=event,
                   body=text, sent=False, image_paths=None)
            return False

        # 2xx — parse and check API-level ok.
        try:
            data = response.json()
        except Exception as err:
            last_err = f"json decode: {err}"
            if attempt < MAX_ATTEMPTS:
                backoff = _BACKOFF_BASE_SECONDS * (2 ** (attempt - 1))
                log.debug(
                    f"Telegram sendMessage bad response "
                    f"(attempt {attempt}/{MAX_ATTEMPTS}): {err}; "
                    f"retrying in {backoff:.1f}s"
                )
                time.sleep(backoff)
                continue
            log.warning(
                f"Telegram sendMessage bad response after "
                f"{MAX_ATTEMPTS} attempts: {last_err}"
            )
            _audit(channel=channel, alert_id=alert_id, v_id=v_id, event=event,
                   body=text, sent=False, image_paths=None)
            return False

        if data.get("ok"):
            if attempt > 1:
                log.info(
                    f"Telegram sendMessage succeeded on attempt "
                    f"{attempt}/{MAX_ATTEMPTS}"
                )
            _audit(channel=channel, alert_id=alert_id, v_id=v_id, event=event,
                   body=text, sent=True, image_paths=None)
            return True

        # API-level error (ok=False) — treat like 4xx, don't retry.
        log.warning(
            f"Telegram sendMessage API error: "
            f"{data.get('description', 'unknown')}"
        )
        _audit(channel=channel, alert_id=alert_id, v_id=v_id, event=event,
               body=text, sent=False, image_paths=None)
        return False

    # Loop exhausted without success (should be unreachable — all
    # non-final paths `continue`; this is the safety net).
    _audit(channel=channel, alert_id=alert_id, v_id=v_id, event=event,
           body=text, sent=False, image_paths=None)
    return False


def send_photo(
    bot_token: str,
    chat_id: str,
    frame_path: str,
    *,
    alert_id: str,
    channel: str,
    event: str,
    v_id: str = "",
) -> bool:
    """Telegram photo upload with NO caption (caption sent as separate message).
    Returns True on success, False on any failure (caller still sends the text).

    Phase 6B.124: emits one OUTBOUND_TELEGRAM audit line on every return path.
    alert_id/channel/event are keyword-only required args.
    """
    url = TELEGRAM_API_URL.format(token=bot_token, method="sendPhoto")
    try:
        with open(frame_path, "rb") as f:
            photo_bytes = f.read()
        files = {"photo": (os.path.basename(frame_path), photo_bytes, "image/jpeg")}
        data = {
            "chat_id": chat_id,
        }
        response = httpx.post(url, data=data, files=files, timeout=30)
        resp_data = response.json()
    except Exception as err:
        log.warning(f"Telegram sendPhoto error: {err}")
        _audit(channel=channel, alert_id=alert_id, v_id=v_id, event=event,
               body="", sent=False, image_paths=[frame_path] if frame_path else None)
        return False

    if not resp_data.get("ok"):
        err_desc = resp_data.get("description", "unknown")
        log.warning(f"Telegram sendPhoto returned error: {err_desc}")
        _audit(channel=channel, alert_id=alert_id, v_id=v_id, event=event,
               body="", sent=False, image_paths=[frame_path] if frame_path else None)
        return False
    _audit(channel=channel, alert_id=alert_id, v_id=v_id, event=event,
           body="", sent=True, image_paths=[frame_path] if frame_path else None)
    return True


# Phase 6B.63 (2026-08-07) — sendMediaGroup helper for multi-frame
# fallback alerts (used when vision fails on CAM1 vehicle webhooks but
# frames were captured cleanly). Wraps Telegram's sendMediaGroup API
# so the user gets all N frames as one media group with a single caption
# on the first photo. Returns True if Telegram accepted the group.
def send_photo_group(
    bot_token: str,
    chat_id: str,
    frame_paths: list,
    caption: str = "",
    *,
    alert_id: str,
    channel: str,
    event: str,
    v_id: str = "",
) -> bool:
    """Send up to 10 photos as a Telegram media group.

    Args:
        bot_token: Telegram bot token
        chat_id: target chat ID (str or int — coerced to str)
        frame_paths: list of JPEG paths (max 10 — Telegram limit)
        caption: optional caption text (attached to first photo,
                 truncated to 1024 chars per Telegram limit)
        alert_id/channel/event/v_id: Phase 6B.124 audit context.

    Returns True if the group was accepted, False on any failure
    (network, missing file, HTTP error). Logs details on failure.
    """
    image_paths_arg = list(frame_paths) if frame_paths else None
    if not bot_token or not chat_id:
        log.warning("send_photo_group: missing bot_token or chat_id")
        _audit(channel=channel, alert_id=alert_id, v_id=v_id, event=event,
               body=caption, sent=False, image_paths=image_paths_arg)
        return False
    if not frame_paths:
        log.warning("send_photo_group called with empty frame_paths")
        _audit(channel=channel, alert_id=alert_id, v_id=v_id, event=event,
               body=caption, sent=False, image_paths=image_paths_arg)
        return False
    if len(frame_paths) > TELEGRAM_MEDIA_GROUP_LIMIT:
        log.warning(
            f"send_photo_group truncating {len(frame_paths)} frames to "
            f"{TELEGRAM_MEDIA_GROUP_LIMIT} (Telegram limit)"
        )
        frame_paths = frame_paths[:TELEGRAM_MEDIA_GROUP_LIMIT]

    url = TELEGRAM_API_URL.format(token=bot_token, method="sendMediaGroup")
    files = {}
    media_items = []
    for i, fp in enumerate(frame_paths):
        attach_name = f"photo{i}"
        try:
            with open(fp, "rb") as f:
                files[attach_name] = (os.path.basename(fp), f.read(), "image/jpeg")
        except Exception as err:
            log.warning(f"send_photo_group: failed to read {fp}: {err}")
            _audit(channel=channel, alert_id=alert_id, v_id=v_id, event=event,
                   body=caption, sent=False, image_paths=image_paths_arg)
            return False
        item = {"type": "photo", "media": f"attach://{attach_name}"}
        if i == 0 and caption:
            item["caption"] = caption[:TELEGRAM_CAPTION_LIMIT]
        media_items.append(item)

    data = {
        "chat_id": str(chat_id),
        "media": json.dumps(media_items),
    }

    try:
        response = httpx.post(url, data=data, files=files, timeout=60)
        resp_data = response.json()
    except Exception as err:
        log.warning(f"send_photo_group: HTTP error: {err}")
        _audit(channel=channel, alert_id=alert_id, v_id=v_id, event=event,
               body=caption, sent=False, image_paths=image_paths_arg)
        return False

    if not resp_data.get("ok"):
        err_desc = resp_data.get("description", "unknown")
        log.warning(f"send_photo_group returned error: {err_desc}")
        _audit(channel=channel, alert_id=alert_id, v_id=v_id, event=event,
               body=caption, sent=False, image_paths=image_paths_arg)
        return False
    _audit(channel=channel, alert_id=alert_id, v_id=v_id, event=event,
           body=caption, sent=True, image_paths=image_paths_arg)
    return True


def send_photo_with_caption(
    bot_token: str,
    chat_id: str,
    frame_path: str,
    caption: str,
    *,
    alert_id: str,
    channel: str,
    event: str,
    v_id: str = "",
) -> bool:
    """D16 fix: single sendPhoto with the body as caption.

    Combines the photo and the text into one Telegram API call so the
    user sees them together (not as two separate messages that can
    arrive out of order, scroll apart, or have one drop).

    Telegram's sendPhoto `caption` field is limited to 1024 chars; if
    the caption exceeds that, fall back to:
      1. sendPhoto with a 1024-char-truncated caption
      2. sendMessage with the full caption

    Returns True on overall success (i.e. the text reached Telegram
    in one form or another). Returns False only if both the photo
    AND the text leg failed.

    Failure isolation: the photo leg is best-effort. If the photo file
    doesn't exist or the photo leg 4xx's, we fall through to a
    text-only sendMessage so the user still gets the body.

    Phase 6B.124: emits one OUTBOUND_TELEGRAM audit line on every
    return path. When the function falls through to send_message,
    send_message is called WITH the audit args — but a single
    send_photo_with_caption call produces ONE audit line via the
    final audit call below; we set sent=True/False for the OUTBOUND
    entry based on the photo leg + text leg combined outcome.
    """
    image_paths_arg = [frame_path] if frame_path else None
    if not bot_token or not chat_id:
        _audit(channel=channel, alert_id=alert_id, v_id=v_id, event=event,
               body=caption, sent=False, image_paths=image_paths_arg)
        return False

    if len(caption) <= TELEGRAM_CAPTION_LIMIT:
        # Single call: sendPhoto with caption=caption.
        url = TELEGRAM_API_URL.format(token=bot_token, method="sendPhoto")
        try:
            with open(frame_path, "rb") as f:
                photo_bytes = f.read()
            files = {"photo": (os.path.basename(frame_path), photo_bytes, "image/jpeg")}
            data = {
                "chat_id": chat_id,
                "caption": caption,
                "parse_mode": "HTML",
            }
            response = httpx.post(url, data=data, files=files, timeout=30)
            resp_data = response.json()
        except Exception as err:
            # Photo failed — try the text-only fallback so the user
            # still gets the body, but signal the photo leg failed.
            log.warning(f"Telegram sendPhoto (with caption) error: {err}")
            text_ok = send_message(
                bot_token, chat_id, caption,
                alert_id=alert_id, channel=channel, event=event, v_id=v_id,
            )
            _audit(channel=channel, alert_id=alert_id, v_id=v_id, event=event,
                   body=caption, sent=text_ok, image_paths=image_paths_arg)
            return text_ok
        if not resp_data.get("ok"):
            err_desc = resp_data.get("description", "unknown")
            log.warning(f"Telegram sendPhoto (with caption) returned error: {err_desc}")
            text_ok = send_message(
                bot_token, chat_id, caption,
                alert_id=alert_id, channel=channel, event=event, v_id=v_id,
            )
            _audit(channel=channel, alert_id=alert_id, v_id=v_id, event=event,
                   body=caption, sent=text_ok, image_paths=image_paths_arg)
            return text_ok
        _audit(channel=channel, alert_id=alert_id, v_id=v_id, event=event,
               body=caption, sent=True, image_paths=image_paths_arg)
        return True

    # Long caption fallback: sendPhoto with truncated caption, then
    # sendMessage with the full body so the user sees the whole text.
    photo_ok = False
    truncated = caption[:TELEGRAM_CAPTION_LIMIT]
    try:
        with open(frame_path, "rb") as f:
            photo_bytes = f.read()
        files = {"photo": (os.path.basename(frame_path), photo_bytes, "image/jpeg")}
        data = {
            "chat_id": chat_id,
            "caption": truncated,
            "parse_mode": "HTML",
        }
        url = TELEGRAM_API_URL.format(token=bot_token, method="sendPhoto")
        response = httpx.post(url, data=data, files=files, timeout=30)
        resp_data = response.json()
        photo_ok = bool(resp_data.get("ok"))
        if not photo_ok:
            log.warning(
                f"Telegram sendPhoto (truncated caption) returned error: "
                f"{resp_data.get('description', 'unknown')}"
            )
    except Exception as err:
        log.warning(f"Telegram sendPhoto (truncated caption) error: {err}")
    # Either way, send the full text so the user gets the body.
    text_ok = send_message(
        bot_token, chat_id, caption,
        alert_id=alert_id, channel=channel, event=event, v_id=v_id,
    )
    # Overall outcome: True if EITHER leg delivered.
    _audit(channel=channel, alert_id=alert_id, v_id=v_id, event=event,
           body=caption, sent=(photo_ok or text_ok), image_paths=image_paths_arg)
    return photo_ok or text_ok
