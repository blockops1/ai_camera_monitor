#!/usr/bin/env python3
"""
enroll_vehicle_from_alert.py — Convert an unknown-vehicle alert into a
known entry in `data/vehicles/known_vehicles.json`.

STATUS: provisional (Phase 6B.166 §11.87.7, 2026-08-30 — rewrote from
    ~/farm-surveillance/ to use the canonical dict-wrapped schema that
    known_vehicles/store.py has used since Phase 6B.83. Prior version
    wrote a top-level list which would have corrupted the file.
    Stabilizes after the next operator-run validation.)
THREAD SAFETY: single-threaded (interactive CLI; not safe to run two
    instances concurrently against the same known_vehicles.json).

INPUTS:
    - env FARMSURV_DATA_DIR (default $FARMSURV_PROJECT_ROOT/data or
      ~/farm-surveillance-refactor/data) — root of the data tree.
    - env FARMSURV_PROJECT_ROOT (default ~/farm-surveillance-refactor).
    - file data/alerts/<YYYY-MM-DD>.jsonl — daily alert JSONL.
    - file data/vehicles/known_vehicles.json — canonical store.
    - CLI args:
        --alert-id      (required): UUID from the Telegram notification.
        --label         (required): human label, e.g. "employee_a's Chevy".
        --date          (optional): YYYY-MM-DD to scope alert lookup.
                                    Default: scan all dates (most recent
                                    first).
        --color         (optional): override color (default: alert's).
        --type          (optional): override body type (default: alert's).
        --make          (optional): override make.
        --model         (optional): override model.
        --plate         (optional): license plate.
        --note          (optional): free-text note.
        --distinctive-features (optional): free-text.
        --dry-run       (optional): print proposed entry, do not write.
        --alerts-dir    (optional): override alerts directory.
        --known-file    (optional): override known_vehicles.json path.

OUTPUTS:
    - writes file data/vehicles/known_vehicles.json via
      KnownVehicleStore.to_file() — dict-wrapped schema
      {"version": 1, "vehicles": [...]} (KNOWN_VEHICLES_SCHEMA).
      Atomic: tmp + os.replace under the hood.
    - stdout: pretty-printed proposed entry on --dry-run, or the new
      entry on success.
    - log line: "[INFO] Enrolled <id> ('<label>') from alert <id>"
    - No network. No Telegram.

PUBLIC API:
    build_entry(alert, label, overrides) -> dict
        Merge alert-derived attributes with CLI overrides, derive id,
        apply defaults. Returns the entry dict that would be appended
        to known_vehicles.json. Does NOT read or write any file.
    enroll(alert, label, overrides, *, dry_run=False,
           known_file=None) -> dict
        Look up the canonical store at known_file (or infra.paths.
        VEHICLE_KNOWN_FILE), check for label/id collision, append the
        entry, write atomically. Returns the entry on success.
    find_alert(alert_id, *, alerts_dir=None, date_str=None) -> dict | None
        Scan one or all date JSONLs for an alert matching alert_id.
        Skips alerts without vehicle_attributes (not unknown-vehicle
        alerts).
    main(argv=None) -> int
        Entry point. Returns process exit code per the codes below.

DOES NOT DO:
    - Capture frames or run vision inference — that's
      infra.frame_capture + vehicle_identifier.identifier.
    - Match alerts against the known store — that's
      vehicle_matcher.matcher.
    - Send Telegram — that's infra.send_telegram.
    - Mint stable V-NNN identities — Phase 6B.57 (2026-08-05) retired
      that layer. Enrollment is a simple append.
    - Validate the alert payload's vehicle_attributes against the
      full vision schema — the listener already validated it before
      emission; we just trust what we find.

WHY HERE:
    Phase 6B.166 §11.87.7 (2026-08-30) rewrite. Old version (copied
    from ~/farm-surveillance/scripts/) wrote the known_vehicles.json
    file as a top-level JSON list. known_vehicles/store.py (Phase
    6B.83, 2026-08-16) had already moved to a dict-wrapped schema
    (`{"version": N, "vehicles": [...]}`), so the old script would
    silently corrupt the file. Rewritten to call
    KnownVehicleStore.from_file + .add + .to_file directly.

    Sibling to scripts/enroll_animal.py (§11.86.5) and
    scripts/enroll_person.py (574a3c4). Same family, same pattern,
    same module-header standard.

CALLED BY:
    - Operator CLI only. Not invoked from listener.

CALLS INTO:
    - known_vehicles.store: KnownVehicleStore, KNOWN_VEHICLES_SCHEMA
    - infra.paths: ALERTS_DIR, VEHICLE_KNOWN_FILE
    - stdlib: argparse, json, logging, os, re, datetime

RELATED:
    - data/vehicles/known_vehicles.json — the file we mutate
    - known_vehicles/store.py — canonical reader/writer
    - data/alerts/<date>.jsonl — the alert source
    - Phase 6B.83 (2026-08-16) — schema migration to dict-wrapped form
    - Phase 6B.57 (2026-08-05) — V-NNN identity layer retired
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from datetime import UTC, datetime
from typing import Any

# Make repo root + infra importable when called as a script.
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from infra.paths import ALERTS_DIR, VEHICLE_KNOWN_FILE
from known_vehicles.store import (
    KNOWN_VEHICLES_SCHEMA,
    KnownVehicleStore,
)

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers — pure functions, no I/O. Trivially testable.
# ---------------------------------------------------------------------------


def _normalize_label(label: str) -> str:
    """Lowercase, strip whitespace, replace non-alphanumeric with '_'.

    Same normalization is used for id derivation and duplicate detection.
    """
    s = label.strip().lower()
    s = re.sub(r"[^a-z0-9]+", "_", s)
    return s.strip("_")


def _label_to_id(label: str) -> str:
    """Derive the JSON `id` field from the human label.

    Examples:
        "employee_a's Chevy pickup" -> "v_jeremiahs_chevy_pickup"
        "maintainer's blue Tesla"      -> "v_mr_vs_blue_tesla"
    """
    norm = _normalize_label(label)
    if not norm:
        return "v_unknown"
    return f"v_{norm}"


def merge_attributes(alert_attrs: dict, overrides: dict) -> dict:
    """Merge alert-derived refined fields with explicit CLI overrides.

    Overrides win. Empty strings and None values from overrides are
    treated as "no override" so `--make ""` doesn't blank out a good
    field. Missing keys in overrides are skipped.

    Only fields the matcher / store actually understand are merged:
    color, type, make, model, plate, distinctive_features, note.
    """
    merged: dict[str, Any] = {}
    for key in ("color", "type", "make", "model", "plate", "distinctive_features"):
        if alert_attrs.get(key):
            merged[key] = alert_attrs[key]
    for key, val in overrides.items():
        if val not in (None, "", []):
            merged[key] = val
    return merged


def _validate_required(merged: dict) -> str | None:
    """Return an error string if `merged` lacks fields required for a
    sane enrollment. Required: color, type.

    Optional but encouraged: make/model. Missing → entry is still
    allowed (operator can patch later), but no blocking error.
    """
    missing = []
    if not merged.get("color"):
        missing.append("color")
    if not merged.get("type"):
        missing.append("type")
    if missing:
        return f"missing required vehicle fields: {', '.join(missing)}"
    return None


def find_alert(
    alert_id: str,
    *,
    alerts_dir: str | None = None,
    date_str: str | None = None,
) -> dict | None:
    """Find an alert by id. Returns the alert dict, or None.

    If `date_str` is given, only that date is searched. Otherwise every
    `*.jsonl` under `alerts_dir` is scanned (most recent first so a
    rerun picks up the latest alert if the same id is ever duplicated).

    Alerts whose id matches but which lack a `vehicle_attributes` block
    are skipped — they're not unknown-vehicle alerts. Callers should
    check `vehicle_attributes` separately for a clear error.
    """
    if alerts_dir is None:
        alerts_dir = ALERTS_DIR

    if not os.path.isdir(alerts_dir):
        return None

    if date_str:
        path = os.path.join(alerts_dir, f"{date_str}.jsonl")
        paths = [path] if os.path.exists(path) else []
    else:
        all_paths = [
            os.path.join(alerts_dir, f)
            for f in os.listdir(alerts_dir)
            if f.endswith(".jsonl")
        ]
        all_paths.sort(reverse=True)
        paths = all_paths

    for path in paths:
        try:
            with open(path, "r") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        alert = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if alert.get("alert_id") == alert_id:
                        # Skip matches that aren't unknown-vehicle alerts —
                        # we only enroll from refined alerts with a
                        # vehicle_attributes block.
                        if "vehicle_attributes" not in alert:
                            continue
                        return alert
        except OSError as err:
            log.warning("Could not read %s: %s", path, err)
    return None


def _find_duplicate(store: KnownVehicleStore, *, label: str, kv_id: str) -> dict | None:
    """Return the existing entry that would collide with the proposed
    enrollment, or None if there's no collision.

    Duplicates are matched on:
      - exact label (case-insensitive, whitespace-trimmed)
      - exact id (case-insensitive)
    """
    norm_label = label.strip().lower()
    norm_id = kv_id.strip().lower()
    for entry in store.all():
        existing_label = str(entry.get("label", "")).strip().lower()
        existing_id = str(entry.get("id", "")).strip().lower()
        if existing_label == norm_label or existing_id == norm_id:
            return entry
    return None


def build_entry(alert: dict, label: str, overrides: dict) -> dict:
    """Build the entry dict that would be appended to known_vehicles.json.

    Does NOT read or write any file. Caller passes the alert, the
    human label, and a dict of CLI overrides (None / empty values are
    dropped by merge_attributes).

    Raises:
        LookupError: if the alert has no vehicle_attributes block.
        ValueError: on missing required fields (color or type).
    """
    attrs = alert.get("vehicle_attributes")
    if not attrs:
        raise LookupError(
            "Alert is not an unknown-vehicle alert (no vehicle_attributes)"
        )

    merged = merge_attributes(attrs, overrides)
    err = _validate_required(merged)
    if err:
        raise ValueError(err)

    new_id = _label_to_id(label)
    entry: dict[str, Any] = {
        "id": new_id,
        "label": label.strip(),
        "color": merged.get("color"),
        "type": merged.get("type"),
        "verified": False,
    }
    # Optional but encouraged fields
    for k in ("make", "model", "plate", "distinctive_features", "note"):
        if merged.get(k):
            entry[k] = merged[k]
    # Source provenance
    if alert.get("alert_id"):
        entry["source_alert_id"] = alert["alert_id"]
    if alert.get("frame_path"):
        entry["source_frame"] = alert["frame_path"]
    entry["enrolled_at"] = datetime.now(UTC).isoformat()
    return entry


# ---------------------------------------------------------------------------
# Core: enroll — does the file I/O via KnownVehicleStore.
# ---------------------------------------------------------------------------


def enroll(
    alert: dict,
    label: str,
    overrides: dict,
    *,
    dry_run: bool = False,
    known_file: str | None = None,
) -> dict:
    """Enroll a vehicle from an alert. Returns the resulting known entry.

    Args:
        alert: The full alert dict (must include `vehicle_attributes`).
        label: Human label, e.g. "employee_a's Chevy pickup".
        overrides: CLI overrides merged onto the alert attributes.
        dry_run: If True, do not write — return the proposed entry.
        known_file: Path to known_vehicles.json. Default: infra.paths.
            VEHICLE_KNOWN_FILE.

    Returns:
        The new entry that was (or would be) appended to
        known_vehicles.json.

    Raises:
        ValueError: on validation failure (label collision, missing
            fields).
        LookupError: if the alert has no vehicle_attributes block.
        FileNotFoundError: if known_file does not exist AND the caller
            asked for live write (--dry-run tolerates a missing file
            because the empty store is fine to reason about).
    """
    entry = build_entry(alert, label, overrides)

    path = known_file or VEHICLE_KNOWN_FILE

    if dry_run:
        log.info("[dry-run] would append %r", entry)
        return entry

    # Load existing store. Missing file → empty store (so a fresh
    # project can enroll without seeding). Matches the spirit of the
    # old script's permissive behavior.
    if os.path.isfile(path):
        store = KnownVehicleStore.from_file(path)
    else:
        log.info("known_vehicles.json not found at %s — starting empty store", path)
        store = KnownVehicleStore(vehicles=[])

    dup = _find_duplicate(store, label=label, kv_id=entry["id"])
    if dup is not None:
        raise ValueError(
            f"duplicate entry — id {dup.get('id')!r} already exists "
            f"with label {dup.get('label')!r}"
        )

    # KnownVehicleStore.add() replaces an existing entry with the same
    # id (id-based dedup). Our _find_duplicate check above already
    # rejected label/id collisions, so this is a clean append.
    store.add(entry)
    # Ensure the parent directory exists. KnownVehicleStore.to_file
    # does NOT mkdir — that's by design (callers control where files
    # land). For a brand-new enrollment, data/vehicles/ is normally
    # already there (paths.ensure_dirs() at listener bootstrap), but a
    # test fixture or a non-default --known-file may point at a path
    # whose parent doesn't exist yet.
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    store.to_file(path)

    log.info("Enrolled %s (%r) from alert %s", entry["id"], label, alert.get("alert_id"))
    return entry


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="enroll_vehicle_from_alert.py",
        description=(
            "Enroll an unknown-vehicle alert as a known entry. "
            "Reads refined vision fields from the alert and writes "
            "data/vehicles/known_vehicles.json via KnownVehicleStore."
        ),
    )
    parser.add_argument(
        "--alert-id", required=True,
        help="Alert id from the Telegram notification (UUID — Phase 6B.57 retired V-NNN suffix)",
    )
    parser.add_argument(
        "--date", default=None,
        help="YYYY-MM-DD of the alert JSONL file. Default: scan all dates.",
    )
    parser.add_argument(
        "--label", required=True,
        help='Human label, e.g. "employee_a\'s Chevy pickup"',
    )
    parser.add_argument("--color", default=None, help="Override color (default: from alert)")
    parser.add_argument("--type", default=None, help="Override body type (default: from alert)")
    parser.add_argument("--make", default=None, help="Override make (default: from alert)")
    parser.add_argument("--model", default=None, help="Override model (default: from alert)")
    parser.add_argument("--plate", default=None, help="License plate if known")
    parser.add_argument("--note", default=None,
                        help='Free-text note, e.g. "Visits Tuesdays + Thursdays"')
    parser.add_argument("--distinctive-features", default=None,
                        help="Free-text distinctive features (e.g. 'wheel flares outside bed')")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print the proposed entry and exit without writing")
    parser.add_argument("--alerts-dir", default=None,
                        help=f"Override alerts directory. Default: {ALERTS_DIR}")
    parser.add_argument("--known-file", default=None,
                        help=f"Override known_vehicles.json path. Default: {VEHICLE_KNOWN_FILE}")
    return parser


def main(argv: list | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stderr,
    )

    alerts_dir = args.alerts_dir or ALERTS_DIR
    known_file = args.known_file or VEHICLE_KNOWN_FILE

    alert = find_alert(args.alert_id, alerts_dir=alerts_dir, date_str=args.date)
    if alert is None:
        log.error(
            "alert %r not found in %s (date=%s)",
            args.alert_id, alerts_dir, args.date or "all",
        )
        return 2

    overrides = {
        "color": args.color,
        "type": args.type,
        "make": args.make,
        "model": args.model,
        "plate": args.plate,
        "note": args.note,
        "distinctive_features": args.distinctive_features,
    }

    try:
        entry = enroll(
            alert, args.label, overrides,
            dry_run=args.dry_run,
            known_file=known_file,
        )
    except LookupError as err:
        log.error("%s", err)
        return 2
    except ValueError as err:
        log.error("%s", err)
        return 3
    except FileNotFoundError as err:
        log.error("%s", err)
        return 2

    print(json.dumps(entry, indent=2, ensure_ascii=False))
    # Sanity: emit schema version so the operator can verify the
    # canonical store wrote the right format. Quiet on stderr.
    log.debug("known_vehicles.json schema version: %d", KNOWN_VEHICLES_SCHEMA)
    return 0


if __name__ == "__main__":
    sys.exit(main())
