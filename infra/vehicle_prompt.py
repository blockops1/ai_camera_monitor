"""
vehicle_prompt.py — §11.115.4 vehicle call-2 prompt + schema (consolidation).

STATUS: provisional (Phase §11.115; will stabilize after live telemetry)
THREAD SAFETY: thread-safe (module-level constants; no shared state)

INPUTS:
  - fn `build_vehicle_prompt()` — no IO, no env vars.

OUTPUTS:
  - SCHEMA_JSON: JSON Schema dict for the llama-server strict-mode
    response_format. Mirrors the v1 vehicle schema (make, model,
    color, body_style_hint, vehicle_features, description, confidence,
    notable_details).
  - PROMPT_TEXT: instruction text directing the model to identify
    a single vehicle, never invent fields, and use null for
    unobservable values.

PUBLIC API:
  - SCHEMA_JSON             dict
  - PROMPT_TEXT             str
  - build_vehicle_prompt()  str

DOES NOT DO:
  - Call Qwen. (See infra.vision_analyzer.analyze_frames_queued.)
  - Validate the model response. (See infra.vehicle_matcher.)
  - Match vehicles to enrolled identities. (See infra.vehicle_matcher.)

CALLED BY:
  - listener.single_pipeline — call 2 prompt factory for ClassLabel.VEHICLE.

RELATED:
  - infra.prompt_templates.VEHICLE_CROP_PROMPT_TEMPLATE — LEGACY module
    with the original schema. Re-exported here for §11.115 routing.
    Will be removed in a follow-up commit once all callers move.

Design notes:
  - Schema is a JSON Schema dict (used via _response_format strict-mode).
  - The two-crop invariant still applies: Qwen receives both crop_a
    and crop_b (the same two crops every other step sees).
"""
from __future__ import annotations

# ============================================================================
# JSON Schema — used by _response_format for server-side enforcement.
# Mirrors v1 vehicle_prompt.py lines 62-90 schema shape.
# ============================================================================
SCHEMA_JSON: dict = {
    "type": "object",
    "properties": {
        "color": {
            "type": ["string", "null"],
            "enum": [
                "black", "white", "gray", "silver", "red", "blue",
                "green", "yellow", "brown", "orange", "other",
                "unknown", None,
            ],
            "description": "Dominant body color. null if unobservable.",
        },
        "body_style_hint": {
            "type": ["string", "null"],
            "enum": [
                "pickup", "sedan", "suv", "van", "hatchback", "coupe",
                "trailer", "tractor", "motorcycle", "truck (commercial)",
                None,
            ],
            "description": "Body style hint. null if unobservable.",
        },
        "make": {
            "type": ["string", "null"],
            "enum": [
                "Ford", "Chevrolet", "Tesla", "Toyota", "Honda",
                "Ram", "GMC", "Jeep", "Nissan", "Subaru", None,
            ],
            "description": "Make. null if unreadable or unobservable.",
        },
        "model": {
            "type": ["string", "null"],
            "description": "Model name. null if unreadable or unobservable.",
        },
        "vehicle_features": {
            "type": ["object", "null"],
            "description": "Detailed vehicle features. null if unobservable.",
            "properties": {
                "wheel_style": {
                    "type": ["string", "null"],
                    "description": "Wheel style. null if unobservable.",
                },
                "wheel_arch": {
                    "type": ["string", "null"],
                    "description": "Wheel arch shape. null if unobservable.",
                },
                "wheel_color": {
                    "type": ["string", "null"],
                    "description": "Wheel color. null if unobservable.",
                },
                "roofline_style": {
                    "type": ["string", "null"],
                    "description": "Roofline shape. null if unobservable.",
                },
                "front_grille_style": {
                    "type": ["string", "null"],
                    "description": "Front grille shape/pattern. null if unobservable.",
                },
                "headlight_signature": {
                    "type": ["string", "null"],
                    "description": "Headlight shape/pattern. null if unobservable.",
                },
                "rear_lights_signature": {
                    "type": ["string", "null"],
                    "description": "Rear taillight shape/pattern. null if unobservable.",
                },
                "tailgate_type": {
                    "type": ["string", "null"],
                    "description": "Tailgate type. null if unobservable.",
                },
                "badge_text_readable": {
                    "type": ["string", "null"],
                    "description": "Badge text if readable. null if unobservable.",
                },
                "window_tint": {
                    "type": ["string", "null"],
                    "enum": [
                        "none", "light", "dark", "factory_privacy", None,
                    ],
                    "description": "Window tint level. null if unobservable.",
                },
                "cab_marker_lights": {
                    "type": ["boolean", "null"],
                    "description": "Whether cab marker lights are visible. null if unobservable.",
                },
                "bed_cover": {
                    "type": ["string", "null"],
                    "enum": [
                        "none", "tonneau", "camper_shell", "topper", None,
                    ],
                    "description": "Bed cover type. null if unobservable.",
                },
            },
        },
        "description": {
            "type": "string",
            "description": "1-2 sentence natural-language identification in plain English.",
        },
        "confidence": {
            "type": "number",
            "minimum": 0.0,
            "maximum": 1.0,
            "description": "Overall confidence in the call, 0.0 to 1.0.",
        },
        "notable_details": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Notable visual details not covered above.",
        },
    },
    "required": [
        "color", "body_style_hint", "make", "model",
        "vehicle_features", "description", "confidence", "notable_details",
    ],
    "additionalProperties": False,
}

PROMPT_TEXT: str = """\
You are looking at TWO crops of one moving vehicle, taken fractions of
a second apart. The crops show the same vehicle from slightly different
angles or moments.

Identify the vehicle. Use the most specific terms you can read; null
when you cannot read something rather than guessing.

Respond ONLY with JSON matching this schema:

{
  "color": "black" | "white" | "gray" | "silver" | "red" | "blue" |
           "green" | "yellow" | "brown" | "orange" | "other" |
           "unknown" | null,
  "body_style_hint": "pickup" | "sedan" | "suv" | "van" | "hatchback" |
                     "coupe" | "trailer" | "tractor" | "motorcycle" |
                     "truck (commercial)" | null,
  "make": "Ford" | "Chevrolet" | "Tesla" | "Toyota" | "Honda" | "Ram" |
          "GMC" | "Jeep" | "Nissan" | "Subaru" | null,
  "model": string|null,
  "vehicle_features": {
    "wheel_style": string|null,
    "wheel_arch": string|null,
    "wheel_color": string|null,
    "roofline_style": string|null,
    "front_grille_style": string|null,
    "headlight_signature": string|null,
    "rear_lights_signature": string|null,
    "tailgate_type": string|null,
    "badge_text_readable": string|null,
    "window_tint": "none" | "light" | "dark" | "factory_privacy" | null,
    "cab_marker_lights": true | false | null,
    "bed_cover": "none" | "tonneau" | "camper_shell" | "topper" | null
  },
  "description": "1-2 sentence free-text identification in plain English",
  "confidence": 0.0-1.0 (number),
  "notable_details": ["detail 1", "detail 2"]
}

Rules:
- Focus on ONE vehicle only.
- Never invent fields that are not in this schema.
- Use null for anything you cannot observe. Never guess.
- Confidence is a number between 0.0 and 1.0.

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
