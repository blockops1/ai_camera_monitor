# VLM + structured JSON: Qwen3-VL on llama.cpp recipe

Worked recipe combining a vision-language model with schema-constrained JSON output, semantic consistency validation, and one corrective retry. Verified pattern from a production-style extraction pipeline (Qwen3-VL family, llama.cpp server, multimodal projector file).

## Server bring-up

```bash
llama-server \
  --hf-repo Qwen/Qwen3-VL-8B-Instruct-GGUF \
  --hf-file <quant>.gguf \
  --mmproj-url-or-path <mmproj-filename>.gguf \
  --host 0.0.0.0 --port 8080
```

The `mmproj-*.gguf` projector file is **separate** from the main model file. Forgetting it makes the server accept text-only requests and silently reject images.

## Request shape

```json
{
  "model": "Qwen3-VL",
  "messages": [{
    "role": "user",
    "content": [
      {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,..."}},
      {"type": "text", "text": "<prompt below>"}
    ]
  }],
  "response_format": {
    "type": "json_schema",
    "json_schema": {
      "name": "scene_analysis",
      "strict": true,
      "schema": { /* see below */ }
    }
  },
  "temperature": 0,
  "max_tokens": 2048
}
```

## Schema pattern (distilled)

Critical rules that apply to any VLM-extraction schema:

- Every field `required`, `additionalProperties: false`.
- For "presence vs. uncertainty", use explicit enum values — `"none"` (not present) and `"unknown"` (present but not determinable). Avoid `null` for these cases.
- For colors, use a closed enum of common values + `"other"` + `"unknown"` + `"none"`.

```json
{
  "type": "object",
  "properties": {
    "scene_description": {"type": "string"},
    "vehicle_present": {"type": "boolean"},
    "colors": {
      "type": "object",
      "properties": {
        "vehicle": {
          "type": "string",
          "enum": ["black","white","gray","silver","red","blue",
                   "green","yellow","brown","orange","other",
                   "unknown","none"]
        },
        "clothing_primary":   {"type": ["string","null"]},
        "clothing_secondary": {"type": ["string","null"]},
        "other":              {"type": ["string","null"]}
      },
      "required": ["vehicle","clothing_primary","clothing_secondary","other"],
      "additionalProperties": false
    },
    "objects_detected": {
      "type": "array",
      "items": {"type": "string"}
    }
  },
  "required": ["scene_description","vehicle_present","colors","objects_detected"],
  "additionalProperties": false
}
```

## Prompt (observation-first)

```text
Analyze the camera frame and return exactly one JSON object.

1. Inspect all visible vehicles and determine each visually supported color.
2. Write scene_description in 2–3 factual sentences.
3. Populate every structured field from the SAME observations.
4. Before answering, silently verify: every vehicle/color mentioned in the
   description agrees with vehicle_present, colors.vehicle, and objects_detected.

Rules:
- colors.vehicle is REQUIRED and never null.
- Use "none" only when no vehicle is visible.
- Use "unknown" only when a vehicle is visible but color cannot be determined.
- If a blue car is mentioned, colors.vehicle must be "blue" and objects_detected
  must include "car".
- Output JSON only; no analysis, preamble, or Markdown.
```

Adding 2–4 image-independent few-shot examples (positive, ambiguous, negative) further reduces omissions. Do **not** ask for free-form chain-of-thought before JSON; it leaks outside the envelope.

## Sampling

For deterministic extraction: `temperature: 0`. Constrained decoding guarantees shape, not factual content; sampling variance still affects values. Compare against the model's official chat defaults (`temperature=0.7, top_p=0.8, top_k=20` for Qwen3-VL VL mode) — those are tuned for general chat, not extraction.

## Semantic consistency gate (Python + Pydantic)

```python
from pydantic import BaseModel, Field, ValidationError

class Colors(BaseModel):
    vehicle: str
    clothing_primary: str | None
    clothing_secondary: str | None
    other: str | None

class SceneAnalysis(BaseModel):
    scene_description: str
    vehicle_present: bool
    colors: Colors
    objects_detected: list[str]

VEHICLE_KEYWORDS = {"car","truck","van","bus","motorcycle","suv"}
COLOR_WORDS = {"red","blue","green","yellow","white","black","gray","silver",
               "orange","brown","purple","pink"}

def validate_semantics(s: SceneAnalysis) -> list[str]:
    errors = []
    desc = s.scene_description.lower()
    if not s.vehicle_present and s.colors.vehicle not in ("none",):
        errors.append("vehicle_present is false but colors.vehicle is not 'none'")
    if s.vehicle_present and s.colors.vehicle == "none":
        errors.append("vehicle_present is true but colors.vehicle is 'none'")
    vehicle_in_desc = any(k in desc for k in VEHICLE_KEYWORDS)
    if vehicle_in_desc and not s.vehicle_present:
        errors.append("description mentions a vehicle but vehicle_present is false")
    if any(f"{c} {k}" in desc or f"{c}car" in desc or f"{c}-colored" in desc
           for c in COLOR_WORDS for k in VEHICLE_KEYWORDS):
        # crude color-mention check — refine per domain
        if s.colors.vehicle == "unknown":
            errors.append("description mentions a colored vehicle but colors.vehicle is 'unknown'")
    return errors
```

## Targeted retry with corrective feedback

```python
messages.append({"role": "assistant", "content": raw_output})
messages.append({
  "role": "user",
  "content": (
    "Your previous output failed validation:\n"
    + "\n".join(f"- {e}" for e in errors)
    + "\nRe-inspect the image, correct all fields, and output one JSON object only."
  )
})
# Re-issue the chat completion request; cap at 1–2 retries.
```

Pass the failed output + a short, specific error message. Naive parse-error retry without feedback repeats the same mistake.

## When to split into two calls

Try one call first. If cross-field consistency is still inadequate after 1–2 retries, split:

- **Call A** (constrained): image → visual facts (`vehicle_present`, type, color, clothing, objects).
- **Call B** (unconstrained or constrained): facts → final object including `scene_description` written from the facts.

This separates perception from prose and makes the facts auditable. Not usually needed for simple extraction.

## Notes on `response_format.type=json_schema` with llama.cpp

- The schema is converted to GBNF; some keywords are silently dropped. **Test the compiled grammar** by running representative inputs through `/v1/chat/completions` and confirming the output matches expected shape. Unsupported: complex `if/then/else`, nested `$ref`, deep `anyOf`.
- The schema is **not** injected into the prompt — it only constrains token selection. Always describe fields in natural language.
- `strict: true` mirrors the OpenAI flag name; llama.cpp's underlying behavior is still "convert JSON Schema → GBNF → mask tokens".

## Verifying the schema before production

Before shipping, hit the running `llama-server` with 10–20 representative images and:

1. Confirm every output parses as JSON.
2. Confirm every required key is present in every output.
3. Run the semantic validator; the failure rate should drop to single-digit percent.
4. Spot-check 5 outputs manually for factual correctness against the image.

## Sources

- llama.cpp server README — `response_format` and multimodal endpoints
- llama.cpp grammars README — JSON Schema → GBNF subset, dropped keywords
- Qwen3-VL model card — recommended VL sampling defaults
- OpenAI Structured Outputs guide — strict schema adherence and best practices