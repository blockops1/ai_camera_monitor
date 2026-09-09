"""
vm1_prompt.py — Vision Model 1: classify subject from 2 crops alone.

Operator-locked contract (the operator 2026-09-06):
  "No I don't want any hint given to vision model one of what it is in
  any image. It needs to figure out from the two crops alone what the
  object is."

Stage 5 of the linear pipeline. Input: 2 motion-streak crops (bytes or
filepath). Output: dict `{class, confidence}`.

Schema (response_format layer):
  {
    "class": enum["vehicle", "person", "animal", "unsure"],
    "confidence": enum["definite", "likely", "unsure"]
  }

The "unsure" landing pad is mandatory (Rule 6 from vision-prompt-design
skill): Qwen defaults toward picking a class, so we give it a fourth
option to admit ambiguity. Cooldown still keys on the YOLO gate's
tentative classification (not VM1's) — VM1 output is informational for
downstream VM2 prompt selection and Telegram body, not for throttling.
"""
from __future__ import annotations

SCHEMA_JSON: dict = {
    "type": "object",
    "properties": {
        "class": {
            "type": "string",
            "enum": ["vehicle", "person", "animal", "unsure"],
            "description": "What is in the crops? Pick unsure if you cannot tell.",
        },
        "confidence": {
            "type": "string",
            "enum": ["definite", "likely", "unsure"],
            "description": "How sure are you? 'unsure' means hedge-bumped thresholds apply downstream.",
        },
    },
    "required": ["class", "confidence"],
    "additionalProperties": False,
}

PROMPT_TEXT: str = """\
You are looking at TWO crops taken in quick succession from one motion event.

The crops show one moving subject. The two crops were taken fractions
of a second apart. They may show the same subject from slightly
different angles, or one subject followed by another.

Classify the subject as exactly one of:
  - vehicle — car, truck, SUV, van, motorcycle, bicycle, bus, tractor
  - person — adult or child; any clothing
  - animal — domestic, wild, on the property
  - unsure — only if you genuinely cannot tell what is in the crops

Pick "unsure" rather than guess. A wrong class triggers an expensive
downstream call. An unsure answer is recoverable; a wrong vehicle-vs-
person call is not.

Set confidence:
  - definite — high visual certainty, clear subject, unambiguous
  - likely   — best call but with caveats (lighting, partial occlusion)
  - unsure   — guessing between plausible alternatives

Respond ONLY with JSON. The JSON object MUST have exactly these two keys:
  - "class"      — one of: vehicle, person, animal, unsure
  - "confidence" — one of: definite, likely, unsure

Example valid output:
{"class": "vehicle", "confidence": "likely"}

No prose, no markdown, no keys outside "class" and "confidence".
"""

# Canonical mode name for the response_format dispatch layer (Rule 8a).
# The dispatch dict in infra/vision_analyzer.py MUST include this exact
# string or this prompt is unreachable.
MODE_NAME: str = "vm1_classify"


def build_vm1_prompt() -> str:
    """Return the VM1 text prompt. The schema constraint lives in
    response_format; the text prompt only states the task in natural
    language.

    Per Rule 8 (vision-prompt-design skill): the schema is the layer
    that constrains Qwen. The text is for Qwen to read; the schema is
    for Qwen to comply with. They must agree; the text says "unsure is
    fine" and the schema's enum makes "unsure" a legal value.
    """
    return PROMPT_TEXT
