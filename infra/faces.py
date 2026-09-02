"""
faces.py — Identity storage for farm-surveillance-refactor.

Lifted 2026-08-22 from ~/farm-surveillance/src/faces.py
(archived at ~/archive/2026-08-22-phase6b106-prelift-archive/) for
Phase 6B.106 (person-gatekeeper tier). Logic is byte-identical; only
the path imports change (bare `from paths import` → `from infra.paths import`).

Two new helpers were added for infra.face_recognition's linear-scan
matching path:
    _iter_identity_paths() — yields every identity JSON path
    load_identity_by_path(path) — load one identity from a path
The original load_identity(name) is preserved for callers that
look up by display name.

STATUS: stable
THREAD SAFETY: single-threaded (load/save are file-IO bound; no
    in-process caching. Caller is responsible for any locking if
    running concurrently.)

INPUTS:
    - file infra.paths.IDENTITIES_DIR env override (default = data/identities/)
        one JSON file per identity, named {slug}.json
    - function arg identity: dict with at minimum {"name", "role",
        "face_embedding", "sample_count"}; optional fields auto-defaulted
    - function arg name: display name (used to derive slug)
    - function arg new_embedding: list of floats (512-dim for ArcFace)
    - env var FARM_IDENTITY_BACKUP_DIR (optional) — NAS backup target

OUTPUTS:
    - save_identity: writes <IDENTITIES_DIR>/<slug>.json; also writes
        a mirror copy to IDENTITY_BACKUP_DIR when set
    - load_identity / load_identity_by_path: returns dict or None
    - list_identities: returns sorted list of display names
    - delete_identity: returns bool (True if file was removed)
    - add_enrollment_sample: modifies the identity JSON (averages in new
        sample, appends history entry)
    - Phase 6B.163: `stable_attributes` block is preserved through save/
        load roundtrips; absence is benign (tier-3 match is skipped)
    - No network. Caller logs.
    - On write: macOS FileVault encrypts at rest; per-file AES-GCM deferred

PUBLIC API:
    IDENTITY_FIELDS: list[str] — minimum fields required on save
    _slug_from_name(name) -> str — "maintainer" → "mr_v", non-alnum → _
    save_identity(identity: dict) -> str
        Write identity JSON; apply defaults for optional fields.
    load_identity(name: str) -> dict | None
        Look up identity by display name.
    delete_identity(name: str) -> bool
        Remove identity file. True if removed, False if not present.
    list_identities() -> list[str]
        Sorted display names of all enrolled identities.
    add_enrollment_sample(name, new_embedding, camera="") -> None
        Average a new embedding into an existing identity (raises
        KeyError if identity doesn't exist).
    _iter_identity_paths() -> Iterator[str]
        For internal use by face_recognition.py's linear-scan matcher.
        Yields each JSON path in IDENTITIES_DIR.
    load_identity_by_path(path: str) -> dict | None
        For internal use by face_recognition.py. Load one identity
        by path without re-deriving the slug.

DOES NOT DO:
    - Compare embeddings to a query vector → that's infra.face_recognition
    - Detect faces → that's infra.face_recognition
    - Persist anything beyond the JSON file the caller passes
    - Lock against concurrent writers — caller is responsible
    - Encrypt per-file (deferred; FileVault covers at-rest)

WHY HERE:
    Lifted from old repo (Phase 6A, 2025) for §11.36 person-gatekeeper.
    Path imports updated to infra.paths. _iter_identity_paths +
    load_identity_by_path added so face_recognition.py can scan all
    identities without re-deriving slugs (cost ~1ms per identity).

CALLED BY:
    - infra.face_recognition._match_one_face (via _iter_identity_paths
        + load_identity_by_path) — match a query embedding against all
        enrolled identities
    - scripts/enroll_person.py — interactive enrollment CLI
    - tests/integration/person_pipeline — rough/pie integration test

CALLS INTO:
    - infra.paths: IDENTITIES_DIR, IDENTITY_BACKUP_DIR
    - stdlib: json, re, os, datetime, shutil, logging
"""

from __future__ import annotations

import datetime
import json
import logging
import os
import re
import shutil
from collections.abc import Iterator
from typing import Any, cast

from infra.paths import IDENTITIES_DIR, IDENTITY_BACKUP_DIR

_log = logging.getLogger(__name__)


# Required fields on save; embeddings are added incrementally via add_enrollment_sample.
IDENTITY_FIELDS = ["name", "role", "face_embedding", "sample_count", "enrolled_at"]


# ---------- Path helpers ----------


def _slug_from_name(name: str) -> str:
    """Turn a display name into a filesystem-safe slug."""
    s = name.strip().lower()
    # Replace any non-alphanumeric run with underscore
    s = re.sub(r"[^a-z0-9]+", "_", s)
    s = s.strip("_")
    return s or "unnamed"


def _identity_path(name: str) -> str:
    """Return absolute path to the identity JSON file for the given name."""
    return os.path.join(IDENTITIES_DIR, f"{_slug_from_name(name)}.json")


# Used by infra.face_recognition to enumerate enrolled identities without
# re-deriving slugs from names.
def _iter_identity_paths() -> Iterator[str]:
    """Yield every JSON identity path in IDENTITIES_DIR (sorted, deterministic)."""
    if not os.path.isdir(IDENTITIES_DIR):
        return
    for filename in sorted(os.listdir(IDENTITIES_DIR)):
        if not filename.endswith(".json"):
            continue
        path = os.path.join(IDENTITIES_DIR, filename)
        if os.path.isfile(path):
            yield path


def load_identity_by_path(path: str) -> dict[str, Any] | None:
    """Load one identity JSON from a path; returns None on missing or corrupt JSON."""
    try:
        with open(path) as f:
            return cast(dict[str, Any], json.load(f))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


# ---------- CRUD ----------


def _backup_identity_to_nas(local_path: str, name: str) -> None:
    """Mirror a freshly-saved identity JSON to the NAS backup share.

    Best-effort: if IDENTITY_BACKUP_DIR is empty, unset, or unwritable, log a
    warning and return. The local copy is authoritative.
    """
    if not IDENTITY_BACKUP_DIR:
        return
    try:
        os.makedirs(IDENTITY_BACKUP_DIR, exist_ok=True)
        backup_path = os.path.join(IDENTITY_BACKUP_DIR, f"{_slug_from_name(name)}.json")
        shutil.copy2(local_path, backup_path)
    except OSError as e:
        _log.warning("identity backup failed for %s → %s: %s", name, IDENTITY_BACKUP_DIR, e)


def save_identity(identity: dict) -> str:
    """Save an identity to disk. Returns the path written.

    On successful write, also mirrors the JSON to IDENTITY_BACKUP_DIR (default
    /private/tmp/nas-backup/farm-identities) so the face DB survives local
    disk loss. Backup failures are logged but do not abort the save — the
    local copy is the primary, the NAS copy is the safety net.
    """
    if "name" not in identity:
        raise KeyError("identity must have a 'name' field")

    os.makedirs(IDENTITIES_DIR, exist_ok=True)
    path = _identity_path(identity["name"])

    # Defaults for any optional fields
    identity.setdefault("role", "unidentified")
    identity.setdefault("face_embedding", [])
    identity.setdefault("sample_count", 0)
    identity.setdefault("vehicle", None)
    # Identity file — face DB JSON, not an audit log. Naive timestamp is
    # fine here; the file's location + filename are the audit trail.
    identity.setdefault("enrolled_at", datetime.datetime.now().isoformat())  # noqa: DTZ005
    identity.setdefault("last_seen", None)
    identity.setdefault("history", [])
    # Phase 6B.163 — stable visual attributes (silhouette, skin_tone,
    # age_range, hair, facial_hair, glasses) extracted by Qwen3.6 from
    # the same face crops used for enrollment. Stored per identity;
    # used by infra.person_matcher's tier-3 weighted ensemble when
    # face recognition fails (back of head, mask, low light, etc.).
    # Default None; absence means tier-3 matching is skipped for this
    # identity. Additive; existing identities without this field
    # continue to load.
    identity.setdefault("stable_attributes", None)

    with open(path, "w") as f:
        json.dump(identity, f, indent=2)

    _backup_identity_to_nas(path, identity["name"])
    return path


def load_identity(name: str) -> dict[str, Any] | None:
    """Load an identity by display name. Returns dict or None if not found."""
    path = _identity_path(name)
    if not os.path.isfile(path):
        return None
    with open(path) as f:
        return cast(dict[str, Any], json.load(f))


def delete_identity(name: str) -> bool:
    """Remove an identity file. Returns True if a file was removed, False otherwise."""
    path = _identity_path(name)
    if os.path.isfile(path):
        os.remove(path)
        return True
    return False


def list_identities() -> list:
    """Return all known identity display names, sorted alphabetically."""
    if not os.path.isdir(IDENTITIES_DIR):
        return []

    names = []
    for path in _iter_identity_paths():
        data = load_identity_by_path(path)
        if data is None:
            continue
        if "name" in data:
            names.append(data["name"])

    return sorted(names)


# ---------- Enrollment helpers ----------


def add_enrollment_sample(name: str, new_embedding: list, camera: str = "") -> None:
    """
    Add a new face embedding sample to an existing identity, averaging it in.

    If face_embedding is empty (first sample), it becomes the new embedding verbatim.
    Otherwise, weighted average: new_count = (old_count * old_avg + new) / (old_count + 1).
    Also appends a history entry.

    If the identity does not exist, raises KeyError.
    """
    identity = load_identity(name)
    if identity is None:
        raise KeyError(f"identity not found: {name}")

    old_count = identity.get("sample_count", 0)
    old_avg = identity.get("face_embedding") or []

    if old_count == 0 or not old_avg:
        new_avg = list(new_embedding)
        new_count = 1
    else:
        new_count = old_count + 1
        new_avg = [(old_count * a + n) / new_count for a, n in zip(old_avg, new_embedding)]
        # Normalize to unit length (ArcFace embeddings are unit-norm; renormalize defensively)
        norm = sum(x * x for x in new_avg) ** 0.5
        if norm > 0:
            new_avg = [x / norm for x in new_avg]

    identity["face_embedding"] = new_avg
    identity["sample_count"] = new_count
    if camera:
        history = identity.setdefault("history", [])
        history.append(
            {
                # Identity history — not an audit log. See enrolled_at comment.
                "ts": datetime.datetime.now().isoformat(),  # noqa: DTZ005
                "camera": camera,
                "event": "enrollment_sample",
            }
        )
        # Keep at most 50 history entries to bound file growth
        if len(history) > 50:
            identity["history"] = history[-50:]

    save_identity(identity)
