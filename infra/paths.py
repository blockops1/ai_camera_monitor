"""
paths.py — Single source of truth for filesystem paths and env-derived config.

STATUS: stable
THREAD SAFETY: single-threaded (constants computed at import time)

INPUTS:
    - env ai_camera_monitor_PRODUCTION (default "0") — bool flag, gates file handlers
    - env ai_camera_monitor_ROOT (default ai_camera_monitor)
    - env ai_camera_monitor_DATA_DIR (default $PROJECT_ROOT/data)
    - env FARM_IDENTITY_BACKUP_DIR (default "") — face-recognition backup dir
    - env FARM_VEHICLE_ARRIVING_ENABLED (default "0") — bool feature flag
    - env FARM_VEHICLE_ESCALATION_ENABLED (default "0") — bool feature flag
    - env BROWSER_CHROME_PATH (default /Applications/Google Chrome.app/...)
      — Phase 6B.166 §11.87.2. Override for cam_browser.py Playwright launch.
    - file camera-creds.env — camera RTSP credentials
    - file telegram-creds.env — Telegram bot credentials
    - file llm-creds.env — LLM and vision model endpoints (Phase 6B.146)

OUTPUTS:
    - PROJECT_ROOT, DATA_DIR, LOGS_DIR, FRAMES_DIR, ALERTS_DIR,
      IDENTITIES_DIR, AUDIT_LOG_DIR, STATE_DIR, VEHICLES_DIR,
      ANIMALS_DIR — top-level data subdirectories
      VEHICLE_ARTIFACTS_DIR — directory paths
    - LISTENER_LOG, CLEANUP_LOG — log file paths
    - VEHICLE_KNOWN_FILE — known vehicles JSON path
    - CAMERA_CREDS_FILE, TELEGRAM_CREDS_FILE, LLM_CREDS_FILE — env
      file paths (Phase 6B.146 added LLM_CREDS_FILE)
    - BROWSER_CHROME_PATH — Phase 6B.166 §11.87.2, Chrome binary path
    - MOTION_RECIPE_FILE — Phase 6B.166 §11.87.3, motion recipe JSON path
    - PRODUCTION_MODE — bool gate for production-mode behavior
    - FRAME_RETENTION_HOURS / FRAME_MAX_BYTES / ALERT_RETENTION_DAYS /
      AUDIT_RETENTION_DAYS / CLEANUP_INTERVAL_S — retention constants
    - LOCAL_TZ — `ZoneInfo("America/New_York")`; all TZ-aware timestamps
      in this repo use this constant so audit logs, cleanup summaries,
      cooldown timestamps, and Telegram-message lines stay consistent.
    - ensure_dirs() — creates the directory tree if missing

PUBLIC API:
    ensure_dirs() -> None
        Create every directory this module exports if it doesn't already
        exist. Idempotent. Called once at listener bootstrap.
    audit_log_path(timestamp: datetime | None = None) -> str
        Return the audit log file path for the given date (default: today).
    _env_flag(name: str) -> bool
        Internal: parse a "1"/"0" env var. Used at import time only.

DOES NOT DO:
    - Read credentials from files (camera-creds.env, telegram-creds.env
      live in their own modules)
    - Mkdir anything not already declared above
    - Validate paths against permissions — caller checks at use time

WHY HERE:
    Module imports it for every path or env-derived constant. Keeping
    the source of truth in one place lets tests override ai_camera_monitor_DATA_DIR
    in conftest.py and target a tmp_path copy without monkey-patching
    individual call sites. Two-system isolation contract (AGENTS.md §1)
    is enforced by _DEFAULT_PROJECT_ROOT pointing at the refactor tree,
    never at ~/ai_camera_monitor/.

CALLED BY:
    - listener.listener: LISTENER_LOG, ensure_dirs, all *_DIR constants
    - infra.cooldown: cooldown override file paths
    - infra.cleanup: FRAME_*, ALERT_RETENTION_DAYS, AUDIT_*
    - infra.logging_setup: LISTENER_LOG, PRODUCTION_MODE
    - infra.recipe: MOTION_RECIPE_FILE (Phase 6B.166 §11.87.3)
    - infra.camera_creds: CAMERA_CREDS_FILE
    - scripts/cam_browser.py: BROWSER_CHROME_PATH, CAMERA_CREDS_FILE
      (Phase 6B.166 §11.87.2)
    - every infra module that touches a file or env var

CALLS INTO:
    - os.environ, os.path.expanduser, os.path.join
    - pathlib.Path (for alert_generator's override config)
    - datetime (for audit_log_path timestamp arg)

RELATED:
    - .gitignore (data/, logs/, person-tracker/) — files this module
      emits live under these ignored dirs
    - tests/sandbox/data/ (copied to tmp_path by conftest.py sandbox
      fixture, used by every test that touches files)
"""

import os
from zoneinfo import ZoneInfo

PRODUCTION_MODE = os.environ.get("ai_camera_monitor_PRODUCTION", "0") == "1"

# Single source of truth for the local timezone. Every TZ-aware timestamp
# in this repo (audit logs, cleanup summaries, cooldown records, Telegram
# message timestamps) uses LOCAL_TZ so we don't mix naive and aware datetimes.
# ZoneInfo is the stdlib tz implementation since 3.9 — no pytz dependency.
LOCAL_TZ = ZoneInfo("America/New_York")

# PROJECT_ROOT — the refactor listener is its own self-contained tree.
# Hardcoded to the refactor root by default; tests override with
# ai_camera_monitor_ROOT. There is NO ai_camera_monitor_ROOT pointing at
# the old repo — that would defeat the two-system isolation contract.
_DEFAULT_PROJECT_ROOT = os.path.expanduser("ai_camera_monitor")
PROJECT_ROOT = os.environ.get(
    "ai_camera_monitor_ROOT", _DEFAULT_PROJECT_ROOT
)

# DATA_DIR can be overridden via ai_camera_monitor_DATA_DIR (used by tests)
# In production this is PROJECT_ROOT/data; in tests it's a tmp_path
# copy of tests/sandbox/data/ copied by conftest.py sandbox_data fixture.
_DATA_DIR_OVERRIDE = os.environ.get("ai_camera_monitor_DATA_DIR")
DATA_DIR = (
    _DATA_DIR_OVERRIDE
    if _DATA_DIR_OVERRIDE
    else os.path.join(PROJECT_ROOT, "data")
)

# Source code
SRC_DIR = os.path.join(PROJECT_ROOT, "src")

# Operational logs (rotated, kept long-term)
LOGS_DIR = os.path.join(PROJECT_ROOT, "logs")

# Captured frames, grouped by alert_id in dated folders (auto-cleaned after FRAME_RETENTION_HOURS)
# NOTE: DATA_DIR is set above (lines 21-25) with ai_camera_monitor_DATA_DIR override
# support. Do NOT reassign here — it would clobber the test sandbox redirect.
FRAMES_DIR = os.path.join(DATA_DIR, "frames")

# Alert JSON history (append-only JSONL, one file per day, kept permanently)
ALERTS_DIR = os.path.join(DATA_DIR, "alerts")

# Identities (face DB, encrypted at rest — biometric data)
IDENTITIES_DIR = os.path.join(DATA_DIR, "identities")

# Identity backup (face embeddings).
#
# POLICY: face embeddings are biometric data. By DEFAULT they stay on the
# Mac only — no off-host mirror, no NAS copy, no remote sync. The Mac mini
# already runs Time Machine to a Synology share, which gives disaster
# recovery without exposing biometric vectors to additional surfaces.
#
# If you ever want to mirror identities off-host, opt in explicitly by
# setting FARM_IDENTITY_BACKUP_DIR=/path/to/backup. Empty string (the
# default) disables the backup step entirely.
#
# Burned 2026-07-20: defaulting to the NAS path meant mr_v.json and
# jeremiah.json were silently mirrored on every face enrollment. Privacy
# stance is "local-only unless explicitly opted in."
IDENTITY_BACKUP_DIR = os.environ.get("FARM_IDENTITY_BACKUP_DIR", "")

# Append-only audit log (one file per day)
AUDIT_LOG_DIR = os.path.join(DATA_DIR, "audit")

# Property state (current occupants)
STATE_DIR = os.path.join(DATA_DIR, "state")

# Vehicle state
#   known_vehicles.json  — vehicles maintainer recognizes (color, type, label)
#   on_property.json     — runtime: is each vehicle on the property right now?
#                         Updated by Outside Front Solar vision sightings
#                         (the gatekeeper camera; previously Building Front
#                         Corner pre-2026-07-29).
#   identity.json        — V-NNN stable IDs + per-vehicle attributes (Phase 6B.6+).
#                         Minted on first sighting, auto-backfilled from
#                         known_vehicles.json on import. Vision attributes
#                         populated now; bluetooth/wifi populated by future
#                         correlators.
VEHICLES_DIR = os.path.join(DATA_DIR, "vehicles")
VEHICLE_KNOWN_FILE = os.path.join(VEHICLES_DIR, "known_vehicles.json")
VEHICLE_STATE_FILE = os.path.join(VEHICLES_DIR, "on_property.json")
VEHICLE_UNKNOWN_STATE_FILE = os.path.join(VEHICLES_DIR, "unknown_state.json")
VEHICLE_IDENTITY_FILE = os.path.join(VEHICLES_DIR, "identity.json")

# Animal state (Phase 6B.165 §11.86.5, 2026-08-30).
#   known_animals.json — animals maintainer recognizes (species, features, label).
#                         Empty until first enrollment via
#                         scripts/enroll_animal.py. Mirrors
#                         data/vehicles/known_vehicles.json shape.
#                         Consumed by infra.animal_matcher.match_animal().
ANIMALS_DIR = os.path.join(DATA_DIR, "animals")
ANIMAL_KNOWN_FILE = os.path.join(ANIMALS_DIR, "known_animals.json")

# Vehicle tracker artifacts (Phase 6B.6 audit trail, 2026-07-24).
# One subdirectory per alert with the 6 captured frames, the cropped
# vehicle region, the raw first-pass vision result, and the raw
# focused_classify (second-pass) output. Exempt from the 12h frame
# cleanup. Disable with FARM_VEHICLE_ARTIFACTS=0 in the env.
VEHICLE_ARTIFACTS_DIR = os.path.join(DATA_DIR, "vehicle_artifacts")

# Channel retirement — vehicle_tracker is the only Telegram source for
# vehicle events (project plan §"Phase 6B.9 vehicle-first two-message
# alert UX" called for this). The two listener-side channels are
# retired by default; flip the env var to 1 to re-enable.
#
#   FARM_VEHICLE_ARRIVING_ENABLED=1   re-enables channel #1, the +2s
#                                     "Vehicle moving on property at
#                                     <camera>, identifying..." heads-up.
#   FARM_VEHICLE_ESCALATION_ENABLED=1 re-enables channel #3, the
#                                     alert_notifier escalation block
#                                     for unknown_arrival events.
#                                     (vehicle_tracker already emits
#                                     "Unknown vehicle V-NNN arrived"
#                                     for the same events.)
def _env_flag(name: str) -> bool:
    """Read a FARM_* env var as a boolean. Truthy values: '1', 'true',
    'yes' (case-insensitive). Anything else (including unset) is False.
    """
    val = os.environ.get(name, "").strip().lower()
    return val in ("1", "true", "yes")

VEHICLE_ARRIVING_ENABLED = _env_flag("FARM_VEHICLE_ARRIVING_ENABLED")
VEHICLE_ESCALATION_ENABLED = _env_flag("FARM_VEHICLE_ESCALATION_ENABLED")

# Credentials
CAMERA_CREDS_FILE = os.path.join(PROJECT_ROOT, "camera-creds.env")
TELEGRAM_CREDS_FILE = os.path.join(PROJECT_ROOT, "telegram-creds.env")
LLM_CREDS_FILE = os.path.join(PROJECT_ROOT, "llm-creds.env")  # Phase 6B.146

# Phase 6B.167 §13.2 NEW-schema camera registry. Precedence:
#   1. $FARMSURV_CAMERAS_ENV (operator can pin an arbitrary path)
#   2. CAMERAS_ENV_FILE (the default location, NEW schema)
#   3. infra.cameras falls back to CAMERA_CREDS_FILE (legacy parse)
# The operator runs the listener with FARMSURV_CAMERAS_ENV unset,
# so CAMERAS_ENV_FILE wins when present; if it's missing, the
# legacy parser takes over (back-compat).
CAMERAS_ENV_FILE = os.path.join(PROJECT_ROOT, "cameras.env")

# Motion recipe (Phase 6B.166 §11.87.3, 2026-08-30).
#   config/motion_recipe.json — fleet default + per-camera overrides
#     for RLC-510A motion/smart/delay sliders. Read by infra/recipe.py,
#     consumed by scripts/tune_510a_motion_sensitivity.py and
#     scripts/apply_all_tuning.py. JSON (not YAML) per proposal —
#     matches the existing *.json pattern in config/ (alert_overrides.json,
#     motion_gate_thresholds.json). Not gitignored — version-controlled
#     so the recipe history is visible in git log.
MOTION_RECIPE_FILE = os.path.join(PROJECT_ROOT, "config", "motion_recipe.json")

# Browser automation — path to the Chrome binary used by
# scripts/cam_browser.py (Playwright). Defaults to system Chrome on
# macOS; override with BROWSER_CHROME_PATH if Playwright falls back to
# bundled chromium and you want to point at a different install
# (e.g. a portable Chrome, a different Chromium build). Phase 6B.166 §11.87.2.
_DEFAULT_CHROME_PATH = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
BROWSER_CHROME_PATH = os.environ.get("BROWSER_CHROME_PATH", _DEFAULT_CHROME_PATH)

# macOS Keychain — biometric encryption key storage
KEYCHAIN_SERVICE = "ai_camera_monitor.faces"
KEYCHAIN_ACCOUNT = "primary"

# Retention
# Frames: 24 hours locally (semi-realtime — historical goes to Synology).
# Was 7 days (2026-07-20); reduced to 12h after observing 564 MB
# accumulating over a couple of days with no cleanup while listener ran.
# Bumped to 24h on 2026-07-28 (<owner-name>) — wanted a full-day buffer for
# late-night forensics. Frame size cap also doubled 5 GB → 10 GB to
# give the 24h window headroom against solar-camera motion storms.
FRAME_RETENTION_HOURS = 24
# Legacy alias — kept so external code referencing FRAME_RETENTION_DAYS
# keeps working. cleanup.py prefers HOURS when both are exported.
FRAME_RETENTION_DAYS = 0  # 0 = use HOURS path; this is a sentinel, NOT days

# Alert JSONL retention (separate from frames)
ALERT_RETENTION_DAYS = 7

# Hard disk budget for FRAMES_DIR. If exceeded, oldest folders deleted
# first regardless of age. Bounded cleanup — without this, a camera
# that floods (e.g. solar-camera-motion-storm) can blow past the time
# retention and fill the disk.
FRAME_MAX_BYTES = 10 * 1024 * 1024 * 1024  # 10 GB

# Audit log rotation
AUDIT_RETENTION_DAYS = 90

# Cleanup cadence — daemon thread runs every N seconds
CLEANUP_INTERVAL_S = 60 * 60  # 1 hour

# Log filenames
LISTENER_LOG = os.path.join(LOGS_DIR, "listener.log")
CLEANUP_LOG = os.path.join(LOGS_DIR, "cleanup.log")


def ensure_dirs() -> None:
    """Create all required directories if they don't exist."""
    for d in [
        LOGS_DIR,
        FRAMES_DIR,
        ALERTS_DIR,
        IDENTITIES_DIR,
        AUDIT_LOG_DIR,
        STATE_DIR,
        VEHICLES_DIR,
        ANIMALS_DIR,
    ]:
        os.makedirs(d, exist_ok=True)


def audit_log_path(timestamp=None) -> str:
    """Return path to today's audit log file. Pass a datetime for another day."""
    from datetime import datetime

    if timestamp is None:
        timestamp = datetime.now(LOCAL_TZ)
    return os.path.join(AUDIT_LOG_DIR, f"{timestamp.strftime('%Y-%m-%d')}.jsonl")
