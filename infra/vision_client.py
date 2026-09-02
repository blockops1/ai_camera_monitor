"""
vision_client.py — HTTP transport for vision LLM calls.

STATUS: stable
THREAD SAFETY: thread-safe (httpx.Client is per-call, no shared state)

INPUTS:
    - function arg api_url: str (required) — chat completions URL.
      Pass an explicit URL for tests / one-off scripts. Pass the
      module-level DEFAULT_URL (or None) to use the configured vision
      endpoint loaded from infra.llm_config.
    - function arg payload: dict (required) — request body
    - env: VISION_LLM_URL, VISION_LLM_TOKEN, VISION_LLM_MODEL — see
      infra.llm_config (load_vision_config)
    - file llm-creds.env — see infra.llm_config

OUTPUTS:
    - return value: dict — parsed JSON response from llama-server
    - raises: httpx.HTTPError on network failures (caller handles)

PUBLIC API:
    DEFAULT_URL
        Resolves at import time to whatever infra.llm_config returns.
        Defaults to "http://127.0.0.1:8093/v1/chat/completions"
        (Phase 6B.158 / §11.81 — unified Qwen3.6-35B-A3B server;
         was 8080 with Qwen3-VL-8B before the swap).
    TIMEOUT = 150
        Seconds per frame batch. Bumped 60s → 150s in Phase 6B.61
        (2026-08-07) to accommodate 6-frame combined prompts (90-120s).
    _post_to_vision(api_url: str, payload: dict) -> dict
        Send `payload` to vision via httpx. If api_url is None,
        substitute the configured URL. Adds Bearer Authorization
        header when llm_config.load_vision_config().token is non-empty.
        Returns parsed JSON dict. Raises on failure — caller decides
        whether to swallow.

DOES NOT DO:
    - Build prompts — infra.prompt_templates owns that
    - Parse responses — infra.vision_response owns that
    - Queue / serialize calls — infra.vision_queue + infra.vision_analyzer
      handle that
    - Decide what camera or event_hint means — pure transport

WHY HERE:
    The HTTP layer is shared between `analyze_frames` (multi-frame batch)
    and `classify_vehicle_crop` (single-crop pass). Both need to:
      1. Send a payload to the vision chat completions endpoint
      2. Use the configured URL (env-driven since 6B.146) when no
      explicit api_url= is passed
      3. Add Bearer auth header when llm_config.token is set
    Pulling this out lets tests mock one transport for all callers.

CALLED BY:
    - infra.vision_analyzer.analyze_frames (orchestrator)
    - infra.vision_analyzer.classify_vehicle_crop (crop-classify orchestrator)

CALLS INTO:
    - infra.llm_config: load_vision_config() — url + auth_headers()
    - httpx: direct HTTP transport (single endpoint; vision pool
      was removed in Phase 6B.147)

RELATED:
    - infra/llm_config.py — VISION_LLM_* env vars and llm-creds.env
    - infra/vision_analyzer.py — orchestrator that calls into this module
"""

import httpx

from infra.llm_config import load_vision_config

# Resolved at import time from infra.llm_config so tests that need
# a specific value can monkey-patch llm_config.reset_for_tests() +
# VISION_LLM_URL before importing this module. Default preserved:
# 127.0.0.1:8093 with no auth — Phase 6B.158 / §11.81 unified
# Qwen3.6-35B-A3B server (was 8080 with Qwen3-VL-8B pre-§11.81).
DEFAULT_URL = load_vision_config().url
# Phase 6B.61 (2026-08-07): bumped 60s → 150s. Multi-frame (6 frames)
# combined prompt takes 90-120s on Qwen3-VL with --parallel 1; 60s httpx
# timeout was cutting off legitimate long-running analysis. With overflow
# drop policy in place, low-priority work can't starve gatekeeper/perimeter
# calls, so we can afford a longer per-call budget.
TIMEOUT = 150  # seconds per frame batch


def _post_to_vision(api_url: str | None, payload: dict) -> dict:
    """
    Send `payload` to vision. If api_url is None, use the configured
    URL from infra.llm_config. Adds Bearer Authorization header when
    llm_config.load_vision_config().token is non-empty.

    Returns parsed JSON dict (same shape as httpx.post().json()).
    Raises on failure — caller decides whether to swallow.
    """
    url = api_url if api_url else DEFAULT_URL
    # Reload config each call (cheap — module-level lru_cache) so
    # tests that reset_for_tests() and monkey-patch env vars see the
    # new values without restarting the listener.
    headers = load_vision_config().auth_headers()
    r = httpx.post(url, json=payload, headers=headers, timeout=TIMEOUT)
    r.raise_for_status()
    parsed: dict = r.json()
    return parsed