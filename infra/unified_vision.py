"""
unified_vision.py — Phase.113 §11.113 unified-prompt variant for Qwen3-VL.

STATUS: experimental (Phase §11.113 leakage test; gated on §11.114)
THREAD SAFETY: thread-safe (module-level constants; no shared state)

INPUTS:
  - fn `build_unified_prompt(camera_name, captured_at, event_hint_block,
        interval_sec)` reads fn args only; no IO, no env vars.

OUTPUTS:
  - The rendered prompt string passed to Qwen3-VL via
    analyze_frames_queued(mode="unified").
  - UNIFIED_SCHEMA_JSON: a JSON-Schema literal that is ALSO passed as
    response_format.json_schema.schema so llama-server grammar-constrains
    the response to exactly these keys. The literal appears in the prompt
    prose for Qwen to read (most runtimes do NOT inject the schema into
    the prompt — see structured-output-recipes skill pitfall #5).

PUBLIC API:
  - UNIFIED_PROMPT_TEMPLATE_FORMAT: raw template with {placeholder}s
  - UNIFIED_SCHEMA_JSON: JSON Schema for response_format
  - build_unified_prompt(...) returns fully-rendered prompt string

DOES NOT DO:
  - Does NOT call Qwen or any model. It only formats the prompt.
  - Does NOT validate the model response. (See infra.vision_response.py
    for schema validation — extension point for §11.114.)
  - Does NOT pick a class. The whole point of this module is to ask
    Qwen to pick a class AND fill in the matching per-class fields in
    ONE call. The validation/retry path is §11.114's job.

WHY HERE:
  Phase §11.113 leakage test. Note's design choice (2026-09-02):
  "test whether variant works properly... design the plan for both
  variant and variant B." This module is VARIANT A — the unified
  single-call schema that asks Qwen to do all three classes at once.
  If leakage is observed (model picks wrong class or fills wrong-class
  fields with non-"none" values), §11.114 falls back to VARIANT B
  (two-call cascade: class-only call + existing per-class prompt).

  Per structured-output-recipes skill:
    - pitfall #1: use enum strings ("none" / "unknown"), not null
    - pitfall #2: additionalProperties: false, every field required
    - pitfall #4: NO free-form chain-of-thought — inline as observations
    - pitfall #5: describe every field in prose (schema is NOT auto-injected)
    - pitfall #6a: boolean → ["boolean", "string"]
    - pitfall #9: schema + prompt MUST be edited in lockstep (Mode 1/2)

CALLED BY:
  - infra.prompt_templates.select_prompt_template(mode="unified")
  - scripts/probe_variant_leakage.py (§11.113 harness)

RELATED:
  - infra.prompt_templates (select_prompt_template dispatcher)
  - infra.person_prompt_template (Variant B's per-person fallback)
  - infra.animal_prompt_template (Variant B's per-animal fallback)
  - infra.prompt_templates.VEHICLE_CROP_PROMPT_TEMPLATE (Variant B's per-vehicle fallback)
"""

from __future__ import annotations

# ============================================================================
# UNIFIED_PROMPT_TEMPLATE_FORMAT — Variant A's prompt body
# ============================================================================
# Asks Qwen to inspect all visible subjects (vehicles, persons, animals),
# pick the primary_class, and fill in BOTH the matching per-class block
# (with real values) AND the non-matching blocks (with "none" placeholders).
# Leakage = the non-matching blocks get real values when they should be
# "none".

UNIFIED_PROMPT_TEMPLATE_FORMAT = """\
You are analyzing a {n_frame_str} from camera "{camera_name}" captured at \
{captured_at}. The camera's on-device AI classified the triggering event as \
"{event_hint}".{event_hint_block}

Your job: identify the primary subject, fill in the matching class block \
with precise attributes, and explicitly set the non-matching class blocks \
to "none" placeholders. Do NOT speculate across classes — if there is no \
vehicle in the scene, every vehicle_features field must be "none", not a \
guess.

Think silently about the scene BEFORE answering, but output ONLY the JSON \
object (no markdown fences, no prose outside the JSON).

============================================================
SCHEMA (every field is required — use "none" or "unknown" when absent):
============================================================

{{
  "primary_class":         <"vehicle" | "person" | "animal" | "none">,
  "vehicle_present":       <true | false>,
  "person_present":        <true | false>,
  "animal_present":        <true | false>,

  // Always required — fill with REAL values for the matching class,
  // "none" for non-matching classes. Do NOT guess.
  "vehicle_features": {{
    "make":        <chevrolet | ford | tesla | toyota | honda | gmc | ram | \
                   buick | cadillac | lincoln | nissan | subaru | \
                   hyundai | kia | jeep | dodge | chrysler | mercedes | \
                   bmw | audi | volkswagen | porsche | lexus | acura | \
                   infiniti | mazda | mitsubishi | volvo | land_rover | \
                   jaguar | mini | fiat | alfa_romeo | maserati | \
                   bentley | rolls_royce | ferrari | lamborghini | \
                   aston_martin | mclaren | bugatti | koenigsegg | \
                   pagani | genesis | rivian | lucid | polestar | \
                   unknown | none>,
    "model":       <free-form string — model name, badge, or "unknown" | "none">,
    "color":       <black | white | silver | gray | blue | red | green | \
                   yellow | orange | brown | tan | gold | beige | \
                   maroon | navy | teal | copper | unknown | none>,
    "body_style":  <sedan | suv | pickup | coupe | hatchback | \
                   convertible | wagon | van | crossover | truck | \
                   minivan | unknown | none>,
    "plate":       <free-form string — plate as read, or "unknown" | "none">,
    "plate_state": <US state abbreviation or "unknown" | "none">,
    "occupants_visible": <integer 0-8 | "unknown" | "none">
  }},

  "person_features": {{
    "clothing_upper": <red | blue | green | yellow | orange | purple | \
                      pink | brown | black | white | gray | tan | \
                      beige | navy | teal | multicolor | unknown | none>,
    "clothing_lower": <red | blue | green | yellow | orange | purple | \
                      pink | brown | black | white | gray | tan | \
                      beige | navy | teal | multicolor | unknown | none>,
    "carrying":       <nothing | bag | package | backpack | tool | \
                      weapon | leash | other | unknown | none>,
    "action":         <walking | standing | running | approaching | \
                      leaving | crouching | climbing | sitting | \
                      riding | unknown | none>,
    "face_visible":   <true | false>
  }},

  "animal_features": {{
    "species":  <deer | bear | coyote | fox | raccoon | opossum | \
                rabbit | squirrel | skunk | bobcat | mountain_lion | \
                dog | cat | bird | hawk | owl | crow | turkey | \
                chicken | rodent | livestock | other | unknown | none>,
    "color":    <brown | black | tan | white | gray | red | mixed | \
                unknown | none>,
    "size":     <small | medium | large | unknown | none>,
    "behavior": <walking | standing | running | eating | drinking | \
                approaching | fleeing | climbing | sleeping | \
                vocalizing | unknown | none>
  }},

  "scene_description": <one or two sentences describing lighting, \
                        setting, and what is happening>,
  "observations":      <silent reasoning that informed the answer — \
                        DO NOT include chain-of-thought, only the \
                        factual observations that justify the chosen \
                        primary_class>
}}

============================================================
RULES (these prevent the model from "leaking" values across classes):
============================================================
1. Pick exactly ONE primary_class. If multiple classes are present, pick
   the one that triggered the camera event (the camera's hint is in
   {event_hint_block}). If the scene is empty, primary_class = "none".
2. If primary_class = "vehicle", the vehicle_features block MUST have
   real values; person_features and animal_features MUST be "none".
3. If primary_class = "person", the person_features block MUST have
   real values; vehicle_features and animal_features MUST be "none".
4. If primary_class = "animal", the animal_features block MUST have
   real values; vehicle_features and person_features MUST be "none".
5. The vehicle_present / person_present / animal_present booleans
   describe WHETHER each class is visible AT ALL (a scene can have a
   vehicle AND a person present but primary_class still picks one).
6. Do NOT guess "unknown" when the value is genuinely absent — use
   "none" for absent and "unknown" for present-but-indeterminable.
7. Output the JSON object only. No markdown fences, no commentary,
   no leading or trailing prose.
"""

# ============================================================================
# UNIFIED_SCHEMA_JSON — same shape, strict JSON for response_format
# ============================================================================
# This is the runtime-enforced schema. llama-server's grammar parser uses
# this to constrain Qwen's output tokens. The literal in the prompt body
# above is for Qwen to read; this is for the grammar-constrained backend.
# Both must stay in sync (structured-output-recipes pitfall #9 Mode 1/2).

UNIFIED_SCHEMA_JSON = """{
  "type": "object",
  "additionalProperties": false,
  "required": [
    "primary_class",
    "vehicle_present",
    "person_present",
    "animal_present",
    "vehicle_features",
    "person_features",
    "animal_features",
    "scene_description",
    "observations"
  ],
  "properties": {
    "primary_class": {
      "type": "string",
      "enum": ["vehicle", "person", "animal", "none"]
    },
    "vehicle_present": {"type": ["boolean", "string"], "enum": [true, false, "true", "false"]},
    "person_present":  {"type": ["boolean", "string"], "enum": [true, false, "true", "false"]},
    "animal_present":  {"type": ["boolean", "string"], "enum": [true, false, "true", "false"]},
    "vehicle_features": {
      "type": "object",
      "additionalProperties": false,
      "required": ["make", "model", "color", "body_style", "plate", "plate_state", "occupants_visible"],
      "properties": {
        "make": {
          "type": ["string", "null"],
          "enum": [
            "chevrolet", "ford", "tesla", "toyota", "honda", "gmc", "ram",
            "buick", "cadillac", "lincoln", "nissan", "subaru",
            "hyundai", "kia", "jeep", "dodge", "chrysler", "mercedes",
            "bmw", "audi", "volkswagen", "porsche", "lexus", "acura",
            "infiniti", "mazda", "mitsubishi", "volvo", "land_rover",
            "jaguar", "mini", "fiat", "alfa_romeo", "maserati",
            "bentley", "rolls_royce", "ferrari", "lamborghini",
            "aston_martin", "mclaren", "bugatti", "koenigsegg",
            "pagani", "genesis", "rivian", "lucid", "polestar",
            "unknown", "none", null
          ]
        },
        "model":            {"type": ["string", "null"], "enum": ["unknown", "none", null]},
        "color": {
          "type": "string",
          "enum": [
            "black", "white", "silver", "gray", "blue", "red", "green",
            "yellow", "orange", "brown", "tan", "gold", "beige",
            "maroon", "navy", "teal", "copper", "unknown", "none"
          ]
        },
        "body_style": {
          "type": "string",
          "enum": [
            "sedan", "suv", "pickup", "coupe", "hatchback",
            "convertible", "wagon", "van", "crossover", "truck",
            "minivan", "unknown", "none"
          ]
        },
        "plate":            {"type": ["string", "null"], "enum": ["unknown", "none", null]},
        "plate_state":      {"type": ["string", "null"], "enum": ["unknown", "none", null]},
        "occupants_visible": {"type": ["integer", "string", "null"], "enum": ["unknown", "none", null]}
      }
    },
    "person_features": {
      "type": "object",
      "additionalProperties": false,
      "required": ["clothing_upper", "clothing_lower", "carrying", "action", "face_visible"],
      "properties": {
        "clothing_upper": {
          "type": "string",
          "enum": [
            "red", "blue", "green", "yellow", "orange", "purple",
            "pink", "brown", "black", "white", "gray", "tan",
            "beige", "navy", "teal", "multicolor", "unknown", "none"
          ]
        },
        "clothing_lower": {
          "type": "string",
          "enum": [
            "red", "blue", "green", "yellow", "orange", "purple",
            "pink", "brown", "black", "white", "gray", "tan",
            "beige", "navy", "teal", "multicolor", "unknown", "none"
          ]
        },
        "carrying": {
          "type": "string",
          "enum": ["nothing", "bag", "package", "backpack", "tool",
                   "weapon", "leash", "other", "unknown", "none"]
        },
        "action": {
          "type": "string",
          "enum": ["walking", "standing", "running", "approaching",
                   "leaving", "crouching", "climbing", "sitting",
                   "riding", "unknown", "none"]
        },
        "face_visible": {"type": ["boolean", "string"], "enum": [true, false, "true", "false"]}
      }
    },
    "animal_features": {
      "type": "object",
      "additionalProperties": false,
      "required": ["species", "color", "size", "behavior"],
      "properties": {
        "species": {
          "type": "string",
          "enum": [
            "deer", "bear", "coyote", "fox", "raccoon", "opossum",
            "rabbit", "squirrel", "skunk", "bobcat", "mountain_lion",
            "dog", "cat", "bird", "hawk", "owl", "crow", "turkey",
            "chicken", "rodent", "livestock", "other", "unknown", "none"
          ]
        },
        "color": {
          "type": "string",
          "enum": [
            "brown", "black", "tan", "white", "gray", "red",
            "mixed", "unknown", "none"
          ]
        },
        "size":     {"type": "string", "enum": ["small", "medium", "large", "unknown", "none"]},
        "behavior": {
          "type": "string",
          "enum": ["walking", "standing", "running", "eating", "drinking",
                   "approaching", "fleeing", "climbing", "sleeping",
                   "vocalizing", "unknown", "none"]
        }
      }
    },
    "scene_description": {"type": "string"},
    "observations":      {"type": "string"}
  }
}"""


# ============================================================================
# build_unified_prompt — substitute the four placeholders the template uses
# ============================================================================

def build_unified_prompt(
    camera_name: str,
    captured_at: str,
    event_hint_block: str = "",
    interval_sec: int = 4,
    n_frames: int = 1,
) -> str:
    """Render UNIFIED_PROMPT_TEMPLATE_FORMAT with the four runtime values.

    Args:
        camera_name: short camera code (e.g. "OFS", "CAM1").
        captured_at: ISO timestamp of the event ("2026-09-02 09:47:53 EDT").
        event_hint_block: pre-rendered block describing the camera's
            on-device AI classification (vehicle/person/animal/motion).
        interval_sec: seconds between frame samples.
        n_frames: how many frames are in the prompt (1, 2, or 3).

    Returns:
        Fully rendered prompt string. Caller sends it as the user message
        to Qwen3-VL (via analyze_frames_queued).
    """
    if n_frames <= 1:
        n_frame_str = "single frame"
    elif n_frames == 2:
        n_frame_str = "pair of frames"
    else:
        n_frame_str = f"{n_frames}-frame sequence ({interval_sec}s apart)"
    out = UNIFIED_PROMPT_TEMPLATE_FORMAT.replace(
        "{camera_name}", camera_name
    ).replace(
        "{captured_at}", captured_at
    ).replace(
        "{event_hint_block}", event_hint_block
    ).replace(
        "{n_frame_str}", n_frame_str
    ).replace(
        "{event_hint}", event_hint_block.split("=")[-1].strip(" ]")
        if event_hint_block
        else "unknown",
    )
    return out