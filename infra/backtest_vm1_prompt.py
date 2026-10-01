"""
backtest_vm1_prompt.py -- NEW VM1 prompt for backtest harness.

This is a STANDALONE prompt module -- separate from the production
infra/vm1_prompt.py. It asks the vision model to identify the MOVING
subject (not stationary objects) and returns bboxes in both frame_2
and frame_3 native pixel coordinates.

STATUS: stable (backtest-only, no production impact)
THREAD SAFETY: thread-safe (module-level constants; no shared state)

PUBLIC API:
    - SCHEMA_JSON  dict  -- JSON Schema for strict-mode response_format
    - PROMPT_TEXT  str  -- instruction text
    - build_backtest_prompt() -> str

DOES NOT DO:
    - Call Qwen / llama-server.
    - Validate the model response.
    - Replace or modify infra/vm1_prompt.py.

RELATED:
    - infra.backtest_vm1_motion -- harness that uses this prompt.
"""
from __future__ import annotations

# ============================================================================
# JSON Schema -- used by the vision POST for server-side enforcement.
# ============================================================================
SCHEMA_JSON: dict = {
    "type": "object",
    "properties": {
        "class": {
            "type": "string",
            "enum": ["vehicle", "person", "animal", "unsure"],
            "description": "What is the MOVING subject in these images? Pick unsure if you cannot determine.",
        },
        "bbox_a": {
            "type": ["array", "null"],
            "items": {"type": "integer"},
            "description": "Bounding box of the moving subject in frame_2 native pixel coords: [x, y, width, height]. null if unsure or no subject.",
        },
        "bbox_b": {
            "type": ["array", "null"],
            "items": {"type": "integer"},
            "description": "Bounding box of the moving subject in frame_3 native pixel coords: [x, y, width, height]. null if unsure or no subject.",
        },
    },
    "required": ["class", "bbox_a", "bbox_b"],
    "additionalProperties": False,
}

PROMPT_TEXT: str = """\
You are looking at THREE images from the same motion event on a security camera.

Image 1: frame_002 -- a single captured frame showing the scene.
Image 2: frame_003 -- the very next frame, fractions of a second later.
Image 3: a composite of the same scene with cumulative motion regions
         painted in RED overlay on the background. This shows exactly
         which areas changed between frames.

IMPORTANT RULES:
- The RED regions in Image 3 show WHERE motion was detected (pixel changes).
- The SUBJECT in Images 1 and 2 is what you must classify and box.
- Focus on the MOVING subject, NOT stationary objects in the scene.
- If the only thing moving is trees/foliage/background, classify as the
  moving object (e.g. "unsure" or whatever is actually moving in the red regions).
- Give the SAME class for both images.
- Provide bboxes in EACH image's native pixel coordinates (not scaled).

Respond ONLY with JSON. The JSON object MUST have exactly these keys:
  - "class"    - one of: vehicle, person, animal, unsure
  - "bbox_a"   - [x, y, width, height] in frame_002 coords, or null
  - "bbox_b"   - [x, y, width, height] in frame_003 coords, or null

Example valid output:
{"class": "unsure", "bbox_a": [1200, 300, 80, 150], "bbox_b": [1210, 305, 75, 145]}

No prose, no markdown, no keys outside "class", "bbox_a", and "bbox_b".
"""


def build_backtest_prompt() -> str:
    """Return the backtest VM1 text prompt."""
    return PROMPT_TEXT
