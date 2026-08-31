"""Vision client — one call to Qwen, one structured response (or error).

Pure I/O adapter. Returns a `VisionResult` on success or a `VisionError`
on any failure (network, timeout, parse, schema mismatch). No retries,
no queueing, no caching — those concerns belong to the caller.

The caller passes the prompt and the image (as base64). The client
formats the OpenAI-compatible chat completion request, POSTs it,
parses the JSON response, and returns the structured dict.

No imports from other domain modules.
"""

from __future__ import annotations

import base64
import json
import logging
from pathlib import Path
from typing import Any

import httpx

from .prompt_template import render_crop_prompt

log = logging.getLogger(__name__)


# Phase.158 (§11.81): unified Qwen3.6-35B-A3B server on :8093.
# Override in tests via api_url= parameter.
from infra.llm_config import load_vision_config
DEFAULT_VISION_URL = load_vision_config().url

# Fail-fast timeout. Qwen takes ~25-30s per call; 180s is generous.
DEFAULT_TIMEOUT_SECONDS = 180.0


class VisionError:
    """Structured sentinel returned when the vision call fails.

    Has the same dict shape as VisionResult so callers can treat both
    uniformly. Use `is_vision_error(result)` to discriminate.
    """

    def __init__(
        self,
        kind: str,
        message: str,
        elapsed_ms: float = 0.0,
    ) -> None:
        self.kind = kind           # "network" | "timeout" | "parse" | "schema"
        self.message = message
        self.elapsed_ms = elapsed_ms

    def to_dict(self) -> dict[str, Any]:
        return {
            "objects_detected": ["error"],
            "error": {
                "kind": self.kind,
                "message": self.message,
            },
            "elapsed_ms": self.elapsed_ms,
        }


class VisionResult:
    """Successful vision call result."""

    def __init__(
        self,
        content: dict[str, Any],
        elapsed_ms: float,
        raw_text: str,
    ) -> None:
        self.content = content          # the parsed Qwen response
        self.elapsed_ms = elapsed_ms
        self.raw_text = raw_text        # for debugging

    def to_dict(self) -> dict[str, Any]:
        out = dict(self.content)
        out.setdefault("elapsed_ms", self.elapsed_ms)
        return out


def is_vision_error(result: VisionResult | VisionError | dict[str, Any]) -> bool:
    """True if result is an error sentinel (either wrapped or raw dict)."""
    if isinstance(result, VisionError):
        return True
    if isinstance(result, dict):
        return (
            result.get("objects_detected") == ["error"]
            or "error" in result
        )
    return False


def _read_image_b64(image_path: str | Path) -> str:
    """Read an image file as base64."""
    p = Path(image_path)
    if not p.is_file():
        raise FileNotFoundError(f"image not found: {p}")
    return base64.b64encode(p.read_bytes()).decode("ascii")


def _parse_response_content(raw_text: str) -> dict[str, Any]:
    """Parse Qwen's chat completion content into a dict.

    Qwen sometimes wraps JSON in markdown fences (```json ... ```)
    even when told not to. Strip those first.
    """
    text = raw_text.strip()
    # Strip markdown fences if present.
    if text.startswith("```"):
        # Remove leading fence (```json or ```).
        first_newline = text.find("\n")
        if first_newline != -1:
            text = text[first_newline + 1 :]
        # Remove trailing fence.
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3].rstrip()
    parsed: dict[str, Any] = json.loads(text)
    return parsed


def call_vision(
    image_paths: list[str | Path],
    camera_name: str,
    captured_at: str,
    api_url: str = DEFAULT_VISION_URL,
    model: str | None = None,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    temperature: float = 0.0,
    max_tokens: int = 2048,
    event_hint_block: str = "",
) -> VisionResult | VisionError:
    """Call the vision model with one or more images + the crop prompt.

    Args:
        image_paths: List of crop image paths. Single image for the
            identifier's per-crop call.
        camera_name: Human-readable camera label (used in the prompt).
        captured_at: ISO-8601 timestamp for when the alert fired.
        api_url: Vision API endpoint (default llama-server :8080).
        model: Model name override; if None the API uses its default.
        timeout_seconds: HTTP timeout.
        temperature: Sampling temperature. 0.0 = deterministic-ish.
        max_tokens: Max completion tokens.
        event_hint_block: Optional hint block for the prompt.

    Returns:
        VisionResult on success, VisionError on any failure.
    """
    import time
    t0 = time.perf_counter()

    if not image_paths:
        return VisionError("validation", "no images provided",
                           elapsed_ms=(time.perf_counter() - t0) * 1000)

    prompt = render_crop_prompt(camera_name, captured_at, event_hint_block)

    # Build image content blocks.
    image_blocks: list[dict[str, Any]] = []
    for path in image_paths:
        try:
            b64 = _read_image_b64(path)
        except FileNotFoundError as e:
            return VisionError("validation", str(e),
                               elapsed_ms=(time.perf_counter() - t0) * 1000)
        image_blocks.append({
            "type": "image_url",
            "image_url": {"url": f"data:image/jpeg;base64,{b64}"},
        })

    payload: dict[str, Any] = {
        "messages": [{
            "role": "user",
            "content": [
                *image_blocks,
                {"type": "text", "text": prompt},
            ],
        }],
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    if model:
        payload["model"] = model

    try:
        with httpx.Client(timeout=timeout_seconds) as client:
            r = client.post(api_url, json=payload)
            r.raise_for_status()
            data = r.json()
    except httpx.TimeoutException as e:
        return VisionError("timeout", f"vision call timed out: {e!r}",
                           elapsed_ms=(time.perf_counter() - t0) * 1000)
    except httpx.HTTPStatusError as e:
        return VisionError("network",
                           f"vision returned HTTP {e.response.status_code}",
                           elapsed_ms=(time.perf_counter() - t0) * 1000)
    except httpx.RequestError as e:
        return VisionError("network", f"vision request failed: {e!r}",
                           elapsed_ms=(time.perf_counter() - t0) * 1000)
    except Exception as e:  # noqa: BLE001  (catch-all for the safety net)
        return VisionError("network", f"unexpected error: {e!r}",
                           elapsed_ms=(time.perf_counter() - t0) * 1000)

    # Extract the assistant text from the chat completion envelope.
    try:
        choices = data["choices"]
        raw_text = choices[0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as e:
        return VisionError("parse",
                           f"unexpected response envelope: {e!r}",
                           elapsed_ms=(time.perf_counter() - t0) * 1000)

    # Parse the JSON content.
    try:
        content = _parse_response_content(raw_text)
    except json.JSONDecodeError:
        return VisionError("parse",
                           f"Qwen returned non-JSON content: {raw_text!r}",
                           elapsed_ms=(time.perf_counter() - t0) * 1000)

    elapsed_ms = (time.perf_counter() - t0) * 1000
    log.info(
        f"vision call ok: camera={camera_name!r} elapsed={elapsed_ms:.0f}ms "
        f"content_keys={list(content.keys())[:5]}..."
    )
    return VisionResult(content, elapsed_ms, raw_text)
