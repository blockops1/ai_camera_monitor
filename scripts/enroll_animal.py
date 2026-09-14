#!/usr/bin/env python3
"""enroll_animal.py — CLI to enroll a known animal with photos and metadata.

STATUS: stable
THREAD SAFETY: single-threaded CLI — no concurrency concerns.

INPUTS:
    - CLI arguments: --name, --id, --species, --breed, --color-patterns,
      --distinctive-features, --images.
    - data/known/animals/known_animals.json — persistent registry (JSON list).
    - data/known/animals/embeddings.npz — per-image and per-animal embeddings.

OUTPUTS:
    - Appends an entry to known_animals.json.
    - Writes / updates embeddings.npz under keys:
        {animal_id}__{image_filename}  — per-image 768-dim vector
        {animal_id}__avg               — mean embedding across all images
    - Prints the enrolled animal's id and label to stdout.

PUBLIC API:
    main() — entry point; parses CLI, loads/creates storage, embeds images,
             persists registry + embeddings, prints summary.

DOES NOT DO:
    - Matching or recognition (see infra.animal_matcher).
    - Daemon or background operation.
    - Network access.

CALLS INTO:
    - infra.animal_embedder.embed_image() — 768-dim vector per image.
    - numpy — .npz save/load for embeddings.
    - json — known_animals.json read/write.
    - PIL.Image — image loading.

RELATED:
    - docs/PHASE-V2-045-PRD-animal-body-recognition.json
    - infra/animal_embedder.py
    - scripts/release/config.yaml: strip_paths exclude data/known/animals/
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

# ---------------------------------------------------------------------------
# Constants — paths under project data/known/animals/
# ---------------------------------------------------------------------------
ANIMALS_DIR: Path = Path(__file__).resolve().parents[2] / "data" / "known" / "animals"
KNOWN_ANIMALS_JSON: Path = ANIMALS_DIR / "known_animals.json"
EMBEDDINGS_NPZ: Path = ANIMALS_DIR / "embeddings.npz"


# ---------------------------------------------------------------------------
# Storage helpers
# ---------------------------------------------------------------------------

def _load_registry() -> list[dict[str, Any]]:
    """Load the known animals JSON list; create file with [] if missing."""
    ANIMALS_DIR.mkdir(parents=True, exist_ok=True)
    if KNOWN_ANIMALS_JSON.exists():
        with open(KNOWN_ANIMALS_JSON, "r") as f:
            data = json.load(f)
        assert isinstance(data, list), (
            f"{KNOWN_ANIMALS_JSON} must contain a JSON list, got {type(data).__name__}"
        )
        return data
    return []


def _save_registry(registry: list[dict[str, Any]]) -> None:
    """Persist the registry to known_animals.json."""
    ANIMALS_DIR.mkdir(parents=True, exist_ok=True)
    with open(KNOWN_ANIMALS_JSON, "w") as f:
        json.dump(registry, f, indent=2, default=str)
        f.write("\n")


def _load_embeddings() -> dict[str, np.ndarray]:
    """Load existing embeddings.npz; return empty dict if missing."""
    if not EMBEDDINGS_NPZ.exists():
        return {}
    return dict(np.load(EMBEDDINGS_NPZ, allow_pickle=True))


def _save_embeddings(embeddings: dict[str, np.ndarray]) -> None:
    """Persist embeddings dict to .npz. All arrays must be saved individually
    because keys may contain slashes (animal_id__image_name)."""
    ANIMALS_DIR.mkdir(parents=True, exist_ok=True)
    if not embeddings:
        # Write empty file so downstream checks see the file.
        np.savez(str(EMBEDDINGS_NPZ))
        return
    # pyright: ignore[reportArgumentType] — **kwargs expands dict[str, ndarray];
    # savez accepts arbitrary keyword arguments with array values.
    np.savez(str(EMBEDDINGS_NPZ), **embeddings)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Embedding
# ---------------------------------------------------------------------------

def _embed_image_path(image_path: str, animal_id: str) -> tuple[np.ndarray, str]:
    """Load an image file, produce a 768-dim embedding, return (vector, key)."""
    img = Image.open(image_path).convert("RGB")
    from infra.animal_embedder import embed_image

    vec = embed_image(img)
    assert vec.shape == (768,), (
        f"Expected embedding shape (768,), got {vec.shape}"
    )
    filename = os.path.basename(image_path)
    key = f"{animal_id}__{filename}"
    return vec, key


# ---------------------------------------------------------------------------
# Enroll logic
# ---------------------------------------------------------------------------

def enroll(
    name: str,
    animal_id: str,
    species: str,
    breed: str = "",
    color_patterns: list[str] | None = None,
    distinctive_features: list[str] | None = None,
    image_paths: list[str] | None = None,
) -> dict[str, Any]:
    """Enroll a known animal.

    Args:
        name: Display name / label for the animal.
        animal_id: Unique identifier string.
        species: e.g. "cow", "horse", "dog".
        breed: Breed string (optional).
        color_patterns: List of color pattern tokens.
        distinctive_features: List of distinctive feature tokens.
        image_paths: List of local image file paths to embed.

    Returns:
        The registry entry dict that was appended.
    """
    color_patterns = color_patterns or []
    distinctive_features = distinctive_features or []
    image_paths = image_paths or []

    # 1. Load state
    registry = _load_registry()
    embeddings = _load_embeddings()

    # 2. Check for duplicate id
    existing_ids = {entry["id"] for entry in registry}
    if animal_id in existing_ids:
        print(
            f"ERROR: animal_id '{animal_id}' already enrolled. "
            f"Existing entry: {next(e for e in registry if e['id'] == animal_id)['label']}",
            file=sys.stderr,
        )
        sys.exit(1)

    # 3. Embed images
    per_image_vectors: dict[str, np.ndarray] = {}
    for img_path in image_paths:
        if not os.path.isfile(img_path):
            print(f"ERROR: image path not found: {img_path}", file=sys.stderr)
            sys.exit(1)
        vec, key = _embed_image_path(img_path, animal_id)
        per_image_vectors[key] = vec

    # 4. Compute per-animal average embedding
    avg_key = f"{animal_id}__avg"
    if per_image_vectors:
        all_vecs = np.stack(list(per_image_vectors.values()))
        avg_vec = all_vecs.mean(axis=0).astype(np.float32)
        per_image_vectors[avg_key] = avg_vec
    else:
        avg_vec = None

    # 5. Merge into embeddings store
    embeddings.update(per_image_vectors)
    _save_embeddings(embeddings)

    # 6. Build registry entry
    from datetime import datetime

    entry = {
        "id": animal_id,
        "label": name,
        "species": species,
        "breed": breed,
        "color_patterns": sorted(set(color_patterns)),
        "distinctive_features": sorted(set(distinctive_features)),
        "first_enrolled_iso": datetime.now(UTC).isoformat(),
        "sample_image_paths": [os.path.abspath(p) for p in image_paths],
    }
    registry.append(entry)
    _save_registry(registry)

    print(f"Enrolled: {name} (id={animal_id}, species={species}, {len(image_paths)} images)")
    return entry


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Enroll a known animal with photos and metadata.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  %(prog)s --name 'Bessie' --id bessie-001 --species cow \\\n"
            "      --breed 'Holstein' --color-patterns 'black white' \\\n"
            "      --distinctive-features 'horned' --images bessie1.jpg bessie2.jpg\n"
        ),
    )
    parser.add_argument("--name", required=True, help="Display name / label for the animal")
    parser.add_argument("--id", required=True, help="Unique animal identifier")
    parser.add_argument("--species", required=True, help="Species (e.g. cow, horse, dog)")
    parser.add_argument("--breed", default="", help="Breed (optional)")
    parser.add_argument(
        "--color-patterns",
        default="",
        help="Space-separated color patterns (e.g. 'brown tan')",
    )
    parser.add_argument(
        "--distinctive-features",
        default="",
        help="Space-separated distinctive features (e.g. 'black collar curly fur')",
    )
    parser.add_argument(
        "images",
        nargs="*",
        help="Paths to sample images for enrollment",
    )

    args = parser.parse_args(argv)

    color_patterns = args.color_patterns.split() if args.color_patterns else []
    distinctive_features = args.distinctive_features.split() if args.distinctive_features else []

    enroll(
        name=args.name,
        animal_id=args.id,
        species=args.species,
        breed=args.breed,
        color_patterns=color_patterns,
        distinctive_features=distinctive_features,
        image_paths=args.images,
    )


if __name__ == "__main__":
    main()
