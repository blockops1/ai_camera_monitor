"""
vision_analyzer.py — VM1 (verify_class) classification of motion-streak crops.

CALLS: httpx POST to vision llama-server.
PUBLIC: verify_class(crop_a, crop_b) -> dict, VisionAnalyzerError.
DOES NOT DO: vehicle/person/animal classification (US-003 scope), crop resizing.

US-002 scope only. US-003 will add detail_class + VM2 dispatch.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

import httpx

from infra.vm1_prompt import SCHEMA_JSON as _vm1_schema, build_vm1_prompt

_VISION_URL = "http://localhost:8080/v1/chat/completions"


class VisionAnalyzerError(Exception):
    """Raised on llama-server HTTP or JSON parse failure."""


def _b64(crop: str) -> dict:
    """Encode a JPEG file path as a base64 image_url content block."""
    data = Path(crop).read_bytes()
    return {
        "type": "image_url",
        "image_url": {
            "url": f"data:image/jpeg;base64,{base64.b64encode(data).decode()}"
        },
    }


def verify_class(crop_a: str, crop_b: str) -> dict:
    """Call VM1 classify model with two crops; return parsed JSON dict."""
    images = [_b64(crop_a), _b64(crop_b)]
    payload = {
        "model": "vm1_classify",
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": build_vm1_prompt()},
                    *images,
                ],
            }
        ],
        "response_format": _vm1_schema,
    }
    try:
        resp = httpx.post(_VISION_URL, json=payload, timeout=30.0)
    except httpx.HTTPError as exc:
        raise VisionAnalyzerError(f"llama-server connection failed: {exc}") from exc
    if resp.status_code != 200:
        raise VisionAnalyzerError(
            f"llama-server returned {resp.status_code}: {resp.text[:200]}"
        )
    try:
        result = resp.json()["choices"][0]["message"]["content"]
        return json.loads(result)
    except (KeyError, json.JSONDecodeError, IndexError) as exc:
        raise VisionAnalyzerError(f"failed to parse response: {exc}") from exc
