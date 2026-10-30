"""
vm1_prompt.py - Vision Model 1: classify subject from 2 crops alone.

Operator-locked contract (the operator 2026-09-06):
  "No I don't want any hint given to vision model one of what it is in
  any image. It needs to figure out from the two crops alone what the
  object is."

Operator reversal (2026-09-19): VM1 now emits subclass, count, face_visible,
and notes alongside the base class. This is a schema extension — the base
class field (vehicle/person/animal/unsure) is unchanged.

Stage 5 of the linear pipeline. Input: 2 motion-streak crops (bytes or
filepath). Output: dict with keys: class, subclass, confidence, count,
face_visible, notes.

New VM1 schema (response_format layer):
  {
    "class":         enum["vehicle", "person", "animal", "unsure"],
    "subclass":      string|null  (e.g. "dog", "sedan", null for unsure),
    "confidence":    enum["definite", "likely", "unsure"],
    "count":         integer|null,
    "face_visible":  boolean|null,
    "notes":         string|null,
  }

DISPATCH surface (for detail_class VM2): one of
  vehicle | person | animal | unsure.
The VM1 class field selects the VM2 mode.

The "unsure" fourth class is mandatory (Rule 6 from vision-prompt-design
skill): Qwen defaults toward picking a class, so we give it a fourth
option to admit ambiguity. Cooldown still keys on the YOLO gate's
tentative classification (not VM1's) - VM1 output is informational for
downstream VM2 prompt selection and Telegram body, not for throttling.
"""
from __future__ import annotations

SCHEMA_JSON: dict = {
    "type": "object",
    "properties": {
        "class": {
            "type": "string",
            "enum": ["vehicle", "person", "animal", "unsure"],
            "description": "What is in the crops? Pick unsure if you cannot tell.",
        },
        "subclass": {
            "type": ["string", "null"],
            "description": "More specific identification within the class. null if class is unsure or unobservable.",
        },
        "confidence": {
            "type": "string",
            "enum": ["definite", "likely", "unsure"],
            "description": "How sure are you? 'definite' = high certainty, 'likely' = best call with caveats, 'unsure' = guessing between alternatives.",
        },
        "count": {
            "type": ["integer", "null"],
            "description": "How many of the subject are visible? null if unobservable.",
        },
        "face_visible": {
            "type": ["boolean", "null"],
            "description": "Is a human face visible in the crops? null if class is not person or face is not visible.",
        },
        "notes": {
            "type": ["string", "null"],
            "description": "Any additional observations. null if none.",
        },
    },
    "required": [
        "class", "subclass", "confidence",
        "count", "face_visible", "notes",
    ],
    "additionalProperties": False,
}

PROMPT_TEXT: str = """\
You are looking at TWO crops taken in quick succession from one motion event.

The crops show one moving subject. The two crops were taken fractions
of a second apart. They may show the same subject from slightly
different angles, or one subject followed by another.

Classify the subject as exactly one of:
  - vehicle - car, truck, SUV, van, motorcycle, bicycle, bus, tractor
  - person - adult or child; any clothing
  - animal - domestic, wild, on the property
  - unsure - only if you genuinely cannot tell what is in the crops

Pick "unsure" rather than guess. A wrong class triggers an expensive
downstream call. An unsure answer is recoverable; a wrong vehicle-vs-
person call is not.

Set confidence:
  - definite - high visual certainty, clear subject, unambiguous
  - likely   - best call but with caveats (lighting, partial occlusion)
  - unsure   - guessing between plausible alternatives

Also report:
  subclass    - more specific identification (e.g. "dog", "sedan").
                null if you cannot identify beyond the main class.
  count       - number of subjects visible. null if unobservable.
  face_visible - true if a human face is visible. false if face is
                  visible but clearly NOT a face. null if class is not
                  person or face is not visible.
  notes       - any other observations (clothing, behavior, environment).
                null if there are no additional observations.

Respond ONLY with JSON. The JSON object MUST have exactly these keys:
  - "class"        - one of: vehicle, person, animal, unsure
  - "subclass"     - string or null
  - "confidence"   - one of: definite, likely, unsure
  - "count"        - integer or null
  - "face_visible" - boolean or null
  - "notes"        - string or null

Example valid output:
{"class": "animal", "subclass": "dog", "confidence": "definite", "count": 1, "face_visible": false, "notes": null}

No prose, no markdown, no keys outside "class", "subclass", "confidence",
"count", "face_visible", and "notes".
"""

# Canonical mode name for the response_format dispatch layer (Rule 8a).
# The dispatch dict in infra/vision_analyzer.py MUST include this exact
# string or this prompt is unreachable.
MODE_NAME: str = "vm1_classify"


def build_vm1_prompt() -> str:
    """Return the VM1 text prompt. The schema constraint lives in
    response_format; the text prompt only states the task in natural
    language.

    Per Rule 8 (vision-prompt-design skill): the schema is the layer
    that constrains Qwen. The text is for Qwen to read; the schema is
    for Qwen to comply with. They must agree.
    """
    return PROMPT_TEXT
