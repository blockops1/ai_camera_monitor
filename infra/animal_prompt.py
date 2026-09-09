"""
animal_prompt.py — Vision Model 2: detail an animal subject.

Stage 6 (VM2) of the linear pipeline. Triggered when VM1 (or the YOLO
gate) classifies the moving subject as `animal`. Output consumed by
the animal matcher (vehicle_matcher.match on an animal subject) and
the Telegram formatter.

Operator-locked contract (the operator 2026-09-06):
  "the threat-level" is NOT requested. TG#2 contains 2 crops + VM2
  output; no threat classification. This prompt follows that —
  animal identification + description only, no threat assessment.

The YOLO gate may flag "dog" as a frequent false-positive for coyote
(or vice versa). Per Rule 1 of the vision-prompt-design skill, vision-
8B OVERRIDES the gate hint. The prompt explicitly grants this
override license so the model can say "coyote" when YOLO said "dog."

Schema (response_format layer):
  {
    "class_confirmed":      enum["vehicle", "person", "animal", "unsure"],
    "species":              string|null,    # free-form per Rule 3
    "size_class":           enum["small", "medium", "large", "unsure"],
    "behavior":             string|null,
    "domesticated":         enum["yes", "no", "unsure"],
    "collar_visible":       enum["yes", "no", "unsure"],
    "distinctive_features": string[],       # 1-5 re-ID markers
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
        "species": {
            "type": ["string", "null"],
            "description": "Most specific name: coyote, Eastern coyote, red fox, gray fox, fisher, raccoon, white-tailed deer, wild turkey, domestic dog (breed if recognizable), domestic cat (breed if recognizable), opossum, bobcat, ... Use null only if truly unidentifiable.",
        },
        "size_class": {
            "type": "string",
            "enum": ["small", "medium", "large", "unsure"],
            "description": "Approximate size relative to common yard fauna. 'small' = squirrel/cat size; 'medium' = dog/fox/coyote; 'large' = deer/horse.",
        },
        "behavior": {
            "type": ["string", "null"],
            "description": "What the animal is doing: 'walking east along fence', 'foraging', 'trotting', 'eating from bowl', 'staring at camera'. null if not discernible.",
        },
        "domesticated": {
            "type": "string",
            "enum": ["yes", "no", "unsure"],
            "description": "Does this look like a domesticated animal? (pet, livestock, working animal)",
        },
        "collar_visible": {
            "type": "string",
            "enum": ["yes", "no", "unsure"],
            "description": "Is a collar visible (a domesticated marker)?",
        },
        "distinctive_features": {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 0,
            "maxItems": 5,
            "description": "1-5 re-ID markers that distinguish THIS individual from other members of the same species. Examples: 'left ear notched', 'white-tipped tail', 'scar on right shoulder', 'limp in left rear leg', 'blue collar', 'asymmetric gait', 'mange patch on left flank'. Generic descriptors like 'brown fur' or 'medium size' are NOT distinctive — they apply to most individuals.",
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
    "required": ["class_confirmed", "size_class", "domesticated", "collar_visible", "distinctive_features", "description", "confidence"],
    "additionalProperties": False,
}

PROMPT_TEXT: str = """\
You are looking at TWO crops of one moving animal, taken fractions of
a second apart. The crops show the same animal from slightly different
angles or moments.

Your species call OVERRIDES any upstream classifier hint. If you see
a coyote, say coyote, even if the gate said dog. If you see a fox,
say fox, even if the gate said cat. Trust your eyes.

Use the most specific species name you can — null only if you truly
cannot tell what is in the crops. Examples of valid specificity:
coyote, Eastern coyote, red fox, gray fox, fisher, raccoon,
white-tailed deer, wild turkey, domestic dog (with breed if you can
read it), domestic cat (with breed if you can read it), opossum,
bobcat.

For distinctive_features, list 1-5 features that distinguish THIS individual
from other members of the same species. Examples: left ear notched,
white-tipped tail, scar on right shoulder, limp in left rear leg,
blue collar, asymmetric gait, mange patch on left flank. Generic
descriptors like 'brown fur' or 'medium size' are NOT distinctive.

Confidence:
  - definite — high visual certainty, clear subject, all fields reliable
  - likely   — best call but caveats (lighting, partial occlusion)
  - unsure   — guessing between plausible alternatives

Respond ONLY with JSON. The JSON object MUST have exactly these keys:
  - "class_confirmed"      — one of: vehicle, person, animal, unsure
  - "species"              — string|null
  - "size_class"           — one of: small, medium, large, unsure
  - "behavior"             — string|null
  - "domesticated"         — one of: yes, no, unsure
  - "collar_visible"       — one of: yes, no, unsure
  - "distinctive_features" — array of 0-5 strings
  - "description"          — string
  - "confidence"           — one of: definite, likely, unsure

No prose, no markdown, no keys outside the list above.
"""

# Canonical mode name for the response_format dispatch layer (Rule 8a).
# The dispatch dict in infra/vision_analyzer.py MUST include this exact
# string or this prompt is unreachable.
MODE_NAME: str = "vm2_animal"


def build_animal_prompt() -> str:
    """Return the VM2 animal text prompt."""
    return PROMPT_TEXT
