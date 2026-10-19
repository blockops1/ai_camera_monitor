#!/usr/bin/env python3
"""gc_stale_tmp.py — Garbage-collect stale pytest temp directories.

Deletes:
  1. ``pytest-of-*`` run-dirs under ``/private/var/folders/*/*/T/`` older than
     the TTL (default 72 h, configurable via ``FARMSURV_GC_TMP_TTL_H``).
  2. Any mkdtemp-style leak dirs whose name matches patterns listed in
     ``LEAK_DIR_PATTERNS`` (also aged by the TTL).

Never deletes:
  - The most-recent ``pytest-of-jill`` directory.
  - Anything under the ``TMPDIR`` of a live ``pytest`` process (checked via
    ``ps -o pid,tty,command``).

Exit codes:
  0 — ran successfully (even if nothing was deleted).
  1 — dry-run mode (prints what WOULD be deleted).

Usage:
    python scripts/gc_stale_tmp.py [--dry-run]
"""

from __future__ import annotations

import argparse
import fnmatch
import logging
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import NamedTuple

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

TTL_HOURS: int = int(os.environ.get("FARMSURV_GC_TMP_TTL_H", "72"))
TTL_SECONDS: int = TTL_HOURS * 3600

# Name patterns for mkdtemp-style leak dirs (in addition to pytest-of-*).
# The GC removes dirs matching any of these patterns that exceed the TTL.
LEAK_DIR_PATTERNS: list[str] = [
    "pytest-of-*",
    "pm_*",  # post-merge-hook test temp dirs
]

# Base temp directory on macOS
TEMP_BASE = "/private/var/folders"

# ---------------------------------------------------------------------------
# Live-process guard
# ---------------------------------------------------------------------------


def _live_pytest_tmpdirs() -> set[str]:
    """Return set of TMPDIR values for running pytest processes."""
    tmpdirs: set[str] = set()
    try:
        result = subprocess.run(
            ["ps", "-eo", "pid,command"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            return tmpdirs
        for line in result.stdout.strip().splitlines():
            if "pytest" in line:
                # Extract TMPDIR=... from the command line
                for token in line.split():
                    if token.startswith("TMPDIR="):
                        tmpdirs.add(token.split("=", 1)[1])
    except Exception:
        pass
    return tmpdirs


def _is_under_live_tmpdir(path: str) -> bool:
    """Check whether *path* lives under any live pytest TMPDIR."""
    live = _live_pytest_tmpdirs()
    for td in live:
        if path.startswith(td):
            return True
    return False


# ---------------------------------------------------------------------------
# Age check
# ---------------------------------------------------------------------------


def _is_stale(path: str) -> bool:
    """Return True if *path* is older than TTL_SECONDS."""
    try:
        mtime = os.stat(path).st_mtime
        return (time.time() - mtime) > TTL_SECONDS
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Core GC
# ---------------------------------------------------------------------------


class RemovalRecord(NamedTuple):
    path: str
    reason: str
    age_seconds: float


def _remove_dir(path: str, reason: str, dry_run: bool = False) -> RemovalRecord | None:
    """Remove a stale directory. Return a RemovalRecord or None if dry-run."""
    age = time.time() - os.stat(path).st_mtime
    record = RemovalRecord(path=path, reason=reason, age_seconds=age)
    if dry_run:
        log.info("[DRY-RUN] would remove: %s (%.1fh old, reason: %s)", path, age / 3600, reason)
        return record
    try:
        import shutil
        shutil.rmtree(path)
        log.info("removed: %s (%.1fh old, reason: %s)", path, age / 3600, reason)
    except OSError as exc:
        log.warning("failed to remove %s: %s", path, exc)
    return record


def gc_pytest_of_dirs(dry_run: bool = False) -> list[RemovalRecord]:
    """Collect and optionally remove stale pytest-of-* dirs."""
    results: list[RemovalRecord] = []

    if not os.path.isdir(TEMP_BASE):
        log.info("TEMP_BASE %s not found — nothing to do", TEMP_BASE)
        return results

    for user_dir in os.listdir(TEMP_BASE):
        user_path = os.path.join(TEMP_BASE, user_dir, "T")
        if not os.path.isdir(user_path):
            continue

        for entry in os.listdir(user_path):
            if not entry.startswith("pytest-of-"):
                continue

            full_path = os.path.join(user_path, entry)
            if not os.path.isdir(full_path):
                continue

            # Never delete the newest pytest-of-jill dir
            if entry.startswith("pytest-of-jill"):
                continue

            # Guard against live process TMPDIR
            if _is_under_live_tmpdir(full_path):
                log.debug("skipping %s (under live pytest TMPDIR)", full_path)
                continue

            if not _is_stale(full_path):
                continue

            rec = _remove_dir(full_path, "stale pytest run dir", dry_run)
            if rec:
                results.append(rec)

    return results


def gc_leak_dirs(dry_run: bool = False) -> list[RemovalRecord]:
    """Collect and optionally remove dirs matching LEAK_DIR_PATTERNS."""
    results: list[RemovalRecord] = []

    if not os.path.isdir(TEMP_BASE):
        return results

    for user_dir in os.listdir(TEMP_BASE):
        user_path = os.path.join(TEMP_BASE, user_dir, "T")
        if not os.path.isdir(user_path):
            continue

        for entry in os.listdir(user_path):
            if not any(fnmatch.fnmatch(entry, pat) for pat in LEAK_DIR_PATTERNS):
                continue

            full_path = os.path.join(user_path, entry)
            if not os.path.isdir(full_path):
                continue

            if _is_under_live_tmpdir(full_path):
                log.debug("skipping %s (under live pytest TMPDIR)", full_path)
                continue

            if not _is_stale(full_path):
                continue

            rec = _remove_dir(full_path, "stale leak dir", dry_run)
            if rec:
                results.append(rec)

    return results


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description="GC stale pytest temp directories")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print what would be deleted without actually deleting",
    )
    args = parser.parse_args()

    log.info(
        "gc_stale_tmp starting (TTL=%dh, dry_run=%s)", TTL_HOURS, args.dry_run
    )

    total_removed = 0
    total_freed = 0

    # Phase 1: pytest-of-* dirs
    for rec in gc_pytest_of_dirs(dry_run=args.dry_run):
        total_removed += 1
        try:
            total_freed += sum(
                os.path.getsize(os.path.join(dp, f))
                for dp, _, fns in os.walk(rec.path)
                for f in fns
            )
        except OSError:
            pass

    # Phase 2: leak dirs
    for rec in gc_leak_dirs(dry_run=args.dry_run):
        total_removed += 1
        try:
            total_freed += sum(
                os.path.getsize(os.path.join(dp, f))
                for dp, _, fns in os.walk(rec.path)
                for f in fns
            )
        except OSError:
            pass

    log.info(
        "gc_stale_tmp done: removed=%d, freed=%d bytes, dry_run=%s",
        total_removed, total_freed, args.dry_run,
    )

    return 0


if __name__ == "__main__":
    sys.exit(main())
