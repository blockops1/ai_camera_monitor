"""match.py — Tier-2 MegaDescriptor cosine + Tier-1 ranking animal matcher.

STATUS: implemented (US-045d)
THREAD SAFETY: thread-safe (pure function, no shared state)

INPUTS:
    - vm2_result: dict (required) — VM2 animal analysis output
        Must contain: crop_a_path (str), class (str='animal')
    - candidates: list[dict] (required) — list of known-animal dicts
        (legacy arg, kept for API compat; real candidates loaded from disk)

OUTPUTS:
    - return dict:
        {
            'matched': bool,
            'top_candidates': list[dict],    # all cosine-acceptable candidates
            'threshold': float,               # Tier-2 cosine threshold
        }
      On match: adds 'candidate' key with the winning entry's info.
      On match: adds 'cosine_score', 'tier1_score' keys.

PUBLIC API:
    match_animal(vm2_result, candidates) -> dict
        Tier-2 MegaDescriptor cosine matching + Tier-1 feature ranking.
        Always runs Tier-2 when called.

DOES NOT DO:
    - Model loading or inference (delegated to infra.animal_embedder).
    - File I/O beyond known_animals.json / embeddings.npz.
    - Telegram messaging.

CALLED BY:
    - listener/pipeline.py stage 12 (animal branch).

CALLS INTO:
    - animal_matcher.config: get_tier2_threshold, TIER1_WEIGHTS.
    - animal_matcher.storage: load_known, load_embeddings, cosine_sim.
    - infra.animal_embedder: embed_image (via _embed_crop).

RELATED:
    - infra/animal_embedder.py: embed_image() for MegaDescriptor.
    - listener/pipeline.py: stage_perform_match stage 12 animal branch.
"""
from __future__ import annotations

import logging
import os

import numpy as np
from PIL import Image

from animal_matcher.config import TIER1_WEIGHTS, get_tier2_threshold
from animal_matcher.storage import cosine_sim, load_embeddings, load_known

log = logging.getLogger(__name__)

# Tier-1 matching weights
_W_SPECIES = TIER1_WEIGHTS["species"]
_W_SIZE = TIER1_WEIGHTS["size"]
_W_COLOR = TIER1_WEIGHTS["color"]
_W_DISTINCTIVE = TIER1_WEIGHTS["distinctive"]


def _embed_crop(crop_path: str) -> np.ndarray | None:
    """Embed an alert crop using the animal embedder.

    Args:
        crop_path: Path to the crop image file.

    Returns:
        768-dim embedding vector, or None if the crop is missing.
    """
    if not os.path.isfile(crop_path):
        log.warning("animal_match: crop file not found: %s", crop_path)
        return None

    try:
        img = Image.open(crop_path).convert("RGB")
    except (OSError, Image.UnidentifiedImageError):
        log.warning("animal_match: failed to open crop: %s", crop_path)
        return None

    # Lazy import to avoid requiring torch/torchvision in test env.
    try:
        from infra.animal_embedder import embed_image
    except ImportError:
        log.warning("animal_match: infra.animal_embedder not available")
        return None

    vec = embed_image(img)
    if vec.shape != (768,):
        log.warning("animal_match: unexpected embedding shape: %s", vec.shape)
        return None
    return vec


def _tier1_score(candidate: dict, vm2_result: dict) -> float:
    """Compute a Tier-1 feature-scoring match against the VM2 result.

    Scores are weighted sums of:
    - species match (binary 0 or 1)
    - size match (not implemented, always 0)
    - color jaccard similarity
    - distinctive features jaccard similarity

    Args:
        candidate: Known animal registry entry dict.
        vm2_result: VM2 animal analysis output.

    Returns:
        Float score [0, max_weight_sum].
    """
    score = 0.0
    vm2_class = vm2_result.get("class", "").lower()

    # Species match: VM2 class matches candidate species.
    candidate_species = candidate.get("species", "").lower()
    if vm2_class == candidate_species or (
        vm2_class and candidate_species and vm2_class in candidate_species
    ):
        score += _W_SPECIES

    # Color Jaccard
    vm2_colors = {
        c.lower().strip()
        for c in vm2_result.get("colors", [])
        if isinstance(c, str) and c.strip()
    }
    candidate_colors = {
        c.lower().strip()
        for c in candidate.get("color_patterns", [])
        if isinstance(c, str) and c.strip()
    }
    if candidate_colors:
        intersection = len(vm2_colors & candidate_colors)
        union = len(vm2_colors | candidate_colors)
        jaccard = intersection / union if union > 0 else 0.0
        score += jaccard * _W_COLOR

    # Distinctive features Jaccard
    vm2_features = {
        f.lower().strip()
        for f in vm2_result.get("distinctive_features", [])
        if isinstance(f, str) and f.strip()
    }
    candidate_features = {
        f.lower().strip()
        for f in candidate.get("distinctive_features", [])
        if isinstance(f, str) and f.strip()
    }
    if candidate_features:
        intersection = len(vm2_features & candidate_features)
        union = len(vm2_features | candidate_features)
        jaccard = intersection / union if union > 0 else 0.0
        score += jaccard * _W_DISTINCTIVE

    return score


def match_animal(vm2_result: dict, candidates: list[dict]) -> dict:
    """Tier-2 MegaDescriptor cosine + Tier-1 ranking animal matcher.

    Always runs Tier-2 embedding cosine matching. Candidates are loaded
    from data/known/animals/ (the disk store), not from the legacy
    candidates argument (kept for API compat).

    Args:
        vm2_result: VM2 animal analysis output.
        candidates: Legacy candidates list (ignored; disk store used).

    Returns:
        Dict with keys: matched, top_candidates, threshold.
        On match also adds: candidate, cosine_score, tier1_score.
    """
    threshold = get_tier2_threshold()

    # Load known animals and embeddings from disk.
    registry = load_known()
    embeddings = load_embeddings()

    if not registry:
        log.info("animal_match: no enrolled animals")
        return {"matched": False, "top_candidates": [], "threshold": threshold}

    # Embed the alert crop.
    crop_path = vm2_result.get("crop_a_path", "")
    animal_vec = _embed_crop(crop_path)
    if animal_vec is None:
        log.warning("animal_match: could not embed alert crop")
        return {"matched": False, "top_candidates": [], "threshold": threshold}

    # Tier-2: compute cosine similarity against each enrolled animal's avg.
    candidates_with_scores: list[tuple[dict, float, float]] = []
    for entry in registry:
        animal_id = entry["id"]
        emb_key = f"{animal_id}__avg"
        if emb_key not in embeddings:
            continue
        avg_vec = embeddings[emb_key]
        cscore = cosine_sim(animal_vec, avg_vec)

        # Tier-1: compute feature score.
        tscore = _tier1_score(entry, vm2_result)

        candidates_with_scores.append((entry, cscore, tscore))

    # Sort by cosine score descending.
    candidates_with_scores.sort(key=lambda x: x[1], reverse=True)

    # Filter candidates above threshold for top_candidates.
    top_candidates = []
    for entry, cscore, tscore in candidates_with_scores:
        if cscore >= threshold:
            top_candidates.append({
                "id": entry["id"],
                "label": entry["label"],
                "cosine_score": round(cscore, 4),
                "tier1_score": round(tscore, 4),
            })

    if top_candidates:
        # Match is the highest cosine-score candidate.
        best = top_candidates[0]
        log.info(
            "animal_match: MATCH %s (cosine=%.4f, tier1=%.4f, threshold=%.2f)",
            best["label"], best["cosine_score"], best["tier1_score"], threshold,
        )
        result = {
            "matched": True,
            "candidate": {
                "id": best["id"],
                "label": best["label"],
            },
            "cosine_score": best["cosine_score"],
            "tier1_score": best["tier1_score"],
            "top_candidates": top_candidates,
            "threshold": threshold,
        }
    else:
        # No candidate above threshold — return all scored candidates.
        log.info(
            "animal_match: no match above threshold %.2f (best cosine=%.4f)",
            threshold,
            candidates_with_scores[0][1] if candidates_with_scores else 0.0,
        )
        result = {
            "matched": False,
            "top_candidates": top_candidates,
            "threshold": threshold,
        }

    # Add runner_ups (top candidates excluding the best match).
    if len(top_candidates) > 1:
        result["runner_ups"] = top_candidates[1:]

    return result
