"""
<nas>_preview.py — Locate <nas> Surveillance Station preview thumbnails by camera + timestamp.

STATUS: stable
THREAD SAFETY: thread-safe (pure file-system lookup, no shared state)

INPUTS:
    - mounted <nas> share at NAS_ROOT (default
      /Users/jill/mnt/<site> — the SMB mount created when the
      user logs into the share)
    - function arg `camera_name: str` (required) — canonical camera
      name (the operator's friendly name from cameras.env). Shorthand
      aliases are not accepted here; resolve through infra.cameras
      before calling this module.
    - function arg `target_ts: int` (required) — Unix timestamp in
      seconds (UTC). Use the EDT->UTC conversion from the caller.

OUTPUTS:
    - return value: str | None — absolute path to the JPEGG closest to
      target_ts within the 30-min recording session covering that
      timestamp. None if no preview exists (camera missing, recording
      not active at that time, or > ~7 days ago — <nas> retention).

PUBLIC API:
    find_preview(camera_name: str, target_ts: int) -> str | None
        Return the absolute path to the preview JPEG closest to
        `target_ts` for `camera_name`, or None if no preview exists.

DOES NOT DO:
    - Mount/unmount the SMB share — that's owned by the user's
      session, not by us
    - Stream video — preview thumbnails are 320x180 / 13 KB; if you
      need the full MP4 frame, use ffmpeg directly on the .mp4 file
    - Handle multiple cameras in one call — single-camera-per-call to
      keep the lookup trivial
    - Cache results — the NAS path is already fast for a single read;
      no caching needed
    - Walk the tree recursively — <nas>'s structure is fixed:
      <camera>/@SSRECMETA/Preview/<YYYYMMDD{AM,PM}>/<epoch_dir>/<ts>

CALLED BY:
    - listener.listener: GET /preview endpoint, post-hoc lookups

CALLS INTO:
    - os, pathlib: filesystem operations
    - datetime, zoneinfo (or fixed EDT offset per memory rule):
      timestamp math

RELATED:
    - /Users/jill/mnt/<site>/<Camera Name>/@SSRECMETA/Preview/
      (the directory tree this module reads)
    - infra.paths.NAS_ROOT (the mount point constant; not yet
      defined — uses default /Users/jill/mnt/<site>)
"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from infra.cameras import load_camera_aliases as _load_camera_aliases

# <nas> mount root — the SMB share ~/mnt/<site> on this Mac.
# Phase.124 (2026-08-22): discovered this path while investigating a
# green-tractor-with-hay-bales event that didn't trigger a webhook.
NAS_ROOT = "/Users/jill/mnt/<site>"

# Module-level logger — used by _iterdir_with_retry's EINTR warning.
log = logging.getLogger(__name__)


# Phase.167 §13.4 Commit 18 (T3 C18): preview aliases are
# hydrated at import-time from cameras.env (operator-private,
# gitignored). The empty hardcoded fallback survives so the
# module imports cleanly without cameras.env (CI, tests, fresh
# checkouts). Operator-supplied shorthand from cameras.env is
# the source of truth when present.
_PREVIEW_CAMERA_ALIASES: dict[str, str] = {
    **_load_camera_aliases()[1],  # index 1 = preview dict
}


# Recording sessions are 30-minute chunks starting at xx:03:39 wall-clock.
# So a preview file at Unix ts T belongs to the session whose start_ts
# is the largest preview-dir name <= T (dir name = recording start ts).
_PREVIEW_RETENTION_DAYS = 7  # <nas> default — confirmed Aug 2026

# Phase.113b: macOS SMB-mount quirk — `os.scandir()` (and the
# `Path.iterdir()` that wraps it) sometimes raises InterruptedError
# (EINTR) spuriously against the <nas> SMB mount at
# /Users/jill/mnt/<site>. The mount layer returns EINTR when
# the kernel's mount-cache invalidates mid-call, which happens during
# large dir listings. Without retry, every call to /preview 500s with
# "InterruptedError: [Errno 4] Interrupted system call".
#
# These constants tune the retry: 3 attempts, backoff 50/100/200 ms.
# Worst-case wall-clock impact per iterdir call = 350 ms. The /preview
# endpoint does 3 iterdirs in the worst case, so worst-case total
# overhead = ~1s. The endpoint already has a 30s budget.
_INTERDIR_RETRY_ATTEMPTS = 3
_INTERDIR_RETRY_BACKOFF_MS = (50, 100, 200)


def _iterdir_with_retry(path: Path) -> list[Path]:
    """Call path.iterdir() with retry on InterruptedError (EINTR).

    macOS SMB mounts intermittently raise EINTR when the kernel's
    mount-cache invalidates during a directory listing. A single retry
    almost always resolves it; we cap at 3 attempts with exponential
    backoff.

    Other exceptions (FileNotFoundError, PermissionError, NotADirectoryError)
    propagate normally — those are real errors, not transient ones.
    """
    last_err: InterruptedError | None = None
    for attempt in range(_INTERDIR_RETRY_ATTEMPTS):
        try:
            return list(path.iterdir())
        except InterruptedError as e:
            last_err = e
            if attempt < _INTERDIR_RETRY_ATTEMPTS - 1:
                backoff_ms = _INTERDIR_RETRY_BACKOFF_MS[attempt]
                log.warning(
                    f"<nas>_preview: iterdir({path}) got EINTR, "
                    f"retry {attempt + 1}/{_INTERDIR_RETRY_ATTEMPTS} "
                    f"in {backoff_ms}ms"
                )
                time.sleep(backoff_ms / 1000.0)
    # All retries exhausted — re-raise the last InterruptedError
    assert last_err is not None
    raise last_err

# Files-per-recording-session. With 20-second cadence and 30-minute
# sessions, that's ~90 files. Real count varies slightly.
_FILES_PER_SESSION = 90


def _resolve_camera(arg: str) -> str | None:
    """Validate that `arg` is a known camera directory on <nas>.

    Returns the canonical name to use as a subdirectory of NAS_ROOT,
    or None if the camera is unknown. With the alias dict empty, the
    canonical name is passed through directly.

    Returns None if the camera is unknown. Case-sensitive on canonical
    names so typos surface as 400 instead of silent acceptance.
    """
    if arg in _PREVIEW_CAMERA_ALIASES:
        return _PREVIEW_CAMERA_ALIASES[arg]
    return arg if os.path.isdir(os.path.join(NAS_ROOT, arg)) else None


def _day_folder_for(target_ts: int) -> list[str]:
    """Return the candidate day-folder names for a target timestamp.

    <nas> uses two half-day folders per recording pool rotation:
    <YYYYMMDD>AM (recordings started before ~8 AM) and <YYYYMMDD>PM
    (recordings started ~8 AM through end of day). The exact split
    depends on when the recording pool rolls over — not wall-clock
    noon.

    To be robust against this drift, return both the AM and PM folder
    names for the target's local date AND the previous date's PM
    folder (handles targets in the 4-8 AM window).
    """
    EDT = timezone(timedelta(hours=-4))
    dt = datetime.fromtimestamp(target_ts, tz=EDT)
    yesterday = datetime.fromtimestamp(target_ts - 86400, tz=EDT)
    seen: set[str] = set()
    result: list[str] = []
    for candidate in [
        dt.strftime("%Y%m%dPM"),
        dt.strftime("%Y%m%dAM"),
        yesterday.strftime("%Y%m%dPM"),
    ]:
        if candidate not in seen:
            seen.add(candidate)
            result.append(candidate)
    return result


def _find_best_session_dir(
    preview_root: Path, target_ts: int
) -> Path | None:
    """Find the session directory whose preview is closest to target_ts.

    Walks all candidate day folders and returns the session whose
    closest-preview-file has the smallest delta to target_ts. Falls
    back to "any session within the day folders" so a target at
    13:10 EDT lands in 20260822PM even though my naive AM/PM split
    rule might suggest AM first.

    Returns the dir + the closest file's ts so the caller doesn't
    re-scan the dir.
    """
    best: tuple[int, Path, int] | None = None  # (abs_delta, dir, ts)
    for day_folder in _day_folder_for(target_ts):
        day_path = preview_root / day_folder
        if not day_path.is_dir():
            continue
        for entry in _iterdir_with_retry(day_path):
            try:
                int(entry.name)
            except ValueError:
                continue
            if not entry.is_dir():
                continue
            # Find closest file in this session dir
            closest_ts = _closest_file_ts(entry, target_ts)
            if closest_ts is None:
                continue
            delta = abs(closest_ts - target_ts)
            # Sanity bound: skip sessions farther than 1 hour from the
            # target (e.g. yesterday's PM session when looking for 8 AM
            # today). Keeps the loop bounded.
            if delta > 3600:
                continue
            if best is None or delta < best[0]:
                best = (delta, entry, closest_ts)
    if best is None:
        return None
    _, session_dir, _ = best
    return session_dir


def _closest_file_ts(session_dir: Path, target_ts: int) -> int | None:
    """Return the ts of the file in session_dir closest to target_ts.

    Helper for _find_best_session_dir; does not return the Path because
    the caller wants to compare across multiple session dirs.
    """
    best: tuple[int, int] | None = None  # (abs_delta, ts)
    for entry in _iterdir_with_retry(session_dir):
        try:
            ts = int(entry.name)
            if entry.is_file():
                delta = abs(ts - target_ts)
                if best is None or delta < best[0]:
                    best = (delta, ts)
        except ValueError:
            continue
    return best[1] if best else None


def _closest_preview_file(session_dir: Path, target_ts: int) -> Path | None:
    """Find the file in session_dir closest to target_ts.

    Returns the path with the smallest absolute time delta. If the
    session is empty, returns None. Files are named by their Unix
    timestamp in seconds (no extension).
    """
    files: list[tuple[int, Path]] = []
    for entry in _iterdir_with_retry(session_dir):
        try:
            ts = int(entry.name)
            if entry.is_file():
                files.append((ts, entry))
        except ValueError:
            continue
    if not files:
        return None
    files.sort(key=lambda t: (abs(t[0] - target_ts), t[0]))
    return files[0][1]


def find_preview(camera_name: str, target_ts: int) -> str | None:
    """Locate the <nas> preview JPEG closest to `target_ts`.

    Args:
        camera_name: canonical camera name (operator-friendly name from
            cameras.env). Unknown names return None.
        target_ts: Unix timestamp in seconds. The caller is responsible
            for converting local EDT to UTC.

    Returns:
        Absolute path to a JPEG file (~13 KB, 320x180), or None if no
        preview exists for this camera at this time. None is also
        returned for cameras unknown to <nas>, for times outside the
        retention window (~7 days), or for any I/O failure.
    """
    canonical = _resolve_camera(camera_name)
    if canonical is None:
        return None

    camera_dir = Path(NAS_ROOT) / canonical / "@SSRECMETA" / "Preview"
    if not camera_dir.is_dir():
        return None

    session_dir = _find_best_session_dir(camera_dir, target_ts)
    if session_dir is None:
        return None
    preview_file = _closest_preview_file(session_dir, target_ts)
    if preview_file is not None:
        return str(preview_file.resolve())
    return None


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def _main() -> int:
    import argparse
    import subprocess
    import sys

    parser = argparse.ArgumentParser(
        description="Locate <nas> preview JPEG for a camera + timestamp.",
    )
    parser.add_argument(
        "--camera", required=True,
        help='Camera canonical name (operator-friendly name from cameras.env).',
    )
    parser.add_argument(
        "--ts", required=True,
        help='Timestamp in "YYYY-MM-DD HH:MM:SS EDT" or ISO 8601 format.',
    )
    parser.add_argument(
        "--open", action="store_true",
        help="Open the result in Preview.app (macOS only).",
    )
    parser.add_argument(
        "--copy-to", default=None,
        help="Copy the result to this path instead of just printing.",
    )
    args = parser.parse_args()

    # Parse ts — accept "YYYY-MM-DD HH:MM:SS EDT" or ISO 8601
    ts_str = args.ts.strip()
    if ts_str.endswith(" EDT"):
        # Local EDT per the project's always-EDT memory rule
        EDT = timezone(timedelta(hours=-4))
        dt = datetime.strptime(ts_str[:-4].strip(), "%Y-%m-%d %H:%M:%S").replace(tzinfo=EDT)
        target_ts = int(dt.timestamp())
    else:
        try:
            # ISO 8601 with timezone
            dt = datetime.fromisoformat(ts_str)
            target_ts = int(dt.timestamp())
        except ValueError:
            print(f"ERROR: cannot parse ts: {ts_str!r}", file=sys.stderr)
            print('Expected "YYYY-MM-DD HH:MM:SS EDT" or ISO 8601.', file=sys.stderr)
            return 2

    result = find_preview(args.camera, target_ts)
    if result is None:
        print(f"No preview found for {args.camera!r} at {args.ts!r}", file=sys.stderr)
        return 1

    if args.copy_to:
        import shutil
        shutil.copy(result, args.copy_to)
        print(args.copy_to)
    else:
        print(result)

    if args.open:
        subprocess.run(["open", result], check=False)

    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
