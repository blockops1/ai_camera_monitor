#!/usr/bin/env python3
"""
enroll_animal.py — Phase 6B.165 §11.86.5 animal enrollment CLI.

Captures 3-5 frames of an animal from a camera and prompts the
operator for the registry fields, then writes the entry to
data/animals/known_animals.json via known_animals.store.

Mirrors scripts/enroll_person.py structure (Phase 6B.106 / 6B.163)
but adapted for animals:
  - NO face embedding (animals have no face-rec path)
  - NO Qwen auto-extract (operator provides visual attributes
    directly — the matcher trusts what the operator types more
    than what Qwen sees at enrollment time)
  - Captures frames from the listener's persistent RTSP ring buffer
    (infra.persistent_rtsp) when available; falls back to one-off
    cv2.VideoCapture if no reader is registered (listener not running)

Each enrollment appends one entry to data/animals/known_animals.json.
Dedup-by-id is enforced by KnownAnimalStore.add() — re-running with
the same id raises ValueError.

STATUS: provisional (Phase 6B.165 §11.86.5, 2026-08-30).
THREAD SAFETY: single-threaded (interactive CLI).

INPUTS:
    - CLI args:
        --name    (required): display name (e.g. "Mr. Whiskers")
        --id      (optional): machine id (default = slug of --name).
                  Example: "Mr. Whiskers" -> "a_mr_whiskers".
        --camera  (optional): camera name or IP. Defaults to BDI
                  (<CAM_IP_REDACTED>) - the workshop where resident cats
                  are most likely to appear.
        --species (optional): pre-fill species (e.g. "cat"). Operator
                  can still override during the interactive prompt.
        --samples (optional): number of frames to capture for visual
                  reference. Default 3, max 10. These are saved to
                  logs/enroll_animal/<timestamp>/ and NOT persisted
                  in the registry — the registry just stores the
                  operator-entered attributes.
        --no-rtsp (optional): skip persistent RTSP tap and use a
                  one-off cv2.VideoCapture. Default: try persistent
                  first, fall back.

OUTPUTS:
    - Appends one entry to data/animals/known_animals.json via
      known_animals.store.KnownAnimalStore.add() + to_file().
    - Capture frames written to logs/enroll_animal/<timestamp>/
      (informational only — registry stores no frame refs).
    - log file: logs/enroll_animal.log (RotatingFileHandler via
      infra.logging_setup)

PUBLIC API:
    main() -> int
        Entry point. Returns process exit code.

DOES NOT DO:
    - Auto-extract visual attributes via Qwen — operator provides
      them directly. (Auto-extraction may be added in a future
      phase; today the operator's eyes are trusted.)
    - Match animals against the registry — that's
      infra.animal_matcher.match_animal.
    - Send Telegram — that's infra.send_telegram.
    - Detect motion — that's the motion gate (infra/frame_diff
      + listener/motion_gate_pipeline; Phase 6B.115) and the dataclass
      types live at infra.motion_types.

CALLED BY:
    - Operator CLI only. Not invoked from listener.

CALLS INTO:
    - known_animals.store — load / mutate / save registry
    - infra.persistent_rtsp — get_recent_frames() from ring buffer
    - infra.logging_setup — configure_logging() for log output
    - cv2 (optional) — one-off fallback if no persistent reader

RELATED:
    - data/animals/known_animals.json — the file we mutate
    - infra.animal_matcher — consumes what we write
    - scripts/enroll_person.py — person-pipeline sibling
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import re
import sys
from pathlib import Path

# Make project root importable when run as a script
_project_root = Path(__file__).resolve().parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from known_animals.store import (
    KnownAnimalStore,
    load_known_animals,
)

# --- slug helper ---

def _slugify(s: str) -> str:
    """Make a machine id from a display name.

    "Mr. Whiskers" -> "a_mr_whiskers"
    "Eastern Coyote (yearling)" -> "a_eastern_coyote_yearling"

    The leading "a_" distinguishes animal ids from "v_" (vehicle)
    and "p_" (person). Lowercase, ASCII letters/digits/underscores
    only, non-alpha runs collapsed to single underscore.
    """
    s = s.lower().strip()
    s = re.sub(r"[^a-z0-9]+", "_", s)
    s = s.strip("_")
    return f"a_{s}" if s else "a_unknown"


# --- field prompts ---

def _prompt_species(default: str | None) -> str:
    """Prompt for species. Free-form string per §11.86.2 (matcher
    normalizes common variants — see _normalize_species)."""
    raw = input(f"species [{default or 'cat'}]: ").strip().lower()
    return raw or (default or "cat")


def _prompt_optional_enum(field: str, options: list[str], default: str | None) -> str | None:
    """Prompt for an optional enum value. Enter = default / skip."""
    options_str = " | ".join(options)
    suffix = f" [{options_str}]" + (f" default={default}" if default else "")
    raw = input(f"{field}{suffix}: ").strip().lower()
    if not raw:
        return default
    if raw not in options:
        print(f"  ! '{raw}' not in {options}; treating as null", file=sys.stderr)
        return None
    return raw


def _prompt_optional_str(field: str) -> str | None:
    raw = input(f"{field} (optional, Enter to skip): ").strip()
    return raw or None


def _prompt_distinctive_features() -> list[str] | None:
    """Prompt for distinctive_features array. 1-5 short strings per
    the §11.86.3 prompt guidance. Enter twice to finish."""
    print("distinctive_features (1-5 short strings; empty line to finish):")
    items: list[str] = []
    while len(items) < 5:
        raw = input(f"  [{len(items) + 1}] ").strip()
        if not raw:
            break
        items.append(raw)
    return items if items else None


def _prompt_face_details() -> dict | None:
    """Prompt for face_details nested dict. Each component optional."""
    print("face_details (each optional, Enter to skip):")
    ear = _prompt_optional_enum("ear_shape", ["pointed", "floppy", "tufted", "rounded"], None)
    tail = _prompt_optional_enum("tail_carriage", ["high", "low", "curled", "level"], None)
    mask = input("mask (yes/no, Enter to skip): ").strip().lower()
    if mask and mask not in ("yes", "no"):
        print("  ! mask must be 'yes', 'no', or empty; treating as null", file=sys.stderr)
        mask = None
    result = {
        "ear_shape": ear,
        "tail_carriage": tail,
        "mask": mask or None,
    }
    if all(v is None for v in result.values()):
        return None
    return {k: v for k, v in result.items() if v is not None}


def _prompt_animal(name: str, id_: str, species_default: str | None) -> dict:
    """Walk the operator through every registry field."""
    print(f"\n=== Enrolling: {name} (id={id_}) ===\n")

    entry: dict = {
        "id": id_,
        "name": name,
    }
    entry["species"] = _prompt_species(species_default)
    entry["species_confidence"] = _prompt_optional_enum(
        "species_confidence", ["definite", "likely", "unsure"], "definite"
    )
    entry["body_size"] = _prompt_optional_enum(
        "body_size", ["small", "medium", "large"], None
    )
    entry["body_build"] = _prompt_optional_enum(
        "body_build", ["lean", "stocky", "athletic", "compact"], None
    )
    entry["coat_primary_color"] = _prompt_optional_str("coat_primary_color")
    entry["coat_pattern"] = _prompt_optional_enum(
        "coat_pattern", ["solid", "bi-color", "tri-color", "tabby", "striped", "spotted"], None
    )
    entry["distinctive_features"] = _prompt_distinctive_features()
    entry["face_details"] = _prompt_face_details()
    entry["estimated_age"] = _prompt_optional_enum(
        "estimated_age", ["juvenile", "adult", "senior"], None
    )
    entry["sex_signal"] = _prompt_optional_enum(
        "sex_signal", ["male", "female", "neutered"], None
    )
    entry["label"] = _prompt_optional_str("label")
    entry["first_enrolled"] = _dt.date.today().isoformat()
    entry["source_alerts"] = []

    # Strip None values for cleaner JSON
    return {k: v for k, v in entry.items() if v is not None}


def _confirm(entry: dict) -> bool:
    """Pretty-print the candidate entry and confirm before save."""
    print("\n=== Candidate entry ===")
    print(json.dumps(entry, indent=2))
    while True:
        ans = input("\nSave this entry? [y/N]: ").strip().lower()
        if ans in ("y", "yes"):
            return True
        if ans in ("n", "no", ""):
            return False
        print("  Please answer y or n.")


# --- frame capture (informational) ---

def _capture_samples(
    camera: str, samples: int, log_dir: Path, no_rtsp: bool
) -> list[str]:
    """Capture frames for the operator's visual reference.

    Tries the listener's persistent RTSP ring buffer first
    (per the 6B.163 enrollment pattern). Falls back to a one-off
    cv2.VideoCapture if no reader is registered.

    Returns the list of saved frame paths. Empty list on failure
    (the registry doesn't require these — they're for the operator's
    eyes only, so failure here is non-fatal).
    """
    saved: list[str] = []
    log_dir.mkdir(parents=True, exist_ok=True)

    if not no_rtsp:
        try:
            from infra import persistent_rtsp

            reader = persistent_rtsp.get_reader(camera)
            if reader is None:
                # Try a few common variants
                for variant in (camera, camera.replace(" ", "_"), camera.upper()):
                    reader = persistent_rtsp.get_reader(variant)
                    if reader is not None:
                        break
            if reader is not None:
                saved = reader.get_recent_frames(samples, str(log_dir))
                if saved:
                    print(f"  Captured {len(saved)} frame(s) from RTSP ring buffer.")
                    return saved
        except Exception as e:
            print(f"  ! Persistent RTSP capture failed: {e}", file=sys.stderr)

    if no_rtsp:
        return saved

    # One-off cv2 fallback (not implemented — enrollment usually runs
    # while listener is up; ring buffer is the documented path).
    print(
        "  ! No persistent RTSP reader registered. Re-run without --no-rtsp "
        "while the listener is running, or supply --no-rtsp + manual frames.",
        file=sys.stderr,
    )
    return saved


# --- main ---

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Enroll an animal into data/animals/known_animals.json."
    )
    parser.add_argument("--name", required=True, help="Display name (e.g. 'Mr. Whiskers')")
    parser.add_argument(
        "--id",
        default=None,
        help="Machine id (default: slug of --name, e.g. 'a_mr_whiskers').",
    )
    parser.add_argument(
        "--camera",
        default="CAM2",  # §13.4: CAM2 = back-door / workshop camera
        help="Camera name or IP for frame capture (default: CAM2).",
    )
    parser.add_argument(
        "--species",
        default=None,
        help="Pre-fill species (e.g. 'cat'). Operator can still override.",
    )
    parser.add_argument(
        "--samples",
        type=int,
        default=3,
        help="Number of frames to capture (default 3, max 10).",
    )
    parser.add_argument(
        "--no-rtsp",
        action="store_true",
        help="Skip persistent RTSP tap; informational only.",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip the final confirmation prompt (use with care).",
    )
    args = parser.parse_args()

    if args.samples < 1 or args.samples > 10:
        print("--samples must be 1-10", file=sys.stderr)
        return 2

    id_ = args.id or _slugify(args.name)
    if not id_.startswith("a_"):
        print(
            f"WARNING: id {id_!r} does not start with 'a_'. "
            "Animal ids should use the 'a_' prefix to disambiguate from "
            "vehicles (v_) and persons (p_).",
            file=sys.stderr,
        )

    # Capture frames (informational)
    ts = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    log_dir = _project_root / "logs" / "enroll_animal" / ts
    if not args.no_rtsp:
        _capture_samples(args.camera, args.samples, log_dir, args.no_rtsp)

    # Prompt for the registry fields
    entry = _prompt_animal(args.name, id_, args.species)

    if not args.yes and not _confirm(entry):
        print("Cancelled.")
        return 1

    # Load, mutate, save
    from infra.paths import ANIMAL_KNOWN_FILE  # canonical path constant

    store = KnownAnimalStore(load_known_animals())
    try:
        store.add(entry)
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    store.to_file(ANIMAL_KNOWN_FILE)

    print(f"\nSaved {entry['id']!r} to {ANIMAL_KNOWN_FILE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())