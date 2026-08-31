"""
telegram_creds.py — Load Telegram bot token and chat ID from env file.

STATUS: stable
THREAD SAFETY: thread-safe (pure function, no shared state)

INPUTS:
    - function arg env_path: str (required) — absolute path to the
      env file containing TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID

OUTPUTS:
    - return value: TelegramCreds dataclass (bot_token, chat_id)
    - raises FileNotFoundError if env_path doesn't exist
    - raises ValueError if either key is missing or has an empty value

PUBLIC API:
    TelegramCreds — frozen dataclass:
        .bot_token: str
        .chat_id: str
    load_telegram_creds(env_path: str) -> TelegramCreds
        Parse the env file and return a populated TelegramCreds.

DOES NOT DO:
    - Validate the token with Telegram's getMe endpoint — caller decides
    - Default to empty strings — ValueError on missing, not silent None
    - Use python-dotenv — explicit parser, same pattern as
      frame_capture.load_camera_creds (no dotenv dependency)

WHY HERE:
    Mirrors the frame_capture.load_camera_creds pattern: explicit
    file path, custom parser, no dotenv dependency. Keeps the env
    files declarative (`KEY=value`) and the parser simple.

CALLED BY:
    - listener.listener.bootstrap: load_telegram_creds(TELEGRAM_CREDS_FILE)
    - tests: load_telegram_creds(fixture_path)

CALLS INTO:
    - stdlib only (os.path.exists, open, str.strip)

RELATED:
    - infra.frame_capture.load_camera_creds — same pattern, for cameras
    - infra.paths.TELEGRAM_CREDS_FILE — the path passed by callers
"""

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class TelegramCreds:
    """Bot token and chat ID for sending Telegram alerts."""

    bot_token: str
    chat_id: str


def load_telegram_creds(env_path: str) -> TelegramCreds:
    """
    Parse telegram-creds.env and return a TelegramCreds instance.

    Args:
        env_path: Absolute path to the env file containing TELEGRAM_BOT_TOKEN
            and TELEGRAM_CHAT_ID.

    Returns:
        TelegramCreds with bot_token and chat_id populated.

    Raises:
        FileNotFoundError: if env_path doesn't exist.
        ValueError: if either key is missing or has an empty value.
    """
    if not os.path.exists(env_path):
        raise FileNotFoundError(f"telegram creds file not found: {env_path}")

    creds = {}
    with open(env_path, "r") as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip()
            # Strip surrounding quotes if present
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
                value = value[1:-1]
            creds[key] = value

    token = creds.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = creds.get("TELEGRAM_CHAT_ID", "").strip()

    if not token:
        raise ValueError(
            f"TELEGRAM_BOT_TOKEN missing or empty in {env_path}. "
            "Add a line like: TELEGRAM_BOT_TOKEN=1234567890:ABC..."
        )
    if not chat_id:
        raise ValueError(
            f"TELEGRAM_CHAT_ID missing or empty in {env_path}. "
            "Add a line like: TELEGRAM_CHAT_ID=5264050975"
        )

    return TelegramCreds(bot_token=token, chat_id=chat_id)
