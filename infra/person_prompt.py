"""
person_prompt.py — §11.115.4 person call-2 prompt + schema.

STATUS: provisional (Phase §11.115; will stabilize after live telemetry)
THREAD SAFETY: thread-safe (module-level constants; no shared state)

INPUTS:
  - fn `build_person_prompt()` — no IO, no env vars.

OUTPUTS:
  - SCHEMA_JSON: JSON Schema dict for the llama-server strict-mode
    response_format. Mirrors v1 person schema (better_crop, attributes,
    signature, confidence, notable_details).
  - PROMPT_TEXT: instruction text directing the model to describe a person,
    use the CONTEXT_WARDROBE clothing vocabulary (jeans, t-shirt, hoodie,
    etc.), emit null for unobservable values, never invent.
  - build_person_prompt() -> str.

PUBLIC API:
  - SCHEMA_JSON             dict
  - PROMPT_TEXT             str
  - build_person_prompt()   str

DOES NOT DO:
  - Call Qwen. (See infra.vision_analyzer.analyze_frames_queued.)
  - Validate the model response. (Class-specific validators handle that
    in their respective matchers.)
  - Detect faces or do face recognition. (See infra.face_recognition.)
  - Match persons to enrolled identities. (See infra.person_matcher.)

CALLED BY:
  - listener.single_pipeline — call 2 prompt factory for ClassLabel.PERSON.

RELATED:
  - PLAN.md §11.115 — design rationale.
  - infra.person_prompt_template.py — LEGACY module with old schema
    (face_bbox + face_visible). Will be removed in a follow-up commit.
  - infra.classify_prompt — Qwen call 1 (shared classify).
  - infra.face_recognition — runs on the chosen better_crop.

Design notes (Operator 2026-09-02 PM):
  - The TWO crops sent to Qwen are crop_a (from pairwise diff frame_2)
    and crop_b (from pairwise diff frame_3). Same two images used
    everywhere — no YOLO crops, no Qwen bbox crops.
  - Qwen picks which crop shows the face better (better_crop), or
    neither if no face is visible / uncertain.
  - Prompt phrasing intentionally biases toward neither:
    "If a face is visible, which of the two images shows it better?
     Only return crop_a/crop_b if you can clearly see a face.
     Otherwise return 'neither'."
  - Downstream: if better_crop="crop_a" -> recognize_faces(crop_a);
    if better_crop="crop_b" -> recognize_faces(crop_b);
    if better_crop="neither" -> skip face recognition.
"""
from __future__ import annotations

# ============================================================================
# JSON Schema — used by _response_format for server-side enforcement.
# Mirrors v1 person_prompt.py lines 69-103 schema shape.
# ============================================================================
SCHEMA_JSON: dict = {
    "type": "object",
    "properties": {
        "better_crop": {
            "type": "string",
            "enum": ["crop_a", "crop_b", "neither"],
            "description": "Which crop shows the face better. crop_a, crop_b, or neither.",
        },
        "attributes": {
            "type": "object",
            "properties": {
                "clothing_upper": {
                    "type": ["string", "null"],
                    "description": "Upper garment: color + type. null if unobservable.",
                },
                "clothing_lower": {
                    "type": ["string", "null"],
                    "description": "Lower garment: color + type. null if unobservable.",
                },
                "carrying": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "What the person is carrying. Empty array if hands free.",
                },
                "action": {
                    "type": ["string", "null"],
                    "enum": [
                        "walking", "standing", "looking at camera",
                        "knocking", "delivering package", "approaching",
                        "leaving", "other", None,
                    ],
                    "description": "Person's action. null if unobservable.",
                },
                "silhouette": {
                    "type": ["object", "null"],
                    "properties": {
                        "build": {
                            "type": ["string", "null"],
                            "enum": [
                                "slim", "athletic", "average",
                                "stocky", "heavy", None,
                            ],
                            "description": "Body build. null if unobservable.",
                        },
                        "height": {
                            "type": ["string", "null"],
                            "enum": ["short", "medium", "tall", None],
                            "description": "Relative height. null if unobservable.",
                        },
                    },
                    "description": "Body silhouette. null if unobservable.",
                },
                "skin_tone": {
                    "type": ["string", "null"],
                    "enum": ["light", "medium", "olive", "dark", None],
                    "description": "Skin tone of face/hands. null if unobservable.",
                },
                "age_range": {
                    "type": ["string", "null"],
                    "enum": [
                        "child", "young_adult", "middle_aged",
                        "senior", None,
                    ],
                    "description": "Coarse age bucket. null if unobservable.",
                },
                "hair": {
                    "type": ["object", "null"],
                    "properties": {
                        "color": {
                            "type": ["string", "null"],
                            "enum": [
                                "black", "brown", "blonde", "gray",
                                "white", "red", None,
                            ],
                            "description": "Hair color. null if unobservable.",
                        },
                        "length": {
                            "type": ["string", "null"],
                            "enum": [
                                "bald", "shaved", "short", "medium",
                                "long", None,
                            ],
                            "description": "Hair length. null if unobservable.",
                        },
                        "style": {
                            "type": ["string", "null"],
                            "enum": ["straight", "wavy", "curly", None],
                            "description": "Hair style. null if unobservable.",
                        },
                    },
                    "description": "Hair details. null if unobservable.",
                },
                "facial_hair": {
                    "type": ["string", "null"],
                    "enum": [
                        "clean_shaven", "stubble", "beard",
                        "mustache", "goatee", None,
                    ],
                    "description": "Facial hair. null if unobservable.",
                },
                "glasses": {
                    "type": ["string", "null"],
                    "enum": ["none", "prescription", "sunglasses", None],
                    "description": "Glasses type. null if unobservable.",
                },
            },
            "description": "Person attributes. All fields null if unobservable.",
        },
        "signature": {
            "type": "object",
            "properties": {
                "stable": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Distinctive long-term features (tattoos, gait, always-worn items). Empty array if none.",
                },
                "transient": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Short-term state (carrying X, walking with Y). Empty array if none.",
                },
            },
            "description": "Person signature — stable and transient markers.",
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
            "description": "Notable visual details not covered above. 1-3 short observations.",
        },
    },
    "required": [
        "better_crop", "attributes", "signature",
        "confidence", "notable_details",
    ],
    "additionalProperties": False,
}

PROMPT_TEXT: str = """\
You are looking at TWO crops of one moving person, taken fractions of
a second apart. The crops show the same person from slightly different
angles or moments.

Inspect BOTH images before deciding.

Central question — face selection:
  If a face is visible, which of the two images shows it better?
  Return exactly one of:
    "crop_a" — image 1 has a clearer / more frontal face
    "crop_b" — image 2 has a clearer / more frontal face
    "neither" — no face is visible in either image, OR you are uncertain

CRITICAL: Only return `crop_a` or `crop_b` if you can CLEARLY see a
human face. If the face is the back of a head, a silhouette, occluded,
or partially visible — return `neither`. We would rather log a miss
than route the wrong image to the face recognizer.

Then describe the person and scene:

  attributes.clothing_upper    — color + type (e.g. "black hoodie", "white t-shirt")
  attributes.clothing_lower    — color + type (e.g. "blue jeans", "dark pants")
  attributes.carrying          — short noun phrases, [] if hands free
  attributes.action            — single verb from enum
  attributes.silhouette.build  — body type
  attributes.silhouette.height — relative height
  attributes.skin_tone         — face/hand tone, null if unclear
  attributes.age_range         — coarse bucket, null if not determinable
  attributes.hair              — color + length + style
  attributes.facial_hair       — one of the enum values
  attributes.glasses           — none | prescription | sunglasses | null

  signature.stable             — distinctive long-term features (tattoos, gait, always-worn items). [] if none.
  signature.transient          — short-term state (carrying X, walking with Y). [] if none.

  confidence                   — 0.0 (no idea) to 1.0 (certain).
  notable_details              — 1-3 short observations the operator should see.

Respond ONLY with JSON matching this schema:

{
  "better_crop": "crop_a" | "crop_b" | "neither",
  "attributes": {
    "clothing_upper": "color + type string" | null,
    "clothing_lower": "color + type string" | null,
    "carrying": ["item1", "item2"] | [],
    "action": "walking" | "standing" | "looking at camera" | "knocking" | "delivering package" | "approaching" | "leaving" | "other" | null,
    "silhouette": {
      "build": "slim" | "athletic" | "average" | "stocky" | "heavy" | null,
      "height": "short" | "medium" | "tall" | null
    },
    "skin_tone": "light" | "medium" | "olive" | "dark" | null,
    "age_range": "child" | "young_adult" | "middle_aged" | "senior" | null,
    "hair": {
      "color": "black" | "brown" | "blonde" | "gray" | "white" | "red" | null,
      "length": "bald" | "shaved" | "short" | "medium" | "long" | null,
      "style": "straight" | "wavy" | "curly" | null
    },
    "facial_hair": "clean_shaven" | "stubble" | "beard" | "mustache" | "goatee" | null,
    "glasses": "none" | "prescription" | "sunglasses" | null
  },
  "signature": {
    "stable": ["distinctive tattoo on right forearm", "always wears red cap"],
    "transient": ["carrying grocery bags", "walking with phone in hand"]
  },
  "confidence": 0.0-1.0,
  "notable_details": ["detail 1", "detail 2"]
}

Rules:
- Use the most specific clothing terms you can read from the CONTEXT_WARDROBE vocabulary:
  clothing_upper: shirt, jacket, hoodie, sweater, t-shirt, coat, vest, suit
  clothing_lower: pants, jeans, shorts, skirt, dress
- Never invent fields that are not in this schema.
- Use null for anything you cannot observe. Never guess.
- Confidence is a number between 0.0 and 1.0.

When uncertain, default to `neither` and lower the confidence.
Do NOT guess the better crop — wrong choice routes the wrong image
to face recognition.

No prose, no markdown, no keys outside the list above.
"""

# Canonical mode name for the response_format dispatch layer (Rule 8a).
# The dispatch dict in infra/vision_analyzer.py MUST include this exact
# string or this prompt is unreachable.
MODE_NAME: str = "vm2_person"


def build_person_prompt() -> str:
    """Return the §11.115 person call-2 text prompt.

    The schema is the constraint layer (Rule 8); the text describes
    the task. Uses CONTEXT_WARDROBE clothing vocabulary, null for
    unobservable, never invent fields.
    """
    return PROMPT_TEXT
