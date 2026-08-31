# Animal prompt — worked recipe (Phase 6B.165)

Worked example of the vision-prompt-design 4-rule playbook applied end-to-end to the animal pipeline. Origin: Phase 6B.165 §11.86.3 commit (2026-08-30). Use this as a template when adding a new pipeline.

## Files

- `infra/animal_prompt_template.py` — `ANIMAL_SCHEMA_JSON`, `ANIMAL_PROMPT_TEMPLATE_FORMAT`, `build_animal_prompt(species_hint=None)`.
- `infra/animal_matcher.py` — `match_animal()`, `SPECIES_NORMALIZATION`, `_feature_tokens()`, `WEIGHTS`, `ANIMAL_MATCH_THRESHOLD`, `ANIMAL_MATCH_THRESHOLD_UNSURE`.
- `infra/prompt_templates.py` — `select_prompt_template(mode="animal", species_hint=None)` dispatches with `species_hint` forwarded.
- `infra/tests/test_animal_prompt_template_6B165_3.py` — 42 tests.
- `infra/tests/test_animal_matcher_6B165_2.py` — 71 tests.

## Schema (text-for-Qwen, not JSON-parseable)

```python
ANIMAL_SCHEMA_JSON = """
{
  "animals": [
    {
      "animal_id": "a1",
      "species": "<free-form string — coyote, wolf, fox, dog, raccoon, deer, fisher, etc.>",
      "species_confidence": "definite" | "likely" | "unsure" | null,
      "body_size": "small" | "medium" | "large" | null,
      "body_build": "lean" | "stocky" | "athletic" | "compact" | null,
      "coat_primary_color": "<free-form string or null>",
      "coat_pattern": "solid" | "bi-color" | "tri-color" | "tabby" | "striped" | "spotted" | null,
      "distinctive_features": ["<1-5 short strings>"],
      "face_details": {
        "ear_shape": "pointed" | "floppy" | "tufted" | "rounded" | null,
        "tail_carriage": "high" | "low" | "curled" | "level" | null,
        "mask": "yes" | "no" | null
      } | null,
      "estimated_age": "juvenile" | "adult" | "senior" | null,
      "sex_signal": "male" | "female" | "neutered" | null,
      "behavior": "<free-form short verb or null>",
      "scene_description": "<1-2 sentences>"
    }
  ],
  "primary_animal_index": 0,
  "confidence": <0.0-1.0>,
  "notable_details": ["<free-form>"],
  "frame_positions": []
}
"""
```

Notice: literal newlines and pipe-enum placeholders are inside the triple-quoted string. **This is fine for Qwen (it's text), but breaks `json.loads`.** Tests assert structural string content instead.

## Required prompt body phrases

The override language (rule 1) MUST appear in the rendered prompt:

```
Your species call OVERRIDES the YOLO hint — if you see a coyote, say coyote,
trust your eyes over the gate. If YOLO says nothing, classify from scratch —
do not emit null just because YOLO was silent.
```

The individual-ID guidance (rule 4) MUST appear with explicit examples:

```
distinctive_features is an ARRAY of 1-5 short strings describing attributes
that distinguish THIS individual from other members of the same species.

Examples for coyotes:
  - "left ear notched"
  - "white-tipped tail"
  - "scar on right shoulder"
  - "limp in left rear leg"
  - "blue collar"

Generic descriptions ("brown", "medium", "looks healthy") are NOT distinctive.
```

The free-form species guidance (rule 3) MUST enumerate the expected vocabulary without constraining to an enum:

```
species: free-form string. Use the most specific name you can:
coyote, Eastern coyote, coy-wolf hybrid, coydog, wolf, red fox, gray fox,
fisher cat, raccoon, deer, wild turkey, opossum, ...
Do NOT constrain yourself to a fixed enum.
```

## species_hint forwarding

`build_animal_prompt(species_hint=None)` accepts an optional YOLO label. When supplied, it's rendered into the prompt body as a "hint" but with the override language still leading. The model is told what YOLO said but explicitly authorized to disagree.

```python
def build_animal_prompt(species_hint: str | None = None) -> str:
    if species_hint:
        hint_block = (
            f"\nThe motion-gate classifier (YOLO) suggested: '{species_hint}'.\n"
            f"This is a HINT, not a constraint — your species call OVERRIDES it.\n"
        )
    else:
        hint_block = "\nNo upstream classifier hint available — classify from scratch.\n"
    return ANIMAL_PROMPT_TEMPLATE_FORMAT.format(
        schema_json=ANIMAL_SCHEMA_JSON,
        species_hint_block=hint_block,
    )
```

## Matcher contract

```python
@dataclass
class AnimalMatchVerdict:
    matched_id: str           # e.g. "coyote_alpha"
    species: str              # normalized
    score: float              # 0.55+ default, 0.65+ if unsure
    reasons: dict[str, float] # per-bucket scores
    is_unsure: bool           # True if species_confidence == "unsure"

@dataclass
class AnimalNoMatch:
    reason: str  # "unknown_species" | "below_threshold" | "species_filter_no_candidates" | ...
    species: str | None
    score: float
    reasons: dict[str, float]

# Weighted ensemble (sum = 1.0)
WEIGHTS = {
    "distinctive_features": 0.30,
    "coat_primary_color":   0.20,
    "body_size":            0.15,
    "body_build":           0.10,
    "face_details":         0.10,
    "coat_pattern":         0.05,
    "estimated_age":        0.05,
    "sex_signal":           0.05,
}

ANIMAL_MATCH_THRESHOLD = 0.55         # default
ANIMAL_MATCH_THRESHOLD_UNSURE = 0.65  # when species_confidence == "unsure"
```

`distinctive_features` uses set-Jaccard with the canonical tokenizer from `references/tokenizer-for-free-form-text.md`.

## Species normalization

10–30 entries, lives in the matcher (not the prompt). Vision returns free-form; matcher applies the dict.

```python
SPECIES_NORMALIZATION = {
    "coyote": "coyote",
    "eastern coyote": "coyote",
    "coy-wolf hybrid": "coydog",
    "coydog": "coydog",
    "wolf": "wolf",
    "gray wolf": "wolf",
    "red fox": "fox",
    "gray fox": "fox",
    "fox": "fox",
    "dog": "dog",
    "raccoon": "raccoon",
    "deer": "deer",
    "white-tailed deer": "deer",
    "fisher": "fisher",
    "fisher cat": "fisher",
    "wild turkey": "wild turkey",
    "opossum": "opossum",
    # ... grows on demand when production data surfaces a new variant
}
```

The dict is intentionally small. New entries are added when production logs show a variant the matcher is treating as unknown (`species_filter_no_candidates` reason). Don't pre-populate.

## Test patterns

The test files for this pipeline are good templates for the next one. Highlights:

- `test_schema_is_a_nonempty_string_with_required_keys` — string-content checks, NOT `json.loads`. Origin: `jill-workflow-style/references/session-corrections-2026-08-30.md` lesson F.
- `test_prompt_contains_yolo_override_language` — assert "OVERRIDES the YOLO" / "trust your eyes" appear in the rendered prompt.
- `test_build_animal_prompt_accepts_species_hint` — verify the hint kwarg is forwarded and rendered.
- `test_distinct_individuals_emphasis_in_prompt` — verify the "1-5 short strings" guidance + examples appear.
- `test_raised_threshold_rejects_match_in_band` — hand-tuned vision scoring IN the band (0.63) between default (0.55) and raised (0.65). Origin: lesson G.

## How to mirror for the next pipeline

1. Pick the open-class field (species, color, behavior, ...). It MUST be free-form.
2. Pick the structured hedge field (`*_confidence ∈ {definite, likely, unsure}`). It MUST be separate from the species field.
3. Pick the individual-ID array field (`distinctive_features`, `clothing_marks`, ...). It MUST be a 1-5 string array with explicit "what makes it distinctive" guidance.
4. Write the prompt with override language + free-form guidance + individual-ID guidance.
5. Write the matcher with weighted ensemble + normalization dict + tokenizer.
6. Write tests with the 4 patterns above.

The `infra/animal_*` files are the canonical reference for shape. `infra/person_*` is the same shape (older contract, less rich — but the playbook applies).
