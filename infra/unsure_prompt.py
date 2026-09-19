"""
unsure_prompt.py -- §11.115.4 unsure-mode VM2 prompt + schema.

STATUS: stable
THREAD SAFETY: thread-safe (module-level constants; no shared state)

INPUTS:
  - fn `build_unsure_prompt()` -- no IO, no env vars.

OUTPUTS:
  - SCHEMA_JSON: JSON Schema dict for the llama-server strict-mode
    response_format. All fields nullable; subclass uses enum constraints.
  - PROMPT_TEXT: instruction text directing the model to describe
    what it sees without forcing a classification, and to give a
    coarse reason (subclass) for its uncertainty.

PUBLIC API:
  - SCHEMA_JSON             dict
  - PROMPT_TEXT             str
  - build_unsure_prompt()   str

DOES NOT DO:
  - Call Qwen. (See infra.vision_analyzer.analyze_frames_queued.)
  - Validate the model response.

CALLED BY:
  - infra.vision_analyzer -- via DISPATCH["unsure"] in detail_class().

RELATED:
  - infra.vm1_prompt: VM1 classify prompt (may emit class="unsure").

Design notes:
  - Schema fields are mostly nullable (type: [string|null], number|null, etc.).
  - subclass uses enum {blurry, partial, ambiguous, other} to capture
    why the model is unsure (coarse-grained reason, not a classification).
  - The prompt text tells the model to give its best identification
    without class constraints and to explain why it is unsure.
"""
from __future__ import annotations

# ============================================================================
# JSON Schema -- used by _response_format for server-side enforcement.
# All fields nullable; subclass uses enum constraints.
# ============================================================================
SCHEMA_JSON: dict = {
    "type": "object",
    "properties": {
        "class_guess": {
            "type": ["string", "null"],
            "description": "Best classification guess. null if unobservable.",
        },
        "confidence": {
            "type": ["number", "null"],
            "minimum": 0.0,
            "maximum": 1.0,
            "description": "Confidence in the guess, 0.0 to 1.0. null if unobservable.",
        },
        "reason": {
            "type": ["string", "null"],
            "description": "Why the model is unsure. null if not applicable.",
        },
        "description": {
            "type": ["string", "null"],
            "description": "Natural-language description of what is moving. null if unobservable.",
        },
        "subclass": {
            "type": ["string", "null"],
            "enum": ["blurry", "partial", "ambiguous", "other"],
            "description": "Coarse reason for uncertainty. null if not applicable.",
        },
        "notable_details": {
            "type": ["array", "null"],
            "items": {"type": "string"},
            "description": "Notable visual details. null if unobservable.",
        },
    },
    "required": [
        "class_guess",
        "confidence",
        "reason",
        "description",
        "subclass",
        "notable_details",
    ],
    "additionalProperties": False,
}

PROMPT_TEXT: str = """\
Describe what is moving in this frame pair. Give your best identification
without forcing a class constraint. You may return null for fields you
cannot determine.

Respond ONLY with JSON matching this schema:

{
  "class_guess": string|null,
  "confidence": 0.0-1.0 (number) or null,
  "reason": string|null,
  "description": string|null,
  "subclass": "blurry" | "partial" | "ambiguous" | "other" | null,
  "notable_details": ["detail 1", "detail 2"] or null
}

Rules:
- Focus on what you can actually see. Never invent details.
- Use null for anything you cannot determine.
- Give your best guess even if you are uncertain.
- Confidence is a number between 0.0 and 1.0, or null if you cannot gauge it.
- subclass: choose one of {blurry, partial, ambiguous, other} to explain
  why you are unsure, or null if you can determine the class.
  - blurry: image quality makes details indistinguishable
  - partial: only a portion of the subject is visible
  - ambiguous: multiple interpretations are plausible
  - other: uncertainty for any other reason

No prose, no markdown, no keys outside the list above.
"""

# Canonical mode name for the response_format dispatch layer (Rule 8a).
# The dispatch dict in infra/vision_analyzer.py MUST include this exact
# string or this prompt is unreachable.
MODE_NAME: str = "vm2_unsure"


def build_unsure_prompt() -> str:
    """Return the VM2 unsure-mode text prompt. The schema is the constraint
    layer (Rule 8); the text describes the task.
    """
    return PROMPT_TEXT
