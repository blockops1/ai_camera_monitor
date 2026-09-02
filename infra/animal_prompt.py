"""
animal_prompt.py — §11.115.4 animal call-2 prompt + schema.

STATUS: provisional (Phase §11.115; will stabilize after live telemetry)
THREAD SAFETY: thread-safe (module-level constants; no shared state)

INPUTS:
  - fn `build_animal_prompt(camera_name, captured_at)` — no IO, no env vars.

OUTPUTS:
  - ANIMAL_SCHEMA_JSON: JSON literal embedded in prompt. Contains
    species, breed, size, color_pattern, distinctive_features,
    confidence, notable_details.
  - ANIMAL_PROMPT_TEMPLATE_FORMAT: raw template.
  - build_animal_prompt(...) -> str.

PUBLIC API:
  - ANIMAL_SCHEMA_JSON             str
  - ANIMAL_PROMPT_TEMPLATE_FORMAT  str
  - build_animal_prompt(...)       str

DOES NOT DO:
  - Call Qwen. (See infra.vision_analyzer.analyze_frames_queued.)
  - Validate the model response. (Class-specific validators handle that.)
  - Match animals to enrolled identities. (See infra.animal_matcher.)

CALLED BY:
  - listener.single_pipeline — call 2 prompt factory for ClassLabel.ANIMAL.

RELATED:
  - PLAN.md §11.115 — design rationale.
  - infra.animal_prompt_template.py — LEGACY module with old schema.
    Will be removed in a follow-up commit.
  - infra.classify_prompt — Qwen call 1 (shared classify).

Design notes:
  - Animal call-2 is much simpler than person call-2: no face selection,
    no crop bias, no two-call Qwen logic inside this module.
  - Schema mirrors the existing animal_prompt_template.py but is keyed
    on the §11.115 single-pipeline model: 2 crops in, 1 JSON out.
  - Breed and color_pattern may be null (wildlife, mixed breeds).
"""
from __future__ import annotations

# ============================================================================
# Schema literal — embedded into the prompt so Qwen emits this exact shape.
# ============================================================================
ANIMAL_SCHEMA_JSON = """\
{
  "species": "dog" | "cat" | "deer" | "raccoon" | "fox" | "coyote" | "rabbit" | "squirrel" | "bird" | "other" | null,
  "breed": "labrador" | "golden retriever" | "german shepherd" | "tabby" | "siamese" | "mixed" | null,
  "size": "small" | "medium" | "large" | null,
  "color_pattern": "black" | "white" | "gray" | "brown" | "tan" | "spotted" | "striped" | "multi" | null,
  "distinctive_features": ["white-tipped tail", "blue collar with name tag"] | [],
  "action": "walking" | "running" | "sitting" | "standing" | "eating" | "sleeping" | "other" | null,
  "confidence": 0.0-1.0,
  "notable_details": ["walking along fence line", "appears to be unleashed"] | []
}"""


# ============================================================================
# Prompt template — sent to Qwen3-VL for animal events.
# ============================================================================
# Same shared-classify design as person call-2: 2 crops in, 1 JSON out.
# Bias toward `null` on uncertainty.
ANIMAL_PROMPT_TEMPLATE_FORMAT = (
    'Camera "{camera_name}". Captured at {captured_at}. '
    "Animal call 2 (Phase §11.115).\n\n"
    "You are seeing TWO images from the SAME camera, captured a few seconds "
    "apart. Inspect BOTH before deciding.\n\n"
    "Identify the animal(s) visible across the frames. For each, report:\n\n"
    "  species               — common name from enum. `other` if not listed.\n"
    "  breed                 — common breed if dog/cat; null if wildlife or mixed.\n"
    "  size                  — small / medium / large.\n"
    "  color_pattern         — primary color + pattern.\n"
    "  distinctive_features  — short noun phrases (collar, tags, markings).\n"
    "  action                — single verb describing what it's doing.\n"
    "  confidence            — 0.0 (no idea) to 1.0 (certain).\n"
    "  notable_details       — 1-3 short observations.\n\n"
    "Output (return EXACTLY this JSON shape, nothing else):\n"
    "{schema}\n\n"
    "When uncertain, return null for the field you can't determine, "
    "and lower confidence. Do NOT guess the breed for wildlife.\n"
)


def build_animal_prompt(camera_name: str, captured_at: str) -> str:
    """Render the §11.115 animal call-2 prompt.

    Args:
        camera_name: friendly camera name (e.g. "Front Porch") for context.
        captured_at: ISO-8601 timestamp for the captured frames.

    Returns:
        Fully-rendered prompt string ready to send to Qwen3-VL.
    """
    return ANIMAL_PROMPT_TEMPLATE_FORMAT.format(
        camera_name=camera_name,
        captured_at=captured_at,
        schema=ANIMAL_SCHEMA_JSON,
    )
