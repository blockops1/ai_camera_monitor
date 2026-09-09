"""
person_prompt.py — Vision Model 2: detail a person subject.

Stage 6 (VM2) of the linear pipeline. Triggered when VM1 (or the YOLO
gate) classifies the moving subject as `person`. Output consumed by
the person matcher (vehicle_matcher.match on a person subject) and the
Telegram formatter.

Operator-locked contract (the operator 2026-09-06):
  "the threat-level" is NOT requested. TG#2 contains 2 crops + VM2
  output; no threat classification. This prompt follows that —
  person identification + description only, no threat assessment.

Schema (response_format layer):
  {
    "class_confirmed":      enum["vehicle", "person", "animal", "unsure"],
    "upper_garment":        string|null,
    "lower_garment":        string|null,
    "footwear":             string|null,
    "carried":              string[],     # backpack, package, tool, ...
    "headwear":             string|null,
    "facing":               enum["toward", "away", "profile"],
    "distinctive_features": string[],     # 1-5 re-ID markers
    "description":          string,
    "confidence":           enum["definite", "likely", "unsure"]
  }
"""
from __future__ import annotations

SCHEMA_JSON: dict = {
    "type": "object",
    "properties": {
        "class_confirmed": {
            "type": "string",
            "enum": ["vehicle", "person", "animal", "unsure"],
            "description": "Confirm the class (should match VM1).",
        },
        "upper_garment": {
            "type": ["string", "null"],
            "description": "Upper garment description: 'red hoodie', 'dark jacket', 'flannel shirt', ... null if not visible.",
        },
        "lower_garment": {
            "type": ["string", "null"],
            "description": "Lower garment description: 'blue jeans', 'dark pants', 'shorts', ... null if not visible.",
        },
        "footwear": {
            "type": ["string", "null"],
            "description": "Footwear: 'white sneakers', 'work boots', ... null if not visible.",
        },
        "carried": {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 0,
            "maxItems": 5,
            "description": "What the person is carrying: backpack, package, handbag, tool, ... Empty array if nothing visible.",
        },
        "headwear": {
            "type": ["string", "null"],
            "description": "Hat, hood, helmet: 'red baseball cap', 'hood up', ... null if none.",
        },
        "facing": {
            "type": "string",
            "enum": ["toward", "away", "profile"],
            "description": "Which direction is the person facing relative to the camera?",
        },
        "distinctive_features": {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 0,
            "maxItems": 5,
            "description": "1-5 re-ID markers that distinguish THIS person from other people in similar clothing. Examples: 'tall', 'limp in left leg', 'long gray beard', 'bright orange backpack', 'cane', 'red gloves', 'walked with dog on leash'. Generic descriptors like 'jeans' or 'jacket' are NOT distinctive — they apply to many people.",
        },
        "description": {
            "type": "string",
            "description": "1-2 sentence natural-language description.",
        },
        "confidence": {
            "type": "string",
            "enum": ["definite", "likely", "unsure"],
            "description": "Overall call certainty.",
        },
    },
    "required": ["class_confirmed", "carried", "facing", "distinctive_features", "description", "confidence"],
    "additionalProperties": False,
}

PROMPT_TEXT: str = """\
You are looking at TWO crops of one moving person, taken fractions of
a second apart. The crops show the same person from slightly different
angles or moments.

Describe the person. Use the most specific terms you can read; null
when you cannot read something rather than guessing.

For carried, list items the person is carrying: backpack, package,
handbag, tool, leash, ... Empty array if nothing is visible.

For facing:
  - toward  — face visible, looking at the camera
  - away    — back of head visible
  - profile — side view, face partly visible

For distinctive_features, list 1-5 features that distinguish THIS person from
other people in similar clothing. Examples: tall, limp in left leg,
long gray beard, bright orange backpack, cane, red gloves, walked with
dog on leash. Generic descriptors like 'jeans' or 'jacket' are NOT
distinctive — they apply to many people.

Confidence:
  - definite — high visual certainty, clear subject, all fields reliable
  - likely   — best call but caveats (lighting, partial occlusion)
  - unsure   — guessing between plausible alternatives

Respond ONLY with JSON. The JSON object MUST have exactly these keys:
  - "class_confirmed"      — one of: vehicle, person, animal, unsure
  - "upper_garment"        — string|null
  - "lower_garment"        — string|null
  - "footwear"             — string|null
  - "carried"              — array of 0-5 strings
  - "headwear"             — string|null
  - "facing"               — one of: toward, away, profile
  - "distinctive_features" — array of 0-5 strings
  - "description"          — string
  - "confidence"           — one of: definite, likely, unsure

No prose, no markdown, no keys outside the list above.
"""

# Canonical mode name for the response_format dispatch layer (Rule 8a).
# The dispatch dict in infra/vision_analyzer.py MUST include this exact
# string or this prompt is unreachable.
MODE_NAME: str = "vm2_person"


def build_person_prompt() -> str:
    """Return the VM2 person text prompt."""
    return PROMPT_TEXT
