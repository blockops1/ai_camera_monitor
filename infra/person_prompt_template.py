"""
person_prompt_template.py — Phase.106 person-event prompt + schema.

STATUS: provisional (Phase.106; will stabilize after live telemetry)
THREAD SAFETY: thread-safe (module-level constants; no shared state)

INPUTS:
  - fn `build_person_prompt(camera_name, captured_at, event_hint_block)`
        reads fn args only; no IO, no env vars.

OUTPUTS:
  - The rendered prompt string passed to Qwen3-VL via analyze_frames_queued.
  - PERSON_SCHEMA_JSON: a plain-string JSON literal embedded in the prompt
    so Qwen emits exactly that shape. Includes 6 stable identity
    attributes per person (silhouette, skin_tone, age_range, hair,
    facial_hair, glasses) added in Phase.163 for tier-3 matching.

PUBLIC API:
  - PERSON_PROMPT_TEMPLATE_FORMAT:   the raw template (with `{placeholder}s`)
  - PERSON_SCHEMA_JSON:               the JSON schema literal embedded in the template
  - build_person_prompt(...)          returns the fully-rendered prompt string

DOES NOT DO:
  - Does NOT call Qwen or any model. It only formats the prompt.
  - Does NOT validate the model response. (See infra.vision_response.py
    for schema validation.)
  - Does NOT detect motion or faces. (See infra.motion_detector and
    infra.face_recognition.)
  - Does NOT match persons to enrolled identities. (See
    infra.person_matcher — built in §11.36 step 6.)

CALLED BY:
  - infra.prompt_templates.select_prompt_template (Phase.106) when
    `mode="person"` is selected for a Front Door Outside person event.
  - listener.person_event_pipeline (built §11.36 step 7) when constructing
    the per-alert vision call for the Front Door Outside gatekeeper camera.

CALLS INTO:
  - infra.prompt_templates._build_event_hint_block (reused, not re-defined)

RELATED:
  - infra.prompt_templates.PROMPT_TEMPLATE — legacy single-frame prompt
    (still used for non-person events; PersonEventPipeline calls the new
    PERSON template via mode="person" routing).
  - infra.prompt_templates.VEHICLE_CROP_PROMPT_TEMPLATE — closest cousin;
    single-subject focused ID prompt. The person template follows the same
    shape: one subject, full attribute enumeration.
  - PLAN.md §11.36 — design plan; expanded Qwen attributes,
    face_bbox coordinate space, multi-frame input.
  - data/vehicle_artifacts/ — real Qwen person outputs we validated the
    schema against (face_visible: false on back-of-head shots; free-form
    clothing strings; populated actions).

Design notes (Phase.106, the operator green-lit 2026-08-22):
  - Qwen3-VL is asked for the FULL attribute set per Qwen2.5-VL +
    Qwen3-VL documentation (clothing_upper/lower color normalized to
    enum, clothing_upper/lower type, carrying[], action, face_visible,
    face_bbox). the operator: "fully use this vision model, do everything
    that you recommended."
  - face_bbox is in RESIZED pixel coords of the image Qwen sees (not
    normalized, not original 4K). The downstream ArcFace cropper
    scales it back to original frame size before cropping.
  - carrying[] uses a free-form list of short noun phrases ("red
    backpack", "white grocery bag", "none") — Qwen handles this
    reliably; normalizing later is cheaper than fighting Qwen on
    enum matching for novel objects.
  - persons[] array supports multiple persons per frame; primary_person_index
    points to the most prominent (mirrors vehicle primary_vehicle_index).
  - Both frames sent simultaneously (single multi-image call, no
    intermediate crops). Per Phase.100 multi-crop pattern.
"""


# ============================================================================
# Schema literal — embedded into the prompt so Qwen emits this exact shape.
# ============================================================================
# Phase.106 schema design (mirrors VEHICLE_CROP_PROMPT_TEMPLATE's "report
# every field, return null if unsure" discipline but for persons). Field
# names match the keys the downstream person_matcher + Telegram formatter
# expect; renaming here is a breaking change to those callers.

PERSON_SCHEMA_JSON = """\
{
  "persons": [
    {
      "person_id": "p1",
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
      "face_visible": true | false,
      "face_bbox": [x1, y1, x2, y2] | null,
      "silhouette": {
        "build":  "slim" | "athletic" | "average" | "stocky" | "heavy" | null,
        "height": "short" | "medium" | "tall" | null
      },
      "skin_tone":   "light" | "medium" | "olive" | "dark" | null,
      "age_range":   "child" | "young_adult" | "middle_aged" | "senior" | null,
      "hair": {
        "color":  "black" | "brown" | "blonde" | "gray" | "white" | "red" | null,
        "length": "bald" | "shaved" | "short" | "medium" | "long" | null,
        "style":  "straight" | "wavy" | "curly" | null
      },
      "facial_hair": "clean_shaven" | "stubble" | "beard" | "mustache" | "goatee" | null,
      "glasses":     "none" | "prescription" | "sunglasses" | null
    }
  ],
  "primary_person_index": 0,
  "scene_description": "1-2 sentence description of the scene",
  "confidence": 0.0-1.0,
  "notable_details": ["detail 1", "detail 2"],
  "frame_positions": ["LM4", "LM4", "LM3", "LM3", "LM2"]
}"""


# ============================================================================
# Prompt template — sent to Qwen3-VL for person events.
# ============================================================================
# Mirrors VEHICLE_CROP_PROMPT_TEMPLATE's structure (single-subject focus,
# precise attribute enumeration, explicit null rules) but adapts for:
#   - multi-frame input (2 frames; both sent simultaneously)
#   - person-specific attribute set (clothing upper/lower, carrying, action,
#     face_visible + face_bbox)
#   - persons[] array supporting multiple persons
#   - frame_positions[] for downstream trajectory injection

PERSON_PROMPT_TEMPLATE_FORMAT = (
    'Camera "{camera_name}". Captured at {captured_at}. '
    "Person-event analysis (Phase.106).\n\n"
    "These are TWO frames from the SAME camera, captured "
    "{interval_sec}s apart. Inspect BOTH frames before deciding.\n\n"
    "Identify every person visible across the frames. For each person, "
    "report the FULL attribute set below — color/type normalized to the "
    "enum, or null if not determinable. Do NOT guess.\n\n"
    "Person fields:\n"
    '  person_id            — "p1", "p2", ... stable across the two frames\n'
    "  clothing_upper       — color enum + type enum (shirt/jacket/hoodie/etc.)\n"
    "  clothing_lower       — color enum + type enum (pants/jeans/shorts/etc.)\n"
    '  carrying[]           — short noun phrases (e.g. "red backpack").\n'
    "                         Empty [] if hands free.\n"
    "  action               — single verb describing what the person is doing.\n"
    '                         Use one of the enum values; "other" + notable_details\n'
    "                         if none fit.\n"
    "  face_visible         — true only if a discernible human face is present\n"
    "                         (NOT back of head, NOT silhouette, NOT occluded).\n"
    "  face_bbox            — [x1, y1, x2, y2] PIXEL coords in the image you are\n"
    "                         looking at (top-left origin). Use the SMALLEST box\n"
    "                         that fully contains the face. null if not visible.\n"
    "                         Used by the downstream pipeline to crop a 640x640\n"
    "                         region for face recognition — keep tight.\n\n"
    "Stable identity attributes (Phase.163):\n"
    '  silhouette.build     — "slim" | "athletic" | "average" | "stocky" | "heavy" | null.\n'
    "                         Estimate body type. null if not determinable.\n"
    '  silhouette.height    — "short" | "medium" | "tall" | null.\n'
    '                         Relative to a typical adult; "medium" is the default.\n'
    '  skin_tone            — "light" | "medium" | "olive" | "dark" | null.\n'
    "                         Visible face/hand tone; null if unclear.\n"
    '  age_range            — "child" | "young_adult" | "middle_aged" | "senior" | null.\n'
    "                         Coarse buckets; null if not determinable.\n"
    '  hair.color           — "black" | "brown" | "blonde" | "gray" | "white" | "red" | null.\n'
    '                         Include "gray"/"white" if partially gray.\n'
    '  hair.length          — "bald" | "shaved" | "short" | "medium" | "long" | null.\n'
    '  hair.style           — "straight" | "wavy" | "curly" | null.\n'
    '  facial_hair          — "clean_shaven" | "stubble" | "beard" | "mustache" | "goatee" | null.\n'
    "                         null if unclear (low light, distance, mask).\n"
    '  glasses              — "none" | "prescription" | "sunglasses" | null.\n'
    '                         "none" if visible face without eyewear.\n\n'
    "These attributes are STABLE over months/years — used to identify recurring\n"
    'people even when face crops are poor. Return null (not "unknown") when the\n'
    "attribute cannot be determined; do NOT guess.\n\n"
    "Top-level fields:\n"
    "  primary_person_index — index into persons[] for the MOST prominent person\n"
    "                         (closest, largest in frame, most centered).\n"
    "                         Use 0 if persons[] is non-empty.\n"
    "  scene_description    — 1-2 sentence plain English description of the scene.\n"
    "  confidence           — 0.0-1.0 reflecting overall identification quality.\n"
    '  notable_details      — short strings for anything notable (e.g. "wearing\n'
    '                         sunglasses", "carrying box", "approaching door").\n'
    "  frame_positions      — leave this empty []. The downstream motion detector\n"
    "                         fills it from the pairwise differential; Qwen should\n"
    "                         not try to infer trajectory from these two frames.\n\n"
    "Color guidance:\n"
    '  - Pick the DOMINANT color of the visible garment. If unsure, return "unknown".\n'
    '  - Do NOT return null for color if the garment is visible — return "unknown".\n'
    "  - clothing_lower.color may legitimately be null (dress, robe, occluded\n"
    "    by vehicle/structure). Use null only when nothing is visible.\n\n"
    "Face bbox guidance:\n"
    "  - face_bbox is in YOUR image space (the resized image Qwen sees), NOT\n"
    "    normalized 0-1, NOT original 4K. The downstream pipeline scales this\n"
    "    back to original frame size for cropping — wrong coord space breaks\n"
    "    face recognition entirely.\n"
    "  - Tight box, top-left origin: x1 < x2, y1 < y2. Coordinates outside the\n"
    "    frame are auto-clamped, but stay inside for clean crops.\n\n"
    "Decision rule: if no person is visible, return:\n"
    '  {"persons": [], "primary_person_index": 0, "scene_description": "...",\n'
    '   "confidence": <high>, "notable_details": [...], "frame_positions": []}\n\n'
    "Output ONLY the JSON object matching this schema. No preamble. No markdown fences.\n\n"
    "{event_hint_block}\n\n"
    "Schema:\n"
    "{schema_json}"
)


def build_person_prompt(
    camera_name: str,
    captured_at: str,
    event_hint_block: str = "",
    interval_sec: int = 4,
) -> str:
    """Return the fully-rendered person-event prompt string.

    Mirrors infra.prompt_templates.select_prompt_template's behavior for
    vehicle templates: the caller passes camera_name + captured_at (already
    formatted by select_prompt_template), and we do the str.replace() so the
    schema's literal `{`/`}` doesn't conflict with .format().

    The event_hint_block is built by infra.prompt_templates._build_event_hint_block
    for consistency with vehicle templates.

    interval_sec: gap between frames. Matches the deferred-capture default
    (4s) for the Front Door Outside camera; parameterised so future
    multi-frame bursts can override.
    """
    rendered = PERSON_PROMPT_TEMPLATE_FORMAT
    rendered = rendered.replace("{camera_name}", str(camera_name))
    rendered = rendered.replace("{captured_at}", str(captured_at))
    rendered = rendered.replace("{event_hint_block}", str(event_hint_block))
    rendered = rendered.replace("{interval_sec}", str(interval_sec))
    rendered = rendered.replace("{schema_json}", PERSON_SCHEMA_JSON)
    return rendered


# Re-export the template for callers that want the raw format string
# (e.g. for testing what got substituted).
__all__ = [
    "PERSON_PROMPT_TEMPLATE_FORMAT",
    "PERSON_SCHEMA_JSON",
    "build_person_prompt",
]
