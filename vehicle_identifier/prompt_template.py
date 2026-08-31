"""Prompt template + JSON schema for the crop-mode vision call.

These are pure constants and a tiny formatter. No I/O. The vision
client uses `render_crop_prompt(...)` to fill in camera_name,
captured_at, event_hint_block.

The schema is the structured contract Qwen must match. It mirrors
VEHICLE_CROP_SCHEMA_JSON in the legacy code but is expressed here
as a Python dict for tests to introspect.

Phase.100: the prompt is unchanged — Qwen still receives the
same template regardless of whether one or many crop images are
attached. Verified by `scripts/probe_multi_crop_vision.py` on two
live alerts: the model consolidates a multi-image input into one
signature describing the actual vehicle (or noting its absence).

Phase.144 (§11.66): the prompt now describes a 3-image payload —
two streak crops (motion-bbox crops from consecutive frames) and a
pairwise differential image. The diff image is the disambiguating
signal: the moving subject lights up the diff; stationary vehicles
stay dark. Qwen uses this to pick the MOVING subject, not whatever
happens to be the most vehicle-looking object in the streak crop.
"""

from __future__ import annotations

from typing import Any

VEHICLE_CROP_PROMPT_TEMPLATE: str = (
    'Camera "{camera_name}". You are receiving THREE images describing '
    "one motion event captured at {captured_at}:\n\n"
    "  1. STREAK CROP A — frame_3 cropped at the camera's motion bbox "
    "(the region where motion was detected between frames 2 and 3).\n"
    "  2. STREAK CROP B — frame_4 cropped at the camera's motion bbox "
    "(motion between frames 3 and 4).\n"
    "  3. PAIRWISE DIFFERENTIAL — abs(frame_3 − frame_4), brightened, "
    "with a green rectangle showing the diff(2,3) bbox and a cyan "
    "rectangle showing the diff(3,4) bbox. Where pixels are bright, "
    "motion happened. Where pixels are dark, the scene is stationary.\n\n"
    "The MOVING subject is whatever lights up the diff. Stationary "
    "vehicles in the same frame stay dark in the diff and are NOT the "
    "moving subject — ignore them. Identify the moving subject only. "
    "If the moving subject is a tractor, riding mower, ATV, or other "
    "non-passenger equipment, say so explicitly with "
    "body_style_hint='tractor' or 'motorcycle' and make/model=null.\n\n"
    "For the moving subject, report:\n"
    "  color (black|white|gray|silver|red|blue|green|yellow|brown|orange|other|unknown)\n"
    "  body_style_hint (pickup|sedan|suv|van|hatchback|coupe|trailer|tractor|motorcycle|'truck (commercial)'|null)\n"
    "  make (Ford|Chevrolet|Tesla|Toyota|Honda|Ram|GMC|Jeep|Nissan|Subaru|...|null)\n"
    "  model (F-150|Silverado 1500|Model Y|RAV4|...|null)\n"
    "  vehicle_features {\n"
    "    wheel_style, wheel_arch, wheel_color, roofline_style,\n"
    "    front_grille_style, headlight_signature, rear_lights_signature,\n"
    "    tailgate_type, badge_text_readable,\n"
    "    window_tint (none|light|dark|factory_privacy|null),\n"
    "    cab_marker_lights (true|false|\"false\"|null),\n"
    "    bed_cover (none|tonneau|camper_shell|topper|null)\n"
    "  } (string or null for each feature)\n"
    "  description (1-2 sentence free-text identification of the\n"
    "    moving subject in plain English — name what you see: color,\n"
    "    body type, make, model, and any distinguishing features like\n"
    "    grille shape, wheels, headlights, badge, roofline. If it's a\n"
    "    tractor or equipment, say \"compact tractor\" / \"riding\n"
    "    mower\" / etc. explicitly.)\n"
    "  confidence (0.0-1.0)\n\n"
    "Rules:\n"
    "- The diff image is the source of truth for which object is\n"
    "  moving. Pick the object whose pixels are bright in the diff.\n"
    "- Inspect badges/lettering/grille shape/taillights FIRST before\n"
    "guessing make/model.\n"
    "- If you can't tell make or model, return null — do NOT guess.\n"
    "- Pickups: F-150/F-250/F-350 vs Silverado 1500/2500 vs Ram 1500/2500\n"
    "vs Tundra vs Tacoma vs Frontier vs Colorado. Make before model.\n"
    "- vehicle_features are the most valuable fields — they let us match\n"
    "this vehicle from a different camera angle next time. Read each one.\n"
    "- Confidence reflects make/model only.\n"
    "- Output ONLY the JSON object. No preamble. No markdown fences.\n"
    "{event_hint_block}"
)


# JSON schema Qwen must match. Mirrors the schema used in production
# but expressed as a Python dict so tests can introspect it.
VEHICLE_CROP_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "color": {
            "type": "string",
            "enum": [
                "black", "white", "gray", "silver", "red", "blue",
                "green", "yellow", "brown", "orange", "other",
                "unknown", "none",
            ],
        },
        "body_style_hint": {
            "type": ["string", "null"],
            "description": (
                "pickup|sedan|suv|van|hatchback|coupe|trailer|tractor|"
                "motorcycle|'truck (commercial)'|null"
            ),
        },
        "make": {"type": ["string", "null"]},
        "model": {"type": ["string", "null"]},
        "vehicle_features": {
            "type": "object",
            "properties": {
                "wheel_style": {"type": ["string", "null"]},
                "wheel_arch": {"type": ["string", "null"]},
                "wheel_color": {"type": ["string", "null"]},
                "roofline_style": {"type": ["string", "null"]},
                "front_grille_style": {"type": ["string", "null"]},
                "headlight_signature": {"type": ["string", "null"]},
                "rear_lights_signature": {"type": ["string", "null"]},
                "tailgate_type": {"type": ["string", "null"]},
                "badge_text_readable": {"type": ["string", "null"]},
                "window_tint": {"type": ["string", "null"]},
                "cab_marker_lights": {
                    "type": ["boolean", "string", "null"],
                    "description": "true|false|'false'|null",
                },
                "bed_cover": {"type": ["string", "null"]},
            },
            "required": [
                "wheel_style", "wheel_arch", "wheel_color", "roofline_style",
                "front_grille_style", "headlight_signature",
                "rear_lights_signature", "tailgate_type",
                "badge_text_readable", "window_tint",
                "cab_marker_lights", "bed_cover",
            ],
        },
        "description": {"type": "string"},
        "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
    },
    "required": [
        "color", "body_style_hint", "make", "model",
        "vehicle_features", "description", "confidence",
    ],
}


def render_crop_prompt(
    camera_name: str,
    captured_at: str,
    event_hint_block: str = "",
) -> str:
    """Fill in the template placeholders and return the final prompt string.

    Uses safe substitution because the template contains literal "{"
    characters in the vehicle_features schema block that .format() would
    mis-interpret.
    """
    out = VEHICLE_CROP_PROMPT_TEMPLATE
    out = out.replace("{camera_name}", camera_name)
    out = out.replace("{captured_at}", captured_at)
    out = out.replace("{event_hint_block}", event_hint_block)
    return out
