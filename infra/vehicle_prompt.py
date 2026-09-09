"""
vehicle_prompt.py — Vision Model 2: detail a vehicle subject.

Stage 6 (VM2) of the linear pipeline. Triggered when VM1 (or the YOLO
gate, defensively) classifies the moving subject as `vehicle`. Output
is consumed by the pure vehicle matcher (vehicle_matcher.match).

Operator-locked contract (the operator 2026-09-06):
  "the threat-level" is NOT requested. TG#2 contains 2 crops + VM2
  output; no threat classification. This prompt follows that —
  vehicle identification + description only, no threat assessment.

Schema (response_format layer):
  {
    "class_confirmed":     enum["vehicle", "person", "animal", "unsure"],
    "make":                string|null,
    "model":               string|null,
    "color":               string|null,
    "type":                string|null,   # sedan, pickup, SUV, ...
    "plate_visible":       enum["yes", "no", "unsure"],
    "license_plate":       string|null,
    "distinctive_features": string[],     # 1-5 re-ID markers
    "description":         string,
    "confidence":          enum["definite", "likely", "unsure"]
  }
"""
from __future__ import annotations

SCHEMA_JSON: dict = {
    "type": "object",
    "properties": {
        "class_confirmed": {
            "type": "string",
            "enum": ["vehicle", "person", "animal", "unsure"],
            "description": "Confirm the class (should match VM1). If you disagree with VM1, set the correct class here.",
        },
        "make": {
            "type": ["string", "null"],
            "description": "Make: Ford, Toyota, Chevy, ... Use the most specific you can read.",
        },
        "model": {
            "type": ["string", "null"],
            "description": "Model name: F-150, Tacoma, Silverado, ... null if unreadable.",
        },
        "color": {
            "type": ["string", "null"],
            "description": "Dominant body color: red, dark-blue, white, ... lowercase.",
        },
        "type": {
            "type": ["string", "null"],
            "description": "Body style: sedan, pickup, SUV, van, hatchback, motorcycle, ... null if unsure.",
        },
        "plate_visible": {
            "type": "string",
            "enum": ["yes", "no", "unsure"],
            "description": "Is a license plate visible in either crop?",
        },
        "license_plate": {
            "type": ["string", "null"],
            "description": "License plate text if readable. null if not visible or unreadable. Don't guess.",
        },
        "distinctive_features": {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 0,
            "maxItems": 5,
            "description": "1-5 re-ID markers that distinguish THIS vehicle from similar ones. Examples: 'front bumper dent', 'magnetic sign on driver door', 'roof rack', 'auxiliary lights', 'rust on rear quarter', 'mud flap decal'. Empty array if nothing distinctive.",
        },
        "description": {
            "type": "string",
            "description": "1-2 sentence natural-language description of what you see.",
        },
        "confidence": {
            "type": "string",
            "enum": ["definite", "likely", "unsure"],
            "description": "Overall call certainty.",
        },
    },
    "required": ["class_confirmed", "plate_visible", "distinctive_features", "description", "confidence"],
    "additionalProperties": False,
}

PROMPT_TEXT: str = """\
You are looking at TWO crops of one moving vehicle, taken fractions of
a second apart. The crops show the same vehicle from slightly different
angles or moments.

Identify the vehicle. Use the most specific terms you can read; null
when you cannot read something rather than guessing.

For distinctive_features, list 1-5 features that distinguish THIS vehicle from
similar vehicles. Examples: front bumper dent, magnetic sign on driver
door, roof rack, auxiliary lights, rust on rear quarter, mud flap
decal, aftermarket wheels, window decal. Generic descriptors like
'red', 'pickup', or '4-door' are NOT distinctive — they apply to many
vehicles.

For plate_visible, return 'yes' only if you can read plate characters
clearly. 'unsure' if a plate is present but blurry. 'no' if no plate
is visible. Don't guess at license_plate when you cannot read it — null
beats a wrong guess.

Confidence:
  - definite — high visual certainty, clear subject, all fields reliable
  - likely   — best call but caveats (lighting, partial occlusion)
  - unsure   — guessing between plausible alternatives

Respond ONLY with JSON. The JSON object MUST have exactly these keys:
  - "class_confirmed"     — one of: vehicle, person, animal, unsure
  - "make"                — string|null
  - "model"               — string|null
  - "color"               — string|null
  - "type"                — string|null (sedan, pickup, SUV, ...)
  - "plate_visible"       — one of: yes, no, unsure
  - "license_plate"       — string|null
  - "distinctive_features" — array of 0-5 strings
  - "description"         — string
  - "confidence"          — one of: definite, likely, unsure

No prose, no markdown, no keys outside the list above.
"""

# Canonical mode name for the response_format dispatch layer (Rule 8a).
# The dispatch dict in infra/vision_analyzer.py MUST include this exact
# string or this prompt is unreachable.
MODE_NAME: str = "vm2_vehicle"


def build_vehicle_prompt() -> str:
    """Return the VM2 vehicle text prompt. The schema is the constraint
    layer (Rule 8); the text describes the task.
    """
    return PROMPT_TEXT
