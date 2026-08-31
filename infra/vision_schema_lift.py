"""
vision_schema_lift.py — Crop-prompt → alert-prompt schema adapter.

STATUS: stable
THREAD SAFETY: thread-safe (pure functions, no shared state)

INPUTS:
    - function arg vision_result: dict (required) — crop-prompt vision
      output (vehicle_identifier/prompt_template.py schema). May already
      contain alert-prompt fields (key preservation). May be empty {} on
      vision failure.

OUTPUTS:
    - return value (lift_crop_to_alert_schema): dict — same dict with
      alert-prompt fields populated if crop-prompt produced identifiable
      vehicle data. Mutates and returns the input dict for caller
      convenience (no copy if no lift happened).

PUBLIC API:
    lift_crop_to_alert_schema(vision_result: object) -> object
        Populate the alert-prompt schema fields (primary_subject,
        objects_detected, actions, scene_description) from the
        crop-prompt schema fields (color, make, model, description,
        confidence) when they're missing. Returns the input unchanged
        if it's not a dict (defensive — accepts dict | None | Any).
        No-op if alert-prompt fields already present.
        No-op if crop-prompt produced no identification.

DOES NOT DO:
    - HTTP transport → infra.alert_client
    - Parse vision JSON → vehicle_identifier.vision_response
    - Generate alert bodies → infra.alert_generator
    - Decide what counts as "a vehicle" → vehicle_identifier.identifier

WHY HERE:
    Two prompt schemas exist in the pipeline:
      - crop prompt (vehicle_identifier/prompt_template.py) returns
        color, body_style_hint, make, model, vehicle_features, description,
        confidence. Tailored for vision → matcher hand-off.
      - alert prompt (infra/alert_prompt.py) expects primary_subject,
        objects_detected, actions, scene_description. Tailored for the
        text alert LLM to render a Telegram body.

    Without translation, the alert LLM receives a vision_result dict
    that has crop-prompt fields but missing alert-prompt fields, and
    writes "empty exterior scene" L0 despite vision correctly
    identifying a vehicle. Verified on alert 0eefa8e9 (Tesla drive-by
    15:08 EDT, CAM2): crop vision returned Tesla Model Y blue conf=0.98,
    alert LLM produced "empty exterior scene" L0.

    The lift is a structural translator between two adjacent modules.
    It does NOT belong in listener.py (orchestration only — Step 4.5
    module-purpose discipline) and does NOT belong in vision_response.py
    (parses Qwen JSON, doesn't decide what fields mean — 6B.78
    anti-pattern). It belongs in its own module so the schema-pair
    contract is auditable and testable.

CALLED BY:
    - listener.listener: immediately after unwrapping VisionResult, before
      generate_alert() consumes the dict

CALLS INTO:
    - (none — pure dict surgery)

RELATED:
    - vehicle_identifier.prompt_template — produces crop-prompt schema
    - infra.alert_prompt — consumes alert-prompt schema
    - vehicle_identifier.identifier — produces VisionResult envelope
    - infra.alert_generator — orchestrates the second LLM call
"""


from typing import Any


def lift_crop_to_alert_schema(vision_result: Any) -> Any:
    """Populate alert-prompt fields from crop-prompt fields.

    The crop prompt (vehicle_identifier/prompt_template.py) returns
    color, body_style_hint, make, model, vehicle_features, description,
    confidence. The alert prompt (infra/alert_prompt.py _build_payload)
    reads primary_subject, objects_detected, actions, scene_description.

    When the crop prompt succeeds with identifiable vehicle data (any of
    make/model/color non-empty), populate the alert-prompt fields so the
    alert LLM can write a meaningful body. When the crop prompt failed
    (empty dict) or produced no identification, return unchanged —
    the alert LLM will fall through to its "empty exterior scene" L0
    fallback, which is correct behavior.

    Idempotent: if alert-prompt fields are already present, the lift
    does not overwrite. This guards against the case where a caller
    pre-populated the fields manually (e.g. for unit tests).

    Returns the input dict for caller chaining convenience.
    """
    if not isinstance(vision_result, dict):
        return vision_result

    # Don't overwrite if caller already set the alert fields.
    if vision_result.get("primary_subject"):
        return vision_result

    # Source fields from the crop-prompt schema.
    color = vision_result.get("color")
    make = vision_result.get("make")
    model = vision_result.get("model")
    description = vision_result.get("description", "")

    # If the crop prompt produced no identification, no lift to do.
    # The alert LLM will fall through to "empty exterior scene" L0.
    if not (color or make or model):
        return vision_result

    # Build primary_subject from the available identification fields.
    # Order: color + make + model, lowercased for the alert LLM.
    parts = [p for p in (color, make, model) if p]
    primary_subject = " ".join(parts).strip().lower()
    if not primary_subject:
        primary_subject = "vehicle"

    # scene_description: prefer Qwen's description; fall back to a
    # constructed sentence so the alert LLM has prose to work with.
    scene_description = description.strip() if description else f"A {primary_subject}."

    # Populate alert-prompt fields. Use setdefault so we don't overwrite
    # caller-set values (matches the early-return guard above).
    vision_result.setdefault("primary_subject", primary_subject)
    vision_result.setdefault("objects_detected", ["vehicle"])
    vision_result.setdefault("actions", [])
    vision_result.setdefault("scene_description", scene_description)

    return vision_result
