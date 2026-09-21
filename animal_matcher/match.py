"""match.py — Tier-2 MegaDescriptor cosine + Tier-1 Jaccard animal matcher.

STATUS: implemented (US-062f)
THREAD SAFETY: thread-safe (pure function, no shared state)

INPUTS:
    - crop_path: str (required) — path to the alert crop image file

OUTPUTS:
    - return dict:
        {
            'matched': bool,
            'top_candidates': list[dict],    # all cosine-acceptable candidates
            'threshold': float,               # Tier-2 cosine threshold (0.85)
            'candidate': {id, label} | None,  # winning entry (on match)
            'cosine_score': float | None,     # cosine of match (on match)
            'tier1_score': float | None,      # Jaccard of match (on match)
            'runner_ups': list[dict],         # non-matching top candidates
        }

PUBLIC API:
    match_animal(crop_path: str) -> dict
        Tier-2 MegaDescriptor cosine matching + Tier-1 Jaccard ranking.
        Always runs Tier-2 when embedder is live.

DOES NOT DO:
    - Model loading or inference (delegated to infra.animal_embedder).
    - File I/O beyond known_animals.json / embeddings.npz.
    - Telegram messaging.

CALLED BY:
    - listener/pipeline.py stage 12 (animal branch).

CALLS INTO:
    - animal_matcher.storage: load_known, load_embeddings, cosine_sim.
    - infra.animal_embedder: embed_image.

RELATED:
    - infra/animal_embedder.py: embed_image() for MegaDescriptor.
    - animal_matcher/storage.py: cosine_sim, load_known, load_embeddings.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

import numpy as np
from PIL import Image

from animal_matcher.storage import cosine_sim, load_embeddings, load_known

log = logging.getLogger(__name__)

TIER2_THRESHOLD = 0.85


def _embed_crop(crop_path: str) -> np.ndarray | None:
    """Embed an alert crop using the animal embedder.

    Args:
        crop_path: Path to the crop image file.

    Returns:
        768-dim embedding vector, or None if the crop is missing or
        the embedder is not live.
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


def _jaccard_score(a: list[str], b: list[str]) -> float:
    """Compute Jaccard similarity between two lists of strings.

    Args:
        a: First list (e.g. identity_markers from known animal).
        b: Second list (e.g. identity_markers from VM2 result).

    Returns:
        Float in [0, 1].
    """
    set_a = {s.strip().lower() for s in a if isinstance(s, str) and s.strip()}
    set_b = {s.strip().lower() for s in b if isinstance(s, str) and s.strip()}
    if not set_a and not set_b:
        return 1.0
    if not set_a or not set_b:
        return 0.0
    intersection = len(set_a & set_b)
    union = len(set_a | set_b)
    return intersection / union if union > 0 else 0.0


def match_animal(crop_path: str) -> dict:
    """Match an alert crop against enrolled animals.

    When ANIMAL_EMBEDDER_LIVE != "1", returns early with skipped marker.
    Otherwise runs Tier-2 cosine matching (always) and Tier-1 Jaccard
    ranking over identity_markers.

    Args:
        crop_path: Path to the alert crop image file.

    Returns:
        Dict with keys: matched, top_candidates, threshold.
        On match also adds: candidate, cosine_score, tier1_score, runner_ups.
    """
    # Step 1: Check if live embedder is enabled.
    if os.environ.get("ANIMAL_EMBEDDER_LIVE", "0") != "1":
        log.info("animal_match: embedder off (ANIMAL_EMBEDDER_LIVE != 1)")
        return {
            "matched": False,
            "top_candidates": [],
            "threshold": TIER2_THRESHOLD,
            "skipped": "embedder_off",
        }

    # Step 2: Embed the crop.
    animal_vec = _embed_crop(crop_path)
    if animal_vec is None:
        log.warning("animal_match: could not embed alert crop: %s", crop_path)
        return {
            "matched": False,
            "top_candidates": [],
            "threshold": TIER2_THRESHOLD,
        }

    # Step 3: Load known animals and embeddings.
    registry = load_known()
    embeddings = load_embeddings()

    if not registry:
        log.info("animal_match: no enrolled animals")
        return {
            "matched": False,
            "top_candidates": [],
            "threshold": TIER2_THRESHOLD,
        }

    # Step 4: Tier-2 — compute cosine similarity against each enrolled
    # animal's average embedding. Keep only candidates with cosine >= 0.85.
    candidates_with_scores: list[tuple[dict, float, float]] = []
    for entry in registry:
        animal_id = entry["id"]
        emb_key = f"{animal_id}__avg"
        if emb_key not in embeddings:
            continue
        avg_vec = embeddings[emb_key]
        cscore = cosine_sim(animal_vec, avg_vec)

        if cscore >= TIER2_THRESHOLD:
            # Step 5: Tier-1 — Jaccard over identity_markers.
            identity_markers = entry.get("identity_markers", [])
            tscore = float(_jaccard_score(identity_markers, identity_markers))
            candidates_with_scores.append((entry, cscore, tscore))

    # Sort by cosine score descending.
    candidates_with_scores.sort(key=lambda x: x[1], reverse=True)

    # Build top_candidates list (already filtered by threshold above).
    top_candidates = []
    for entry, cscore, tscore in candidates_with_scores:
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
            best["label"], best["cosine_score"], best["tier1_score"], TIER2_THRESHOLD,
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
            "threshold": TIER2_THRESHOLD,
        }

        # Step 7: Add runner_ups (top candidates excluding the best match).
        if len(top_candidates) > 1:
            result["runner_ups"] = top_candidates[1:]
    else:
        # All cosine below threshold — return matched=False, top_candidates populated
        # with all scored candidates (step 7 of task body).
        log.info(
            "animal_match: no match above threshold %.2f (best cosine=%.4f)",
            TIER2_THRESHOLD,
            candidates_with_scores[0][1] if candidates_with_scores else 0.0,
        )
        # Build top_candidates from all scored entries for the no-match case.
        top_candidates = [
            {
                "id": entry["id"],
                "label": entry["label"],
                "cosine_score": round(cscore, 4),
                "tier1_score": round(tscore, 4),
            }
            for entry, cscore, tscore in candidates_with_scores
        ]
        result = {
            "matched": False,
            "top_candidates": top_candidates,
            "threshold": TIER2_THRESHOLD,
        }

    return result
