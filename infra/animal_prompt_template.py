"""
animal_prompt_template.py — Phase 6B.165 animal-event prompt + schema
(wider-scope revision 2026-08-29).

STATUS: provisional (Phase 6B.165 §11.86.3; wider-scope schema)
THREAD SAFETY: thread-safe (module-level constants; no shared state)

INPUTS:
  - fn `build_animal_prompt(camera_name, captured_at, event_hint_block,
        species_hint)` reads fn args only; no IO, no env vars.
        species_hint is the YOLO gate's class label passed through as
        context — Qwen is told to OVERRIDE this if the visual evidence
        contradicts it (maintainer 2026-08-29: "vision model is smarter
        than Yolo").

OUTPUTS:
  - The rendered prompt string passed to Qwen3-VL via
    analyze_frames_queued.
  - ANIMAL_SCHEMA_JSON: a plain-string JSON literal embedded in the
    prompt so Qwen emits exactly that shape. Wider-scope schema
    designed 2026-08-29 to support individual-animal ID (e.g. two
    distinct coyotes on the property).

PUBLIC API:
  - ANIMAL_PROMPT_TEMPLATE_FORMAT: the raw template (with
        `{placeholder}s`)
  - ANIMAL_SCHEMA_JSON: the JSON schema literal embedded in the
        template
  - build_animal_prompt(...) returns the fully-rendered prompt string

DOES NOT DO:
  - Does NOT call Qwen or any model. It only formats the prompt.
  - Does NOT validate the model response. (See infra.vision_response.py
    for schema validation.)
  - Does NOT match animals to enrolled identities. (See
    infra.animal_matcher — built in §11.86.2.)
  - Does NOT classify threat level (concerning vs routine). Threat
    classification is a downstream concern (§11.86.6) — Qwen is asked
    to describe the animal, not to decide if it warrants an alert.
  - Does NOT detect faces or persons. (Animal events are orthogonal
    to person events; the gate emits `event_type='animal'` for these.)

WHY HERE:
  Phase 6B.165 §11.86.3 (wider-scope revision 2026-08-29). Mirrors
  infra.person_prompt_template's structure (single-subject focus,
  precise attribute enumeration, explicit null rules) but adapted for
  the wider-scope schema: free-form species (Qwen authoritative),
  distinctive_features[] array (so two coyotes are distinguishable),
  face_details{} nested dict, body_build + coat_pattern + estimated_age
  + sex_signal.

CALLED BY:
  - infra.prompt_templates.select_prompt_template (Phase 6B.165) when
    `mode="animal"` is selected for an animal event.
  - listener.animal_event_pipeline (built §11.86.7) when constructing
    the per-alert vision call for the animal gate.

CALLS INTO:
  - infra.prompt_templates._build_event_hint_block (reused, not
    re-defined)

RELATED:
  - infra.animal_matcher.match_animal — downstream consumer; reads
    the schema fields directly to compute stable-attribute match
    score.
  - infra.person_prompt_template.PERSON_PROMPT_TEMPLATE_FORMAT —
    closest cousin for matching philosophy: 5+ stable identity
    attributes, full enum normalization, null if not determinable.
  - PLAN.md §11.86.3 — design plan; wider-scope schema fields,
    match-weight rationale, Qwen-overrides-YOLO override.

Design notes (Phase 6B.165 §11.86.3, wider-scope revision 2026-08-29):

  Species is free-form (Qwen authoritative). maintainer 2026-08-29:
    "If yolo see that an animal is a dog, but it's actually a coyote
    or a wolf or a fox, I want the prompt that goes to Qwen to be
    wide open enough that that's the actual identification that comes
    back." The schema's species field is a string, not an enum. Qwen
    may return "coyote", "Eastern coyote", "coy-wolf hybrid", "red
    fox", "fisher cat", "raccoon", "deer", "wild turkey", etc. The
    downstream animal_matcher normalizes variants to canonical
    buckets (see SPECIES_NORMALIZATION in animal_matcher).

  species_confidence lets Qwen hedge. definite / likely / unsure.
    The matcher raises its threshold from 0.55 to 0.65 when Qwen is
    unsure, requiring stronger evidence on the other attributes.

  distinctive_features[] is an ARRAY, not a string. maintainer 2026-08-29:
    "the distinguishing characteristics result should be enough that
    it could identify different coyotes from one another, for
    example." Multiple features per animal (e.g. ["left ear notched",
    "white-tipped tail", "limp in left rear leg"]) scored via
    token-set Jaccard in the matcher. Single-string distinctive_
    markings was too coarse to distinguish individuals.

  face_details{} captures the textbook coyote-vs-wolf-vs-fox
    discriminators: ear_shape (pointed/tufted/floppy/rounded),
    tail_carriage (high/low/curled/level), mask (yes/no). These three
    together are the most diagnostic wild-canid features when the
    body is too small/blurry to see clearly.

  body_build (lean/stocky/athletic/compact) is separate from
    body_size (small/medium/large). Lean vs stocky is the meaningful
    axis for distinguishing individual coyotes (a lean adult vs a
    stocky adult are visually different).

  coat_pattern (solid/bi-color/tri-color/tabby/striped/spotted)
    separates from coat_primary_color. Two coyotes can both be
    brown/gray bi-colored but differ in tail carriage.

  estimated_age (juvenile/adult/senior) + sex_signal (male/female/
    neutered) recurs across nights — same coyote returns.

  Single-subject focus (animals[] has one entry, primary_animal_index
    always 0) for now. Multiple animals per frame are rare in our
    camera coverage; if we see two, the prompt asks for them as
    animals[0] + animals[1] but the matcher only scores
    primary_animal_index.

  Behavior field (free-form short verb) is captured for the threat-
    classification step (§11.86.6) — "approaching door" vs "passing
    through" is a key signal but doesn't belong in matching.

  YOLO hint is passed through but with explicit override language.
    maintainer 2026-08-29: "vision model is smarter than Yolo." Qwen is
    told: "Your species call OVERRIDES the YOLO hint. If YOLO saw a
    dog but you see a coyote, say coyote."
"""


# ============================================================================
# Schema literal — embedded into the prompt so Qwen emits this exact shape.
# ============================================================================
# Wider-scope schema (2026-08-29). Mirrors PERSON_PROMPT_TEMPLATE_FORMAT's
# "report every field, return null if unsure" discipline but for animals.
# Field names match the keys the downstream animal_matcher + threat
# classifier (§11.86.6) expect; renaming here is a breaking change to
# those callers.

ANIMAL_SCHEMA_JSON = """\
{
  "animals": [
    {
      "animal_id": "a1",
      "species": "free-form species name (coyote, wolf, fox, dog, cat, bear, fisher, raccoon, deer, wild turkey, red-tailed hawk, etc.)" | null,
      "species_confidence": "definite" | "likely" | "unsure" | null,
      "body_size": "small" | "medium" | "large" | null,
      "body_build": "lean" | "stocky" | "athletic" | "compact" | null,
      "coat_primary_color": "black" | "white" | "gray" | "silver" | "red" | "blue" | "green" | "yellow" | "brown" | "orange" | "pink" | "purple" | "other" | null,
      "coat_pattern": "solid" | "bi-color" | "tri-color" | "tabby" | "striped" | "spotted" | null,
      "distinctive_features": [
        "feature 1 (e.g. left ear notched)",
        "feature 2 (e.g. white-tipped tail)",
        "feature 3 (e.g. blue collar)"
      ] | [],
      "face_details": {
        "ear_shape": "pointed" | "tufted" | "floppy" | "rounded" | null,
        "tail_carriage": "high" | "low" | "curled" | "level" | null,
        "mask": "yes" | "no" | null
      } | null,
      "estimated_age": "juvenile" | "adult" | "senior" | null,
      "sex_signal": "male" | "female" | "neutered" | null,
      "behavior": "free-form short verb" | null,
      "scene_description": "1-2 sentence description of the scene"
    }
  ],
  "primary_animal_index": 0,
  "confidence": 0.0-1.0,
  "notable_details": ["detail 1", "detail 2"],
  "frame_positions": []
}"""


# ============================================================================
# Prompt template — sent to Qwen3-VL for animal events.
# ============================================================================
# Wider-scope revision 2026-08-29. Mirrors PERSON_PROMPT_TEMPLATE_FORMAT's
# structure (single-subject focus, precise attribute enumeration, explicit
# null rules) but adapts for:
#   - multi-frame input (2 frames; both sent simultaneously)
#   - animal-specific attribute set (free-form species, distinctive_
#     features[] array, face_details{})
#   - animals[] array supporting multiple animals per frame (rare)
#   - simpler schema (no face_bbox, no clothing, no carrying)
#   - behavior field for downstream threat classification
#   - YOLO hint is passed but Qwen is told to override

ANIMAL_PROMPT_TEMPLATE_FORMAT = (
    'Camera "{camera_name}". Captured at {captured_at}. '
    "Animal-event analysis (Phase 6B.165, wider-scope schema).\n\n"
    "These are TWO frames from the SAME camera, captured "
    "{interval_sec}s apart. Inspect BOTH frames before deciding.\n\n"
    "YOLO on-device gate saw a class hint: \"{species_hint}\".\n"
    "Your species call OVERRIDES the YOLO hint. If YOLO saw a dog "
    "but you see a coyote, say coyote. If YOLO saw a bear but you "
    "see a raccoon, say raccoon. Trust your eyes over the gate.\n\n"
    "Identify every animal visible across the frames. For each "
    "animal, report the FULL attribute set below — values "
    "normalized to the enum, or null if not determinable. Do NOT "
    "guess.\n\n"
    "Animal fields:\n"
    '  animal_id            — "a1", "a2", ... stable across the two '
    'frames\n'
    "  species              — FREE-FORM species name. Examples: "
    "\"coyote\", \"Eastern coyote\", \"wolf\", \"red fox\", \"gray "
    "fox\", \"fisher\", \"fisher cat\", \"raccoon\", \"deer\", "
    "\"white-tailed deer\", \"black bear\", \"red-tailed hawk\", "
    "\"wild turkey\", \"bobcat\", \"coyote-dog hybrid\" (coydog), "
    "\"domestic dog\", \"house cat\", \"feral cat\". Hybrids are "
    "fine — call them out. Use \"likely\" or \"unsure\" in "
    "species_confidence below if you can't be sure. null if no "
    "animal is visible.\n"
    "  species_confidence   — your confidence in the species call. "
    "\"definite\" = textbook example. \"likely\" = confident but "
    "with a hedge (\"looks like a coyote but could be a young "
    "wolf\"). \"unsure\" = genuine ambiguity. The downstream "
    "matcher raises its match threshold when you say \"unsure\" — "
    "so use it honestly, not as a default.\n"
    '  body_size            — "small" | "medium" | "large". '
    "Relative to typical adult of that species. small: cat, small "
    "dog, rabbit, raccoon. medium: large dog, coyote, fox, deer. "
    "large: horse, cow, black bear, moose. null if not "
    "determinable.\n"
    '  body_build           — "lean" | "stocky" | "athletic" | '
    '"compact". Lean = wolf/coyote build. Stocky = bulldog/'
    "pot-bellied build. Athletic = greyhound/working dog build. "
    "Compact = short-legged/cobby build (corgi, raccoon). Useful "
    "for distinguishing individuals — two adult coyotes can both "
    "be lean, but one might be more athletic than the other.\n"
    "  coat_primary_color   — DOMINANT coat color of the animal. "
    "Same enum as clothing colors (black, white, gray, silver, "
    "red, blue, green, yellow, brown, orange, pink, purple, other). "
    "null only if nothing visible. For wild canids (coyote/wolf/"
    "fox) the dominant color is usually brown, gray, or red — be "
    "specific.\n"
    '  coat_pattern         — "solid" | "bi-color" | "tri-color" | '
    '"tabby" | "striped" | "spotted". Bi-color = two distinct '
    "color zones (e.g. German Shepherd black-and-tan). Tri-color = "
    "three zones (e.g. calico cat, beagle). Tabby = the swirled "
    "pattern on tabby cats. Striped/spotted self-explanatory. "
    "null if the coat appears solid or if not determinable.\n"
    "  distinctive_features — ARRAY of short strings. ONE PER "
    "FEATURE. Examples for coyotes: [\"left ear notched\", "
    "\"white-tipped tail\", \"limp in left rear leg\"]. For dogs: "
    "[\"blue collar\", \"torn left ear\", \"missing right eye\"]. "
    "For cats: [\"white paws\", \"notched left ear\", \"orange "
    "tabby\"] (use orange tabby in coat_pattern, NOT here). Be "
    "specific and identifying — these features are how we tell "
    "individual animals apart across nights. Empty array [] if "
    "nothing notable.\n"
    "  face_details         — nested object, NOT a string. "
    "ear_shape (pointed/tufted/floppy/rounded) and tail_carriage "
    "(high/low/curled/level) are textbook coyote-vs-wolf-vs-fox "
    "discriminators: coyote ears are tall+pointed with a slight "
    "tuft; wolf ears are rounded; fox ears are very tall+pointed. "
    "Coyote tail is carried low when running; fox tail is "
    "carried level or curled with a white tip; wolf tail is "
    "level. mask (yes/no) means a facial mask pattern (dark fur "
    "around the eyes/muzzle, like a raccoon or German Shepherd). "
    "null components are fine; only fill in what you can see.\n"
    '  estimated_age        — "juvenile" | "adult" | "senior". '
    "Juvenile = puppy/kitten/cub, oversized paws or fuzzy coat. "
    "Adult = full size, healthy. Senior = graying muzzle, slower "
    "movement, grizzled coat. null if not determinable.\n"
    '  sex_signal           — "male" | "female" | "neutered". '
    "Male dogs/cats are usually visibly larger and blockier; "
    "neutered = obvious spay/neuter scar or missing tail tip "
    "(bobcat). For wild animals, use null unless the evidence is "
    "unambiguous (e.g. a doe with fawn).\n"
    "  behavior             — short verb describing what the "
    "animal is doing. Examples: \"walking\", \"running\", "
    "\"standing\", \"sniffing\", \"approaching door\", \"passing "
    "through\", \"eating\", \"sleeping\", \"grooming\". null if "
    "unclear.\n\n"
    "These attributes are STABLE over time — used to identify "
    "recurring animals (the resident fox, a neighborhood cat, a "
    "specific coyote that hunts the chicken coop) vs transient "
    "ones (a bear passing through). Return null (not \"unknown\") "
    "when the attribute cannot be determined; do NOT guess.\n\n"
    "Top-level fields:\n"
    "  primary_animal_index — index into animals[] for the MOST "
    "prominent animal (closest, largest in frame, most centered). "
    "Use 0 if animals[] is non-empty.\n"
    "  confidence           — 0.0-1.0 reflecting overall "
    "identification quality. Lower for distant / blurry / "
    "unusual-angle frames. Cap at 0.7 when species_confidence "
    "is \"unsure\".\n"
    "  notable_details      — short strings for anything NOT "
    "covered above (e.g. \"carrying prey in mouth\", \"limping "
    "heavily on left rear\", \"appeared with two fawns\").\n"
    "  frame_positions      — leave this empty []. The downstream "
    "motion detector fills it from the pairwise differential; "
    "Qwen should not try to infer trajectory from these two "
    "frames.\n\n"
    "Decision rule: if no animal is visible, return:\n"
    '  {"animals": [], "primary_animal_index": 0, "confidence": '
    "<high>, \"notable_details\": [...], \"frame_positions\": []}"
    "\n\n"
    "Output ONLY the JSON object matching this schema. No "
    "preamble. No markdown fences.\n\n"
    "{event_hint_block}\n\n"
    "Schema:\n"
    "{schema_json}"
)


def build_animal_prompt(
    camera_name: str,
    captured_at: str,
    event_hint_block: str = "",
    interval_sec: int = 4,
    species_hint: str = "unknown",
) -> str:
    """Return the fully-rendered animal-event prompt string.

    Mirrors infra.person_prompt_template.build_person_prompt's behavior:
    the caller passes camera_name + captured_at (already formatted by
    select_prompt_template), and we do the str.replace() so the schema's
    literal `{`/`}` doesn't conflict with .format().

    The event_hint_block is built by
    infra.prompt_templates._build_event_hint_block for consistency with
    vehicle + person templates.

    interval_sec: gap between frames. Matches the deferred-capture
    default (4s) used by the person + vehicle pipelines.

    species_hint: the YOLO gate's class label (e.g. "dog", "bear",
    "bird"). Passed through to Qwen as context — the prompt body
    instructs Qwen to OVERRIDE this hint if visual evidence
    contradicts it.
    """
    rendered = ANIMAL_PROMPT_TEMPLATE_FORMAT
    rendered = rendered.replace("{camera_name}", str(camera_name))
    rendered = rendered.replace("{captured_at}", str(captured_at))
    rendered = rendered.replace("{event_hint_block}", str(event_hint_block))
    rendered = rendered.replace("{interval_sec}", str(interval_sec))
    rendered = rendered.replace("{species_hint}", str(species_hint))
    rendered = rendered.replace("{schema_json}", ANIMAL_SCHEMA_JSON)
    return rendered


# Re-export the template for callers that want the raw format string
# (e.g. for testing what got substituted).
__all__ = [
    "ANIMAL_PROMPT_TEMPLATE_FORMAT",
    "ANIMAL_SCHEMA_JSON",
    "build_animal_prompt",
]
