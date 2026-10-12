#!/usr/bin/env python3
"""enroll_person.py -- CLI to enroll a known person with photos and metadata.

STATUS: stable
THREAD SAFETY: single-threaded CLI - no concurrency concerns.

INPUTS:
    - CLI arguments: --name, --id, --identity-markers.
    - data/known/people/known_people.json - persistent registry (JSON list).
    - data/known/people/embeddings.npz - per-image and per-person embeddings.

OUTPUTS:
    - Appends an entry to known_people.json.
    - Writes / updates embeddings.npz under keys:
        {person_id}__{cam_dir}__{image_filename}  - per-image 512-dim vector
        {person_id}__avg               - mean embedding across all images
    - Prints the enrolled person's id and label to stdout.

PUBLIC API:
    main() - entry point; parses CLI, loads/creates storage, embeds images,
             persists registry + embeddings, prints summary.

DOES NOT DO:
    - Matching or recognition (see infra.person_matcher).
    - Daemon or background operation.
    - Network access.

CALLS INTO:
    - infra.person_embedder.embed_person_crop() - 512-dim vector per image.
    - numpy - .npz save/load for embeddings.
    - json - known_people.json read/write.

RELATED:
    - docs/PHASE-V2-059-PRD-person-body-recognition.json
    - infra/person_embedder.py
    - scripts/release/config.yaml: strip_paths exclude data/known/people/
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

# ---------------------------------------------------------------------------
# Constants - paths under project data/known/people/
# ---------------------------------------------------------------------------
PEOPLE_DIR: Path = Path(__file__).resolve().parents[1] / "data" / "known" / "people"
KNOWN_PEOPLE_JSON: Path = PEOPLE_DIR / "known_people.json"
EMBEDDINGS_NPZ: Path = PEOPLE_DIR / "embeddings.npz"


# ---------------------------------------------------------------------------
# Storage helpers
# ---------------------------------------------------------------------------

def _load_registry() -> list[dict[str, Any]]:
    """Load the known people JSON list; create file with [] if missing."""
    PEOPLE_DIR.mkdir(parents=True, exist_ok=True)
    if KNOWN_PEOPLE_JSON.exists():
        with open(KNOWN_PEOPLE_JSON, "r") as f:
            data = json.load(f)
        assert isinstance(data, list), (
            f"{KNOWN_PEOPLE_JSON} must contain a JSON list, got {type(data).__name__}"
        )
        return data
    return []


def _save_registry(registry: list[dict[str, Any]]) -> None:
    """Persist the registry to known_people.json."""
    PEOPLE_DIR.mkdir(parents=True, exist_ok=True)
    with open(KNOWN_PEOPLE_JSON, "w") as f:
        json.dump(registry, f, indent=2, default=str)
        f.write("\n")


def _load_embeddings() -> dict[str, np.ndarray]:
    """Load existing embeddings.npz; return empty dict if missing."""
    if not EMBEDDINGS_NPZ.exists():
        return {}
    return dict(np.load(EMBEDDINGS_NPZ, allow_pickle=True))


def _save_embeddings(embeddings: dict[str, np.ndarray]) -> None:
    """Persist embeddings dict to .npz. All arrays must be saved individually
    because keys may contain slashes (person_id__image_name)."""
    PEOPLE_DIR.mkdir(parents=True, exist_ok=True)
    if not embeddings:
        # Write empty file so downstream checks see the file.
        np.savez(str(EMBEDDINGS_NPZ))
        return
    # pyright: ignore[reportArgumentType] - **kwargs expands dict[str, ndarray];
    # savez accepts arbitrary keyword arguments with array values.
    np.savez(str(EMBEDDINGS_NPZ), **embeddings)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Embedding
# ---------------------------------------------------------------------------

def _embed_image_path(image_path: str, person_id: str) -> tuple[np.ndarray, str]:
    """Load an image file, produce a 512-dim embedding, return (vector, key)."""
    from infra.person_embedder import embed_person_crop

    vec = embed_person_crop(image_path)
    assert vec.shape == (512,), (
        f"Expected embedding shape (512,), got {vec.shape}"
    )
    # Use the full resolved path as key to avoid filename collisions
    # when multiple crop_a.png / crop_b.png from different alerts are enrolled.
    resolved = Path(image_path).resolve()
    key = f"{person_id}__{resolved.parent.name}__{resolved.name}"
    return vec, key


# ---------------------------------------------------------------------------
# Enroll logic
# ---------------------------------------------------------------------------

def enroll(
    name: str,
    person_id: str,
    identity_markers: list[str] | None = None,
    image_paths: list[str] | None = None,
) -> dict[str, Any]:
    """Enroll a known person.

    Args:
        name: Display name / label for the person.
        person_id: Unique identifier string.
        identity_markers: List of identity marker tokens for matching.
        image_paths: List of local image file paths to embed.

    Returns:
        The registry entry dict that was appended.
    """
    identity_markers = identity_markers or []
    image_paths = image_paths or []

    # 1. Load state
    registry = _load_registry()
    embeddings = _load_embeddings()

    # 2. Check for duplicate id
    existing_ids = {entry["id"] for entry in registry}
    if person_id in existing_ids:
        print(
            f"ERROR: person_id '{person_id}' already enrolled. "
            f"Existing entry: {next(e for e in registry if e['id'] == person_id)['label']}",
            file=sys.stderr,
        )
        sys.exit(1)

    # 3. Embed images
    per_image_vectors: dict[str, np.ndarray] = {}
    for img_path in image_paths:
        if not os.path.isfile(img_path):
            print(f"ERROR: image path not found: {img_path}", file=sys.stderr)
            sys.exit(1)
        vec, key = _embed_image_path(img_path, person_id)
        per_image_vectors[key] = vec

    # 4. Compute per-person average embedding
    avg_key = f"{person_id}__avg"
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
        "id": person_id,
        "label": name,
        "identity_markers": sorted(set(identity_markers)),
        "first_enrolled_iso": datetime.now(UTC).isoformat(),
        "sample_image_paths": [os.path.abspath(p) for p in image_paths],
    }
    registry.append(entry)
    _save_registry(registry)

    print(f"Enrolled: {name} (id={person_id}, {len(image_paths)} images)")
    return entry


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Enroll a known person with photos and metadata.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  %(prog)s --name 'John Doe' --id john-001 \\\n"
            "      --identity-markers 'red-jacket beard tall' \\\n"
            "      --images john1.jpg john2.jpg\n"
        ),
    )
    parser.add_argument("--name", required=True, help="Display name / label for the person")
    parser.add_argument("--id", required=True, help="Unique person identifier")
    parser.add_argument(
        "--identity-markers",
        nargs="*",
        default=[],
        help="Identity markers for matching (e.g. 'red-jacket beard tall')",
    )
    parser.add_argument(
        "images",
        nargs="*",
        help="Paths to sample images for enrollment",
    )

    args = parser.parse_args(argv)

    enroll(
        name=args.name,
        person_id=args.id,
        identity_markers=args.identity_markers,
        image_paths=args.images,
    )


if __name__ == "__main__":
    main()
