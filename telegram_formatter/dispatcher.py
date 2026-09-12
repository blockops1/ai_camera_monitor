"""Telegram HTTP dispatcher — closes the v2 delivery gap.

Pure function `dispatch(messages, *, bot_token, chat_id, base_url)` that
takes the dicts pipeline.run() already builds (tg1/tg2/tg3 with caption +
photos) and POSTs them to api.telegram.org.

No fallback paths (per operator 2026-09-09). Empty bot_token or chat_id
raises ConfigError at call time. 4xx/5xx from api.telegram.org raises
DeliveryError. No retries, no silent drops.

Photo delivery uses sendMediaGroup with input_media_photo (pre-converted
JPEGs, q=88, max-dim 1920px cached on disk). type=photo renders inline in
the Telegram client (not as downloadable files).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger(__name__)


class ConfigError(RuntimeError):
    """Raised when bot_token or chat_id is empty/missing."""


class DeliveryError(RuntimeError):
    """Raised when api.telegram.org returns 4xx/5xx."""


def _sniff_mime(path: str) -> tuple[str, str]:
    """Sniff the first 16 bytes of a file to determine MIME type and suffix.

    Returns (mime_type, suffix) where suffix is the correct file extension
    ('.png', '.jpg', or '.bin' for unknown types).

    Raises logger.warning for unknown byte sequences.
    """
    data = Path(path).read_bytes()[:16]
    if not data:
        logger.warning("empty file at %s — sending as octet-stream", path)
        return "application/octet-stream", ".bin"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png", ".png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg", ".jpg"
    logger.warning("unknown magic bytes at %s — sending as octet-stream", path)
    return "application/octet-stream", ".bin"


def _require(bot_token: str, chat_id: str) -> None:
    """Fail loudly on empty config (no silent defaults, per operator 2026-09-09)."""
    if not bot_token or not chat_id:
        missing = []
        if not bot_token:
            missing.append("TELEGRAM_BOT_TOKEN")
        if not chat_id:
            missing.append("TELEGRAM_HOME_CHAT_ID")
        raise ConfigError(
            f"dispatcher: required env var(s) empty: {', '.join(missing)}"
        )


def _send_message(
    client: httpx.Client,
    bot_token: str,
    chat_id: str,
    caption: str,
    base_url: str,
) -> httpx.Response:
    """sendMessage — text-only delivery (TG#3)."""
    url = f"{base_url}/bot{bot_token}/sendMessage"
    return client.post(url, json={"chat_id": chat_id, "text": caption})


def _send_media_group(
    client: httpx.Client,
    bot_token: str,
    chat_id: str,
    caption: str,
    photo_paths: list[str],
    base_url: str,
) -> httpx.Response:
    """sendMediaGroup with input_media_photo — inline JPEG rendering (TG#1/TG#2).

    Uses type=photo so Telegram renders the images inline in the chat
    (not as downloadable files). Photos are pre-converted to JPEG by
    the caller (codec.encode_jpeg) before reaching this function.
    """
    url = f"{base_url}/bot{bot_token}/sendMediaGroup"
    media: list[dict[str, str]] = []
    for i, path in enumerate(photo_paths):
        attach_ref = f"attach://photo_{i}"
        media.append(
            {
                "type": "photo",
                "media": attach_ref,
            }
        )
        if i == 0:
            # Telegram requires the first media entry to also carry caption.
            media[-1]["caption"] = caption
    files: list[tuple[str, tuple[str, bytes, str]]] = []
    for i, path in enumerate(photo_paths):
        with open(path, "rb") as f:
            data = f.read()
        mime, suffix = _sniff_mime(path)
        files.append((f"photo_{i}", (f"photo_{i}{suffix}", data, mime)))
    return client.post(
        url,
        data={"chat_id": chat_id, "media": str(media).replace("'", '"')},
        files=files,
    )


def dispatch(
    messages: list[dict[str, Any]],
    *,
    bot_token: str,
    chat_id: str,
    base_url: str = "https://api.telegram.org",
    client: httpx.Client | None = None,
) -> list[httpx.Response]:
    """Send each TG#1/TG#2/TG#3 dict to api.telegram.org.

    Args:
        messages: list of dicts, each with keys 'caption' (str) and 'photos' (list of paths).
            Empty photos list => sendMessage. Non-empty => sendMediaGroup.
        bot_token: required, from env TELEGRAM_BOT_TOKEN.
        chat_id: required, from env TELEGRAM_HOME_CHAT_ID.
        base_url: override for testing (default api.telegram.org).
        client: optional httpx.Client. If None, a fresh Client is created.

    Returns:
        list of httpx.Response, one per message.

    Raises:
        ConfigError: bot_token or chat_id empty.
        DeliveryError: any 4xx/5xx response. Caller decides whether to surface
            this to the camera (daemon logs and continues, never returns 5xx).
    """
    _require(bot_token, chat_id)
    owns_client = client is None
    if owns_client:
        client = httpx.Client(timeout=10.0)
    try:
        responses: list[httpx.Response] = []
        for msg in messages:
            caption = msg.get("caption", "")
            photo_paths = msg.get("photos", []) or []
            if photo_paths:
                resp = _send_media_group(
                    client, bot_token, chat_id, caption, photo_paths, base_url
                )
            else:
                resp = _send_message(client, bot_token, chat_id, caption, base_url)
            responses.append(resp)
            if 400 <= resp.status_code < 600:
                # Don't bail — finish remaining messages, but raise at the end.
                pass
        # Check for any 4xx/5xx after the loop.
        for resp in responses:
            if 400 <= resp.status_code < 600:
                raise DeliveryError(
                    f"telegram api returned {resp.status_code}: {resp.text[:200]}"
                )
        return responses
    finally:
        if owns_client:
            client.close()
