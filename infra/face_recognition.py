"""
face_recognition.py — Face detection + identification from a single frame.

Lifted 2026-08-22 from ~/ai_camera_monitor/src/face_recognition.py
(archived at ~/archive/2026-08-22-phase6b106-prelift-archive/) for
Phase 6B.106 (person-gatekeeper tier). Logic is byte-identical; only
the import structure changes (bare `import faces` → `from infra import faces`).

STATUS: stable
THREAD SAFETY: single-threaded for model load (lazy singleton),
    thread-safe for recognize_faces() calls (model is read-only after init)

INPUTS:
    - frame: np.ndarray | str (file path) | PIL.Image — single frame
    - function arg _get_app (internal): the cached FaceAnalysis instance
    - identity JSON files at infra.paths.IDENTITIES_DIR/{slug}.json
    - Face embeddings: 512-dim ArcFace float vectors from buffalo_l model
    - ~/.insightface/models/buffalo_l/ (5 ONNX files, ~340MB total)

OUTPUTS:
    - return value (recognize_faces): dict with shape
      {"faces": [{"bbox", "embedding", "identified_name", "confidence",
       "is_known"}, ...], "identified_person": str | None,
       "best_confidence": float | None}
    - return value (cosine_similarity): float in [-1.0, 1.0]
    - No file writes. No network. No logging (caller logs).
    - First call: ~5 seconds to load model. Subsequent: <100ms.

PUBLIC API:
    MATCH_THRESHOLD: float = 0.4
        Cosine similarity above which an embedding is considered a positive
        ID. ArcFace default; tune only with a real benchmark.
    MIN_BBOX_SIZE: int = 35
        Minimum face bounding box width AND height (pixels). Smaller
        detections are filtered to limit false positives from cluttered
        scenes. Calibrated empirically for our camera distances; only
        change after a live sweep.
    cosine_similarity(a: list[float], b: list[float]) -> float
        Standard cosine = (a · b) / (||a|| * ||b||); clamped to [-1, 1];
        returns 0.0 if either vector is zero (avoids NaN).
    recognize_faces(frame) -> dict
        Detect + identify faces in a single frame. Lazy-loads the model
        on first call. Caller handles audit + identity updates.

DOES NOT DO:
    - Persist identities → infra.faces
    - Capture frames → infra.frame_capture
    - Send Telegram → infra.outbound_telegram
    - Identify non-faces (vehicles, animals) → other modules
    - Load multiple models (buffalo_l only) — switch to buffalo_s for
      higher accuracy only after a benchmark
    - Validate bbox math (clamping, IOU) — that's infra.image_prep
    - Return raw embeddings to disk — caller decides whether to enroll

WHY HERE:
    Lifted from old repo (Phase 6A, 2025) for §11.36 person-gatekeeper.
    Logic unchanged; only `import faces` → `from infra import faces`.
    Old version was gated behind PHASE6A_ENABLED=false in production;
    this lift is the first time it ships active in the refactor.

CALLED BY:
    - listener.person_event_pipeline:match_stage() after Qwen confirms
      face_visible=true; crops face_bbox from infra.image_prep, then
      calls recognize_faces(cropped_frame)
    - scripts/enroll_person.py: interactive enrollment captures N samples,
      averages embeddings, calls infra.faces.save_identity()

CALLS INTO:
    - infra.faces: load_identity(name) to look up enrolled embeddings
    - infra.image_prep: crop_face_region_from_4k() (caller usually does this)
    - insightface.app.FaceAnalysis (lazy import)
    - numpy: embeddings, bbox normalization
    - PIL.Image: frame normalization when input is a file path
"""

from __future__ import annotations

from typing import Any

import numpy as np

# Threshold above which a cosine similarity is considered a positive ID.
# ArcFace thresholds typically range 0.3-0.5; 0.4 is the middle ground and
# matches the default in InsightFace's own FaceAnalysis code.
MATCH_THRESHOLD = 0.4

# Minimum face bounding box (width, height) in pixels to be considered a real
# face. Detector returns false positives on cluttered workshop scenes as
# 30-60px blobs. Empirically calibrated for our camera distances: real faces
# at our camera distances are >=40px in at least one dimension after the
# 4K → small_size downscale. 35px is a conservative floor that catches the
# walk-by case (real faces observed at 40-50px) while rejecting clutter hits.
MIN_BBOX_SIZE = 35


# Module-level cache for the FaceAnalysis instance.
# Lazy-loaded so the heavy model only spins up on first use.
_APP_CACHE: Any | None = None


# ----------------------------------------------------------------------
# Model loading (lazy singleton)
# ----------------------------------------------------------------------


def _get_app():
    """
    Return the cached insightface FaceAnalysis instance, loading on first call.

    InsightFace caches downloaded models at ~/.insightface/models/buffalo_l/
    so the first call downloads ~300MB; subsequent calls reuse the cache.
    """
    global _APP_CACHE
    if _APP_CACHE is None:
        # Imported here so unit tests can patch _get_app() without needing
        # insightface actually importable in CI environments.
        from insightface.app import FaceAnalysis

        _APP_CACHE = FaceAnalysis(name="buffalo_l", providers=["CPUExecutionProvider"])
        _APP_CACHE.prepare(ctx_id=-1, det_size=(640, 640))
    return _APP_CACHE


def _get_app_safe():
    """
    Return the cached FaceAnalysis instance, or None if insightface is missing.

    Used by callers that want to gracefully degrade when insightface is not
    installed (e.g., during cold-venv bootstrapping). recognize_faces() itself
    raises ImportError on missing insightface; this is the lazy probe.
    """
    global _APP_CACHE  # noqa: PLW0602 (assignment happens inside _get_app, not here)
    if _APP_CACHE is not None:
        return _APP_CACHE
    try:
        return _get_app()
    except ImportError:
        return None


def reset_app_cache() -> None:
    """Clear the cached model. For tests that want to reload with different params."""
    global _APP_CACHE
    _APP_CACHE = None


# ----------------------------------------------------------------------
# Frame loading
# ----------------------------------------------------------------------


def _load_frame(frame) -> np.ndarray:
    """
    Normalize the frame input into a numpy uint8 RGB array.

    Accepts:
    - numpy ndarray (returned unchanged if already (H, W, 3) uint8)
    - str / Path (a JPEG/PNG file path; loaded via PIL)
    - PIL.Image.Image (converted to ndarray)
    """
    if isinstance(frame, np.ndarray):
        return frame
    if isinstance(frame, str):
        from PIL import Image

        return np.array(Image.open(frame).convert("RGB"))
    # PIL Image or anything with a .convert() method
    if hasattr(frame, "convert"):
        return np.array(frame.convert("RGB"))
    raise TypeError(f"frame must be ndarray, str, or PIL.Image; got {type(frame)}")


# ----------------------------------------------------------------------
# Similarity
# ----------------------------------------------------------------------


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """
    Cosine similarity between two equal-length numeric vectors.

    Returns 0.0 if either vector is zero (avoids NaN). Otherwise returns the
    standard cosine = (a · b) / (||a|| * ||b||), clamped to [-1, 1] for
    numerical safety.
    """
    a_arr = np.asarray(a, dtype=np.float64)
    b_arr = np.asarray(b, dtype=np.float64)
    norm_a = float(np.linalg.norm(a_arr))
    norm_b = float(np.linalg.norm(b_arr))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    sim = float(np.dot(a_arr, b_arr) / (norm_a * norm_b))
    return max(-1.0, min(1.0, sim))


# ----------------------------------------------------------------------
# Main entry point
# ----------------------------------------------------------------------


def _filter_by_bbox(faces_list: list) -> list:
    """Drop detections smaller than MIN_BBOX_SIZE x MIN_BBOX_SIZE."""
    kept = []
    for face in faces_list:
        bbox = face.bbox  # [x1, y1, x2, y2]
        w = bbox[2] - bbox[0]
        h = bbox[3] - bbox[1]
        if w >= MIN_BBOX_SIZE and h >= MIN_BBOX_SIZE:
            kept.append(face)
    return kept


def _match_one_face(face) -> dict:
    """Try to identify a single face by matching its embedding against enrolled identities.

    Returns a face-info dict in the canonical shape, with identification fields
    populated if a match was found.
    """
    # Lazy import to avoid circular dependency at module load (faces.py
    # imports nothing from us, but this keeps the import graph clean).
    from infra import faces as faces_mod

    embedding = face.normed_embedding if hasattr(face, "normed_embedding") else face.embedding

    identified_name: str | None = None
    confidence: float | None = None

    # Try each enrolled identity. For v1 the enrolled set is small
    # (<10 typical for a property) so a linear scan is fine; if it ever
    # exceeds ~50, swap to a faiss index (faiss.IndexFlatIP).
    for identity_path in faces_mod._iter_identity_paths():
        identity = faces_mod.load_identity_by_path(identity_path)
        if identity is None:
            continue
        emb = identity.get("face_embedding", [])
        if not emb:
            continue
        sim = cosine_similarity(embedding, emb)
        if sim >= MATCH_THRESHOLD and (confidence is None or sim > confidence):
            confidence = sim
            identified_name = identity["name"]

    return {
        "bbox": [float(x) for x in face.bbox],
        "embedding": [float(x) for x in embedding],
        "identified_name": identified_name,
        "confidence": confidence,
        "is_known": identified_name is not None,
    }


def recognize_faces(frame) -> dict:
    """
    Detect + identify all faces in a frame.

    Args:
        frame: numpy ndarray, file path (str), or PIL.Image.

    Returns:
        {
            "faces": [
                {
                    "bbox": [x1, y1, x2, y2],
                    "embedding": [512 floats],
                    "identified_name": str | None,
                    "confidence": float | None,   # cosine sim in [0, 1]
                    "is_known": bool,
                },
                ...
            ],
            "identified_person": str | None,  # best-confidence name across faces
            "best_confidence": float | None,
        }

    Lazy-loads the model on first call (~5s). Subsequent calls <100ms.
    Returns empty lists when no faces are detected.
    """
    app = _get_app()
    img = _load_frame(frame)
    detected = app.get(img)
    filtered = _filter_by_bbox(detected)

    face_results = [_match_one_face(face) for face in filtered]

    # Pick the best-confidence face as the "identified_person"
    best_name: str | None = None
    best_confidence: float | None = None
    for face_info in face_results:
        conf = face_info["confidence"]
        if conf is not None and (best_confidence is None or conf > best_confidence):
            best_confidence = conf
            best_name = face_info["identified_name"]

    return {
        "faces": face_results,
        "identified_person": best_name,
        "best_confidence": best_confidence,
    }
