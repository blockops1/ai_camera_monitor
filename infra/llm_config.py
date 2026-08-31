"""
llm_config.py — Vision + text LLM endpoint config (URL, token, model).

STATUS: stable
THREAD SAFETY: thread-safe (module-level cached configs are immutable
    frozen dataclasses; loaders are pure parsers)

INPUTS:
    - function arg env_path: str (optional) — absolute path to the
      llm-creds.env file. Default: infra.paths.LLM_CREDS_FILE
      (~/ai_camera_monitor/llm-creds.env).
    - env VISION_LLM_URL — overrides file value
    - env VISION_LLM_TOKEN — overrides file value (Bearer auth)
    - env VISION_LLM_MODEL — overrides file value
    - env TEXT_LLM_URL — overrides file value
    - env TEXT_LLM_TOKEN — overrides file value (Bearer auth)
    - env TEXT_LLM_MODEL — overrides file value
    - file llm-creds.env — declarative KEY=value lines

OUTPUTS:
    - VisionLLMConfig, TextLLMConfig frozen dataclasses returned by
      load_vision_config() and load_text_config()
    - Defaults match the previously hard-coded values
      (127.0.0.1:8080 for vision, 127.0.0.1:8081 for text,
      qwen3-vl / qwen3.5). Zero behavior change when llm-creds.env
      is absent and no env vars are set.
    - log line on first load per process (INFO: "llm_config loaded")
    - Token is NEVER logged in any form — module enforces this.

PUBLIC API:
    VisionLLMConfig — frozen dataclass:
        .url: str   (chat completions URL; default
                     http://127.0.0.1:8080/v1/chat/completions)
        .token: str (empty → no Authorization header;
                     non-empty → Bearer auth)
        .model: str (default "qwen3-vl")
        .auth_headers() -> dict: returns {"Authorization":
            f"Bearer {token}"} when token set, else {}
    TextLLMConfig — frozen dataclass:
        .url: str   (default http://127.0.0.1:8081/v1/chat/completions)
        .token: str (default "")
        .model: str (default "qwen3.5")
        .auth_headers() -> dict
    _parse_llm_creds_file(env_path: str) -> dict[str, str]
        Parse llm-creds.env and return flat dict of KEY=value pairs.
        Returns {} when the file doesn't exist (no error).
        Skips blank lines, comments (#-prefix), and lines without '='.
    load_vision_config() -> VisionLLMConfig
        Lazy singleton. Reads file + env on first invocation.
    load_text_config() -> TextLLMConfig
        Lazy singleton. Reads file + env on first invocation.
    reset_for_tests() -> None
        Drop the cached singletons. Tests use this between cases so
        env-var monkey-patches take effect.

DOES NOT DO:
    - Validate that the URL is reachable — caller does that
    - Decide which model is "best" — operator's choice
    - Send any HTTP request — pure config loader
    - Log tokens — defensive contract

WHY HERE:
    Per AGENTS.md §4 (one module = one job), config loading was
    scattered across infra/vision_client.py (DEFAULT_URL = "..."),
    infra/alert_generator.py (DEFAULT_URL = "..."), and the
    hard-coded "model": "qwen3-vl" / "qwen3.5" strings in
    infra/vision_analyzer.py and infra/alert_prompt.py. Phase.146
    (the operator 2026-08-27: "set the system up so it can be more general
    in the type of LLM and vision model that it uses") consolidated
    the five references into one config module with three knobs
    (url, token, model) per endpoint. Operators can swap providers
    without code changes — point at OpenAI, Anthropic, a remote
    llama-server, or anything that speaks the OpenAI chat completions
    protocol.

CALLED BY:
    - infra.vision_client: load_vision_config() for url + auth_headers()
    - infra.alert_generator: load_text_config() for url + auth_headers()
    - infra.vision_analyzer: load_vision_config().model for payload
    - infra.alert_prompt: load_text_config().model for payload
    - listener.listener: indirectly (via the above)

CALLS INTO:
    - stdlib os.environ, os.path.exists, open, str.strip
    - infra.paths: LLM_CREDS_FILE (default env_path)
    - functools.lru_cache (singleton caching)

RELATED:
    - infra.camera_creds: load_camera_creds(env_path) — same pattern,
      structured per-camera dict
    - infra.telegram_creds: load_telegram_creds(env_path) — same
      pattern, raises on missing values (we don't, since defaults
      are intentional)
    - llm-creds.env.example: committed template
    - .gitignore: llm-creds.env excluded (token-bearing file)
"""

import logging
import os
from dataclasses import dataclass
from functools import lru_cache

from infra.paths import LLM_CREDS_FILE

log = logging.getLogger("llm_config")

# Phase.158 (2026-08-28) — §11.81 swap from Qwen3-VL-8B + Qwen3.5-9B
# (separate vision/text servers on 8080/8081/8082) to a single
# Qwen3.6-35B-A3B server on port 8093. The new server runs both vision
# AND text via the same model (it has mmproj-BF16.gguf attached for
# image understanding + plain text decoding). One URL serves both.
# Override via env vars or llm-creds.env if you ever split again.
_DEFAULT_LLM_URL = "http://127.0.0.1:8093/v1/chat/completions"
_DEFAULT_LLM_MODEL = "qwen3.6"


@dataclass(frozen=True)
class VisionLLMConfig:
    """Vision LLM endpoint config. Immutable after construction."""

    url: str
    token: str
    model: str

    def auth_headers(self) -> dict[str, str]:
        """Return Bearer auth header dict when token is set; empty otherwise.

        Caller should merge into httpx headers. Empty token → no header,
        so localhost llama-server (no auth) keeps working unchanged.
        """
        if self.token:
            return {"Authorization": f"Bearer {self.token}"}
        return {}


@dataclass(frozen=True)
class TextLLMConfig:
    """Text LLM endpoint config. Immutable after construction."""

    url: str
    token: str
    model: str

    def auth_headers(self) -> dict[str, str]:
        """Return Bearer auth header dict when token is set; empty otherwise."""
        if self.token:
            return {"Authorization": f"Bearer {self.token}"}
        return {}


def _parse_llm_creds_file(env_path: str) -> dict[str, str]:
    """Parse llm-creds.env and return flat dict of KEY=value pairs.

    Returns {} when the file doesn't exist (zero behavior change vs.
    the pre-6B.146 hard-coded defaults). Skips blank lines, comments,
    and lines without '='. Strips surrounding quotes from values so
    VISION_LLM_TOKEN="abc*** works as well as VISION_LLM_TOKEN=abc.
    """
    if not env_path or not os.path.exists(env_path):
        return {}

    result: dict[str, str] = {}
    try:
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
                if (
                    len(value) >= 2
                    and value[0] == value[-1]
                    and value[0] in ('"', "'")
                ):
                    value = value[1:-1]
                result[key] = value
    except OSError as err:
        # Don't crash the listener if the env file is unreadable;
        # fall back to defaults + log a warning. The credential file
        # is operator-supplied and could legitimately be absent.
        log.warning(
            "llm_config: failed to read %s (%s); using defaults", env_path, err
        )
        return {}

    return result


@lru_cache(maxsize=1)
def load_vision_config() -> VisionLLMConfig:
    """Load Vision LLM config from llm-creds.env + env vars.

    Resolution order (later wins):
      1. Built-in defaults (127.0.0.1:8093, qwen3.6, no token)
      2. Values from llm-creds.env
      3. Environment variables (VISION_LLM_URL, VISION_LLM_TOKEN,
         VISION_LLM_MODEL)

    Cached after first call. Use reset_for_tests() to clear.
    """
    file_values = _parse_llm_creds_file(LLM_CREDS_FILE)
    cfg = VisionLLMConfig(
        url=os.environ.get("VISION_LLM_URL")
        or file_values.get("VISION_LLM_URL")
        or _DEFAULT_LLM_URL,
        token=os.environ.get("VISION_LLM_TOKEN")
        or file_values.get("VISION_LLM_TOKEN", ""),
        model=os.environ.get("VISION_LLM_MODEL")
        or file_values.get("VISION_LLM_MODEL")
        or _DEFAULT_LLM_MODEL,
    )
    # Log everything EXCEPT the token. Tokens must never appear in
    # log files (per AGENTS.md + system memory).
    log.info(
        "llm_config: vision url=%s model=%s token_set=%s",
        cfg.url,
        cfg.model,
        bool(cfg.token),
    )
    return cfg


@lru_cache(maxsize=1)
def load_text_config() -> TextLLMConfig:
    """Load Text LLM config from llm-creds.env + env vars.

    Resolution order (later wins):
      1. Built-in defaults (127.0.0.1:8093, qwen3.6, no token)
      2. Values from llm-creds.env
      3. Environment variables (TEXT_LLM_URL, TEXT_LLM_TOKEN,
         TEXT_LLM_MODEL)

    Cached after first call. Use reset_for_tests() to clear.
    """
    file_values = _parse_llm_creds_file(LLM_CREDS_FILE)
    cfg = TextLLMConfig(
        url=os.environ.get("TEXT_LLM_URL")
        or file_values.get("TEXT_LLM_URL")
        or _DEFAULT_LLM_URL,
        token=os.environ.get("TEXT_LLM_TOKEN")
        or file_values.get("TEXT_LLM_TOKEN", ""),
        model=os.environ.get("TEXT_LLM_MODEL")
        or file_values.get("TEXT_LLM_MODEL")
        or _DEFAULT_LLM_MODEL,
    )
    log.info(
        "llm_config: text url=%s model=%s token_set=%s",
        cfg.url,
        cfg.model,
        bool(cfg.token),
    )
    return cfg


def reset_for_tests() -> None:
    """Drop the cached config singletons. Tests call this between cases
    so monkey-patched env vars take effect on the next load_* call.
    """
    load_vision_config.cache_clear()
    load_text_config.cache_clear()