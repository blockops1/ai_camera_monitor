#!/usr/bin/env python3
"""
enroll_person.py — Phase 6B.106 / 6B.163 person enrollment CLI.

Captures 3-5 face samples from a camera and computes the averaged
ArcFace embedding, then optionally extracts 6 stable visual
attributes via Qwen3.6 (Phase 6B.163), and writes a JSON identity
file under data/identities/<slug>.json.

Phase 6B.163 changes:
  - Capture frames from the listener's persistent RTSP ring buffer
    (infra.persistent_rtsp) instead of opening a one-off
    cv2.VideoCapture connection. Falls back to one-off if no reader
    is registered (listener not running).
  - After face capture, optionally send the same crops to Qwen3.6
    via infra.vision_analyzer.analyze_frames(mode="person") and
    extract the 6 stable attributes (silhouette, skin_tone,
    age_range, hair, facial_hair, glasses). Operator confirms or
    edits before saving.

Mirrors the vehicle enrollment pattern (skill: vehicle-enrollment).
Each identity file is one JSON per person; the directory is the
canonical schema for the matcher (infra/person_matcher + infra/faces).

STATUS: provisional (Phase 6B.163; will stabilize after first live
    enroll with stable attributes)
THREAD SAFETY: single-threaded (interactive CLI)

INPUTS:
    - CLI args:
        --name    (required): display name (e.g. "<owner-name>", "<visitor-name>")
        --role    (optional): role string (e.g. "owner", "resident",
                  "contractor"). Defaults to "unknown".
        --samples (optional): number of face samples to capture.
                  Default 3, max 10. More samples → better embedding.
        --camera  (optional): camera IP for RTSP capture. Defaults
                  to CAM1 (<CAM_IP_REDACTED>).
        --rtsp-user / --rtsp-password (optional): RTSP credentials.
                  Required for non-local captures. Loaded from
                  ~/.env via infra.creds by default.
        --no-stable-attrs (optional): skip Qwen3.6 attribute extraction.
                  Use for fast re-enrollments or when Qwen3.6 is
                  unreachable. Default: extract (and confirm).
        --camera-name (optional): canonical camera name (e.g.
                  "CAM1") used to look up the persistent
                  RTSP reader. Defaults to a fixed mapping table; the
                  IP→name lookup can be overridden.

OUTPUTS:
    - JSON identity file at data/identities/<slug>.json with shape:
      {
        "name": "<owner-name>",
        "role": "owner",
        "face_embedding": [512 floats],
        "sample_count": 3,
        "enrolled_at": "2026-08-22T15:00:00-04:00",
        "history": [
          {"ts": "...", "camera": "...", "sample_count": 1, "...": ...},
          ...
        ],
        "stable_attributes": {        <-- Phase 6B.163 (optional)
          "silhouette": {"build": "...", "height": "..."},
          "skin_tone": "...",
          "age_range": "...",
          "hair": {"color": "...", "length": "...", "style": "..."},
          "facial_hair": "...",
          "glasses": "..."
        }
      }
    - log file: logs/enroll_person.log (RotatingFileHandler)

PUBLIC API:
    main() -> int
        Entry point. Returns process exit code.

DOES NOT DO:
    - Auto-install dependencies — user manages venv (per SOUL.md)
    - Touch /tmp — uses the project's data/ + logs/ directories
    - Re-enroll without confirmation — if the identity already exists,
      the user is asked before overwriting
    - Force Qwen3.6 to run when --no-stable-attrs is set — the
      flag means "skip attribute extraction entirely"
    - Auto-restart the listener if no persistent reader is registered;
      fall back to one-off RTSP connection instead

WHY HERE:
    Phase 6B.106 step 10 (initial enrollment); Phase 6B.163 adds
    stable attribute extraction. Lives in scripts/ because it's an
    interactive entry point (not a long-running daemon). Imports
    infra.face_recognition (lazy-loads InsightFace on first use),
    infra.faces (JSON storage), infra.persistent_rtsp (ring buffer),
    infra.vision_analyzer (Qwen3.6 attribute extraction).

CALLED BY:
    - manual invocation: source .venv/bin/activate && python3 scripts/enroll_person.py --name <owner-name>

CALLS INTO:
    - infra.face_recognition: recognize_faces (captures embeddings)
    - infra.faces: save_identity, load_identity, add_enrollment_sample
    - infra.persistent_rtsp: get_reader_for_url (Phase 6B.163)
    - infra.vision_analyzer: analyze_frames(mode="person") (Phase 6B.163)

RELATED:
    - data/identities/ — JSON identity storage
    - infra/face_recognition.py — InsightFace lazy-load wrapper
    - infra/faces.py — identity JSON CRUD
    - infra/persistent_rtsp.py — 24/7 ring buffer (Phase 6B.163)
    - infra/vision_analyzer.py — Qwen3.6 wrapper (Phase 6B.163)
    - infra/person_prompt_template.py — person schema (Phase 6B.163)
    - PLAN §11.85 — Phase 6B.163 design plan
"""

# venv: ai_camera_monitor/.venv
# packages: insightface>=0.7, onnxruntime, numpy, Pillow
# activate before running:  source ai_camera_monitor/.venv/bin/activate

from __future__ import annotations

import argparse
import logging
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

# Phase 6B.183 — ensure repo root is on sys.path so `import infra.*`
# works when this script is invoked as `python3 scripts/enroll_person.py`
# (Python prepends scripts/ to sys.path[0], which does NOT contain infra/).
_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
from logging.handlers import RotatingFileHandler
from pathlib import Path

# Project-local constants (per script-authoring rule 1: known good places)
SCRIPT_NAME = Path(__file__).stem
PROJECT_ROOT = Path("ai_camera_monitor")
EXPECTED_VENV = "ai_camera_monitor/.venv"
LOG_DIR = PROJECT_ROOT / "logs"
LOG_FILE = LOG_DIR / f"{SCRIPT_NAME}.log"
DATA_DIR = PROJECT_ROOT / "data" / "identities"

EDT = timezone(timedelta(hours=-4))

# Phase 6B.167 §13.4 Commit 17 (T3 C17): the hardcoded IP→name table
# is gone. Camera IP/host lookup now goes through
# infra.cameras.by_ip(); canonical CAM{N} code is resolved via
# infra.cameras.code_for(). No operator-flavored values in source.
def _camera_name_by_ip(ip: str) -> str | None:
    """Return the canonical CAM{N} code for `ip`, or None if unknown.

    Phase 6B.167 §13.4: replaces the (deleted) CAMERA_NAME_BY_IP
    dict. Uses infra.cameras.by_ip() which parses the operator's
    legacy env (FRONT_IP, BACK_IP, OUTSIDE_*_IP) via the §13.4
    legacy fallback — no operator-flavored values in source.
    """
    from infra.cameras import by_ip  # §13.4: avoids import at top
    try:
        spec = by_ip(ip)
    except KeyError:
        return None
    return spec.code


def _check_venv() -> None:
    """Rule 4 (script-authoring): refuse to run outside the expected venv.

    InsightFace + onnxruntime only exist in the project venv. Running
    this script under system Python would fail at import time anyway,
    but failing here with a clear message is faster and self-documenting.
    """
    if EXPECTED_VENV not in sys.executable:
        sys.exit(
            f"ERROR: must run inside {EXPECTED_VENV}\n"
            f"  currently: {sys.executable}\n"
            f"  activate:  source {EXPECTED_VENV}/bin/activate\n"
            f"  then:      python3 scripts/{SCRIPT_NAME}.py --name 'Name'"
        )


def _setup_logging() -> logging.Logger:
    """Configure file logger with rotation."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(SCRIPT_NAME)
    logger.setLevel(logging.INFO)
    if logger.handlers:
        return logger
    fh = RotatingFileHandler(LOG_FILE, maxBytes=10 * 1024 * 1024, backupCount=5)
    fh.setFormatter(
        logging.Formatter(
            fmt="[%(asctime)s] [%(name)s] [%(levelname)s] %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S%z",
        )
    )
    logger.addHandler(fh)
    return logger


def _bbox_area(bbox) -> float:
    """Compute bbox area for selecting the largest face."""
    if not bbox or len(bbox) != 4:
        return 0.0
    w = max(0.0, bbox[2] - bbox[0])
    h = max(0.0, bbox[3] - bbox[1])
    area: float = w * h
    return area


def _resolve_rtsp_reader(
    camera_ip: str, rtsp_user: str, rtsp_password: str, logger: logging.Logger
):
    """Phase 6B.163 — resolve a persistent RTSP reader for the given camera.

    Returns (reader, rtsp_url) or (None, rtsp_url) if no reader is
    registered (listener not running). The caller uses the reader when
    present, else falls back to opening a one-off cv2.VideoCapture
    connection.
    """
    rtsp_url = f"rtsp://{camera_ip}:554/h264Preview_01_main"
    if rtsp_user and rtsp_password:
        # Inject creds for get_reader_for_url (it strips creds before match)
        rtsp_url_with_creds = (
            f"rtsp://{rtsp_user}:{rtsp_password}@{camera_ip}:554/h264Preview_01_main"
        )
    else:
        rtsp_url_with_creds = rtsp_url

    try:
        from infra.persistent_rtsp import get_reader_for_url

        reader = get_reader_for_url(rtsp_url_with_creds)
    except ImportError as err:
        logger.warning(f"persistent_rtsp not importable, falling back to one-off: {err}")
        return None, rtsp_url_with_creds

    if reader is None:
        logger.info(f"no persistent reader for {camera_ip}; using one-off RTSP")
    elif reader.is_healthy():
        logger.info(f"using persistent RTSP reader for {camera_ip}")
    else:
        logger.warning(f"persistent reader for {camera_ip} is unhealthy; using one-off")
        reader = None
    return reader, rtsp_url_with_creds


def _capture_samples_from_reader(
    reader,
    n_samples: int,
    logger: logging.Logger,
) -> list[list[float]]:
    """Phase 6B.163 — capture N face samples from the persistent reader.

    For each sample: operator hits Enter (to give them time to position),
    then we ask the reader for the 5 most-recently-decoded frames. We
    run recognize_faces on each frame and pick the largest detected
    face. Stop early if we hit n_samples or the operator aborts.
    """
    try:
        import cv2

        from infra.face_recognition import recognize_faces
    except ImportError as err:
        logger.error(
            f"could not import face_recognition / cv2: {err}. is insightface installed in the venv?"
        )
        sys.exit(2)

    embeddings: list[list[float]] = []
    # Use a per-call temp dir; the reader writes JPEGs there.
    with tempfile.TemporaryDirectory(prefix="enroll_") as tmp:
        for i in range(n_samples):
            print(f"\n--- Sample {i + 1} of {n_samples} ---")
            print("Position your face clearly in frame, then press Enter...")
            try:
                input("Press Enter when ready: ")
            except EOFError:
                logger.warning("input EOF, aborting capture")
                break

            # 5 recent frames from the ring buffer; we'll use the largest face.
            try:
                frame_paths = reader.get_recent_frames(n=5, output_dir=tmp)
            except Exception as e:
                logger.warning(f"get_recent_frames failed: {e}")
                continue

            if not frame_paths:
                logger.warning(f"reader returned no frames for sample {i + 1}")
                continue

            # Run face recognition on each frame; pick the frame with the
            # highest-confidence largest face. Mirrors the one-off flow.
            best_face = None
            best_path = None
            for path in frame_paths:
                try:
                    frame = cv2.imread(path)
                    if frame is None:
                        continue
                    result = recognize_faces(frame)
                    faces = result.get("faces") or []
                    if not faces:
                        continue
                    if len(faces) > 1:
                        face = max(faces, key=lambda f: _bbox_area(f.get("bbox")))
                    else:
                        face = faces[0]
                    confidence = face.get("confidence") or 0.0
                    if best_face is None or confidence > (best_face.get("confidence") or 0.0):
                        best_face = face
                        best_path = path
                except Exception as e:
                    logger.warning(f"recognize_faces failed on {path}: {e}")
                    continue

            if best_face is None:
                logger.warning(
                    f"no face detected across {len(frame_paths)} frames for sample {i + 1}"
                )
                continue
            embedding = best_face.get("embedding")
            if not embedding:
                logger.warning(f"no embedding in best face for sample {i + 1}")
                continue
            embeddings.append(embedding)
            assert best_path is not None  # narrowed by the assignment below
            print(
                f"  → captured (confidence: {best_face.get('confidence'):.2f}, from {Path(best_path).name})"
            )

    return embeddings


def _capture_samples_one_off(
    camera_ip: str,
    n_samples: int,
    logger: logging.Logger,
) -> list[list[float]]:
    """Fallback path (Phase 6B.163): open a one-off RTSP connection.

    Used when no persistent reader is registered (listener not running).
    Same flow as before — operator hits Enter, cv2 grabs a frame.
    """
    try:
        import cv2

        from infra.face_recognition import recognize_faces
    except ImportError as err:
        logger.error(
            f"could not import face_recognition / cv2: {err}. is insightface installed in the venv?"
        )
        sys.exit(2)

    rtsp_url = f"rtsp://{camera_ip}:554/h264Preview_01_main"
    logger.info(f"connecting to RTSP (one-off): {rtsp_url}")
    print(f"Connecting to {rtsp_url} (one-off, listener not running)...")
    cap = cv2.VideoCapture(rtsp_url)
    if not cap.isOpened():
        logger.error(f"could not open RTSP stream: {rtsp_url}")
        sys.exit(1)

    embeddings: list[list[float]] = []
    try:
        for i in range(n_samples):
            print(f"\n--- Sample {i + 1} of {n_samples} ---")
            print("Position your face clearly in frame, then press Enter...")
            try:
                input("Press Enter when ready: ")
            except EOFError:
                logger.warning("input EOF, aborting capture")
                break

            ok, frame = cap.read()
            if not ok or frame is None:
                logger.warning(f"failed to read frame {i + 1}")
                continue

            print("Detecting face...")
            result = recognize_faces(frame)
            faces = result.get("faces") or []
            if not faces:
                logger.warning(f"no face detected in frame {i + 1}")
                continue
            if len(faces) > 1:
                logger.warning(
                    f"{len(faces)} faces detected in frame {i + 1}; using the largest by bbox area"
                )
                face = max(faces, key=lambda f: _bbox_area(f.get("bbox")))
            else:
                face = faces[0]
            embedding = face.get("embedding")
            if not embedding:
                logger.warning(f"no embedding in frame {i + 1}")
                continue
            embeddings.append(embedding)
            print(f"  → captured (confidence: {face.get('confidence'):.2f})")
    finally:
        cap.release()

    return embeddings


def _capture_samples(
    camera_ip: str,
    n_samples: int,
    rtsp_user: str,
    rtsp_password: str,
    logger: logging.Logger,
) -> list[list[float]]:
    """Phase 6B.163 — capture N face samples.

    Tries the persistent RTSP reader first; falls back to a one-off
    cv2.VideoCapture connection if no reader is registered.
    """
    reader, _ = _resolve_rtsp_reader(camera_ip, rtsp_user, rtsp_password, logger)
    if reader is not None:
        return _capture_samples_from_reader(reader, n_samples, logger)
    return _capture_samples_one_off(camera_ip, n_samples, logger)


def _capture_samples_from_crops(
    crops_dir: str,
    logger: logging.Logger,
    similarity_threshold: float = 0.5,
) -> tuple[list[list[float]], list[str]]:
    """Phase 6B.163 — capture face samples from existing JPEG/PNG crops.

    Used when the operator wants to enroll from alert archive (e.g., a
    visitor seen 15 minutes ago whose face is in the saved crops but
    no longer in the live RTSP ring buffer). The from-crops path
    mirrors _capture_samples_from_reader: each crop is run through
    InsightFace, the largest face in each crop wins, and its embedding
    is captured. A per-batch consistency filter rejects faces whose
    embedding is too dissimilar from the others — usually that means
    a reflection, a background figure, or a sticker caught by the
    detector.

    Args:
        crops_dir: path to a directory containing the crops. All .jpg,
            .jpeg, and .png files at the top level are processed in
            sorted order.
        logger: logging.Logger.
        similarity_threshold: minimum pairwise cosine similarity for an
            embedding to be retained (compared to the centroid of the
            surviving set). Faces below this are treated as different
            persons and dropped. Default 0.5 — well below typical
            same-person matches (>=0.7 in ArcFace) but above the
            random-pair baseline (~0.05).

    Returns:
        (embeddings, paths_used). paths_used is a list of crop paths
        that successfully contributed an embedding — useful for the
        history record in the saved identity.
    """
    try:
        import cv2

        from infra.face_recognition import recognize_faces
    except ImportError as err:
        logger.error(
            f"could not import face_recognition / cv2: {err}. is insightface installed in the venv?"
        )
        sys.exit(2)

    crops_path = Path(crops_dir)
    if not crops_path.is_dir():
        logger.error(f"--from-crops path is not a directory: {crops_dir}")
        sys.exit(1)

    crop_files = sorted(
        f for f in crops_path.iterdir()
        if f.is_file() and f.suffix.lower() in {".jpg", ".jpeg", ".png"}
    )
    if not crop_files:
        logger.error(f"no .jpg/.jpeg/.png files found in {crops_dir}")
        sys.exit(1)

    logger.info(f"from-crops: {len(crop_files)} crop(s) in {crops_dir}")
    embeddings: list[list[float]] = []
    paths_used: list[str] = []

    for crop in crop_files:
        frame = cv2.imread(str(crop))
        if frame is None:
            logger.warning(f"could not read crop: {crop}")
            continue
        try:
            result = recognize_faces(frame)
        except Exception as e:
            logger.warning(f"recognize_faces failed on {crop.name}: {e}")
            continue

        faces = result.get("faces") or []
        if not faces:
            logger.warning(f"no face detected in {crop.name}")
            continue
        # Same selection rule as _capture_samples_from_reader:
        # if multiple faces, pick the largest by bbox area.
        if len(faces) > 1:
            face = max(faces, key=lambda f: _bbox_area(f.get("bbox")))
        else:
            face = faces[0]
        embedding = face.get("embedding")
        confidence = face.get("confidence") or 0.0
        if not embedding:
            logger.warning(f"no embedding in best face of {crop.name}")
            continue
        embeddings.append(embedding)
        paths_used.append(str(crop))
        logger.info(f"  captured embedding from {crop.name} (confidence={confidence:.2f})")

    if not embeddings:
        logger.error(
            f"no usable embeddings from {len(crop_files)} crop(s) in {crops_dir}; "
            "check that crops contain a frontal face with eyes/nose/mouth visible"
        )
        sys.exit(1)

    # Consistency filter: drop embeddings that look like a different person.
    # Reflects/relics are usually low-similarity outliers; with 3+ raw
    # detections we can pick the dominant identity. With only 1 raw
    # detection there's no group to compare against, so we keep it
    # (operator is on the hook for "did you point this at the right
    # person?").
    if len(embeddings) >= 3:
        import numpy as np

        arr = np.array(embeddings, dtype=np.float64)
        norms = np.linalg.norm(arr, axis=1, keepdims=True)
        # Zero-norm guard (shouldn't happen for ArcFace output but be defensive)
        norms[norms == 0.0] = 1.0
        arr_n = arr / norms
        centroid = arr_n.mean(axis=0)
        centroid /= max(np.linalg.norm(centroid), 1e-9)
        sims = arr_n @ centroid  # similarity of each embedding to the centroid

        kept_emb: list[list[float]] = []
        kept_paths: list[str] = []
        for emb, path, sim in zip(embeddings, paths_used, sims):
            if sim >= similarity_threshold:
                kept_emb.append(emb)
                kept_paths.append(path)
            else:
                logger.warning(
                    f"dropping {Path(path).name}: similarity to centroid {sim:.3f} "
                    f"< threshold {similarity_threshold} (likely different person)"
                )

        if not kept_emb:
            logger.error(
                f"all {len(embeddings)} detected faces failed consistency check; "
                "the crops may contain multiple different people or no real faces"
            )
            sys.exit(1)

        dropped = len(embeddings) - len(kept_emb)
        if dropped:
            logger.info(
                f"consistency filter kept {len(kept_emb)}/{len(embeddings)} "
                f"embeddings (dropped {dropped} outlier face(s))"
            )
        embeddings = kept_emb
        paths_used = kept_paths

    return embeddings, paths_used


def _average_embeddings(embeddings: list[list[float]]) -> list[float]:
    """Average multiple embeddings into one."""
    if not embeddings:
        return []
    n = len(embeddings[0])
    avg = [0.0] * n
    for emb in embeddings:
        for i, v in enumerate(emb):
            avg[i] += v / len(embeddings)
    return avg


# Phase 6B.163 — stable attribute enum validators (mirror
# infra.person_prompt_template.PERSON_SCHEMA_JSON enums). Used by
# _prompt_for_stable_attributes for CLI confirmation/edit.
_VALID_BUILD = {"slim", "athletic", "average", "stocky", "heavy", None}
_VALID_HEIGHT = {"short", "medium", "tall", None}
_VALID_SKIN_TONE = {"light", "medium", "olive", "dark", None}
_VALID_AGE_RANGE = {"child", "young_adult", "middle_aged", "senior", None}
_VALID_HAIR_COLOR = {"black", "brown", "blonde", "gray", "white", "red", None}
_VALID_HAIR_LENGTH = {"bald", "shaved", "short", "medium", "long", None}
_VALID_HAIR_STYLE = {"straight", "wavy", "curly", None}
_VALID_FACIAL_HAIR = {"clean_shaven", "stubble", "beard", "mustache", "goatee", None}
_VALID_GLASSES = {"none", "prescription", "sunglasses", None}


def _extract_stable_attributes_from_qwen(
    face_frames: list,
    camera_name: str,
    logger: logging.Logger,
) -> dict | None:
    """Phase 6B.163 — send face crops to Qwen3.6 for stable attribute extraction.

    Args:
        face_frames: list of decoded frames (numpy arrays) used for
            face capture. The full frames are sent; Qwen3.6 sees the
            body/face region.
        camera_name: canonical camera name (used in the prompt).

    Returns:
        dict matching the PERSON_SCHEMA_JSON stable_attributes subset, or
        None if Qwen3.6 call failed.
    """
    try:
        import cv2

        from infra.vision_analyzer import analyze_frames
    except ImportError as err:
        logger.warning(f"vision_analyzer not importable: {err}")
        return None

    # Write frames to a temp dir as JPEGs so analyze_frames can read them.
    paths: list[str] = []
    with tempfile.TemporaryDirectory(prefix="enroll_qwen_") as tmp:
        tmp_path = Path(tmp)
        for idx, frame in enumerate(face_frames):
            p = tmp_path / f"frame_{idx:03d}.jpg"
            try:
                cv2.imwrite(str(p), frame)
                paths.append(str(p))
            except Exception as e:
                logger.warning(f"failed to write temp frame {p}: {e}")

        if not paths:
            logger.warning("no frames available for Qwen3.6 extraction")
            return None

        try:
            result = analyze_frames(
                frame_paths=paths,
                camera_name=camera_name,
                mode="person",
            )
        except Exception as e:
            logger.warning(f"analyze_frames(mode='person') failed: {e}")
            return None

    # Extract stable_attributes from the primary person in the result.
    persons = result.get("persons") or []
    if not persons:
        logger.warning("Qwen3.6 returned no persons; cannot extract stable attributes")
        return None
    primary_idx = result.get("primary_person_index", 0)
    if not isinstance(primary_idx, int) or primary_idx < 0 or primary_idx >= len(persons):
        primary_idx = 0
    primary = persons[primary_idx]
    if not isinstance(primary, dict):
        return None

    stable: dict = {}
    silhouette = primary.get("silhouette") or {}
    stable["silhouette"] = {
        "build": silhouette.get("build"),
        "height": silhouette.get("height"),
    }
    stable["skin_tone"] = primary.get("skin_tone")
    stable["age_range"] = primary.get("age_range")
    hair = primary.get("hair") or {}
    stable["hair"] = {
        "color": hair.get("color"),
        "length": hair.get("length"),
        "style": hair.get("style"),
    }
    stable["facial_hair"] = primary.get("facial_hair")
    stable["glasses"] = primary.get("glasses")

    logger.info(f"Qwen3.6 extracted stable attributes: {stable}")
    return stable


def _confirm_or_edit_stable_attributes(
    extracted: dict,
    name: str,
    logger: logging.Logger,
) -> dict | None:
    """Phase 6B.163 — CLI confirm/edit extracted stable attributes.

    For each field, show the extracted value and let the operator press
    Enter to accept, type a new value to override, or 'n' to skip
    attribute extraction entirely.
    """
    print(f"\n--- Stable attributes for {name!r} (Phase 6B.163) ---")
    print("Qwen3.6 extracted these from the captured frames.")
    print("Press Enter to accept, type a new value to override, or 's' to skip extraction.\n")

    silhouette = extracted.get("silhouette") or {}
    final: dict = {"silhouette": {}, "hair": {}}

    def _confirm(label: str, value: str | None, valid: set[str | None]) -> str | None:
        valid_strs = sorted(v for v in valid if v is not None)
        prompt = f"  {label} [{value!r} → {', '.join(valid_strs)} | null]: "
        raw = input(prompt).strip().lower()
        if raw == "":
            return value
        if raw == "s" or raw == "skip":
            return None
        if raw == "null" or raw == "n":
            return None
        if raw in valid_strs:
            return raw
        print(f"    ⚠ '{raw}' not in valid set; keeping extracted value {value!r}")
        return value

    final["silhouette"]["build"] = _confirm(
        "silhouette.build", silhouette.get("build"), _VALID_BUILD
    )
    final["silhouette"]["height"] = _confirm(
        "silhouette.height", silhouette.get("height"), _VALID_HEIGHT
    )
    final["skin_tone"] = _confirm("skin_tone", extracted.get("skin_tone"), _VALID_SKIN_TONE)
    final["age_range"] = _confirm("age_range", extracted.get("age_range"), _VALID_AGE_RANGE)

    hair = extracted.get("hair") or {}
    final["hair"]["color"] = _confirm("hair.color", hair.get("color"), _VALID_HAIR_COLOR)
    final["hair"]["length"] = _confirm("hair.length", hair.get("length"), _VALID_HAIR_LENGTH)
    final["hair"]["style"] = _confirm("hair.style", hair.get("style"), _VALID_HAIR_STYLE)

    final["facial_hair"] = _confirm("facial_hair", extracted.get("facial_hair"), _VALID_FACIAL_HAIR)
    final["glasses"] = _confirm("glasses", extracted.get("glasses"), _VALID_GLASSES)

    logger.info(f"operator-confirmed stable attributes: {final}")
    return final


def _prompt_stable_attributes_from_scratch(
    name: str,
    logger: logging.Logger,
) -> dict | None:
    """Phase 6B.163 — operator-driven stable-attribute entry (no Qwen3.6).

    Used when enrolling from crops where we have no fresh frames to feed
    Qwen3.6. The operator types each field (with empty/skip supported).
    Mirrors the same enum validation as _confirm_or_edit_stable_attributes.
    """
    print(f"\n--- Stable attributes for {name!r} (operator entry, no Qwen3.6) ---")
    print("Type a value, 's' to skip that field, or '?' to see valid options.\n")

    final: dict = {"silhouette": {}, "hair": {}}

    def _pick(label: str, valid: set[str | None]) -> str | None:
        valid_strs = sorted(v for v in valid if v is not None)
        prompt = f"  {label} [{', '.join(valid_strs)} | skip]: "
        while True:
            raw = input(prompt).strip().lower()
            if raw == "" or raw == "s" or raw == "skip":
                return None
            if raw == "?":
                print(f"    valid: {', '.join(valid_strs)}")
                continue
            if raw in valid_strs:
                return raw
            print(f"    ⚠ '{raw}' not in valid set; try again ('?' for options)")

    final["silhouette"]["build"] = _pick("silhouette.build", _VALID_BUILD)
    final["silhouette"]["height"] = _pick("silhouette.height", _VALID_HEIGHT)
    final["skin_tone"] = _pick("skin_tone", _VALID_SKIN_TONE)
    final["age_range"] = _pick("age_range", _VALID_AGE_RANGE)
    final["hair"]["color"] = _pick("hair.color", _VALID_HAIR_COLOR)
    final["hair"]["length"] = _pick("hair.length", _VALID_HAIR_LENGTH)
    final["hair"]["style"] = _pick("hair.style", _VALID_HAIR_STYLE)
    final["facial_hair"] = _pick("facial_hair", _VALID_FACIAL_HAIR)
    final["glasses"] = _pick("glasses", _VALID_GLASSES)

    # Treat "operator skipped every field" the same as the Qwen path's
    # "skip extraction entirely" — return None so identity is saved
    # without a stable_attributes key.
    all_values = [
        final["silhouette"]["build"], final["silhouette"]["height"],
        final["skin_tone"], final["age_range"],
        final["hair"]["color"], final["hair"]["length"], final["hair"]["style"],
        final["facial_hair"], final["glasses"],
    ]
    if all(v is None for v in all_values):
        logger.info(f"operator skipped all stable attributes for {name!r}")
        return None

    logger.info(f"operator-entered stable attributes: {final}")
    return final


def main() -> int:
    _check_venv()
    parser = argparse.ArgumentParser(
        description="Enroll a person via ArcFace face samples (and Qwen3.6 stable attributes).",
    )
    parser.add_argument(
        "--name",
        required=True,
        help='Display name (e.g. "<owner-name>", "<visitor-name>")',
    )
    parser.add_argument(
        "--role",
        default="unknown",
        help='Role label (e.g. "owner", "resident", "contractor")',
    )
    parser.add_argument(
        "--samples",
        type=int,
        default=3,
        help="Number of face samples (default 3, max 10)",
    )
    parser.add_argument(
        "--camera",
        default="<CAM_IP_REDACTED>",
        help="Camera IP for RTSP capture (default CAM1 = <CAM_IP_REDACTED>)",
    )
    parser.add_argument(
        "--camera-name",
        default=None,
        help="Canonical camera name (e.g. 'CAM1') for Qwen3.6 prompt. "
        "Default: looked up from CAMERA_NAME_BY_IP.",
    )
    parser.add_argument(
        "--rtsp-user",
        default=os.environ.get("RTSP_USER", ""),
        help="RTSP username (default: RTSP_USER env or empty)",
    )
    parser.add_argument(
        "--rtsp-password",
        default=os.environ.get("RTSP_PASSWORD", ""),
        help="RTSP password (default: RTSP_PASSWORD env or empty)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing identity without prompting",
    )
    parser.add_argument(
        "--no-stable-attrs",
        action="store_true",
        help="Skip Qwen3.6 stable-attribute extraction (Phase 6B.163). "
        "Default: extract attributes from face crops.",
    )
    parser.add_argument(
        "--from-crops",
        default=None,
        help="Enroll from existing alert crops instead of live RTSP capture. "
        "Argument is the directory containing the .jpg/.jpeg/.png crop files. "
        "Useful when the person is no longer in the live ring buffer "
        "(persistent RTSP ring buffer is ~10s; alert crops persist on disk). "
        "Qwen3.6 stable-attribute extraction is skipped in this mode "
        "(no fresh frames available); the operator is prompted for each "
        "field instead.",
    )
    parser.add_argument(
        "--source-alerts",
        default="",
        help="Comma-separated alert IDs that produced the crops in --from-crops. "
        "Recorded in the identity history for traceability. Example: "
        "--source-alerts f1e61217-a2b2-4989-bcbf-34e31938344d,eb17b3ef-c68e-4c30-aabc-649cd05e94c4",
    )
    args = parser.parse_args()

    logger = _setup_logging()
    logger.info(
        f"starting: name={args.name!r} role={args.role!r} "
        f"samples={args.samples} camera={args.camera} "
        f"no_stable_attrs={args.no_stable_attrs}"
    )

    # Validate inputs
    if args.samples < 1 or args.samples > 10:
        parser.error("--samples must be between 1 and 10")

    # Resolve camera_name for Qwen3.6 prompt (Phase 6B.163).
    # §13.4 Commit 17 (T3 C17): legacy CAMERA_NAME_BY_IP dict removed;
    # IP→code lookup now goes through infra.cameras.by_ip().
    if args.camera_name:
        camera_name = args.camera_name
    else:
        camera_code = _camera_name_by_ip(args.camera)
        camera_name = camera_code or args.camera  # fallback to literal IP

    # Check for existing identity (don't overwrite without confirmation)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    from infra.faces import load_identity, save_identity

    existing = load_identity(args.name)
    if existing is not None and not args.force:
        print(f"\nIdentity for {args.name!r} already exists.")
        print(f"  enrolled_at: {existing.get('enrolled_at')}")
        print(f"  sample_count: {existing.get('sample_count')}")
        confirm = input("Overwrite? [y/N]: ").strip().lower()
        if confirm != "y":
            print("Aborted.")
            logger.info(f"aborted: existing identity for {args.name!r}")
            return 1

    # Capture samples (Phase 6B.163: uses persistent reader when available;
    # --from-crops branch reuses alert-archive crops instead of live RTSP).
    sample_paths: list[str] = []
    if args.from_crops:
        embeddings, sample_paths = _capture_samples_from_crops(
            args.from_crops,
            logger,
        )
        # In from-crops mode the camera arg is metadata, not a live source.
        # Override it with whatever the operator passed via --camera-name
        # (or "(archive)" if absent) so history reflects reality.
        if args.camera_name:
            history_camera = args.camera_name
        else:
            history_camera = f"(archive from {args.from_crops})"
    else:
        embeddings = _capture_samples(
            args.camera,
            args.samples,
            args.rtsp_user,
            args.rtsp_password,
            logger,
        )
        history_camera = args.camera

    if not embeddings:
        print("No embeddings captured; enrollment failed.")
        logger.error(f"no embeddings captured for {args.name!r}")
        return 1

    # Average embeddings (Phase6A convention — averaging multiple
    # samples improves match accuracy)
    averaged = _average_embeddings(embeddings)
    if len(averaged) != 512:
        logger.warning(f"averaged embedding has {len(averaged)} dims (expected 512)")

    # Build identity record
    now_iso = datetime.now(EDT).isoformat(timespec="seconds")
    history_entry: dict = {
        "ts": now_iso,
        "camera": history_camera,
        "sample_count": len(embeddings),
    }
    if args.from_crops:
        history_entry["event"] = "from_crops_enroll"
        history_entry["crops_dir"] = args.from_crops
        history_entry["sample_files"] = [Path(p).name for p in sample_paths]
        if args.source_alerts:
            history_entry["source_alerts"] = [
                s.strip() for s in args.source_alerts.split(",") if s.strip()
            ]
    identity = {
        "name": args.name,
        "role": args.role,
        "face_embedding": averaged,
        "sample_count": len(embeddings),
        "enrolled_at": now_iso,
        "history": [history_entry],
    }

    # Phase 6B.163 — extract stable attributes from the same crops.
    # We re-grab recent frames from the reader (or one-off connection)
    # to feed Qwen3.6; this is a separate call from face capture.
    if not args.no_stable_attrs:
        if args.from_crops:
            # From-crops path: no fresh frames to feed Qwen. Prompt the
            # operator for each stable-attribute field directly. They
            # can still 'skip' to leave the identity without
            # stable_attributes.
            entered = _prompt_stable_attributes_from_scratch(args.name, logger)
            if entered is not None:
                identity["stable_attributes"] = entered
            else:
                print("Operator skipped all stable attributes.")
                logger.info(
                    f"operator skipped stable-attribute entry for {args.name!r}"
                )
        else:
            try:
                import cv2

                # Grab one fresh frame from the same source we used for capture
                reader, _ = _resolve_rtsp_reader(
                    args.camera,
                    args.rtsp_user,
                    args.rtsp_password,
                    logger,
                )
                face_frames: list = []
                if reader is not None:
                    with tempfile.TemporaryDirectory(prefix="enroll_attr_") as tmp:
                        try:
                            fps = reader.get_recent_frames(n=3, output_dir=tmp)
                            for fp in fps:
                                frame = cv2.imread(fp)
                                if frame is not None:
                                    face_frames.append(frame)
                        except Exception as e:
                            logger.warning(f"failed to grab frames for Qwen3.6: {e}")
                else:
                    # Fallback: open one-off, grab 3 frames
                    cap = cv2.VideoCapture(
                        f"rtsp://{args.rtsp_user}:{args.rtsp_password}@{args.camera}:554/h264Preview_01_main"
                        if args.rtsp_user and args.rtsp_password
                        else f"rtsp://{args.camera}:554/h264Preview_01_main"
                    )
                    if cap.isOpened():
                        try:
                            for _ in range(3):
                                ok, frame = cap.read()
                                if ok and frame is not None:
                                    face_frames.append(frame)
                        finally:
                            cap.release()

                if face_frames:
                    extracted = _extract_stable_attributes_from_qwen(
                        face_frames,
                        camera_name,
                        logger,
                    )
                    if extracted is not None:
                        confirmed = _confirm_or_edit_stable_attributes(
                            extracted,
                            args.name,
                            logger,
                        )
                        if confirmed is not None:
                            identity["stable_attributes"] = confirmed
                        else:
                            print("Skipped stable-attribute extraction (operator choice).")
                            logger.info("operator skipped stable-attribute extraction")
                    else:
                        print("Qwen3.6 extraction failed; continuing without stable attributes.")
                        logger.warning("Qwen3.6 extraction failed; saved without stable_attributes")
            except ImportError as err:
                logger.warning(f"cv2 not importable for Qwen3.6 frame grab: {err}")

    saved_path = save_identity(identity)
    print(f"\n✓ Enrolled {args.name!r} ({len(embeddings)} samples)")
    print(f"  Saved to: {saved_path}")
    print(f"  Role: {args.role}")
    print(f"  Embedding dims: {len(averaged)}")
    if identity.get("stable_attributes"):
        print(f"  Stable attributes: {identity['stable_attributes']}")
    logger.info(
        f"enrolled: name={args.name!r} role={args.role!r} "
        f"samples={len(embeddings)} path={saved_path} "
        f"has_stable_attrs={bool(identity.get('stable_attributes'))}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
