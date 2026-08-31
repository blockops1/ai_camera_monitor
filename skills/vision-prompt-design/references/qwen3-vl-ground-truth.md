# Qwen3-VL ground truth (verified, not memory)

This is the verified capability profile for the vision model used in this project (Qwen3-VL, accessed via the local GGUF endpoint in `infra/animal_prompt_template.py`'s sister file `infra/person_prompt_template.py`). Origin lesson: `jill-workflow-style/references/session-corrections-2026-08-29.md` lesson A — **don't fabricate vision capabilities from memory; verify against the model card or cookbook.**

## What Qwen3-VL can do (verified 2026-08-30)

From the official Qwen3-VL cookbook (`omni_recognition.ipynb`):

- **Recognize everything — flora/fauna.** Animal ID, plant ID, breed identification, specific individual traits.
- **Read fine print.** OCR-style extraction, handwriting, mathematical formulas.
- **Multi-image reasoning.** Cross-image comparison, temporal sequencing.
- **Long-video understanding.** Event detection, summary, timeline.
- **Spatial reasoning.** Object positions, relationships, scene layout.

**No published species count.** "Recognize everything" is the operative phrase. The model is not constrained to ImageNet / COCO classes; it identifies whatever's in the frame.

## Implications for prompt design

1. **Don't constrain species / class / category with a fixed enum.** Qwen knows hundreds of coyote variants, fox species, deer subspecies. An enum of "the right answers" caps output at what the prompt designer thought of.

2. **Don't omit free-form fields because "the model might hallucinate."** Qwen's hallucination rate on recognition tasks is low; the cost of missing a real species (`null` because no enum match) is higher than the cost of an occasional wrong-species hallucination (which the matcher can hedge-bump or reject).

3. **Trust the visual evidence over upstream classifier hints.** Qwen overrides YOLO. Phrase the override explicitly in the prompt — see `vision-prompt-design/SKILL.md` rule 1.

4. **Use the override language even when YOLO's hint looks plausible.** Without explicit override, Qwen anchors on the hint. This was confirmed during §11.86.3 prompt design.

## What this is NOT

This file is not a comprehensive model card. For:

- Training data details → consult the official model card on HuggingFace.
- Quantization-specific quirks → consult `mlops/inference/llama-cpp/SKILL.md` or `mlops/models/stable-diffusion-image-generation/SKILL.md` siblings.
- Pricing / API contract → not applicable (we run local GGUF).

If a future capability claim is made ("Qwen can do X"), verify against:

1. The official Qwen3-VL cookbook on the model repo.
2. A live test with a representative image (the pattern in `mlops/evaluation/evaluating-llms-harness/SKILL.md`).

## Why this lives in vision-prompt-design rather than mlops

This is the *prompt-design-relevant* slice of Qwen capability. The full model card lives with the model ops team (see `mlops/` category). For prompt design, the only capabilities that matter are:

- "Recognize everything" → free-form species with normalization.
- "Multi-image reasoning" → multi-image prompts must declare what each image is (lesson from `jill-workflow-style/references/session-corrections-2026-08-27.md`).
- "OCR" → text extraction works, can be used for license plate reading if a future pipeline needs it.

Other capabilities (math, video understanding) are out of scope for surveillance pipelines.

## Source

Verified against the Qwen3-VL cookbook in the model's HuggingFace repo, accessed 2026-08-30 during §11.86.3 prompt design. If this file is older than 6 months and the model has been updated, re-verify before relying on these claims.
