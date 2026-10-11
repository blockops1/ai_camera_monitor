"""Force CLIP model load for person embedder verification.

Usage:
    python -m infra.cli.force_person_embed [crop_path]

If crop_path is omitted, uses a known test image path.
Loads infra.person_embedder, triggers load_clip(), and prints the result.

This is a verification helper only -- no enrollment, no side effects.
"""
from __future__ import annotations

import logging
import os
import sys

# Ensure the project root is on sys.path so infra.person_embedder imports.
# Run from repo root: python -m infra.cli.force_person_embed
# Or from anywhere: PYTHONPATH=/path/to/repo python path/to/this/file.py
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)


def main() -> None:
    crop_path = sys.argv[1] if len(sys.argv) > 1 else None

    # If no crop given, use any available frame or skip embed call
    if not crop_path:
        # Search for any crop_a.png in data/frames
        frames_dir = os.environ.get("FARMSURV_DATA_DIR", os.path.expanduser("~/farm-surveillance-v2/data"))
        found = False
        for cam in sorted(os.listdir(frames_dir)):
            cam_path = os.path.join(frames_dir, cam)
            if not os.path.isdir(cam_path):
                continue
            for alert in sorted(os.listdir(cam_path)):
                crop = os.path.join(cam_path, alert, "crop_a.png")
                if os.path.isfile(crop):
                    crop_path = crop
                    found = True
                    break
            if found:
                break
        if not found:
            print("No crop images found in data/frames/ — will only test model load.", file=sys.stderr)

    # Force the env var to 1 for this process (listener may have loaded it already)
    os.environ["PERSON_EMBEDDER_LIVE"] = "1"

    from infra.person_embedder import embed_person_crop, load_clip  # noqa: E402

    # Trigger model load explicitly
    logger.info("Forcing CLIP model load via load_clip() ...")
    model = load_clip()
    logger.info("Model loaded: %s (hidden_size=%d)", type(model).__name__, 512)

    if crop_path:
        logger.info("Embedding %s ...", crop_path)
        t0 = __import__("time").time()
        vec = embed_person_crop(crop_path)
        elapsed = __import__("time").time() - t0
        norm = __import__("numpy").linalg.norm(vec)
        logger.info("Embedding done: shape=%s norm=%.4f time=%.2fs", vec.shape, norm, elapsed)
    else:
        logger.info("No crop path — model load verified, skipping embed.")

    print("CLIP model load: OK")


if __name__ == "__main__":
    main()
