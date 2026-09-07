"""
vision_analyzer.py — Call vision llama-server (VM1/VM2) to classify crops.

STATUS: provisional
THREAD SAFETY: single-threaded

INPUTS:
    - crop_a, crop_b: str paths to motion-streak JPEGs (required)
    - mode: str for VM2 dispatch (detail_class only)

OUTPUTS:
    - dict from VM1 or VM2 JSON response
    - raises VisionAnalyzerError on HTTP non-200 or parse failure

PUBLIC API:
    verify_class(crop_a, crop_b) -> dict
    detail_class(mode, crop_a, crop_b, motion_diff_image=None) -> dict

DOES NOT DO:
    - Gate classification (upstream caller decides)
    - Crop resizing / encoding (caller handles)
    - Threat-level analysis (no threat fields at vision layer)

CALLS INTO:
    - httpx: POST JSON+images to llama-server
    - infra.vm1_prompt: VM1 prompt + response schema
    - infra.vehicle/person/animal_prompt: VM2 schemas + builders
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

import httpx

from infra.vm1_prompt import SCHEMA_JSON as _vm1_schema, build_vm1_prompt

from infra.vehicle_prompt import (
    MODE_NAME as _vehicle_mode, SCHEMA_JSON as _vehicle_schema,
    build_vehicle_prompt,
)
from infra.person_prompt import (
    MODE_NAME as _person_mode, SCHEMA_JSON as _person_schema,
    build_person_prompt,
)
from infra.animal_prompt import (
    MODE_NAME as _animal_mode, SCHEMA_JSON as _animal_schema,
    build_animal_prompt,
)

_VISION_URL = "http://localhost:8080/v1/chat/completions"


class VisionAnalyzerError(Exception):
    """Raised when the vision llama-server returns an error."""


DISPATCH = {
    "vehicle": (_vehicle_schema, build_vehicle_prompt),
    "person": (_person_schema, build_person_prompt),
    "animal": (_animal_schema, build_animal_prompt),
}


def _b64(path: str) -> dict:
    data = Path(path).read_bytes()
    b64 = base64.b64encode(data).decode()
    ext = Path(path).suffix.lower()
    mime = "image/png" if ext == ".png" else "image/jpeg"
    return {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}}


def _call_llama(messages: list, schema: dict, timeout: float) -> dict:
    payload = {"model": "vm2", "messages": messages, "response_format": schema}
    try:
        resp = httpx.post(_VISION_URL, json=payload, timeout=timeout)
    except httpx.HTTPError as exc:
        raise VisionAnalyzerError(
            f"llama-server connection failed: {exc}"
        ) from exc
    if resp.status_code != 200:
        raise VisionAnalyzerError(
            f"llama-server returned {resp.status_code}: {resp.text[:200]}"
        )
    try:
        result = resp.json()["choices"][0]["message"]["content"]
        return json.loads(result)
    except (KeyError, json.JSONDecodeError, IndexError) as exc:
        raise VisionAnalyzerError(
            f"failed to parse response: {exc}"
        ) from exc


def verify_class(crop_a: str, crop_b: str) -> dict:
    """Call VM1 classify model with two crops."""
    images = [_b64(crop_a), _b64(crop_b)]
    payload = {"model": "vm1_classify", "messages": [
        {"role": "user", "content": [
            {"type": "text", "text": build_vm1_prompt()}, *images,
        ]}
    ], "response_format": _vm1_schema}
    try:
        resp = httpx.post(_VISION_URL, json=payload, timeout=30.0)
    except httpx.HTTPError as exc:
        raise VisionAnalyzerError(
            f"llama-server connection failed: {exc}"
        ) from exc
    if resp.status_code != 200:
        raise VisionAnalyzerError(
            f"llama-server returned {resp.status_code}: {resp.text[:200]}"
        )
    try:
        result = resp.json()["choices"][0]["message"]["content"]
        return json.loads(result)
    except (KeyError, json.JSONDecodeError, IndexError) as exc:
        raise VisionAnalyzerError(
            f"failed to parse response: {exc}"
        ) from exc


def detail_class(
    mode: str, crop_a: str, crop_b: str,
    motion_diff_image: str | None = None,
) -> dict:
    """Call VM2 model with mode-keyed dispatch for detailed analysis.

    Raises VisionAnalyzerError on unknown mode.
    """
    entry = DISPATCH.get(mode)
    if entry is None:
        raise VisionAnalyzerError(
            f"unknown detail_class mode {mode!r}; "
            f"expected one of {sorted(DISPATCH.keys())}"
        )
    schema, prompt_fn = entry
    images = [_b64(crop_a), _b64(crop_b)]
    if motion_diff_image is not None:
        images.append(_b64(motion_diff_image))
    return _call_llama(
        [{"role": "user", "content": [
            {"type": "text", "text": prompt_fn()}, *images,
        ]}],
        schema, 60.0,
    )
