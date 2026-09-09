"""
vision_analyzer.py — Call vision llama-server (VM1/VM2) to classify crops.

STATUS: stable
THREAD SAFETY: single-threaded (httpx per-call, no shared state)

INPUTS:
    - crop_a, crop_b: str paths to motion-streak JPEGs (required)
    - mode: str for VM2 dispatch (detail_class only) — one of
      'vehicle', 'person', 'animal'

OUTPUTS:
    - dict from VM1 or VM2 JSON response (schema-enforced)
    - raises VisionAnalyzerError on HTTP non-200 or parse failure

PUBLIC API:
    verify_class(crop_a, crop_b) -> dict
        VM1 two-crop classify call. Returns {"class": ..., "confidence": ...}.
    detail_class(mode, crop_a, crop_b, motion_diff_image=None) -> dict
        VM2 detail call, dispatched by mode. Returns mode-specific schema dict.

DOES NOT DO:
    - Gate classification (upstream caller decides)
    - Crop resizing / encoding (caller handles)
    - Threat-level analysis (no threat fields at vision layer)

CALLS INTO:
    - httpx: POST JSON+images to llama-server
    - infra.vm1_prompt: VM1 prompt + response schema
    - infra.vehicle/person/animal_prompt: VM2 schemas + builders

DESIGN NOTES (refactor parity):
    llama-server supports server-side JSON Schema enforcement via
    `response_format: {"type": "json_schema", "strict": True, "schema": ...}`.
    Using `strict: True` means:
      - The model cannot emit keys outside `properties`
      - Required fields are enforced (model must emit them)
      - `enum` values are coerced / rejected
    This is the llama-server contract used by `farm-surveillance-refactor`
    for the same vision calls. The `SCHEMA_JSON` dicts in
    `infra/*_prompt.py` are the canonical contract.
"""
from __future__ import annotations

import base64
import json
from pathlib import Path

import httpx

from infra.animal_prompt import SCHEMA_JSON as ANIMAL_SCHEMA, build_animal_prompt
from infra.person_prompt import SCHEMA_JSON as PERSON_SCHEMA, build_person_prompt
from infra.vehicle_prompt import SCHEMA_JSON as VEHICLE_SCHEMA, build_vehicle_prompt
from infra.vm1_prompt import SCHEMA_JSON as VM1_SCHEMA, build_vm1_prompt

_VISION_URL = "http://localhost:8080/v1/chat/completions"
_VISION_MODEL = "qwen3-vl-8b"


def _response_format(schema: dict, name: str) -> dict:
    """Wrap a JSON Schema dict in the llama-server strict-mode envelope.

    llama-server (Qwen3-VL backend) accepts response_format of shape:
        {"type": "json_schema", "strict": True, "schema": <dict>}
    Per the refactor (infra/vision_analyzer.py:classify_vehicle_crop).
    Using `strict: True` makes the server enforce key names + required
    fields + enums. Without it, the model can emit arbitrary keys.
    """
    return {
        "type": "json_schema",
        "strict": True,
        "schema": schema,
    }


class VisionAnalyzerError(Exception):
    """Raised when the vision llama-server returns an error."""


# Mode dispatch — centralizes (response_format, prompt_builder) per mode.
# Adding a new VM2 mode = add an entry here + create a prompt module.
# The canonical mode set is exactly four (vm1 + 3 vm2). Anything else
# raises VisionAnalyzerError loudly.
DISPATCH = {
    "vehicle": (_response_format(VEHICLE_SCHEMA, "vehicle"), build_vehicle_prompt),
    "person": (_response_format(PERSON_SCHEMA, "person"), build_person_prompt),
    "animal": (_response_format(ANIMAL_SCHEMA, "animal"), build_animal_prompt),
}


def _b64(path: str) -> dict:
    data = Path(path).read_bytes()
    b64 = base64.b64encode(data).decode()
    ext = Path(path).suffix.lower()
    mime = "image/png" if ext == ".png" else "image/jpeg"
    return {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}}


def _post_vision(payload: dict, timeout: float) -> dict:
    """POST to llama-server, parse JSON, return content dict. Raises VisionAnalyzerError."""
    try:
        resp = httpx.post(_VISION_URL, json=payload, timeout=timeout)
    except httpx.HTTPError as exc:
        raise VisionAnalyzerError(
            f"llama-server connection failed: {exc}"
        ) from exc
    if resp.status_code != 200:
        raise VisionAnalyzerError(
            f"llama-server returned {resp.status_code}: {resp.text[:300]}"
        )
    try:
        raw = resp.json()["choices"][0]["message"]["content"]
        return json.loads(raw)
    except (KeyError, json.JSONDecodeError, IndexError) as exc:
        raise VisionAnalyzerError(
            f"failed to parse response: {exc}"
        ) from exc


def verify_class(crop_a: str, crop_b: str) -> dict:
    """VM1: classify subject as vehicle/person/animal/unsure from 2 crops.

    Server-side schema enforcement: model must emit exactly
    {"class": <enum>, "confidence": <enum>}.
    """
    payload = {
        "model": _VISION_MODEL,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": build_vm1_prompt()},
                _b64(crop_a),
                _b64(crop_b),
            ],
        }],
        "response_format": _response_format(VM1_SCHEMA, "vm1"),
    }
    return _post_vision(payload, timeout=30.0)


def detail_class(
    mode: str, crop_a: str, crop_b: str,
    motion_diff_image: str | None = None,
) -> dict:
    """VM2: mode-specific detail call. Raises VisionAnalyzerError on unknown mode."""
    entry = DISPATCH.get(mode)
    if entry is None:
        raise VisionAnalyzerError(
            f"unknown detail_class mode {mode!r}; "
            f"expected one of {sorted(DISPATCH.keys())}"
        )
    response_format, prompt_fn = entry
    images = [_b64(crop_a), _b64(crop_b)]
    if motion_diff_image is not None:
        images.append(_b64(motion_diff_image))
    payload = {
        "model": _VISION_MODEL,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": prompt_fn()},
                *images,
            ],
        }],
        "response_format": response_format,
    }
    return _post_vision(payload, timeout=60.0)
