"""
person_prompt.py — §11.115.4 person call-2 prompt + schema.

STATUS: provisional (Phase §11.115; will stabilize after live telemetry)
THREAD SAFETY: thread-safe (module-level constants; no shared state)

INPUTS:
  - fn `build_person_prompt(camera_name, captured_at)`
        reads fn args only; no IO, no env vars.

OUTPUTS:
  - PERSON_SCHEMA_JSON: JSON literal embedded in prompt. Contains
    `better_crop: enum`, `attributes: {...}`, `signature: {...}`.
    NO `face_bbox` (Bug C root cause — bbox hallucination).
    NO `face_visible: bool` (Bug C root cause — false positives).
  - PERSON_PROMPT_TEMPLATE_FORMAT: raw template with `{placeholder}s`.
  - build_person_prompt(camera, captured_at) -> str.

PUBLIC API:
  - PERSON_SCHEMA_JSON             str
  - PERSON_PROMPT_TEMPLATE_FORMAT  str
  - build_person_prompt(...)       str

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
  - infra.face_recognition — runs on the chosen `better_crop`.

Design notes (Note 2026-09-02 PM):
  - The TWO crops sent to Qwen are crop_a (from pairwise diff frame_2)
    and crop_b (from pairwise diff frame_3). Same two images used
    everywhere — no YOLO crops, no Qwen bbox crops.
  - Qwen picks which crop shows the face better (`better_crop`), or
    `neither` if no face is visible / uncertain.
  - Prompt phrasing intentionally biases toward `neither`:
    "If a face is visible, which of the two images shows it better?
     Only return crop_a/crop_b if you can clearly see a face.
     Otherwise return 'neither'."
  - Downstream: if better_crop="crop_a" → recognize_faces(crop_a);
    if better_crop="crop_b" → recognize_faces(crop_b);
    if better_crop="neither" → skip face recognition.
"""
from __future__ import annotations

# ============================================================================
# Schema literal — embedded into the prompt so Qwen emits this exact shape.
# ============================================================================
# Schema fields per §11.115:
#   - better_crop: enum["crop_a" | "crop_b" | "neither"]
#   - attributes: {...}     (kept for tier-3 matching)
#   - signature: {...}      (kept for tier-3 matching)
# Removed:
#   - face_bbox: list[int]  (was Bug C root cause — Qwen hallucinated
#                             boxes that didn't contain a face)
#   - face_visible: bool    (was Bug C root cause — Qwen said True
#                             even when no face was present)
PERSON_SCHEMA_JSON = """\
{
  "better_crop": "crop_a" | "crop_b" | "neither",
  "attributes": {
    "clothing_upper": {
      "color": "black" | "white" | "gray" | "silver" | "red" | "blue" | "green" | "yellow" | "brown" | "orange" | "pink" | "purple" | "other" | "unknown" | null,
      "type": "shirt" | "jacket" | "hoodie" | "sweater" | "t-shirt" | "coat" | "vest" | "suit" | null
    },
    "clothing_lower": {
      "color": "black" | "white" | "gray" | "silver" | "red" | "blue" | "green" | "yellow" | "brown" | "orange" | "pink" | "purple" | "other" | "unknown" | null,
      "type": "pants" | "jeans" | "shorts" | "skirt" | "dress" | null
    },
    "carrying": ["red backpack", "white grocery bag"] | [],
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
    "stable": ["distinctive tattoo on right forearm", "always wears red cap"] | [],
    "transient": ["carrying grocery bags", "walking with phone in hand"] | []
  },
  "confidence": 0.0-1.0,
  "notable_details": ["detail 1", "detail 2"]
}"""


# ============================================================================
# Prompt template — sent to Qwen3-VL for person events.
# ============================================================================
# The prompt's central question is: which of the two crops shows the face
# better (or neither). Note's verbatim phrasing drives the bias toward
# `neither` to fix the Bug C false-positive pattern.
PERSON_PROMPT_TEMPLATE_FORMAT = (
    'Camera "{camera_name}". Captured at {captured_at}. '
    "Person call 2 (Phase §11.115).\n\n"
    "You are seeing TWO images from the SAME camera, captured a few seconds "
    "apart. Inspect BOTH before deciding.\n\n"
    "Central question — face selection:\n"
    "  If a face is visible, which of the two images shows it better?\n"
    "  Return exactly one of:\n"
    '    "crop_a" — image 1 has a clearer / more frontal face\n'
    '    "crop_b" — image 2 has a clearer / more frontal face\n'
    '    "neither" — no face is visible in either image, OR you are uncertain\n\n'
    "CRITICAL: Only return `crop_a` or `crop_b` if you can CLEARLY see a "
    "human face. If the face is the back of a head, a silhouette, occluded, "
    "or partially visible — return `neither`. We would rather log a miss "
    "than route the wrong image to the face recognizer.\n\n"
    "Then describe the person and scene:\n\n"
    "  attributes.clothing_upper    — color enum + type enum\n"
    "  attributes.clothing_lower    — color enum + type enum\n"
    '  attributes.carrying          — short noun phrases, [] if hands free\n'
    "  attributes.action            — single verb from enum\n"
    "  attributes.silhouette.build  — body type\n"
    "  attributes.silhouette.height — relative height\n"
    "  attributes.skin_tone         — face/hand tone, null if unclear\n"
    "  attributes.age_range         — coarse bucket, null if not determinable\n"
    "  attributes.hair              — color + length + style\n"
    "  attributes.facial_hair       — one of the enum values\n"
    "  attributes.glasses           — none | prescription | sunglasses | null\n\n"
    "  signature.stable             — distinctive long-term features "
    "(tattoos, gait, always-worn items). [] if none.\n"
    "  signature.transient          — short-term state (carrying X, "
    "walking with Y). [] if none.\n\n"
    "  confidence                   — 0.0 (no idea) to 1.0 (certain).\n"
    "  notable_details              — 1-3 short observations the operator "
    "should see (e.g. 'wearing reflective vest', 'looking at phone').\n\n"
    "Output (return EXACTLY this JSON shape, nothing else):\n"
    "{schema}\n\n"
    "When uncertain, default to `neither` and lower the confidence. "
    "Do NOT guess the better crop — wrong choice routes the wrong image "
    "to face recognition.\n"
)


def build_person_prompt(camera_name: str, captured_at: str) -> str:
    """Render the §11.115 person call-2 prompt.

    Args:
        camera_name: friendly camera name (e.g. "Front Porch") for context.
        captured_at: ISO-8601 timestamp for the captured frames.

    Returns:
        Fully-rendered prompt string ready to send to Qwen3-VL.
    """
    return PERSON_PROMPT_TEMPLATE_FORMAT.format(
        camera_name=camera_name,
        captured_at=captured_at,
        schema=PERSON_SCHEMA_JSON,
    )
