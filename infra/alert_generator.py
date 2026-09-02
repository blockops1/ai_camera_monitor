"""
alert_generator.py — Orchestrate alert generation with Qwen3.5-9B + overrides.

STATUS: stable
THREAD SAFETY: thread-safe (httpx.Client is per-call, no shared state)

INPUTS:
    - function arg vision_result: dict (required) — Qwen-Vision's
      structured output (vehicles[], colors, etc.)
    - function arg camera_name: str (required)
    - function arg timestamp: str (required, ISO 8601)
    - function arg source: str (required, "motion" | "match" | ...)
    - function arg api_url: str (optional, default DEFAULT_URL)
    - config/alert_overrides.json (loaded by alert_overrides_baseline)

OUTPUTS:
    - return value: dict matching the Alert Output JSON schema
      (threat_level always present; on failure, returns the error sentinel
      with threat_level=-1)
    - network call: POST to {api_url}/v1/chat/completions
    - log line on every generation (success or failure)

PUBLIC API:
    DEFAULT_URL
        Resolves at import time to whatever infra.llm_config returns.
        Defaults to "http://127.0.0.1:8093/v1/chat/completions"
        (Phase 6B.158 / §11.81 — unified Qwen3.6-35B-A3B server;
         was 8081 with Qwen3.5-9B before the swap).
    TIMEOUT = 90  (seconds; generous — local MPS is slow but reliable)
    generate_alert(vision_result: dict, camera_name: str,
                   timestamp: str, source: str = "rtsp_frames",
                   api_url: str = DEFAULT_URL) -> dict
        Generate a structured alert from a vision result. Always
        returns a dict; failure cases return the error sentinel
        {"threat_level": -1, ...} so the caller never has to handle None.
        Order of operations:
          1. Build prompt payload (infra.alert_prompt._build_payload)
          2. POST to LLM (httpx) with Bearer auth header when token set
          3. Parse response (infra.alert_prompt._parse_response)
          4. On parse failure: retry once with same payload
          5. On retry failure: return error sentinel
          6. Apply off-hours escalation (infra.alert_overrides_offhours)
          7. Apply 4 baseline suppressions (infra.alert_overrides_baseline)

DOES NOT DO:
    - HTTP transport implementation → uses httpx directly; could be
      extracted to infra.alert_client in a follow-on if needed
    - Prompt building / parsing → infra.alert_prompt
    - Off-hours escalation → infra.alert_overrides_offhours
    - Baseline suppressions → infra.alert_overrides_baseline
    - Send the alert to Telegram → infra.notifier

WHY HERE:
    Splits an 877-line monolith into 4 single-concern modules per
    PLAN.md Part 9 step 5. The orchestrator owns the sequence
    (LLM call → parse → retry → off-hours → baseline) and the
    decision points (which override chain to run, which error to return).
    Other modules are pure functions — this is the only stateful module
    (network I/O). Re-exports every extracted symbol so existing
    `from infra.alert_generator import _apply_off_hours_override` callers
    keep working without changes.

CALLED BY:
    - listener.listener._process_alert: generate_alert()
    - infra.alert_history.append_alert: generate_alert() (comment ref)
    - infra.notifier.notify: generate_alert() (comment ref)

CALLS INTO:
    - infra.alert_prompt: SYSTEM_PROMPT, _build_payload, _parse_response,
      _error_result, _to_local_iso
    - infra.alert_overrides_offhours: _apply_off_hours_override
    - infra.alert_overrides_baseline: _apply_baseline_overrides
    - infra.llm_config: load_text_config() — URL + Bearer auth header
    - httpx: POST to text LLM
    - json: parse LLM response

RELATED:
    - config/alert_overrides.json — loaded by alert_overrides_baseline at import
    - infra.alert_history.append_alert — consumer of generated alerts
    - infra.notifier.notify — next pipeline stage
"""

import json
import logging
import uuid

import httpx

from infra.alert_overrides_baseline import _apply_baseline_overrides
from infra.alert_overrides_offhours import _apply_off_hours_override
from infra.alert_prompt import _build_payload, _error_result, _parse_response
from infra.llm_config import load_text_config

# Resolved at import time from infra.llm_config. Defaults to
# 127.0.0.1:8093 with no auth — Phase 6B.158 / §11.81 unified
# Qwen3.6-35B-A3B server (was 8081 with Qwen3.5-9B pre-§11.81).
DEFAULT_URL = load_text_config().url
TIMEOUT = 90  # Generous — local MPS is slow but reliable. Property-context prompt + JSON output takes ~70s in worst case.

log = logging.getLogger("alert_generator")


def generate_alert(
    vision_result: dict,
    camera_name: str,
    timestamp: str,
    source: str = "rtsp_frames",
    api_url: str = DEFAULT_URL,
) -> dict:
    """
    Generate a structured alert from vision analysis results.

    Deterministic safety net:
        If vision sees a person during off-hours (20:00 – 06:00) and the model
        returns Level 0 (or an error), this function escalates to Level 1
        regardless of model output. The LLM is advisory; this rule is absolute.
    """
    payload = _build_payload(vision_result, camera_name, timestamp, source)
    # Reload each call so test monkey-patches + reset_for_tests() pick up.
    _text_headers = load_text_config().auth_headers()

    try:
        response = httpx.post(
            api_url, json=payload, headers=_text_headers, timeout=TIMEOUT
        )
        response.raise_for_status()
        data = response.json()
    except (httpx.HTTPError, httpx.TimeoutException, json.JSONDecodeError) as err:
        log.error(f"API call failed: {err}")
        alert = _apply_off_hours_override(
            _error_result(str(err)), vision_result, timestamp
        )
        return _apply_baseline_overrides(alert, vision_result, camera_name, timestamp)

    choices = data.get("choices", [])
    raw = ""
    if choices:
        msg = choices[0].get("message", {})
        # Qwen3.5-9B puts response in reasoning_content
        raw = msg.get("reasoning_content", "") or msg.get("content", "")

    result = _parse_response(raw)
    if result:
        result.setdefault("alert_id", str(uuid.uuid4()))
        result.setdefault("camera", camera_name)
        result.setdefault("timestamp", timestamp)
        result.setdefault("source", source)
        alert = _apply_off_hours_override(result, vision_result, timestamp)
        return _apply_baseline_overrides(alert, vision_result, camera_name, timestamp)

    # Retry once on parse failure
    log.warning("First parse failed, retrying...")
    try:
        response = httpx.post(
            api_url, json=payload, headers=_text_headers, timeout=TIMEOUT
        )
        response.raise_for_status()
        data = response.json()
        choices = data.get("choices", [])
        if choices:
            msg = choices[0].get("message", {})
            # Qwen3.5-9B puts response in reasoning_content
            raw = msg.get("reasoning_content", "") or msg.get("content", "")
        result = _parse_response(raw)
        if result:
            result.setdefault("alert_id", str(uuid.uuid4()))
            result.setdefault("camera", camera_name)
            result.setdefault("timestamp", timestamp)
            result.setdefault("source", source)
            alert = _apply_off_hours_override(result, vision_result, timestamp)
            return _apply_baseline_overrides(alert, vision_result, camera_name, timestamp)
    except (json.JSONDecodeError, KeyError, AttributeError, TypeError) as err2:
        log.error(f"Retry parse failed: {err2}")

    alert = _apply_off_hours_override(
        _error_result(f"could not parse response: {raw[:100]}"),
        vision_result,
        timestamp,
    )
    return _apply_baseline_overrides(alert, vision_result, camera_name, timestamp)


# ---------------------------------------------------------------------------
# Backward-compatible re-exports. Extracted modules own these symbols; the
# orchestrator re-exports them so existing
#   `from infra.alert_generator import _apply_off_hours_override`
# callers keep working without changes.
# ---------------------------------------------------------------------------

from infra.alert_overrides_baseline import (  # noqa: E402, F401
    _DISTANT_VEHICLE_KEYWORDS,
    _OVERRIDE_CONFIG,
    _apply_distant_vehicle_baseline_override,
    _apply_parked_vehicle_baseline_override,
    _apply_static_object_baseline_override,
    _apply_vision_none_baseline_override,
    _get_distant_vehicle_cameras,
    _get_parked_vehicle_cameras,
    _get_static_object_cameras,
    _get_vision_none_cameras,
    _load_override_config,
    _vision_returns_none,
    _vision_signals_distant_vehicle,
)
from infra.alert_overrides_offhours import (  # noqa: E402, F401
    OFF_HOURS_END_HOUR,
    OFF_HOURS_MIN_LEVEL,
    OFF_HOURS_START_HOUR,
    _is_off_hours,
    _vision_sees_person,
)
from infra.alert_prompt import (  # noqa: E402, F401
    SYSTEM_PROMPT,
    _to_local_iso,
)