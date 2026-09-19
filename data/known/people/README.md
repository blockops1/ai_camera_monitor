# known_people.json

Enrolled person identities for the person identity matcher (Phase V2-059).

## Schema

Each entry is a dict with:

- `id` (str): Unique person identifier
- `label` (str): Human-readable name for the enrolled person
- `identity_markers` (list[str]): Features used for matching (Jaccard + cosine)
- `first_enrolled_iso` (str): ISO 8601 timestamp of first enrollment
- `sample_image_paths` (list[str]): Absolute paths to sample images used for enrollment

## Embeddings storage

`embeddings.npz` stores numpy arrays keyed by:

- `{person_id}__{image_filename}` - per-image 512-dim embedding vector
- `{person_id}__avg` - mean embedding across all images for that person

## Schema notes

- The registry is gated by `PERSON_EMBEDDER_LIVE=0` (default): in offline mode,
  `_synthetic_embedding()` returns deterministic 512-dim vectors for unit tests.
- When `PERSON_EMBEDDER_LIVE=1`, real CLIP-ViT-B/32 embeddings are produced.
- Embeddings are computed via `infra.person_embedder.embed_image()` which uses
  timm's CLIP-ViT-B/32 from HuggingFace Hub.

## Example

```json
[
  {
    "id": "john-001",
    "label": "John Doe",
    "identity_markers": ["red-jacket", "beard", "tall"],
    "first_enrolled_iso": "2026-09-19T12:00:00+00:00",
    "sample_image_paths": ["/absolute/path/to/john1.jpg", "/absolute/path/to/john2.jpg"]
  }
]
```

## Privacy

This file is **gitignored**. The repository contains an empty placeholder `[]`
only. Real enrollments live outside version control.
