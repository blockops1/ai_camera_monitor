"""
animal_prompt.py — §11.115.4 animal call-2 prompt + schema.

STATUS: provisional (Phase §11.115; will stabilize after live telemetry)
THREAD SAFETY: thread-safe (module-level constants; no shared state)

INPUTS:
  - fn `build_animal_prompt()` — no IO, no env vars.

OUTPUTS:
  - SCHEMA_JSON: JSON Schema dict for the llama-server strict-mode
    response_format. Mirrors v1 animal schema (species, breed, size,
    color_pattern, distinctive_features, action, confidence,
    notable_details).
  - PROMPT_TEXT: instruction text directing the model to identify an
    animal, use tail-shape / ear-shape / coat-pattern vocabulary,
    emit null for unobservable values, never invent breed.
  - build_animal_prompt() -> str.

PUBLIC API:
  - SCHEMA_JSON             dict
  - PROMPT_TEXT             str
  - build_animal_prompt()   str

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
# JSON Schema — used by _response_format for server-side enforcement.
# Mirrors v1 animal_prompt.py lines 48-58 schema shape.
# ============================================================================
SCHEMA_JSON: dict = {
    "type": "object",
    "properties": {
        "species": {
            "type": ["string", "null"],
            "enum": [
                "dog", "cat", "deer", "raccoon", "fox",
                "coyote", "rabbit", "squirrel", "bird",
                "other", None,
            ],
            "description": "Common name. null if unidentifiable.",
        },
        "breed": {
            "type": ["string", "null"],
            "enum": [
                "labrador", "golden retriever", "german shepherd",
                "tabby", "siamese", "mixed", None,
            ],
            "description": "Common breed if dog/cat; null if wildlife or mixed.",
        },
        "size": {
            "type": ["string", "null"],
            "enum": ["small", "medium", "large", None],
            "description": "Approximate size. null if unobservable.",
        },
        "color_pattern": {
            "type": ["string", "null"],
            "enum": [
                "black", "white", "gray", "brown", "tan",
                "spotted", "striped", "multi", None,
            ],
            "description": "Primary color + pattern. null if unobservable.",
        },
        "distinctive_features": {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 0,
            "description": "Short noun phrases (collar, tags, markings). Empty array if none.",
        },
        "action": {
            "type": ["string", "null"],
            "enum": [
                "walking", "running", "sitting",
                "standing", "eating", "sleeping",
                "other", None,
            ],
            "description": "Single verb describing what it's doing. null if unobservable.",
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
            "minItems": 0,
            "description": "1-3 short observations the operator should see. Empty array if none.",
        },
        "better_crop": {
            "type": "string",
            "enum": ["crop_a", "crop_b", "neither"],
            "description": "Which crop shows the animal more clearly. crop_a, crop_b, or neither.",
        },
    },
    "required": [
        "species", "breed", "size", "color_pattern",
        "distinctive_features", "action", "confidence", "notable_details",
        "better_crop",
    ],
    "additionalProperties": False,
}

PROMPT_TEXT: str = """\
You are looking at TWO crops of one moving animal, taken fractions of
a second apart. The crops show the same animal from slightly different
angles or moments.

Identify the animal(s) visible. For each, report:

  species               — common name from enum. "other" if not listed.
  breed                 — common breed if dog/cat; null if wildlife or mixed.
  size                  — small / medium / large.
  color_pattern         — primary color + pattern.
  distinctive_features  — short noun phrases (collar, tags, markings).
  action                — single verb describing what it's doing.
  confidence            — 0.0 (no idea) to 1.0 (certain).
  notable_details       — 1-3 short observations.

Output (return EXACTLY this JSON shape, nothing else):

{
  "species": "dog" | "cat" | "deer" | "raccoon" | "fox" | "coyote" |
             "rabbit" | "squirrel" | "bird" | "other" | null,
  "breed": "labrador" | "golden retriever" | "german shepherd" |
           "tabby" | "siamese" | "mixed" | null,
  "size": "small" | "medium" | "large" | null,
  "color_pattern": "black" | "white" | "gray" | "brown" | "tan" |
                   "spotted" | "striped" | "multi" | null,
  "distinctive_features": ["white-tipped tail", "blue collar with name tag"] | [],
  "action": "walking" | "running" | "sitting" | "standing" | "eating" |
            "sleeping" | "other" | null,
  "confidence": 0.0-1.0 (number),
  "notable_details": ["walking along fence line", "appears to be unleashed"] | [],
  "better_crop": "crop_a" | "crop_b" | "neither"
}

After analyzing both crops, decide which crop shows the animal more clearly:
  "crop_a" — image 1 has the clearer view of the animal
  "crop_b" — image 2 has the clearer view of the animal
  "neither" — neither image shows the animal clearly enough

Rules:
- Focus on ONE animal only.
- Never invent fields that are not in this schema.
- Use null for anything you cannot observe. Never guess.
- Breed and color_pattern may be null (wildlife, mixed breeds).
- Use the most specific species name you can — null only if you truly
  cannot tell what is in the crops.
- For distinctive_features, list 1-5 features that distinguish THIS
  individual from other members of the same species. Generic descriptors
  like "brown fur" or "medium size" are NOT distinctive.
- Tail-shape, ear-shape, and coat-pattern are key identification markers.

When uncertain, return null for the field you can't determine,
and lower confidence. Do NOT guess the breed for wildlife.

No prose, no markdown, no keys outside the list above.
"""

# Canonical mode name for the response_format dispatch layer (Rule 8a).
# The dispatch dict in infra/vision_analyzer.py MUST include this exact
# string or this prompt is unreachable.
MODE_NAME: str = "vm2_animal"


def build_animal_prompt() -> str:
    """Return the §11.115 animal call-2 text prompt.

    The schema is the constraint layer (Rule 8); the text describes
    the task. Uses tail-shape / ear-shape / coat-pattern vocabulary,
    null for unobservable, never invent breed.
    """
    return PROMPT_TEXT
