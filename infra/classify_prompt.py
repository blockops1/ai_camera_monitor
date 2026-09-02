"""
classify_prompt.py — §11.115.2 shared classify prompt for Qwen call 1.

STATUS: provisional (Phase §11.115; will stabilize after live telemetry)
THREAD SAFETY: thread-safe (module-level constants; no shared state)

INPUTS:
  - fn `build_classify_prompt(camera_name, captured_at)`
        reads fn args only; no IO, no env vars.

OUTPUTS:
  - The rendered prompt string passed to Qwen3-VL via analyze_frames_queued.
  - CLASSIFY_PROMPT_TEMPLATE_FORMAT: the raw template (with `{placeholder}s`)

PUBLIC API:
  - CLASSIFY_PROMPT_TEMPLATE_FORMAT    the raw template
  - build_classify_prompt(...)         returns fully-rendered prompt string

DOES NOT DO:
  - Does NOT call Qwen or any model. It only formats the prompt.
  - Does NOT validate the model response. (See infra.classify_validator.)
  - Does NOT detect motion, faces, or class-specific attributes.
    (Class-specific prompts: infra.person_prompt, infra.animal_prompt,
    infra.vehicle_prompt.)

CALLED BY:
  - listener.single_pipeline.run() — first Qwen call for every event.

RELATED:
  - PLAN.md §11.115 — design rationale (shared classify first, then diverge).
  - infra.classify_schema — schema literal embedded in the prompt.

Design notes:
  - Two images: crop_a and crop_b. Both sent simultaneously (mirrors
    Phase 6B.100 multi-crop pattern).
  - Prompt explicitly tells Qwen to default to `other` when uncertain.
    Bug C (Qwen hallucinating `face_visible: true` on back-of-head shots)
    motivated the same discipline here — Qwen should NOT guess.
  - Confidence is 0.0-1.0; 0.5+ is "confident enough to route", <0.5
    routes to OTHER via the validator.
"""
from __future__ import annotations

from infra.classify_schema import CLASSIFY_SCHEMA_JSON

# ============================================================================
# Prompt template — sent to Qwen3-VL as the shared classify call.
# ============================================================================
# Output drives the class-specific Qwen call 2 (person/vehicle/animal).
# `other` class → log only, no Telegram (per §11.115 maintainer directive).
CLASSIFY_PROMPT_TEMPLATE_FORMAT = (
    'Camera "{camera_name}". Captured at {captured_at}. '
    "Shared classify call (Phase §11.115).\n\n"
    "You are seeing TWO images from the SAME camera, captured a few seconds "
    "apart. Inspect BOTH before deciding.\n\n"
    "Classify what is in the images. Use exactly one of:\n"
    '  vehicle  — a car, truck, SUV, motorcycle, or other road vehicle\n'
    '  person   — a human being (any pose, any clothing)\n'
    '  animal   — a non-human living creature (dog, cat, deer, etc.)\n'
    '  other    — anything that does not clearly fit the above three\n\n'
    "If you are uncertain, return `other`. Do NOT guess — picking the wrong "
    "class routes the event down the wrong pipeline.\n\n"
    "Output (return EXACTLY this JSON shape, nothing else):\n"
    "{schema}\n\n"
    "Constraints:\n"
    "  - `class`     — must be exactly one of the 4 strings above.\n"
    "  - `confidence` — 0.0 (not at all sure) to 1.0 (certain). Be honest;\n"
    "                   0.5 means 'coin flip'.\n"
    "  - `reasoning`  — 1-2 short phrases (e.g. 'silver sedan, partial view',\n"
    "                   'back of head, no face visible'). Do not exceed ~20 words.\n\n"
    "Return `other` if:\n"
    "  - you cannot tell whether the subject is a vehicle, person, or animal\n"
    "  - the images are blurred, occluded, or empty\n"
    "  - the only motion is vegetation, lighting, or weather\n"
)


def build_classify_prompt(camera_name: str, captured_at: str) -> str:
    """Render the shared classify prompt for Qwen call 1.

    Args:
        camera_name: friendly camera name (e.g. "Front Porch") for context.
        captured_at: ISO-8601 timestamp string for the captured frames.

    Returns:
        Fully-rendered prompt string ready to send to Qwen3-VL.
    """
    return CLASSIFY_PROMPT_TEMPLATE_FORMAT.format(
        camera_name=camera_name,
        captured_at=captured_at,
        schema=CLASSIFY_SCHEMA_JSON,
    )
