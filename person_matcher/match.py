"""match.py — Tier-2 CLIP-ViT-B/32 cosine + Tier-1 Jaccard person matcher.

STATUS: implemented (US-059d)
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
    match_person_vm2(vm2_result: dict, candidates: list[dict], crop_path: str = "") -> dict
        Tier-2 CLIP cosine matching + Tier-1 Jaccard ranking.
        When crop_path is empty, falls back to tag+Jaccard only (legacy path).
        When crop_path is set, runs full cosine matching.

DOES NOT DO:
    - Model loading or inference (delegated to infra.person_embedder).
    - File I/O beyond known_people.json / embeddings.npz.
    - Telegram messaging.

CALLED BY:
    - listener/pipeline.py stage 12 (person branch).

CALLS INTO:
    - person_matcher.storage: load_known_people, load_person_embeddings, cosine_sim.
    - infra.person_embedder: embed_person_crop.

RELATED:
    - infra/person_embedder.py: embed_person_crop() for CLIP-ViT-B/32.
    - person_matcher/storage.py: cosine_sim, load_known_people, load_person_embeddings.
"""
from __future__ import annotations

import logging
import os

import numpy as np
from PIL import Image

from person_matcher.storage import cosine_sim, load_known_people, load_person_embeddings

log = logging.getLogger(__name__)

TIER2_THRESHOLD = 0.85


def _embed_crop(crop_path: str) -> np.ndarray | None:
    """Embed an alert crop using the person embedder.

    Args:
        crop_path: Path to the crop image file.

    Returns:
        512-dim embedding vector, or None if the crop is missing or
        the embedder is not live.
    """
    if not os.path.isfile(crop_path):
        log.warning("person_match: crop file not found: %s", crop_path)
        return None

    try:
        Image.open(crop_path).convert("RGB")
    except (OSError, Image.UnidentifiedImageError):
        log.warning("person_match: failed to open crop: %s", crop_path)
        return None

    # Lazy import to avoid requiring torch/torchvision in test env.
    try:
        from infra.person_embedder import embed_person_crop
    except ImportError:
        log.warning("person_match: infra.person_embedder not available")
        return None

    vec = embed_person_crop(crop_path)
    if vec.shape != (512,):
        log.warning("person_match: unexpected embedding shape: %s", vec.shape)
        return None
    return vec


def _jaccard_score(a: list[str], b: list[str]) -> float:
    """Compute Jaccard similarity between two lists of strings.

    Args:
        a: First list (e.g. identity_markers from known person).
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


def _match_by_tag(sig: dict, candidates: list[dict]) -> dict | None:
    """Try tag match first (fast path, no embedding cost).

    The first identity_marker from the signature is used as the query tag.
    It must exactly match a candidate's 'tag' (case-insensitive,
    whitespace-stripped).
    """
    markers = sig.get("identity_markers") or []
    if not markers:
        return None
    query_tag = markers[0].strip().lower().replace(" ", "")
    for candidate in candidates:
        cand_tag = candidate.get("tag")
        if cand_tag:
            norm_cand = cand_tag.strip().lower().replace(" ", "")
            if query_tag == norm_cand:
                result = dict(candidate)
                result["matched"] = True
                return result
    return None


def _match_by_jaccard(sig: dict, candidates: list[dict]) -> dict | None:
    """Fallback to identity_markers Jaccard >= 0.5."""
    query_markers = set(sig.get("identity_markers") or [])
    if not query_markers:
        return None
    for candidate in candidates:
        cand_markers = set(candidate.get("identity_markers") or [])
        if cand_markers:
            intersection = query_markers & cand_markers
            union = query_markers | cand_markers
            jaccard = len(intersection) / len(union) if union else 0.0
            if jaccard >= 0.5:
                result = dict(candidate)
                result["matched"] = True
                return result
    return None


def match_person(vm2_result: dict, candidates: list[dict], crop_path: str = "") -> dict:
    """Match a VM2 result against known-person candidates.

    Matching order:
      1. **Tag match** — first identity_marker must exactly match a
         candidate's 'tag' (case-insensitive, whitespace-stripped).
         This is a fast path that avoids the embedding cost.
      2. **Cosine match** (Tier-2) — when crop_path is set and embedder
         is live, embed the crop and run CLIP-ViT-B/32 cosine against
         enrolled person embeddings.
      3. **Jaccard fallback** on identity_markers (threshold >= 0.5).

    Returns the matched candidate dict with 'matched': True appended, or
    {'matched': False} if no candidate satisfies any criterion.
    Returns {'matched': False} immediately if identity_markers is absent.

    When crop_path is set and embedder is live, also returns:
        top_candidates, threshold, candidate, cosine_score, tier1_score, runner_ups

    Args:
        vm2_result: VM2 detail dict with 'identity_markers'.
        candidates: List of known-person dicts (from known_people.json).
        crop_path: Path to the alert crop image (optional, enables cosine).

    Returns:
        Dict with 'matched' key; extra keys on match.
    """
    # Absence of identity_markers -> no match, no raise
    if not vm2_result.get("identity_markers"):
        return {"matched": False}

    # --- Fast path: tag match (no embedding cost) ---
    tag_result = _match_by_tag(vm2_result, candidates)
    if tag_result:
        return tag_result

    # --- Tier-2: CLIP cosine matching (only when crop_path is provided) ---
    if crop_path:
        cosine_result = _match_cosine(vm2_result, candidates, crop_path)
        if cosine_result.get("matched"):
            return cosine_result

        # Cosine ran but no match — return with top_candidates for visibility
        if cosine_result.get("top_candidates"):
            return cosine_result

    # --- Fallback: Jaccard ---
    jaccard_result = _match_by_jaccard(vm2_result, candidates)
    if jaccard_result:
        return jaccard_result

    return {"matched": False}


def _match_cosine(vm2_result: dict, candidates: list[dict], crop_path: str) -> dict:
    """Run Tier-2 CLIP cosine matching.

    When embedder is not live, returns early with skipped marker.
    Otherwise runs Tier-2 cosine matching + Tier-1 Jaccard ranking.

    Args:
        vm2_result: VM2 detail dict (unused here, kept for API consistency).
        candidates: List of known-person dicts.
        crop_path: Path to the crop image to embed.

    Returns:
        Dict with matching result (matched, top_candidates, etc.).
    """
    # Check if live embedder is enabled.
    if os.environ.get("PERSON_EMBEDDER_LIVE", "0") != "1":
        log.info("person_match: embedder off (PERSON_EMBEDDER_LIVE != 1)")
        return {
            "matched": False,
            "top_candidates": [],
            "threshold": TIER2_THRESHOLD,
            "skipped": "embedder_off",
        }

    # Embed the crop.
    person_vec = _embed_crop(crop_path)
    if person_vec is None:
        log.warning("person_match: could not embed alert crop: %s", crop_path)
        return {
            "matched": False,
            "top_candidates": [],
            "threshold": TIER2_THRESHOLD,
        }

    # Load known people and embeddings.
    registry = load_known_people()
    embeddings = load_person_embeddings()

    if not registry:
        log.info("person_match: no enrolled people")
        return {
            "matched": False,
            "top_candidates": [],
            "threshold": TIER2_THRESHOLD,
        }

    # Tier-2: compute cosine similarity against each enrolled person's
    # average embedding. Keep only candidates with cosine >= 0.85.
    candidates_with_scores: list[tuple[dict, float, float]] = []
    for entry in registry:
        person_id = entry["id"]
        emb_key = f"{person_id}__avg"
        if emb_key not in embeddings:
            continue
        avg_vec = embeddings[emb_key]
        cscore = cosine_sim(person_vec, avg_vec)

        if cscore >= TIER2_THRESHOLD:
            # Tier-1: Jaccard over identity_markers.
            identity_markers = entry.get("identity_markers", [])
            query_markers = vm2_result.get("identity_markers", [])
            tscore = float(_jaccard_score(identity_markers, query_markers))
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
            "person_match: MATCH %s (cosine=%.4f, tier1=%.4f, threshold=%.2f)",
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

        # Add runner_ups (top candidates excluding the best match).
        if len(top_candidates) > 1:
            result["runner_ups"] = top_candidates[1:]
    else:
        # All cosine below threshold — return matched=False, top_candidates populated
        # with all scored candidates.
        log.info(
            "person_match: no match above threshold %.2f (best cosine=%.4f)",
            TIER2_THRESHOLD,
            candidates_with_scores[0][1] if candidates_with_scores else 0.0,
        )
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
