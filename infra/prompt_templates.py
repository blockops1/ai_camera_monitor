"""
prompt_templates.py — Qwen3-VL prompt text + JSON schemas + dispatcher + builder.

STATUS: stable
THREAD SAFETY: thread-safe (module-level constants are read-only)

INPUTS:
    - function args: camera_name, event_hint, captured_at, n_frames,
      interval_sec, frame_paths (paths to JPEG files)

OUTPUTS:
    - return value (select_prompt_template): str — fully rendered prompt text
    - return value (_build_messages): list[dict] — OpenAI-compatible
      multi-modal messages (text + base64 images)
    - return value (_build_event_hint_block): str — formatted trigger block

PUBLIC API:
    PROMPT_TEMPLATE, VEHICLE_STATIC_PROMPT_TEMPLATE, VEHICLE_CROP_PROMPT_TEMPLATE
        Raw prompt templates with {placeholder} syntax. Use select_prompt_template()
        for the rendered version.
    VISION_SCHEMA_JSON, VEHICLE_CROP_SCHEMA_JSON, VEHICLE_CLASSIFY_SCHEMA
        JSON schemas for llama-server response_format. Tightly coupled to
        their corresponding prompt templates (the prompt must describe
        every required field). Owned here together with the templates.
    select_prompt_template(event_hint, n_frames, interval_sec=4, camera_name="",
                           event_hint_block="", captured_at=None, mode="auto") -> str
        Pick the prompt template based on event_hint + n_frames + mode, render
        placeholders, return the final prompt string.
    _build_event_hint_block(event_hint: str | None) -> str
        Format the camera's on-device AI classification (vehicle / person /
        motion / animal) into a 1-3 sentence block that surfaces the trigger
        type to Qwen.
    _build_messages(frame_paths, camera_name, event_hint=None, captured_at=None,
                    mode=None) -> list[dict]
        Build the OpenAI-compatible multi-modal message list: text prompt
        + one base64-encoded image per frame (native resolution, no downscaling).

DOES NOT DO:
    - HTTP transport — infra.vision_client owns that
    - Parse / validate responses — infra.vision_response owns that
    - Orchestrate retries — infra.vision_analyzer owns that
    - Decide what camera / event_hint means at the listener level
    - Decide motion — listener.motion_gate_pipeline owns that
      (Phase.115, refactored out of infra.motion_detector; the
      dataclasses live in infra.motion_types since Phase §11.90).
      This module asks Qwen to describe vehicles, not to judge whether
      they are moving. Motion is the differential's job.

WHY HERE:
    Every vision call sends a payload to Qwen3-VL. The payload has two
    sides that change together:
      1. The prompt text (what we ask Qwen to do)
      2. The JSON schema (what shape Qwen must return)
    These two MUST be edited together — adding a field to the schema
    without updating the prompt produces empty/missing responses (the
    6B.74 schema-dispatch bug was exactly this failure mode). Keeping
    them in one module prevents the two sides from drifting.

    The 3 prompt templates (legacy + 2 vehicle variants) each have a
    use case:
      - PROMPT_TEMPLATE: legacy, non-vehicle event webhooks
      - VEHICLE_STATIC_PROMPT_TEMPLATE: 6-frame burst, no motion framing
      - VEHICLE_CROP_PROMPT_TEMPLATE: tight bbox crop, ID-only

    Phase.78 (2026-08-14) — removed VEHICLE_MOTION_PROMPT_TEMPLATE
    and VEHICLE_COMBINED_PROMPT_TEMPLATE. Both asked Qwen for motion
    judgments (motion / motion_justification / motion_vector /
    vehicle_motion / moving_vehicle_indices) that the differential
    already owns. Asking both systems produced contradictions.

CALLED BY:
    - infra.vision_analyzer.analyze_frames → _build_messages → select_prompt_template
    - infra.vision_analyzer.classify_vehicle_crop → uses VEHICLE_CLASSIFY_PROMPT inline
    - listener.listener /status route → lazy-imports VEHICLE_*_PROMPT_TEMPLATE for
      prompt-mode reporting

CALLS INTO:
    - (Phase.131) no downscale: frames are sent at native resolution
    - (no env vars — Phase.78 removed FARMSURV_COMBINED_PROMPT)

RELATED:
    - data/vision_prompts/ — the prompt templates (if persisted; currently inline)
    - infra/vision_analyzer.py — orchestrator that calls into this module
"""

import base64
import logging
from typing import Any

log = logging.getLogger(__name__)


# ===========================================================================
# Legacy single-frame prompt (Phase 6A / pre-vehicle)
# ===========================================================================

PROMPT_TEMPLATE = (
    'You are a security camera analyst. Analyze these frames from camera "{camera_name}".'
    '{event_hint_block}\n\n'
    "Inspect the frames FIRST, then write your JSON.\n"
    "Step 1: Identify every visible vehicle (car, truck, SUV, trailer, etc.) and its dominant exterior color.\n"
    "Step 2: Write scene_description (2-3 sentences) describing the scene and any notable activity.\n"
    "Step 3: Populate every structured field below from those same observations.\n"
    "Step 4: Silently verify — every color/vehicle you mention in scene_description must agree with colors.vehicle and objects_detected.\n\n"
    "Output EXACTLY ONE JSON object matching the schema. No preamble. No markdown fences.\n\n"
    "{{\n"
    '  "objects_detected": ["list of object labels"],\n'
    '  "primary_subject": "main subject or \\"none\\"",\n'
    '  "actions": ["action 1", "action 2"],\n'
    '  "scene_description": "2-3 sentence description of the scene",\n'
    '  "confidence": 0.0-1.0,\n'
    '  "notable_details": ["detail 1", "detail 2"],\n'
    '  "colors": {{\n'
    '    "vehicle": "black" | "white" | "gray" | "silver" | "red" | "blue" | "green" | "yellow" | "brown" | "orange" | "other" | "unknown" | "none",\n'
    '    "clothing_primary": "blue shirt" | "red jacket" | "black hoodie" | null,\n'
    '    "clothing_secondary": null,\n'
    '    "other": "any other prominent colors, e.g. bright red backpack" | null\n'
    "  }},\n"
    '  "species": "dog" | "cat" | "deer" | "bear" | "coyote" | "fox" | "raccoon" | "bird" | null,\n'
    '  "vehicles": [\n'
    '    {{\n'
    '      "color": "black" | "white" | "gray" | "silver" | "red" | "blue" | "green" | "yellow" | "brown" | "orange" | "other" | "unknown" | "none",\n'
    '      "body_style_hint": "pickup" | "sedan" | "suv" | "van" | "hatchback" | "coupe" | "trailer" | "tractor" | "motorcycle" | "truck (commercial)" | null,\n'
    '      "bbox": [x1, y1, x2, y2] | null\n'
    '    }}, ...\n'
    '  ],\n'
    '  "primary_vehicle_index": 0,\n'
    '  "face_visibility": {{\n'
    '    "any_face_visible": true | false,\n'
    '    "best_frame_index": 1 | 2 | 3,\n'
    '    "best_frame_face_fraction": 0.0-1.0,\n'
    '    "front_facing": true | false,\n'
    '    "per_frame": [\n'
    '      {{"index": 1, "face_fraction": 0.0-1.0, "front_facing": true | false, "bbox": [x1, y1, x2, y2] | null}},\n'
    '      {{"index": 2, "face_fraction": 0.0-1.0, "front_facing": true | false, "bbox": [x1, y1, x2, y2] | null}},\n'
    '      {{"index": 3, "face_fraction": 0.0-1.0, "front_facing": true | false, "bbox": [x1, y1, x2, y2] | null}}\n'
    '    ],\n'
    '    "notes": "short description of why a face is or is not usable, e.g. \'back turned\', \'too distant\', \'looking down\', \'wearing sunglasses\' | null"\n'
    "}}\n\n"
    "Color guidance:\n"
    '- vehicle: dominant exterior color of any vehicle visible (car, truck, SUV). Use "none" if no vehicle visible. Use "unknown" if a vehicle is visible but its color cannot be determined. Never leave this field null or absent.\n'
    '- clothing_primary: most visible clothing item on the primary person (e.g. "blue shirt", "black hoodie", "red jacket"). null if no person or clothing not visible.\n'
    '- clothing_secondary: secondary clothing item if distinct (e.g. "khaki pants" if shirt is primary). null if not applicable.\n'
    '- other: any other prominent color detail (e.g. "bright red backpack", "yellow caution tape"). null if none.\n\n'
    "Species guidance:\n"
    "- Identify the specific animal if visible: domestic pets (dog, cat) vs wildlife (deer, bear, coyote, fox, raccoon, bird).\n"
    '- null if no animal visible. Use "unknown animal" only if clearly an animal but species cannot be determined.\n\n'
    "Face visibility guidance:\n"
    "- any_face_visible: true only if ANY frame contains a discernible human face (not back of head); false if all frames show back/side/hats/occlusion.\n"
    "- best_frame_index: 1, 2, or 3 — which of the input frames (in their original order) has the LARGEST and MOST FRONT-FACING face. Used by the downstream pipeline to decide which frame to run face detection on first. If no face is visible in any frame, set this to 1.\n"
    "- best_frame_face_fraction: estimated area of the largest visible face in the BEST frame, divided by that frame's area (0.0–1.0). Example: a face taking ~5% of the best frame → 0.05. Faces under ~1% are too small to embed reliably.\n"
    "- front_facing: true if the face in best_frame_index is roughly facing the camera (frontal, eye-line-of-sight); false for back-of-head, side profile, looking down, occluded.\n"
    "- per_frame: array with one entry per input frame, in input order. face_fraction is the largest visible face in that specific frame (0 if no face visible). front_facing per-frame. bbox is the bounding box of the largest visible face in that frame as [x1, y1, x2, y2] in PIXEL coords of the NATIVE-RESOLUTION image you're looking at (top-left origin); null if no face visible. The downstream pipeline uses this bbox to crop a 640x640 region from the same high-res frame for face recognition, so coordinates must be in your image space."
    "- notes: short phrase explaining why a face is or is not usable (e.g. 'back turned', 'side profile', 'too distant', 'wearing sunglasses'). null if face is clearly visible and front-facing.\n"
    ")\n"
    "\n"
    "Vehicles guidance (Phase.6):\n"
    "- vehicles: array — one entry per distinct vehicle visible across all frames. Detect EVERY vehicle, including ones partially occluded or distant.\n"
    "- For each vehicle: report color (or \"unknown\" if not determinable, \"none\" if slot is reserved but no vehicle), body_style_hint, and bbox.\n"
    "- body_style_hint: just the SHAPE — pickup/sedan/suv/van/hatchback/coupe/trailer/tractor/motorcycle/'truck (commercial)'. null if shape is ambiguous. Do NOT report make or model here — a separate focused pass handles that.\n"
    "- bbox: [x1, y1, x2, y2] in NORMALIZED coords (0-1) of the NATIVE-RESOLUTION image you're looking at. Top-left origin. Use the SMALLEST box that fully contains the vehicle (tight around visible body, not loosely around any splash/spillover). null if not determinable.\n"
    "- primary_vehicle_index: index into vehicles[] for the MOST prominent vehicle — closest, largest in frame, or most centered. Use 0 if vehicles[] is non-empty. (The 0 default for empty list matches the JSON schema.)\n"
    # Phase.78 (2026-08-14) — removed vehicle_motion instruction from
    # the legacy prompt. Motion is owned by the motion gate
    # (infra/frame_diff + listener/motion_gate_pipeline; Phase.115); the
    # parser does not enforce this instruction any more (the schema
    # does not require the field). Qwen describing motion in prose
    # is fine; we don't ask for it as a structured field.
    "- Backward-compat: also populate colors.vehicle with the dominant vehicle's color so the legacy single-color path still works. The downstream extractor now reads from vehicles[] first and only falls back to colors.vehicle if vehicles[] is empty.\n"
)


# ===========================================================================
# Vehicle-motion-aware multi-frame prompt (Phase.13 + 6B.19 + 6B.48)
# ===========================================================================
# Phase.78 (2026-08-14) — removed VEHICLE_MOTION_PROMPT_TEMPLATE.
# Motion is owned by the motion gate (pairwise differential via
# infra/frame_diff + listener/motion_gate_pipeline; Phase.115).
# infra.motion_detector is a dataclass shim since Phase §11.90.
# Asking Qwen to make the motion judgment produced hallucinations:
# it returned motion_state="PARKED" for trucks the differential had
# just tracked across 6 frames. The schema's vehicle_motion /
# motion / moving_vehicle_indices fields are stripped in the same
# commit. See PLAN.md §11.9.
# ===========================================================================

VEHICLE_STATIC_PROMPT_TEMPLATE = (
    'Camera "{camera_name}". Captured at {captured_at}.\n\n'
    "This is a single-frame vehicle analysis (no motion signal).\n\n"
    "List EVERY distinct vehicle visible in the frame — parked, partially "
    "occluded, distant, all of them. For each: color, body_style_hint "
    "(pickup|sedan|suv|van|hatchback|null), make, model, vehicle_features "
    "{ wheel_style, roofline_style, front_grille_style, headlight_signature, "
    "rear_lights_signature, tailgate_type, badge_text_readable, "
    "window_tint (none|light|dark|factory_privacy|null), "
    "cab_marker_lights (true|false|null), bed_cover (none|tonneau|camper_shell|"
    "topper|null) } (string or null).\n\n"
    "Decision rule: decide based on what you SEE. A parked truck is still a "
    "vehicle and must be enumerated. Do NOT filter by motion — there is no "
    "motion signal in a single frame.\n\n"
    "Top-level fields: vehicles[], primary_subject, scene_description "
    "(1 sentence), confidence, notable_details (no invented dates), "
    "colors, species, primary_vehicle_index, best_frame_index (1), "
    "face_visibility.\n\n"
    "Use captured_at for any date references. vehicles=[] is only valid if "
    "no vehicle is visible.\n\n"
    "{event_hint_block}"
)


# ===========================================================================
# §11.115.22 — Diff-aware vehicle prompt (3-image payload)
#
# Phase §11.115.22 (2026-09-03). Note's diagnosis: one moving vehicle
# past two parked vehicles caused Qwen to enumerate all 3 in the static
# template because it had no way to disambiguate the moving subject.
# Fix: when the cascade payload is [pairwise_diff, crop_a, crop_b], use
# this template. It names the three images and instructs Qwen to pick
# the MOVING subject from the diff signal — the legacy "list every
# vehicle" rule is wrong here because the motion signal IS authoritative.
#
# Vehicles[] JSON shape is preserved so downstream (matcher, telegram)
# is unchanged. Only the per-vehicle identification scope narrows: the
# moving subject gets full make/model/features; parked vehicles may
# appear as one-line entries with color only (or omitted if you are
# confident the diff shows a clear single subject).
#
# Selected by select_prompt_template when n_frames >= 3 and event_hint
# == "vehicle" (auto-dispatch for §11.115.22 cascade payloads).
# ===========================================================================
VEHICLE_DIFF_STATIC_PROMPT_TEMPLATE = (
    'Camera "{camera_name}". Captured at {captured_at}.\n\n'
    "You are receiving THREE images describing one motion event:\n\n"
    "  1. STREAK CROP A — frame_3 cropped at the camera's motion bbox "
    "(the region where motion was detected between frames 2 and 3).\n"
    "  2. STREAK CROP B — frame_4 cropped at the camera's motion bbox "
    "(motion between frames 3 and 4).\n"
    "  3. PAIRWISE DIFFERENTIAL — abs(frame_3 − frame_4), brightened, "
    "with a green rectangle showing the diff(2,3) bbox and a cyan "
    "rectangle showing the diff(3,4) bbox. Where pixels are bright, "
    "motion happened. Where pixels are dark, the scene is stationary.\n\n"
    "The MOVING subject is whatever lights up the diff. Stationary "
    "vehicles in the same frame stay dark in the diff and are NOT the "
    "moving subject — ignore them when identifying the primary vehicle. "
    "If the moving subject is a tractor, riding mower, ATV, or other "
    "non-passenger equipment, say so explicitly with "
    "body_style_hint='tractor' or 'motorcycle' and make/model=null.\n\n"
    "Return a JSON object with a `vehicles` array:\n"
    "  - ONE entry for the moving subject with full identification: "
    "color, body_style_hint "
    "(pickup|sedan|suv|van|hatchback|coupe|trailer|tractor|motorcycle|'truck (commercial)'|null), "
    "make, model, vehicle_features, description, confidence.\n"
    "  - Parked vehicles that are clearly visible AND close to the "
    "moving subject MAY be included with color only (no make/model "
    "needed) so the matcher knows they exist — but the moving subject "
    "is always primary_vehicle_index=0.\n\n"
    "Top-level fields:\n"
    "  vehicles: array (the moving subject is index 0 — REQUIRED, may "
    "have 1 entry if no other vehicles are visible)\n"
    "  primary_vehicle_index: int (always 0 here — the moving subject "
    "is index 0 by construction)\n"
    "  scene_description: 1-2 sentence free-text description naming "
    "the moving subject and any nearby parked vehicles.\n\n"
    "Rules:\n"
    "- The diff image is the source of truth for which object is "
    "moving. Pick the object whose pixels are bright in the diff.\n"
    "- Inspect badges/lettering/grille shape/taillights FIRST before "
    "guessing make/model.\n"
    "- If you can't tell make or model, return null — do NOT guess.\n"
    "- Pickups: F-150/F-250/F-350 vs Silverado 1500/2500 vs Ram "
    "1500/2500 vs Tundra vs Tacoma vs Frontier vs Colorado. Make "
    "before model.\n"
    "- Tractors: body_style_hint='tractor', make/model if lettering is "
    "visible (Kubota, John Deere, Massey Ferguson, Yanmar, etc.). If "
    "no lettering, set make=null and model=null but still include "
    "the entry — tractors are common farm vehicles and the matcher "
    "treats them as separate from cars/SUVs.\n"
    "- vehicle_features are the most valuable fields — they let us "
    "match this vehicle from a different camera angle next time. Read "
    "each one.\n"
    "- Confidence reflects make/model only.\n"
    "- Output ONLY the JSON object. No preamble. No markdown fences.\n"
    "{event_hint_block}"
)


# ===========================================================================
# Crop-only ID prompt (Phase.66)
# ===========================================================================

VEHICLE_CROP_PROMPT_TEMPLATE = (
    'Camera "{camera_name}". Cropped bbox of the detection zone '
    "(subject is the bbox-centered mover; the crop may also include "
    "adjacent vehicles — identify EVERY distinct vehicle you can see). "
    "Event captured at {captured_at}.\n\n"
    "Return a JSON object with a `vehicles` array — one entry per "
    "distinct vehicle visible in this crop. For EACH vehicle, report "
    "the full identification (not just for the dominant one):\n"
    "  color (black|white|gray|silver|red|blue|green|yellow|brown|orange|other|unknown)\n"
    "  body_style_hint (pickup|sedan|suv|van|hatchback|coupe|trailer|tractor|motorcycle|'truck (commercial)'|null)\n"
    "  make (Ford|Chevrolet|Tesla|Toyota|Honda|Ram|GMC|Jeep|Nissan|Subaru|...|null)\n"
    "  model (F-150|Silverado 1500|Model Y|RAV4|...|null)\n"
    "  vehicle_features {\n"
    "    wheel_style, wheel_arch, wheel_color, roofline_style,\n"
    "    front_grille_style, headlight_signature, rear_lights_signature,\n"
    "    tailgate_type, badge_text_readable,\n"
    "    window_tint (none|light|dark|factory_privacy|null),\n"
    '    cab_marker_lights (true|false|"false"|null),\n'
    "    bed_cover (none|tonneau|camper_shell|topper|null)\n"
    "  } (string or null for each feature)\n"
    # Phase.77 (2026-08-11) — free-text description field. Qwen
    # already produces natural-language descriptions when asked; this
    # makes them part of the structured response so the Motion
    # Telegram can render Qwen's exact identification verbatim
    # instead of relying on the matcher's label. Verbatim: no
    # summarization, no curation.
    "  description (1-2 sentence free-text identification of the\n"
    "    vehicle in plain English — name what you see: color, body\n"
    "    type, make, model, and any distinguishing features like\n"
    "    grille shape, wheels, headlights, badge, roofline)\n"
    "  confidence (0.0-1.0)\n\n"
    "Top-level fields:\n"
    "  vehicles: array (one entry per visible vehicle — REQUIRED, may "
    "    have 1 entry if only the subject is visible)\n"
    "  primary_vehicle_index: int (0-based index into vehicles[] for "
    "    the dominant/subject vehicle — the one the gate's bbox "
    "    centered on. Use 0 if only one vehicle.)\n"
    "  scene_description: 1-2 sentence free-text description of the "
    "    crop's overall scene (where vehicles are, what's happening).\n\n"
    "Rules:\n"
    "- Inspect badges/lettering/grille shape/taillights FIRST before "
    "guessing make/model.\n"
    "- If you can't tell make or model, return null — do NOT guess.\n"
    "- Pickups: F-150/F-250/F-350 vs Silverado 1500/2500 vs Ram 1500/2500 "
    "vs Tundra vs Tacoma vs Frontier vs Colorado. Make before model.\n"
    "- Tractors: list as `body_style_hint=tractor`, make/model if "
    "lettering is visible (Kubota, John Deere, Massey Ferguson, etc.). "
    "If no lettering is visible, set make=null and model=null but still "
    "include the entry — tractors are common farm vehicles and the "
    "matcher treats them as separate vehicles from cars/SUVs.\n"
    "- vehicle_features are the most valuable fields — they let us match "
    "this vehicle from a different camera angle next time. Read each one.\n"
    "- Confidence reflects make/model only.\n"
    "- Output ONLY the JSON object. No preamble. No markdown fences.\n"
    "{event_hint_block}"
)


# ===========================================================================
# Phase.78 (2026-08-14) — removed VEHICLE_COMBINED_PROMPT_TEMPLATE.
# Same rationale as VEHICLE_MOTION_PROMPT_TEMPLATE: motion is owned by
# Motion is owned by the motion gate (infra/frame_diff +
# listener/motion_gate_pipeline; Phase.115). infra.motion_detector
# is a dataclass shim since Phase §11.90.
# motion_vector / moving_vehicle_indices / vehicle_motion fields. The
# schema strip and the prompt deletion land together. See PLAN.md §11.9.
# ===========================================================================

# ===========================================================================
# Focused crop classifier prompt (Phase.6)
# ===========================================================================

VEHICLE_CLASSIFY_PROMPT = """\
Identify the vehicle in this image. Be precise but concise.

Return JSON:
{{
  "make": "Ford" | "Chevrolet" | "Tesla" | "Toyota" | "Honda" | "Ram" | "GMC" | "Jeep" | "Nissan" | "Subaru" | ... | null,
  "model": "F-150" | "Model 3" | "Silverado 1500" | "RAV4" | ... | null,
  "body_style": "pickup" | "sedan" | "suv" | "van" | "hatchback" | "coupe" | "trailer" | "tractor" | "motorcycle" | "truck (commercial)" | null,
  "trim_level": "XLT" | "Lariat" | "Long Range" | "Limited" | ... | null,
  "year_range": "2018-2023" | "2020s" | ... | null,
  "visible_modifications": "aftermarket wheels and 2-inch lift" | ... | null,
  "distinctive_features": "dented rear quarter panel, bumper sticker of [brand]" | ... | null,
  "confidence": 0.0-1.0
}}

Rules:
- Inspect the image FIRST, look at badges/lettering/grille shape/taillights.
- If you can't tell make or model, return null for that field. Do NOT guess.
- For pickups: F-150/F-250/F-350 vs Silverado 1500/2500 vs Ram 1500/2500 vs Tundra vs Tacoma vs Frontier vs Colorado. Make before model.
- For sedans: Model 3 / Camry / Accord / Civic / Corolla / F-150 Lightning.
- Distinctive_features is the most valuable field — it's what helps us match THIS truck from a different camera angle next time.
- Confidence should reflect make/model only, not body_style.
- Output ONLY the JSON object. No preamble. No markdown.
"""


# ===========================================================================
# JSON schemas (tightly coupled to prompts — owned together)
# ===========================================================================

# JSON Schema passed to llama-server via response_format.json_schema.
# Forces the model to populate colors.vehicle (no null ambiguity) and to
# emit every required field. Schema is not injected into the prompt, so
# the prompt must still describe every field in plain language.
# Verified working on llama.cpp 9960 against Qwen3-VL-8B-Instruct 2026-07-20.
# Phase.74 (2026-08-10) — VEHICLE_CROP_SCHEMA_JSON.
# Matches the VEHICLE_CROP_PROMPT_TEMPLATE output shape (flat, NOT
# nested in vehicles[]). The crop prompt asks Qwen for ONE vehicle
# with make/model/vehicle_features at the TOP level, but analyze_frames
# was historically hardcoded to use VISION_SCHEMA_JSON (the legacy
# multi-frame schema) for ALL modes. With additionalProperties=false
# and the legacy schema's required field list (objects_detected,
# primary_subject, actions, scene_description, ...), Qwen was
# silently dropping make/model/vehicle_features from the response
# even though the prompt asked for them — see Pitfall #29 + 6B.66.
# This schema gives Qwen permission to return the rich identification
# fields the crop prompt needs. Top-level fallback path in
# extract_signature() (vehicle_state.py:241) already handles this shape.
VEHICLE_CROP_SCHEMA_JSON: dict[str, Any] = {
    "type": "object",
    "properties": {
        "vehicles": {
            "type": "array",
            "description": (
                "One entry per distinct vehicle visible in the crop. "
                "Always non-empty when the gate emitted a vehicle bbox. "
                "May contain a single entry if only the subject vehicle "
                "is visible (the common case for tight crops). Multi-"
                "vehicle entries (2+) when the bbox crop captures an "
                "adjacent vehicle too \u2014 see Phase.129b \u00a711.52."
            ),
            "items": {
                "type": "object",
                "properties": {
                    "color": {
                        "type": "string",
                        "enum": [
                            "black", "white", "gray", "silver", "red",
                            "blue", "green", "yellow", "brown", "orange",
                            "other", "unknown", "none",
                        ],
                    },
                    "body_style_hint": {
                        "type": ["string", "null"],
                        "description": "pickup|sedan|suv|van|hatchback|coupe|trailer|tractor|motorcycle|'truck (commercial)'|null",
                    },
                    "make": {
                        "type": ["string", "null"],
                        "description": "Ford|Chevrolet|Tesla|Toyota|Honda|Ram|GMC|Jeep|Nissan|Subaru|...|null",
                    },
                    "model": {
                        "type": ["string", "null"],
                        "description": "F-150|Silverado 1500|Model Y|RAV4|...|null",
                    },
                    "vehicle_features": {
                        "type": "object",
                        "properties": {
                            "wheel_style": {"type": ["string", "null"]},
                            "wheel_arch": {"type": ["string", "null"]},
                            "wheel_color": {"type": ["string", "null"]},
                            "roofline_style": {"type": ["string", "null"]},
                            "front_grille_style": {"type": ["string", "null"]},
                            "headlight_signature": {"type": ["string", "null"]},
                            "rear_lights_signature": {"type": ["string", "null"]},
                            "tailgate_type": {"type": ["string", "null"]},
                            "badge_text_readable": {"type": ["string", "null"]},
                            "window_tint": {"type": ["string", "null"]},
                            "cab_marker_lights": {
                                "type": ["boolean", "string", "null"],
                                "description": "true|false|'false'|null \u2014 accept string form because Qwen has been returning \"false\" consistently",
                            },
                            "bed_cover": {"type": ["string", "null"]},
                        },
                        "required": [
                            "wheel_style", "wheel_arch", "wheel_color",
                            "roofline_style", "front_grille_style",
                            "headlight_signature", "rear_lights_signature",
                            "tailgate_type", "badge_text_readable",
                            "window_tint", "cab_marker_lights", "bed_cover",
                        ],
                        "additionalProperties": False,
                    },
                    "description": {"type": ["string", "null"]},
                    "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                },
                "required": [
                    "color", "body_style_hint", "make", "model",
                    "vehicle_features", "description", "confidence",
                ],
                "additionalProperties": False,
            },
        },
        "primary_vehicle_index": {
            "type": "integer",
            "minimum": 0,
            "description": (
                "0-based index into vehicles[] for the dominant/subject "
                "vehicle \u2014 the one the gate's bbox centered on. Default "
                "0 when vehicles[] has exactly one entry."
            ),
        },
        "scene_description": {
            "type": ["string", "null"],
            "description": (
                "Free-text 1-2 sentence description of the crop's "
                "overall scene (where vehicles are, what's happening). "
                "Mirrors the per-vehicle description field but at scene "
                "level \u2014 useful for the alert body when vehicles[] is "
                "non-empty."
            ),
        },
        # Backward-compat fields. Populated by infra.vision_response
        # from vehicles[primary_vehicle_index] so legacy consumers
        # (e.g. slim match_stage _extract_signature) keep working.
        # Phase.129b \u00a711.52 \u2014 these are no longer emitted by Qwen;
        # the parser copies them from vehicles[primary_vehicle_index].
        "color": {"type": ["string", "null"]},
        "body_style_hint": {"type": ["string", "null"]},
        "make": {"type": ["string", "null"]},
        "model": {"type": ["string", "null"]},
        "vehicle_features": {"type": ["object", "null"]},
        "description": {"type": ["string", "null"]},
        "confidence": {"type": ["number", "null"]},
        "type": {"type": ["string", "null"]},
    },
    "required": [
        "vehicles", "primary_vehicle_index", "scene_description",
        # Backward-compat fields required so legacy consumers don't break.
        "color", "body_style_hint", "make", "model",
        "vehicle_features", "description", "confidence", "type",
    ],
    "additionalProperties": False,
}


VISION_SCHEMA_JSON: dict[str, Any] = {
    "type": "object",
    "properties": {
        "objects_detected": {"type": "array", "items": {"type": "string"}},
        "primary_subject": {"type": "string"},
        "actions": {"type": "array", "items": {"type": "string"}},
        "scene_description": {"type": "string"},
        "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
        "notable_details": {"type": "array", "items": {"type": "string"}},
        "colors": {
            "type": "object",
            "properties": {
                "vehicle": {
                    "type": "string",
                    "enum": [
                        "black", "white", "gray", "silver", "red", "blue",
                        "green", "yellow", "brown", "orange", "other",
                        "unknown", "none",
                    ],
                },
                "clothing_primary": {"type": ["string", "null"]},
                "clothing_secondary": {"type": ["string", "null"]},
                "other": {"type": ["string", "null"]},
            },
            "required": ["vehicle", "clothing_primary", "clothing_secondary", "other"],
            "additionalProperties": False,
        },
        "species": {"type": ["string", "null"]},
        # Phase.6 — per-vehicle detection list with bbox. The first
        # pass captures vehicles[] + bboxes cheaply (don't ask for make/
        # model here — accuracy is poor when the prompt is also trying to
        # detect people/animals/colors). A second focused pass on each
        # cropped vehicle bbox does the make/model/body_style work.
        # Cameras see multiple vehicles at once (F150 + trailer, visitor
        # parked behind name four's, etc.). We capture ALL of them so the
        # tracker can break color-collision ties (white pickup vs another
        # white pickup). primary_vehicle_index picks the most prominent
        # for the vehicle tracker's first attempt.
        "vehicles": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "color": {
                        "type": "string",
                        "enum": [
                            "black", "white", "gray", "silver", "red", "blue",
                            "green", "yellow", "brown", "orange", "other",
                            "unknown", "none",
                        ],
                    },
                    "body_style_hint": {
                        "type": ["string", "null"],
                        "description": "Coarse body style: pickup|sedan|suv|van|hatchback|coupe|trailer|tractor|motorcycle|truck (commercial)|null. null if not determinable from first pass.",
                    },
                    # Phase.19 (2026-07-27) — per-vehicle motion.
                    # Qwen must classify motion for EACH vehicle, not
                    # scene-level. This is what the matcher iterates
                    # over to emit one alert per moving vehicle while
                    # Phase.78 (2026-08-14) — removed motion /
                    # motion_justification / motion_vector fields from
                    # Motion is owned by the motion gate
                    # (infra/frame_diff + listener/motion_gate_pipeline;
                    # Phase.115). infra.motion_detector is a dataclass
                    # shim since Phase §11.90.
                    # (pairwise differential). Qwen describes what's
                    # visible; the differential decides if it's moving.
                    # Asking both systems produced contradictions:
                    # motion_state="PARKED" on a truck the differential
                    # had just tracked across 6 frames. See PLAN.md §11.9.
                    # Phase.18 — discriminating per-vehicle features.
                    # Used by matcher to break ties between vehicles with
                    # the same color (e.g. 4Runner vs F150 both gray SUVs)
                    # and to identify EVs (aero_cover wheel style,
                    # closed_blank front grille).
                    "vehicle_features": {
                        "type": "object",
                        "properties": {
                            "make": {"type": ["string", "null"]},
                            "model": {"type": ["string", "null"]},
                            "wheel_style": {
                                "type": ["string", "null"],
                                "enum": [
                                    "alloy", "steel", "chrome", "aero_cover",
                                    "aftermarket", None,
                                ],
                            },
                            "roofline_style": {
                                "type": ["string", "null"],
                                "enum": [
                                    "fastback", "traditional_suv_box",
                                    "sedan_traditional", "pickup_open_bed",
                                    "van_box", None,
                                ],
                            },
                            "front_grille_style": {"type": ["string", "null"]},
                            "headlight_signature": {"type": ["string", "null"]},
                            "rear_lights_signature": {"type": ["string", "null"]},
                            "tailgate_type": {
                                "type": ["string", "null"],
                                "enum": [
                                    "liftback", "swing_up",
                                    "trunk_traditional", None,
                                ],
                            },
                            "badge_text_readable": {"type": ["string", "null"]},
                            # Phase.48 — bigger, more permanent
                            # distinguishing features (Note 2026-08-01 OOB
                            # rejected 6B.46's hitch_present as too small
                            # and too easily confused). Window tint,
                            # cab marker lights, and bed cover are all:
                            # - Larger in the frame than a hitch
                            # - Permanent (hard to swap)
                            # - Less likely to be confused by Qwen
                            "window_tint": {
                                "type": ["string", "null"],
                                "enum": ["none", "light", "dark",
                                         "factory_privacy", None],
                                "description": "Visible window darkness: 'none'/'light' = clear or minimal; 'dark' = aftermarket dark tint; 'factory_privacy' = rear-only factory privacy glass (common on SUVs).",
                            },
                            "cab_marker_lights": {
                                # Phase.66 — accept string OR boolean.
                                # Vision has been returning "false" (string)
                                # consistently even when the schema said
                                # boolean, causing strict-mode parse to fail
                                # and the signature to come back null. Fix
                                # verified 2026-08-08 against the 09:26 OFS
                                # arrival (alert 653ba844) — all 3 crops
                                # returned "cab_marker_lights":"false" with
                                # make=Chevrolet model=Silverado confidence=0.98
                                # and the parser dropped the whole response.
                                "type": ["boolean", "string", "null"],
                                "description": "true = 5 amber cab marker lights on roof (heavy-duty/commercial signal: F-350, medium-duty); false = clean roof; null = roof not visible in frame.",
                            },
                            "bed_cover": {
                                "type": ["string", "null"],
                                "enum": ["none", "tonneau", "camper_shell",
                                         "topper", None],
                                "description": "Pickup bed covering: 'none' = open bed; 'tonneau' = low-profile cover; 'camper_shell' or 'topper' = tall fiberglass shell (interchangeable in Qwen output).",
                            },
                        },
                        "required": [
                            "make", "model", "wheel_style", "roofline_style",
                            "front_grille_style", "headlight_signature",
                            "rear_lights_signature", "tailgate_type",
                            "badge_text_readable",
                            "window_tint", "cab_marker_lights", "bed_cover",
                        ],
                        "additionalProperties": False,
                    },
                    "bbox": {
                        "type": ["array", "null"],
                        "items": {"type": "number"},
                        "minItems": 4,
                        "maxItems": 4,
                        "description": "Bounding box [x1, y1, x2, y2] in NORMALIZED 0-1 coords of the NATIVE-RESOLUTION image. Use the SMALLEST box that fully contains the vehicle. null if bbox not determinable.",
                    },
                },
                "required": ["color", "body_style_hint", "vehicle_features", "bbox"],
            },
        },
        "primary_vehicle_index": {
            "type": "integer",
            "minimum": 0,
            "description": "DEPRECATED 2026-07-27 (Phase.19). Kept for backward compat with old consumers. Defaults to 0. Use vehicles[0] directly — motion is no longer a per-vehicle concept (Phase.78).",
        },
        # Phase.27 (2026-07-30) — best frame for the moving vehicle.
        # Qwen picks the 1-based index of the input frame where the moving
        # vehicle is largest / most centered / most visible. The alert
        # pipeline uses this to attach the right Telegram photo (frame 0
        # always catches the vehicle at trigger time when it's smallest
        # and farthest). Set to 1 if no clear best frame.
        "best_frame_index": {
            "type": "integer",
            "minimum": 1,
            "description": "1-based index of the input frame that has the moving vehicle largest, most centered, or most visible. Used by the alert pipeline to pick the Telegram attachment. Default to 1 if no clear best frame.",
        },
        # Phase.78 (2026-08-14) — removed moving_vehicle_indices.
        # Motion is owned by the motion gate
        # (infra/frame_diff + listener/motion_gate_pipeline; Phase.115).
        # The differential is the sole authority. Qwen describes vehicles;
        # the differential decides which ones are moving.
        # Phase.78 (2026-08-14) — removed vehicle_motion.
        # Same rationale: motion is owned by the motion gate.
        "face_visibility": {
            "type": "object",
            "properties": {
                "any_face_visible": {"type": "boolean"},
                "best_frame_index": {"type": "integer", "minimum": 1, "maximum": 3},
                "best_frame_face_fraction": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                "front_facing": {"type": "boolean"},
                "per_frame": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "index": {"type": "integer"},
                            "face_fraction": {"type": "number"},
                            "front_facing": {"type": "boolean"},
                            "bbox": {
                                "oneOf": [
                                    {"type": "array", "items": {"type": "number"}, "minItems": 4, "maxItems": 4},
                                    {"type": "null"},
                                ]
                            },
                        },
                        "required": ["index", "face_fraction", "front_facing", "bbox"],
                    },
                },
                "notes": {"type": ["string", "null"]},
            },
            "required": [
                "any_face_visible", "best_frame_index", "best_frame_face_fraction",
                "front_facing", "per_frame", "notes",
            ],
        },
    },
    "required": [
        "objects_detected", "primary_subject", "actions", "scene_description",
        "confidence", "notable_details", "colors", "species",
        "vehicles", "primary_vehicle_index",
        "face_visibility", "best_frame_index",
    ],
    "additionalProperties": False,
}


VEHICLE_CLASSIFY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "make": {
            "type": ["string", "null"],
            "description": "Brand/manufacturer (e.g. 'Ford', 'Chevrolet', 'Tesla', 'Toyota', 'Honda'). null if not determinable.",
        },
        "model": {
            "type": ["string", "null"],
            "description": "Specific model name (e.g. 'F-150', 'Model 3', 'Silverado 1500', 'RAV4'). null if not determinable.",
        },
        "body_style": {
            "type": ["string", "null"],
            "description": "Coarse shape classification: pickup|sedan|suv|van|hatchback|coupe|trailer|tractor|motorcycle|'truck (commercial)'. null if ambiguous.",
        },
        "trim_level": {
            "type": ["string", "null"],
            "description": "Trim level if visible (e.g. 'XLT', 'Lariat', 'Long Range', 'Limited'). null if not determinable or not visible.",
        },
        "year_range": {
            "type": ["string", "null"],
            "description": "Approximate model year range (e.g. '2018-2023', '2020s'). null if not determinable.",
        },
        "visible_modifications": {
            "type": ["string", "null"],
            "description": "Any aftermarket parts, accessories, racks, lifts, etc. null if stock.",
        },
        "distinctive_features": {
            "type": ["string", "null"],
            "description": "Anything that would help identify THIS vehicle vs similar ones (bumper stickers, dents, color of wheels, rust spots, missing trim). null if stock.",
        },
        "confidence": {
            "type": "number",
            "minimum": 0.0,
            "maximum": 1.0,
            "description": "Your confidence in the make/model identification. 0.0-1.0.",
        },
    },
    "required": [
        "make", "model", "body_style", "trim_level", "year_range",
        "visible_modifications", "distinctive_features", "confidence",
    ],
    "additionalProperties": False,
}


# ===========================================================================
# Public functions
# ===========================================================================


def select_prompt_template(
    event_hint: str | None,
    n_frames: int,
    interval_sec: int = 4,
    camera_name: str = "",
    event_hint_block: str = "",
    captured_at: str | None = None,
    # Phase.66 — accept Optional[str] so analyze_frames can pass
    # through `mode=None` (use existing auto-dispatch) or `mode="crop"`
    # (force the new crop-only template). Default "auto" preserves
    # all pre-6B.66 callers that don't pass mode.
    mode: str | None = "auto",
    # Phase.165 §11.86.3 (wider-scope revision 2026-08-29) —
    # YOLO's on-device class label (e.g. "dog", "bear", "bird"),
    # passed through to build_animal_prompt as context. Qwen is
    # told to OVERRIDE this hint if visual evidence contradicts
    # it ("vision model is smarter than Yolo" — Note 2026-08-29).
    # Default "unknown" so callers without YOLO context still work.
    species_hint: str = "unknown",
) -> str:
    """Return the fully-rendered prompt string for this analysis call.

    Phase.78 (2026-08-14): simplified vehicle dispatch. The motion
    prompt (mode="moving") and combined prompt (mode="combined") are
    removed. Motion is owned by the motion gate
    (infra/frame_diff + listener/motion_gate_pipeline; Phase.115).
    infra.motion_detector is a dataclass shim since Phase §11.90.
    Qwen only
    describes what's in the frame.

      - mode="auto"     → crop for n_frames <= 1, static for n_frames >= 2
      - mode="crop"     → VEHICLE_CROP_PROMPT_TEMPLATE (single tight
        crop of one vehicle; identification-only, no motion, no face,
        no scene narrative — Phase.66, 2026-08-08). Used by
        vehicle_identifier.py for each of the up-to-3 bbox crops.
      - mode="static"   → VEHICLE_STATIC_PROMPT_TEMPLATE (multi-frame,
        no motion bias, asks for ALL visible vehicles).
      - mode="moving"   → removed in 6B.78. Raises ValueError.
      - mode="combined" → removed in 6B.78. Raises ValueError.
      - mode="person"   → PERSON_PROMPT_TEMPLATE_FORMAT (Phase.106,
        2026-08-22). Person-event prompt with full Qwen3-VL attribute
        set (clothing_upper/lower color+type enum, carrying[], action,
        face_visible + face_bbox in PIXEL coords). Used by
        listener.person_event_pipeline for Front Door Outside
        gatekeeper events.
      - mode="animal"   → ANIMAL_PROMPT_TEMPLATE_FORMAT (Phase.165,
        2026-08-29). Animal-event prompt with full attribute set
        (species from YOLO's 7 classes + other/unknown, color enum,
        size trinary, distinctive_markings free-form, breed soft,
        behavior for downstream threat classification). Used by
        listener.animal_event_pipeline for animal gate events.
        Per Note 2026-08-29: "vision model is smarter than Yolo" —
        Qwen is authoritative for species.

    For non-vehicle events the legacy PROMPT_TEMPLATE is always used.

    The JSON schema block in PROMPT_TEMPLATE uses literal `{`/`}` for
    valid JSON, which conflicts with `.format()`. We use str.replace()
    for placeholder substitution instead of .format() so we can safely
    include raw JSON in the template.

    captured_at (Phase.20): the wall-clock ISO timestamp of the
    triggering event. Injected into the prompt as a hard date anchor
    so Qwen stops hallucinating dates in `notable_details`.
    Verified 2026-07-28: without this anchor, the model wrote
    "February 27, 2026" for a 2026-07-27 capture.

    Returned string is ready to send to the LLM -- caller does NOT need
    to format it further.
    """
    # Phase.74 (2026-08-10) — crop mode short-circuit BEFORE the
    # event_hint check. mode="crop" is identification-only and should
    # always return VEHICLE_CROP_PROMPT_TEMPLATE regardless of event_hint.
    # Without this, callers like vehicle_identifier.identify_from_crops()
    # that pass event_hint=None fall through to PROMPT_TEMPLATE (legacy),
    # even though they explicitly asked for the crop prompt. That was the
    # root cause of [name two]'s pickup being misidentified as [name one]'s pickup
    # since 6B.65 — Qwen was being asked the legacy 6-field question
    # instead of make/model/vehicle_features.
    if mode == "crop":
        out = VEHICLE_CROP_PROMPT_TEMPLATE
        out = out.replace("{event_hint_block}", event_hint_block)
        out = out.replace("{camera_name}", camera_name)
        out = out.replace(
            "{captured_at}",
            captured_at if captured_at else "unknown",
        )
        return out

    # Phase.106 (2026-08-22) — person mode short-circuit. Same shape
    # as the crop short-circuit above: dispatched BEFORE the vehicle
    # branch so the vehicle `event_hint != "vehicle"` guard doesn't
    # swallow person calls. PersonEventPipeline passes event_hint=None
    # and mode="person" so we route to the new template. The template
    # is built by infra.person_prompt_template.build_person_prompt()
    # (imported lazily below to keep this module's import surface small).
    if mode == "person":
        from infra.person_prompt_template import build_person_prompt

        return build_person_prompt(
            camera_name=camera_name,
            captured_at=captured_at or "unknown",
            event_hint_block=event_hint_block,
            interval_sec=interval_sec,
        )

    # Phase.165 (2026-08-29) — animal mode short-circuit. Same shape
    # as the person short-circuit above: dispatched BEFORE the vehicle
    # branch so the vehicle `event_hint != "vehicle"` guard doesn't
    # swallow animal calls. AnimalEventPipeline passes event_hint=None
    # and mode="animal" so we route to the new template. The template
    # is built by infra.animal_prompt_template.build_animal_prompt()
    # (imported lazily below to keep this module's import surface small).
    if mode == "animal":
        from infra.animal_prompt_template import build_animal_prompt

        return build_animal_prompt(
            camera_name=camera_name,
            captured_at=captured_at or "unknown",
            event_hint_block=event_hint_block,
            interval_sec=interval_sec,
            species_hint=species_hint,
        )

    # Phase §11.115.20 (2026-09-03): classify mode short-circuit.
    # Sends the shared CLASSIFY_PROMPT_TEMPLATE_FORMAT (4-class enum:
    # vehicle/person/animal/other) so cascade_call1's
    # validate_classify_response receives the right JSON shape.
    # Without this branch the prompt falls through to the legacy
    # vision_analysis template and Qwen returns objects_detected /
    # primary_subject / vehicles[] — which the classify validator
    # can't parse, leading to OTHER/fallback_used=True (zero Telegram).
    if mode == "classify":
        from infra.classify_prompt import build_classify_prompt

        return build_classify_prompt(
            camera_name=camera_name,
            captured_at=captured_at or "unknown",
        )

    # Phase §11.113 (2026-09-02) — unified single-call prompt for the
    # leakage test. Asks Qwen to pick the primary_class AND fill in the
    # matching per-class block in ONE call. See infra.unified_vision for
    # the prompt + schema design rationale. variant_a in the probe.
    if mode == "unified":
        from infra.unified_vision import build_unified_prompt

        return build_unified_prompt(
            camera_name=camera_name,
            captured_at=captured_at or "unknown",
            event_hint_block=event_hint_block,
            interval_sec=interval_sec,
            n_frames=n_frames,
        )

    if event_hint != "vehicle":
        return PROMPT_TEMPLATE.replace(
            "{camera_name}", camera_name
        ).replace(
            "{event_hint_block}", event_hint_block
        )

    if mode is None:
        mode = "auto"
    # Vehicle path — pick the right prompt family.
    # Phase.78 (2026-08-14): VEHICLE_MOTION_PROMPT_TEMPLATE and
    # VEHICLE_COMBINED_PROMPT_TEMPLATE are removed. The only multi-frame
    # vehicle prompt left is VEHICLE_STATIC_PROMPT_TEMPLATE. Auto-dispatch
    # picks crop for n=1 and static for n>=2. Modes "moving" and
    # "combined" raise — callers should use "static" instead.
    n = max(1, int(n_frames))
    if mode == "auto":
        # §11.115.22 — when the cascade payload carries the pairwise
        # differential image, the prompt must be diff-aware (it tells
        # Qwen "moving subject is what lights up the diff, ignore
        # parked vehicles"). Without this, Qwen enumerates parked
        # vehicles alongside the moving one and we end up with N TG#3
        # for one motion event. n=1 → crop, n=2 → static (legacy
        # backward compat), n>=3 → diff_static.
        if n <= 1:
            mode = "crop"
        elif n == 2:
            mode = "static"
        else:
            mode = "diff_static"

    if mode == "crop":
        # Phase.66 — single tight crop, identification-only.
        # No motion / face / scene narrative — see template docstring.
        out = VEHICLE_CROP_PROMPT_TEMPLATE
        out = out.replace("{event_hint_block}", event_hint_block)
        out = out.replace("{camera_name}", camera_name)
        out = out.replace(
            "{captured_at}",
            captured_at if captured_at else "unknown",
        )
        return out

    if mode == "diff_static":
        # §11.115.22 — 3-image payload (pairwise_diff + crop_a + crop_b).
        # See VEHICLE_DIFF_STATIC_PROMPT_TEMPLATE docstring for rationale.
        out = VEHICLE_DIFF_STATIC_PROMPT_TEMPLATE
        out = out.replace("{event_hint_block}", event_hint_block)
        out = out.replace("{camera_name}", camera_name)
        out = out.replace(
            "{captured_at}",
            captured_at if captured_at else "unknown",
        )
        return out

    if mode == "static":
        out = VEHICLE_STATIC_PROMPT_TEMPLATE
        out = out.replace("{event_hint_block}", event_hint_block)
        out = out.replace("{camera_name}", camera_name)
        out = out.replace(
            "{captured_at}",
            captured_at if captured_at else "unknown",
        )
        return out

    # mode == "moving" or "combined" — templates deleted in 6B.78.
    # The differential owns motion; we don't ask Qwen about it any more.
    # Callers should pass mode="static" for multi-frame vehicle analysis.
    raise ValueError(
        f"select_prompt_template: mode={mode!r} is no longer supported "
        f"(Phase.78). The VEHICLE_{mode.upper()}_PROMPT_TEMPLATE was "
        f"removed because motion is owned by the motion gate "
        f"(infra/frame_diff + listener/motion_gate_pipeline; Phase.115). "
        f"infra.motion_detector is a dataclass shim since Phase §11.90."
        f"Use mode='static' for multi-frame vehicle analysis, or "
        f"mode='crop' for a single tight crop."
    )


def _build_event_hint_block(event_hint: str | None) -> str:
    """Format the camera's on-device AI classification into a prompt block.

    The Reolink camera's built-in AI classifies the triggering event as
    one of vehicle / person / motion / animal BEFORE the frame is sent
    here. Without surfacing that fact, the vision LLM only sees a frozen
    snapshot and may describe a vehicle as 'parked' even when the camera
    just classified the trigger as 'vehicle in motion'. Pass that
    context through so the LLM can answer the right question.

    Args:
        event_hint: Lowercase event type from the webhook (e.g. "vehicle",
            "person", "motion", "animal"). None means no hint is added.

    Returns:
        Empty string when no hint. Otherwise a 1-3 sentence block
        instructing the LLM to align its description with the trigger
        type, with extra weight on motion-state for vehicles.
    """
    if not event_hint:
        return ""

    # Map webhook event types to human-readable trigger descriptions.
    # The camera-side classifier uses these Reolink values: PEOPLE,
    # VEHICLE, ANIMAL, MD (motion detection). Lowercased at the listener
    # boundary (alert_listener._parse_reolink_payload).
    mapping = {
        "vehicle": "vehicle in motion",
        "person": "person",
        "people": "person",
        "motion": "motion",
        "animal": "animal",
        "unknown": "unknown trigger",
    }
    desc = mapping.get(event_hint, event_hint)

    # Extra guidance for vehicle triggers — the original problem the
    # user surfaced: the LLM was describing parked vehicles as static
    # objects, not as "vehicle arriving / leaving / in transit". With
    # this hint the LLM pays explicit attention to motion state.
    motion_guidance = ""
    if event_hint in ("vehicle",):
        motion_guidance = (
            " Pay particular attention to the vehicle's motion state: "
            "is it ARRIVING (entering frame from outside), LEAVING "
            "(exiting frame), PARKED (stationary, present in prior "
            "context), or IN TRANSIT (moving across the frame)? "
            "Describe that motion state in scene_description."
        )

    return (
        f"\n\nTrigger context: The camera's on-device AI classified the "
        f"triggering event as '{desc}'. Align your description with "
        f"this trigger type. If the frame shows the expected object "
        f"(a vehicle when the trigger is vehicle, etc.), treat that as "
        f"the alert subject.{motion_guidance} Do not infer that the "
        f"trigger type is independent of what's in the frame."
    )


def _build_messages(
    frame_paths: list[str],
    camera_name: str,
    event_hint: str | None = None,
    captured_at: str | None = None,
    mode: str | None = None,
) -> list[dict]:
    """Build a multi-modal message list: text prompt + one image per frame.

    Frames are sent to Qwen at NATIVE resolution — no downscaling. Note
    directive 2026-08-26: "since we're no longer sending full frame images
    to Qwen, instead just boxes, I don't want to do down scaling anymore,
    that just causes problems." The pre-6B.130 downscale (4K→720p) was
    a token-budget workaround for --parallel 4; the listener now runs
    --parallel 1 with --ctx-size 16384, and the vehicle pipeline runs
    crop-only (Phase.100), so the only path hitting this function is
    the non-crop fallback (single-frame first-pass for non-vehicle events
    or motion-detector-missed vehicle events). One image, no batching.

    Token cost: 2K frame (2304x1296) is ~2700 image tokens. 4K was
    ~4100. llama-server ctx is 16384. Even at max_tokens=2048 + system
    prompt overhead, one 2K image + 2K output fits comfortably with room
    for the 4-frame rare case.

    The full-resolution file on disk is the single source of truth — bbox
    coords reported by Qwen are in THIS image's pixel coords.

    captured_at (Phase.20): forwarded to the prompt template as a
    hard date anchor so Qwen doesn't hallucinate dates in
    notable_details.
    """
    image_parts = []
    for path in frame_paths:
        try:
            # Read native-resolution bytes directly. No resize.
            with open(path, "rb") as f:
                b64 = base64.b64encode(f.read()).decode("ascii")
            # §11.115.22 — pairwise_diff is a PNG (lossless diff signal,
            # §11.88) while crops are JPEGs. llama-server accepts either
            # MIME, but the data URL must match the file bytes. Detect
            # by extension rather than trusting a hardcoded label.
            mime = "image/png" if path.lower().endswith(".png") else "image/jpeg"
            image_parts.append(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:{mime};base64,{b64}"},
                }
            )
        except Exception as err:
            log.info(f"Could not read {path}: {err}")
            continue

    rendered_text = select_prompt_template(
        event_hint,
        len(frame_paths),
        interval_sec=2,
        camera_name=camera_name,
        event_hint_block=_build_event_hint_block(event_hint),
        captured_at=captured_at,
        mode=mode,
    )
    text_part = {"type": "text", "text": rendered_text}

    return [{"role": "user", "content": [text_part] + image_parts}]