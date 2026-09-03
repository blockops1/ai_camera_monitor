"""
vision_analyzer.py — Orchestrator for Qwen3-VL vision calls.

STATUS: stable
THREAD SAFETY: thread-safe (httpx.Client is per-call)

INPUTS:
    - function args: frame_paths, crop_path, camera_name, alert_id,
      event_hint, captured_at, mode, api_url, timeout_s, context
    - env: FARMSURV_COMBINED_PROMPT — opt-in for combined-prompt dispatch

OUTPUTS:
    - return value (analyze_frames): dict matching VISION_SCHEMA_JSON.
        On error: error sentinel from vision_response._error_result.
    - return value (analyze_frames_queued): same as analyze_frames.
        On queue timeout: error sentinel.
    - return value (classify_vehicle_crop): dict matching
        VEHICLE_CLASSIFY_SCHEMA. On error: error sentinel from
        vision_response._vehicle_classify_error.

PUBLIC API:
    DEFAULT_URL, TIMEOUT
        Re-exported from infra.vision_client (preserved for callers that
        import them from this module).
    analyze_frames(frame_paths, camera_name, api_url=DEFAULT_URL,
                   alert_id=None, event_hint=None, captured_at=None,
                   mode=None) -> dict
        One multi-frame analysis call. Returns validated vision result or
        error sentinel.
    analyze_frames_queued(frame_paths, camera_name, api_url=DEFAULT_URL,
                          timeout_s=QUEUE_CALLER_TIMEOUT_S, alert_id=None,
                          event_hint=None, captured_at=None, mode=None) -> dict
        Queue-serialized wrapper around analyze_frames. Blocks on the
        queue's future and applies retry-with-backoff on error sentinels.
        This is the function the listener imports for production use.
    classify_vehicle_crop(crop_path, api_url=DEFAULT_URL, context=None,
                          alert_id=None) -> dict
        Focused make/model classification on a single cropped vehicle.
        Returns validated classification result or error sentinel.
    QUEUE_CALLER_TIMEOUT_S = 95
        Seconds the queued wrapper waits for the queue future before
        timing out. Lower than TIMEOUT because the queue already has its
        own retries.

DOES NOT DO:
    - Build prompts — infra.prompt_templates owns that
    - Parse responses — infra.vision_response owns that
    - HTTP transport — infra.vision_client owns that
    - Decide schema content — infra.prompt_templates owns that

WHY HERE:
    The orchestrator is the only thing that ties the four concerns
    together: pick a prompt template → build the payload → call vision
    → parse the response → persist artifact if needed → retry once on
    parse failure. Splitting the orchestrator further would force the
    caller (listener) to assemble the pipeline manually, and every
    caller would have to maintain identical retry/artifact logic.
    Better to keep the pipeline locally defined here and re-export the
    extracted modules' primitives for backward compatibility.

    Backward compatibility: every public symbol that was in this file
    pre-split is re-exported from its new home:
      - vision_client:  DEFAULT_URL, TIMEOUT, _post_to_vision
      - prompt_templates:  PROMPT_TEMPLATE, VEHICLE_*_PROMPT_TEMPLATE,
        VEHICLE_CLASSIFY_PROMPT, VISION_SCHEMA_JSON, VEHICLE_CROP_SCHEMA_JSON,
        VEHICLE_CLASSIFY_SCHEMA, select_prompt_template, _build_event_hint_block,
        _build_messages
      - vision_response:  _parse_response, _try_recover_stringified_lists,
        _validate_vision_result, _error_result, _parse_vehicle_classify_response,
        _vehicle_classify_error
    The listener imports `analyze_frames_queued` directly and lazy-imports
    `classify_vehicle_crop` + 4 VEHICLE_*_PROMPT_TEMPLATE constants. Both
    paths resolve through the re-exports.

CALLED BY:
    - listener/listener.py:69 — `from infra.vision_analyzer import analyze_frames_queued`
    - listener/listener.py:934 — `from vision_analyzer import (VEHICLE_*)`
    - listener/listener.py:1385 — `from vision_analyzer import classify_vehicle_crop`
    - vehicle_identifier — uses select_prompt_template + analyze_frames via mode="crop"

CALLS INTO:
    - infra.vision_client._post_to_vision (HTTP transport)
    - infra.prompt_templates.select_prompt_template, _build_messages
      (prompt + schema selection)
    - infra.vision_response._parse_response, _validate_vision_result,
      _error_result, _parse_vehicle_classify_response, _vehicle_classify_error
      (response parsing + validation)
    - infra.vision_queue.get_queue (queue serialization)
    - vehicle_artifacts (artifact persistence; optional, lazy-imported)

RELATED:
    - infra/vision_client.py — single-endpoint HTTP transport
      (Phase.146; vision pool removed in 6B.147)
    - infra/prompt_templates.py — prompt text + JSON schemas
    - infra/vision_response.py — parse + validate + error sentinels
    - infra/vision_queue.py — single-flight priority queue
"""

import base64
import logging
import sys
import time

log = logging.getLogger(__name__)


# Re-exports — backward compat for callers that import these from
# infra.vision_analyzer. Listener/listener.py's lazy imports for the
# /status endpoint (VEHICLE_*_PROMPT_TEMPLATE) and classify_vehicle_crop
# resolve through this block.
# Phase.78 (2026-08-14) — VEHICLE_MOTION_PROMPT_TEMPLATE and
# VEHICLE_COMBINED_PROMPT_TEMPLATE removed from infra.prompt_templates.
# They are no longer re-exported here.
from infra.classify_schema import CLASSIFY_SCHEMA_JSON
from infra.llm_config import load_vision_config
from infra.prompt_templates import (
    PROMPT_TEMPLATE,
    VEHICLE_CLASSIFY_PROMPT,
    VEHICLE_CLASSIFY_SCHEMA,
    VEHICLE_CROP_SCHEMA_JSON,
    VISION_SCHEMA_JSON,
    _build_messages,
)
from infra.vision_client import (  # noqa: F401
    DEFAULT_URL,
    TIMEOUT,
    _post_to_vision,
)
from infra.vision_response import (  # noqa: F401
    _error_result,
    _parse_response,
    _parse_vehicle_classify_response,
    _try_recover_stringified_lists,
    _validate_vision_result,
    _vehicle_classify_error,
)

# Phase 6A queue caller timeout. The VisionQueue's per-slot ctx budget
# is 90s; the caller adds a small buffer and returns the error sentinel
# if the future doesn't resolve. Lower than TIMEOUT (150s) because the
# queue already retries once internally.
QUEUE_CALLER_TIMEOUT_S = 95


def analyze_frames(
    frame_paths: list[str],
    camera_name: str,
    api_url: str = DEFAULT_URL,
    alert_id: str | None = None,
    event_hint: str | None = None,
    captured_at: str | None = None,
    # Phase.66 (2026-08-08): prompt-mode override for the vehicle path.
    # None → use the existing auto-dispatch in select_prompt_template
    # (mode="static"/"moving"/"combined"). mode="crop" forces the new
    # identification-only VEHICLE_CROP_PROMPT_TEMPLATE, used by the
    # 3-crop vehicle identifier (Phase.65). Default None preserves
    # all existing call sites.
    mode: str | None = None,
) -> dict:
    """
    Analyze one or more frames with Qwen3-VL-8B.

    Args:
        frame_paths: List of absolute paths to JPEG files.
        camera_name: Human-readable camera label (e.g. "<FRIENDLY_NAME>").
        api_url: Full URL to the llama-server chat completions endpoint.
        alert_id: Optional alert ID for artifact persistence (vehicle_artifacts).
        event_hint: Optional camera-side event classification (e.g. "vehicle",
            "person", "motion", "animal"). When set, this is surfaced to the
            vision LLM in the prompt so it can align its scene_description
            with the trigger type. See `_build_event_hint_block` for details.

    Returns:
        A dict matching the Vision Analysis schema:
        {
            "objects_detected": [...],
            "primary_subject": "...",
            "actions": [...],
            "scene_description": "...",
            "confidence": 0.0-1.0,
            "notable_details": [...],
            "colors": {
                "vehicle": "black"|"white"|"gray"|"silver"|"red"|"blue"|"green"|
                            "yellow"|"brown"|"orange"|"other"|"unknown"|"none",
                "clothing_primary": "blue shirt"|null,
                "clothing_secondary": null,
                "other": "..."|null
            },
            "species": "dog"|"deer"|"bear"|...|null
        }
        On error, returns {"objects_detected": ["error"], ...}.
    """
    if not frame_paths:
        return _error_result("no frames provided")

    messages = _build_messages(
        frame_paths,
        camera_name,
        event_hint=event_hint,
        captured_at=captured_at,
        mode=mode,
    )
    payload = {
        "model": load_vision_config().model,
        "messages": messages,
        "temperature": 0,
        "max_tokens": 2048,
        # Phase.74 (2026-08-10) — schema dispatch by mode.
        # The crop prompt (mode="crop") asks for make/model/vehicle_features
        # at the TOP level of the JSON response. The legacy VISION_SCHEMA_JSON
        # has additionalProperties=false with a required field list
        # (objects_detected, primary_subject, actions, scene_description,
        # ...) that Qwen can't simultaneously satisfy with the crop prompt's
        # flat identification fields. Result: Qwen drops make/model/
        # vehicle_features from the response, extract_signature() sees only
        # {color: white, body_style_hint: pickup}, the matcher falls back to
        # color+type-only scoring, and name two's white Silverado gets
        # misidentified as name one's F-350 (alert ddeaefc5 false positive,
        # 14:02 EDT). VEHICLE_CROP_SCHEMA_JSON gives Qwen permission to
        # return the rich identification fields the crop prompt needs.
        # Top-level fallback path in extract_signature()
        # (vehicle_state.py:241) already handles this shape.
        "response_format": {
            "type": "json_schema",
            "name": (
                "classify"
                if mode == "classify"
                else "vehicle_crop"
                if mode == "crop"
                else "vision_analysis"
            ),
            "strict": True,
            "schema": (
                CLASSIFY_SCHEMA_JSON
                if mode == "classify"
                else VEHICLE_CROP_SCHEMA_JSON
                if mode == "crop"
                else VISION_SCHEMA_JSON
            ),
        },
    }

    # Audit trail (2026-07-24): if an alert_id is provided, persist the
    # prompt text + the list of frames that went to Qwen for this call.
    # The base64 image bodies are NOT saved (too big) — the filenames
    # tell the operator what was sent in what order.
    if alert_id:
        try:
            import vehicle_artifacts as _va
            _va.write_first_pass_request(alert_id, frame_paths, PROMPT_TEMPLATE)
        except Exception as _va_err:
            log.warning(f"vehicle_artifacts first-pass request save failed: {_va_err}")

    try:
        data = _post_to_vision(api_url, payload)
    except Exception as err:
        log.warning(f"API error: {err}")
        # §11.115.19: include 'raw' so cascade_call1 can post-mortem.
        # Without it, the cascade returns OTHER+fallback and the alert
        # is logged-only (no Telegram).
        err_result = _error_result(str(err))
        err_result["raw"] = ""
        return err_result

    # Extract content — Qwen3-VL may use reasoning_content or content
    raw = ""
    choices = data.get("choices", [])
    if choices:
        msg = choices[0].get("message", {})
        raw = msg.get("content", "") or msg.get("reasoning_content", "")

    result = _parse_response(raw)
    if result:
        # §11.115.19: surface the raw Qwen response text so cascade_call1
        # can run validate_classify_response against it. Without 'raw',
        # cascade_call1's call1_response.get("raw", "") returns "" and
        # every alert is classified as OTHER (live bug: zero Telegram
        # notifications 2026-09-02 20:21:54 onward).
        result["raw"] = raw
        if alert_id:
            try:
                import vehicle_artifacts as _va
                _va.write_first_pass_response(alert_id, result)
            except Exception as _va_err:
                log.warning(f"vehicle_artifacts first-pass response save failed: {_va_err}")
        return result

    # Retry once with fresh prompt on parse failure
    log.warning("First parse failed, retrying...")
    try:
        data = _post_to_vision(api_url, payload)
        choices = data.get("choices", [])
        if choices:
            msg = choices[0].get("message", {})
            raw = msg.get("content", "") or msg.get("reasoning_content", "")
        result = _parse_response(raw)
        if result:
            # §11.115.19: same as happy path — include 'raw' for cascade_call1.
            result["raw"] = raw
            if alert_id:
                try:
                    import vehicle_artifacts as _va
                    _va.write_first_pass_response(alert_id, result)
                except Exception as _va_err:
                    log.warning(f"vehicle_artifacts first-pass response save failed: {_va_err}")
            return result
    except Exception as err2:
        log.warning(f"Retry error: {err2}")

    # §11.115.19: even on total parse failure, surface 'raw' so cascade_call1
    # can attempt to parse the failed Qwen output (and downstream loggers
    # can post-mortem).
    final_err = _error_result(f"could not parse response: {raw[:100]}")
    final_err["raw"] = raw
    return final_err


def classify_vehicle_crop(
    crop_path: str,
    api_url: str = DEFAULT_URL,
    context: dict | None = None,
    alert_id: str | None = None,
) -> dict:
    """Focused make/model classification on a single cropped vehicle.

    Args:
        crop_path: Absolute path to a JPEG of a single vehicle (already
            cropped from the bbox returned by analyze_frames). Should be
            approximately square, ~640x640 or so.
        api_url: llama-server URL.
        context: Optional first-pass data (vehicles[].color, body_style_hint)
            for logging. NOT sent to the model — the prompt doesn't get it
            to keep the focused pass clean.

    Returns:
        Dict matching VEHICLE_CLASSIFY_SCHEMA. On error, returns
        {"make": null, "model": null, ..., "confidence": 0.0,
         "_error": "<reason>"}.
    """
    try:
        with open(crop_path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode("ascii")
    except Exception as err:
        log.warning(
            f"classify_vehicle_crop: could not read {crop_path}: {err} "
            f"(context={context or 'none'})"
        )
        return _vehicle_classify_error(f"read failed: {err}")

    payload = {
        "model": load_vision_config().model,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": VEHICLE_CLASSIFY_PROMPT},
                {"type": "image_url",
                 "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
            ],
        }],
        "temperature": 0,
        "max_tokens": 512,
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "vehicle_classification",
                "strict": True,
                "schema": VEHICLE_CLASSIFY_SCHEMA,
            },
        },
    }

    # Audit trail (2026-07-24): save the prompt + crop filename for the
    # focused pass so the operator can verify what Qwen saw.
    if alert_id:
        try:
            import vehicle_artifacts as _va
            _va.write_second_pass_request(alert_id, crop_path, VEHICLE_CLASSIFY_PROMPT)
        except Exception as _va_err:
            log.warning(f"vehicle_artifacts second-pass request save failed: {_va_err}")

    try:
        data = _post_to_vision(api_url, payload)
    except Exception as err:
        log.warning(f"classify_vehicle_crop: API error: {err}")
        return _vehicle_classify_error(str(err))

    raw = ""
    choices = data.get("choices", [])
    if choices:
        msg = choices[0].get("message", {})
        raw = msg.get("content", "") or msg.get("reasoning_content", "")

    result = _parse_vehicle_classify_response(raw)
    if result is not None:
        if alert_id:
            try:
                import vehicle_artifacts as _va
                _va.write_second_pass_response(alert_id, result)
            except Exception as _va_err:
                log.warning(f"vehicle_artifacts second-pass response save failed: {_va_err}")
        return result

    # Single retry on parse failure
    log.warning("classify_vehicle_crop: first parse failed, retrying")
    try:
        data = _post_to_vision(api_url, payload)
        choices = data.get("choices", [])
        if choices:
            raw = choices[0].get("message", {}).get("content", "") or ""
        result = _parse_vehicle_classify_response(raw)
        if result is not None:
            if alert_id:
                try:
                    import vehicle_artifacts as _va
                    _va.write_second_pass_response(alert_id, result)
                except Exception as _va_err:
                    log.warning(f"vehicle_artifacts second-pass response save failed: {_va_err}")
            return result
    except Exception as _retry_err:
        # Retry failed; fall through to _vehicle_classify_error below.
        log.debug(f"classify_vehicle_crop retry failed: {_retry_err}")

    return _vehicle_classify_error(f"could not parse response: {raw[:100]}")


# ----------------------------------------------------------------------
# Queued variant
# ----------------------------------------------------------------------


def analyze_frames_queued(
    frame_paths: list[str],
    camera_name: str,
    api_url: str = DEFAULT_URL,
    timeout_s: float = QUEUE_CALLER_TIMEOUT_S,
    alert_id: str | None = None,
    event_hint: str | None = None,
    captured_at: str | None = None,
    # Phase.66 — forwarded to analyze_frames → select_prompt_template.
    # None → existing auto-dispatch (preserves all callers).
    mode: str | None = None,
):
    """Submit analyze_frames() to the VisionQueue and block on the result.

    Why this exists: with --parallel 1 on the vision llama-server, two
    concurrent webhook arrivals would race for the same Qwen slot and
    one would get HTTP 500 or context-overflow. The queue serializes
    calls and prioritizes outside-camera (Phase 6A-eligible) cameras
    over inside cameras.

    event_hint is forwarded to analyze_frames so the camera-side AI
    classification (vehicle / person / motion / animal) is surfaced
    in the prompt. See _build_event_hint_block for the rendered text.

    Returns the same dict as analyze_frames(). On queue timeout returns
    the error sentinel (`{"objects_detected": ["error"], ...}`) so the
    caller can treat it identically to a network error.

    The blocking wait here is acceptable: alert_listener already calls
    this on a per-camera worker thread (via acquire_for_camera), so
    blocking the worker is fine — the queue's job is to ORDER the work,
    not to make the listener async.

    The queue receives `analyze_frames` looked up from THIS module's
    globals at submit time, so tests that monkeypatch
    `vision_analyzer.analyze_frames` are honored — the queue will
    dispatch through the patched function.
    """
    from infra.vision_queue import get_queue

    queue = get_queue()
    fn = sys.modules[__name__].analyze_frames  # late-bound for test patches

    # Phase 2.3 (2026-08-05) — retry-with-backoff around the queue call.
    # Closes the transient-llama-server-hiccup failure mode (DESIGN-
    # PATTERNS §2.3). On the first error sentinel, sleep 100ms and retry;
    # on the second, 400ms; on the third, 1.6s. If all three fail,
    # return the last error sentinel — caller already handles that shape.
    def _submit_once(**kw):
        return queue.submit(
            fn,
            frame_paths,
            camera_name,
            api_url=api_url,
            camera=camera_name,
            timeout_s=timeout_s,
            alert_id=alert_id,
            event_hint=event_hint,
            captured_at=captured_at,
            mode=mode,
        )

    return _vision_call_with_retry(
        _submit_once,
        max_attempts=3,
        backoff_s=DEFAULT_BACKOFF_S,
    )


# ----------------------------------------------------------------------
# Phase 2.3 (2026-08-05) — retry-with-backoff around the vision call
# ----------------------------------------------------------------------
def _is_vision_error_result(d) -> bool:
    """Return True if a vision_result dict is the error sentinel.

    The queue returns `{"objects_detected": ["error"], "scene_description":
    "..."}` on any failure. We retry ONLY on this sentinel — not on
    "empty objects_detected list" (which is a valid vision result:
    the scene had nothing for the prompt to classify).
    """
    return isinstance(d, dict) and "error" in (d.get("objects_detected") or [])


# Default backoff schedule (seconds): 100ms, 400ms, 1.6s.
# Tuned empirically — long enough for a llama-server hiccup to clear,
# short enough that the worst-case latency stays under 2.5s.
DEFAULT_BACKOFF_S = (0.1, 0.4, 1.6)


def _vision_call_with_retry(
    submit_fn,
    *args,
    max_attempts: int = 3,
    backoff_s=DEFAULT_BACKOFF_S,
    **kwargs,
):
    """Submit one vision call via the queue, retrying on transient errors.

    Args:
        submit_fn: callable that submits the work to the queue and returns
            a Future. In practice this is the closure built by
            `analyze_frames_queued` (calls `queue.submit(...)`).
        *args / **kwargs: forwarded to submit_fn unchanged.
        max_attempts: total attempts (including the first). 3 = original
            + 2 retries. Capped at len(backoff_s) + 1.
        backoff_s: tuple of seconds to sleep BEFORE each retry. Index 0
            is the wait between attempt 1 and 2, etc.

    Returns:
        The first non-error vision_result dict, or the last error sentinel
        if all attempts fail.

    Why this lives in vision_analyzer and not alert_listener: the 4
    call sites in alert_listener all funnel through this queue — adding
    retry at each one would be 4x the diff and a maintenance liability.
    """
    if max_attempts < 1:
        raise ValueError("max_attempts must be >= 1")
    max_attempts = min(max_attempts, len(backoff_s) + 1)

    last_err_result = None
    for attempt in range(1, max_attempts + 1):
        future = submit_fn(*args, **kwargs)
        try:
            result = future.result(timeout=30 + 5)
        except Exception as err:
            # The queue itself blew up. Build an error sentinel and retry.
            result = _error_result(f"queue error: {err}")

        if not _is_vision_error_result(result):
            return result

        last_err_result = result
        if attempt < max_attempts:
            wait = backoff_s[attempt - 1]
            log.warning(
                f"Vision call attempt {attempt}/{max_attempts} returned "
                f"error sentinel ({result.get('scene_description', '')!r}); "
                f"retrying in {wait}s"
            )
            time.sleep(wait)

    log.error(
        f"Vision call failed after {max_attempts} attempts: "
        f"{last_err_result.get('scene_description', '') if last_err_result else 'unknown'!r}"
    )
    return last_err_result