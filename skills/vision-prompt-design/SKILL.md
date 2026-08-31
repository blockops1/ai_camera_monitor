---
name: vision-prompt-design
description: 'Use when designing or revising a vision-LLM prompt (Qwen3-VL, GPT-4o, Claude-with-vision) that ingests an upstream classifier hint (YOLO, CLIP, custom CNN) and emits structured JSON for downstream matching. Captures the 4-rule playbook: Qwen-overrides-classifier language, hedge-as-separate-field, free-form + thin normalization layer, distinctive_features[] for individual re-ID. Applies to every new pipeline (animal, person, vehicle-face, scene, etc.) that needs structured vision output.'
version: 1
author: Hermes Agent
license: project
metadata:
  hermes:
    tags: ['vision', 'prompt', 'qwen', 'design', 'schema']
    related_skills: ['jill-workflow-style']
---

# vision-prompt-design

Class-level skill for designing prompts that drive a vision LLM to produce structured JSON for downstream matchers. The pattern emerged from §11.86.3 (animal pipeline) and will recur for every new pipeline that needs Qwen (or similar) output. Origin lessons: `jill-workflow-style/references/session-corrections-2026-08-30.md` (lesson E).

## When to use

Trigger this skill when ANY of these conditions hold:

- Writing or revising a prompt template for a vision LLM.
- The prompt schema includes a `species` / `class` / `category` field that maps to an upstream classifier (YOLO, COCO, CLIP).
- Downstream code does set-Jaccard or weighted-similarity matching on vision-emitted strings (distinctive features, clothing, color, behavior).
- The matcher has multiple thresholds (default + hedge-bumped + future variants).
- A new pipeline is being added to the farm-surveillance system (or any system that mirrors vehicle→person→animal→scene).

If the answer is "write a JSON-only prompt and pipe it to the matcher," you need this skill — the playbook below is what makes that pipeline actually work.

## The 4-rule playbook

### Rule 1 — Qwen OVERRIDES the upstream classifier, not the other way around

Upstream classifiers (YOLO, COCO, ImageNet, custom CNNs) are constrained by their training classes. Qwen sees more, knows more, and is the authority. The prompt must say so explicitly with override language.

**Required prompt phrase (use verbatim or close paraphrase):**

```
Your species call OVERRIDES the YOLO hint.
If you see a coyote, say coyote, even if YOLO says dog.
If YOLO says nothing, classify from scratch — don't emit null
just because YOLO was silent.
Trust your eyes over the gate.
```

**Why this matters:** without override language, Qwen anchors on the YOLO hint and reports whatever YOLO said even when visual evidence contradicts it. This was confirmed during §11.86.3 prompt design.

**If upstream is CLIP / COCO / custom CNN:** substitute the actual classifier name in the override phrase. The structure is identical.

### Rule 2 — Hedge confidence as a separate field, not by mutating the species

The vision model emits `species_confidence ∈ {definite, likely, unsure}` as a structured field, separate from `species` itself. The matcher can then apply different thresholds per hedge level.

**Correct shape:**
```json
{
  "species": "coyote",
  "species_confidence": "likely"
}
```

**Wrong shape (hedge collapsed into species):**
```json
{"species": "unknown"}
```

**Why this matters:** the matcher can't apply a relaxed threshold (e.g., §11.86.2 bumps from 0.55 → 0.65 for `unsure`) if the hedge is smudged into the species. With a separate confidence field, the matcher can selectively relax without changing the match logic.

**Standard confidence levels (use these unless you have a strong reason to invent new ones):**
- `definite` — high visual certainty, no hedge.
- `likely` — best call but with caveats (lighting, partial occlusion, similar species).
- `unsure` — model is guessing between plausible alternatives; downstream should apply hedge-bumped threshold.

`null` species with non-null confidence is an error state — surface as `unknown_species` NoMatch.

### Rule 3 — Free-form fields with a thin normalization layer

For open-class fields (species, color, clothing type, scene description, behavior), the prompt asks for free-form text. The matcher applies a small normalization dict at the edge.

**Prompt guidance (the model needs explicit license to vary):**
```
species: free-form string. Use the most specific name you can:
coyote, Eastern coyote, coydog, coy-wolf hybrid, wolf, red fox,
gray fox, fisher cat, raccoon, deer, wild turkey, opossum, ...
Do NOT constrain yourself to a fixed enum.
```

**Matcher normalization (10–30 entries, grows on demand):**
```python
SPECIES_NORMALIZATION = {
    "coyote": "coyote",
    "eastern coyote": "coyote",
    "coy-wolf hybrid": "coydog",
    "coydog": "coydog",
    "red fox": "fox",
    "gray fox": "fox",
    "fisher cat": "fisher",
    "raccoon": "raccoon",
    # ... ~20 entries total
}
```

**Why free-form + thin normalization beats a constrained enum:**

- Qwen knows hundreds of species / colors / behaviors; an enum caps output at what you thought of.
- Forcing Qwen to pick between wrong options (e.g., "is it a fox or a dog?") produces worse matches than letting it say "red fox."
- A 30-entry normalization dict is small, lives in one place, and grows only when a new variant appears in production data.

**Don't** ship a 200-entry normalization table pre-emptively — it caps future model flexibility without proving the new entries are needed. **Don't** omit the normalization layer — free-form strings become a maintenance nightmare when `"Eastern Coyote"`, `"eastern coyote"`, `"Eastern coyote"`, `"coyote (eastern variant)"` are treated as four different species.

### Rule 4 — Distinctive features as a 1-5 string ARRAY for individual re-ID

When the goal includes "distinguish individual X from individual Y" (coyotes, people in low-res crops, marked animals), the schema must have a `distinctive_features: string[]` field with explicit guidance on what makes a feature distinctive.

**Required prompt language:**

```
distinctive_features is an ARRAY of 1-5 short strings describing
attributes that distinguish THIS individual from other members of
the same species. Examples for coyotes:
  - "left ear notched"
  - "white-tipped tail"
  - "scar on right shoulder"
  - "limp in left rear leg"
  - "blue collar"
  - "asymmetric gait"
Empties and generic descriptions ("brown", "medium") are NOT distinctive.
```

**Why explicit examples matter:** without them, vision models return "brown fur" / "medium size" / "appears healthy" — true but useless for individual ID. The prompt must say what makes a feature distinctive.

**The matcher pattern:** set-Jaccard on the array, with hyphen-splitting (white-tipped → {white, tipped}) and stopword filtering. See `references/tokenizer-for-free-form-text.md`.

## Pipeline template

A pipeline that follows this playbook has these files:

| File | Responsibility |
|---|---|
| `infra/<class>_prompt_template.py` | `SCHEMA_JSON` (text-for-LLM, not parseable JSON), `PROMPT_TEMPLATE_FORMAT`, `build_<class>_prompt(**kwargs)` |
| `infra/<class>_matcher.py` | `match_<class>(vision_result, known_<class>) → MatchVerdict \| NoMatch`, normalization dicts, weighted-similarity scoring |
| `infra/tests/test_<class>_prompt_template_*.py` | Schema has required keys (string-content checks, not json.loads); prompt contains override language; hint kwarg is honored |
| `infra/tests/test_<class>_matcher_*.py` | Free-form species + normalization variants; Jaccard features overlap (1/2, 1/3, partial with stopwords); threshold-bumping under `unsure`; unknown-species reason |

For the animal pipeline (`infra/animal_*`), `infra/person_*`, etc., this is the established shape. Mirror it for new pipelines.

## Anti-patterns

1. **Constraining Qwen with an enum of "right answers."** Caps what the model can emit, forces wrong guesses.
2. **Letting Qwen anchor on the upstream classifier.** Always include override language.
3. **Hedging by mutating the species instead of emitting a confidence field.** Loses the ability to apply hedge-bumped thresholds.
4. **Constraining `distinctive_features` to a single string.** Loses individual ID capability.
5. **Returning `null` species when YOLO was silent.** Qwen should classify from scratch; `null` is reserved for truly unknown cases.
6. **Parsing the schema as JSON in tests.** The schema is text-for-Qwen; use string-content checks. Origin lesson: `jill-workflow-style/references/session-corrections-2026-08-30.md` lesson F.
7. **Omitting the stopword + hyphen-split tokenizer in the matcher.** Free-form text matching without these produces inconsistent Jaccard scores. See `references/tokenizer-for-free-form-text.md`.

## Reference files

- `references/qwen3-vl-ground-truth.md` — what Qwen3-VL can do (verified ground truth, not memory). Confirms animal / plant / scene recognition and the "OVERRIDES YOLO" rationale.
- `references/tokenizer-for-free-form-text.md` — canonical tokenizer for distinctive-features-style strings: hyphen-split, stopword filter, edge-punctuation strip. Includes 3 mandatory test patterns.
- `references/animal-prompt-recipe.md` — worked example: §11.86.3 animal prompt from the farm-surveillance tree, showing the playbook applied end-to-end.
