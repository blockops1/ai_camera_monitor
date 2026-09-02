"""
vehicle_prompt.py — §11.115.4 vehicle call-2 prompt + schema (consolidation).

STATUS: provisional (Phase §11.115; will stabilize after live telemetry)
THREAD SAFETY: thread-safe (module-level constants; no shared state)

INPUTS:
  - fn `build_vehicle_prompt(camera_name, captured_at)` — no IO, no env vars.

OUTPUTS:
  - VEHICLE_SCHEMA_JSON: JSON literal embedded in prompt. Mirrors the
    VEHICLE_CROP_PROMPT_TEMPLATE schema (make, model, color, body_style,
    vehicle_features, description, confidence, ...).
  - VEHICLE_PROMPT_TEMPLATE_FORMAT: raw template.
  - build_vehicle_prompt(...) -> str.

PUBLIC API:
  - VEHICLE_SCHEMA_JSON             str
  - VEHICLE_PROMPT_TEMPLATE_FORMAT  str
  - build_vehicle_prompt(...)       str

DOES NOT DO:
  - Call Qwen. (See infra.vision_analyzer.analyze_frames_queued.)
  - Validate the model response. (See infra.vehicle_matcher.)
  - Match vehicles to enrolled identities. (See infra.vehicle_matcher.)

CALLED BY:
  - listener.single_pipeline — call 2 prompt factory for ClassLabel.VEHICLE.

RELATED:
  - PLAN.md §11.115 — design rationale.
  - infra.prompt_templates.VEHICLE_CROP_PROMPT_TEMPLATE — LEGACY module
    with the original schema. Re-exported here for §11.115 routing.
    Will be removed in a follow-up commit once all callers move.

Design notes:
  - This is a CONSOLIDATION module: the schema and prompt text come
    from infra.prompt_templates.VEHICLE_CROP_PROMPT_TEMPLATE, which
    has been the production vehicle prompt for ~6 months. No schema
    change — vehicles don't have face-selection routing.
  - The two-crop invariant still applies: Qwen receives both crop_a
    and crop_b (the same two crops every other step sees).
  - We do not introduce a new `description` field here — the existing
    one from VEHICLE_CROP_PROMPT_TEMPLATE is kept verbatim.
"""
from __future__ import annotations

from infra.prompt_templates import VEHICLE_CROP_PROMPT_TEMPLATE

# Re-export the legacy template under the §11.115 name.
VEHICLE_PROMPT_TEMPLATE_FORMAT: str = VEHICLE_CROP_PROMPT_TEMPLATE


# ============================================================================
# Schema literal — embedded into the prompt.
# ============================================================================
# Mirror the schema that VEHICLE_CROP_PROMPT_TEMPLATE's free-form text
# instructs Qwen to emit. Keeping this here lets callers and tests
# reference a single, named schema literal without depending on the
# legacy VEHICLE_CROP_PROMPT_TEMPLATE shape directly.
VEHICLE_SCHEMA_JSON = """\
{
  "vehicles": [
    {
      "vehicle_id": "v1",
      "color": "black" | "white" | "gray" | "silver" | "red" | "blue" | "green" | "yellow" | "brown" | "orange" | "other" | "unknown" | null,
      "body_style_hint": "pickup" | "sedan" | "suv" | "van" | "hatchback" | "coupe" | "trailer" | "tractor" | "motorcycle" | "truck (commercial)" | null,
      "make": "Ford" | "Chevrolet" | "Tesla" | "Toyota" | "Honda" | "Ram" | "GMC" | "Jeep" | "Nissan" | "Subaru" | null,
      "model": "F-150" | "Silverado 1500" | "Model Y" | "RAV4" | null,
      "vehicle_features": {
        "wheel_style": "..." | null,
        "wheel_arch": "..." | null,
        "wheel_color": "..." | null,
        "roofline_style": "..." | null,
        "front_grille_style": "..." | null,
        "headlight_signature": "..." | null,
        "rear_lights_signature": "..." | null,
        "tailgate_type": "..." | null,
        "badge_text_readable": "..." | null,
        "window_tint": "none" | "light" | "dark" | "factory_privacy" | null,
        "cab_marker_lights": true | false | null,
        "bed_cover": "none" | "tonneau" | "camper_shell" | "topper" | null
      },
      "description": "1-2 sentence free-text identification in plain English"
    }
  ],
  "primary_vehicle_index": 0,
  "confidence": 0.0-1.0,
  "notable_details": ["detail 1", "detail 2"]
}"""


def build_vehicle_prompt(camera_name: str, captured_at: str) -> str:
    """Render the §11.115 vehicle call-2 prompt.

    Args:
        camera_name: friendly camera name (e.g. "Driveway") for context.
        captured_at: ISO-8601 timestamp for the captured frames.

    Returns:
        Fully-rendered prompt string ready to send to Qwen3-VL.

    Notes:
        Delegates to VEHICLE_CROP_PROMPT_TEMPLATE — same template that
        has been in production since Phase 6B.50. We use plain
        str.replace() (not str.format()) because the legacy template
        body contains literal `{...}` braces that conflict with
        format() substitution syntax.

        The consolidated `vehicle_prompt` module just gives callers a
        §11.115-shaped factory.
    """
    return (
        VEHICLE_CROP_PROMPT_TEMPLATE.replace("{camera_name}", camera_name).replace(
            "{captured_at}", captured_at
        )
    )
