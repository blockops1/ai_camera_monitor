"""
listener/listener.py — Flask HTTP server that ties the pipeline together.

Receives camera alerts via POST /alert, validates them against known
camera configuration, and runs the full pipeline:
    capture_frames → analyze_frames → generate_alert → notify → append_alert

STATUS: stable
THREAD SAFETY: uses threading.Lock (the persistent RTSP reader holds a
ring buffer that's read on the request thread; alerts are processed
sequentially via a single-process Flask dev server).

INPUTS:
    - env vars:
        MOTION_GATE_ENABLED — when "1", runs the motion gate
        PIPELINE_USES_GATE_CROPS — when "1", reuses gate's crops
        GATE_KEEP_DISK_ARTIFACTS — when "1", writes crops to disk
        FARMSURV_PRODUCTION — when "1", enables production paths
        telegram-creds.env — bot_token + chat_id (mode 600)
    - HTTP POST /alert payload (Reolink alarm JSON)
    - camera configuration (loaded from data/cameras.json at startup)

OUTPUTS:
    - Telegram messages (TG#1 arriving, TG#2 composite, TG#3 match)
    - data/audit_telegram/ JSONL audit lines (Phase 6B.124)
    - data/alerts/YYYY-MM-DD.jsonl append-only alert history
    - data/frames/<alert_id>/ persisted frame + crop artifacts
    - logs/listener.log INFO/WARNING/ERROR lines
    - HTTP GET /health returns {"cameras_loaded":N, "status":"ok"}
    - Phase 6B.154 (§11.77): cooldown-suppressed alerts logged but produce no
      frame artifacts, no audit row, no Telegram. The cooldown decision is
      silent to the user (no notification fires) — only logs/listener.log
      records that the alert was filtered.

PUBLIC API:
    create_app(test_config=None) -> Flask
        Flask app factory. Wires /alert, /health, /frame/<id>, /video_feed.
    _process_alert(alert_id, camera_name, timestamp, event, rtsp_url,
                    gate_verdict=None) -> None
        Composes the 6-stage vehicle pipeline (capture → identify →
        match → select_best_frame → generate_alert → emit_result) plus
        the motion gate pre-step when MOTION_GATE_ENABLED=1.
    _process_person_alert(alert_id, camera_name, timestamp, event,
                          rtsp_url, gate_verdict=None) -> None
        Composes the person pipeline (mirror of _process_alert minus
        vehicle-matching + threat-level LLM).

DOES NOT DO:
    - Frame capture is delegated to infra.frame_capture (not here).
    - Vision parsing is delegated to infra.vision_response (not here).
    - Match scoring is delegated to vehicle_matcher (not here).
    - Telegram formatting is delegated to telegram_formatter (not here).

CALLED BY:
    - launchd (ai.farm.surveillance-listener-refactor.plist, port 8090)
    - tests under listener/tests/

CALLS INTO:
    - listener.vehicle_event_pipeline (the 6 vehicle stages)
    - listener.person_event_pipeline (the 4 person stages)
    - listener._motion_gate_dispatch (motion gate pre-step)
    - listener._gate_aware_capture (gate frame reuse)
    - infra.gate_cooldown.is_in_gate_cooldown (per-camera × per-event-type
      suppression at the gate; Phase 6B.154 / §11.77. Runs BEFORE the gate.)
    - infra.frame_capture, infra.send_telegram, infra.vision_response
    - vehicle_matcher.match_with_details
    - telegram_formatter.{composite_telegram, match_telegram,
                           person_telegram}
    - data/audit_telegram.log_outbound_telegram

RELATED:
    - listener.state — module-level mutable state for /health
    - data/cameras.json — camera configuration
    - data/alerts/YYYY-MM-DD.jsonl — alert history
    - PLAN.md §11.51 — Phase 6B.129a event promotion (md → vehicle)
    - PLAN.md §11.38 — Phase 6B.106 refactor (this module's current shape)
    - PLAN.md §11.61 — Phase 6B.140 (2026-08-27) CAM1 → CAM2 person-gatekeeper swap

Historical reference (Phase 6B.81 — extracted in 6B.106):
    format_qwen_confidence_line, format_detector_metadata_lines,
    annotate_frame_bboxes — moved to telegram_formatter/vehicle_alert.py.

Run:
    python3 -m listener.listener
or
    REPO_PATH/.venv/bin/python \\
        REPO_PATH/listener/listener.py

Listens on port 8090. SIGTERM via launchd triggers auto-restart
(~1s measured 2026-08-26; plist has ThrottleInterval=10).
"""

import os
import pathlib
import queue
import sys
import tempfile
import threading
import time
import uuid
from concurrent.futures import Future
from datetime import datetime
from importlib import import_module

try:
    # When listener.py runs as __main__, sys.path[0] = listener/, so 'state'
    # resolves to listener/state.py directly.
    from state import STATE
except ImportError:
    # When listener.py is imported as listener.listener (e.g. by tests under
    # pytest), the listener package's __path__ is used, so we have to reach
    # into it explicitly.
    from listener.state import STATE
from typing import Any, ClassVar, NamedTuple, cast

from flask import Flask, jsonify, request, send_file

# Refactor listener rule: every infra module is imported via the
# `infra.X` package path so the listener's sys.path need only contain
# REPO_PATH. Nothing in this file references
# LEGACY_PATH/src/.
from infra.cleanup import run_all as run_cleanup
from infra.cleanup import start_cleanup_thread
from infra.cooldown import MOTION_COOLDOWN
from infra.frame_capture import load_camera_creds, resolve_camera_name
from infra.cameras import (
    all_codes as _all_camera_codes,
    by_code as _by_camera_code,
    by_ip as _by_camera_ip,
    code_for as _code_for_camera,
    load_cameras as _load_camera_specs,
    load_camera_aliases as _load_camera_aliases,
)
from infra.matcher_failures import MATCHER_FAILURES
from infra.paths import (
    CAMERA_CREDS_FILE,
    CLEANUP_LOG,
    FRAMES_DIR,
    TELEGRAM_CREDS_FILE,
    ensure_dirs,
)
from infra.persistent_rtsp import get_reader
from infra.telegram_creds import load_telegram_creds
from infra.timezone import EDT
from infra.timezone import to_edt_string as _to_edt

# ----------------------------------------------------------------------
# Logging (PLAN §10.3)
# ----------------------------------------------------------------------

ensure_dirs()

# Single source of truth for logger configuration. configure_logging()
# is idempotent — re-imports of this module don't double-attach handlers.
# PRODUCTION_MODE (env FARMSURV_PRODUCTION=1) gates the file handler;
# tests get StreamHandler + NullHandler so pytest's caplog captures via
# root and records don't leak to a default handler.
from infra.logging_setup import configure_logging

_listener_logger = configure_logging("alert_listener")

# Phase 6B.45 / 6B.55 — sibling modules log under their own __name__
# (vehicle_matcher, frame_capture, persistent_rtsp, send_telegram,
# audit_telegram).
# They propagate to root, which we just configured to INFO, so their
# records reach both the file (production) and pytest's caplog (tests)
# automatically. No per-sibling handler attachment is needed here.

log = _listener_logger


# ----------------------------------------------------------------------
# Cleanup-log file handler
# ----------------------------------------------------------------------
# cleanup.py uses the `cleanup` logger (module-level). Its log records are
# handled by the root logger configured above via logging.basicConfig — but
# the FileHandler only writes to LISTENER_LOG, not CLEANUP_LOG. Historically
# cleanup.py appended a summary line to CLEANUP_LOG via a manual `open(..., "a")`
# in run_all(). To get the rotation behaviour we want for both files, attach a
# dedicated RotatingFileHandler for CLEANUP_LOG here (Phase 1, 2026-07-24).
# 5 MB × 3 backups = 15 MB ceiling.
# Cleanup logger — writes to its own file (CLEANUP_LOG) so it doesn't
# pollute listener.log. Idempotent via configure_file_logger.
from infra.logging_setup import configure_file_logger

_cleanup_logger = configure_file_logger(
    "cleanup",
    CLEANUP_LOG,
    max_bytes=5 * 1024 * 1024,  # 5 MB
    backup_count=3,
)


# ----------------------------------------------------------------------
# Bounded webhook executor (Phase 1, 2026-07-24)
# ----------------------------------------------------------------------
# Previously: each accepted webhook spawned a fresh `threading.Thread` to
# run _process_alert_safe. Debounce helps, but floods from different
# event types or different cameras still created unbounded threads.
#
# Now: a module-level ThreadPoolExecutor with a small fixed worker count
# handles accepted-alert work. When the queue is full, /alert returns
# 503 + a `queue_full` log line so the postmortem grep catches it.
#
# Limits chosen to match established design constraints:
#   - max_workers = 4 matches llama-server --parallel 4
#   - queue capacity = 64 (~16× workers) drains in seconds at normal traffic
#
# The inner vision queue (vision_queue.VisionQueue) is unchanged — it is
# the serial, priority-aware queue that fronts llama-server and must
# stay single-flight. This executor wraps the *outer* per-alert work.
#
# Exposed via get_webhook_executor() so tests can introspect counters and
# pause/resume workers.
# Imported mid-module (noqa E402): scoping the alias avoids clashing with the
# stdlib `threading` already imported at the top of this file (which is used
# for `threading.Thread(...)` in unrelated paths).
import threading as _threading_mod

# Phase 6B.16 — Per-class queues with vehicle priority.
# Burned 2026-07-26: single FIFO queue let a wave of person-event alerts
# starve a Solar `event=vehicle` alert by 11m39s (Tesla departure frame
# captured AFTER the car had cleared the gate). Class-priority dispatch
# routes vehicle events ahead of person/animal/motion events, with
# gatekeeper-camera vehicle events at the top.
# Phase 6B.104 (2026-08-20) — one camera was demoted from gatekeeper tier.
# That camera's vehicle events no longer route to QUEUE_GATEKEEPER_VEHICLE,
# the capture-delay path, or the match-alert Telegram stack. It is now a
# normal camera for vehicle events — same as the other 4 non-gatekeepers.
# It keeps its persistent RTSP reader (still in the boot loop) because
# that's about reliable frame capture, not vehicle handling. If a future
# phase adds a person-gatekeeper tier, that camera may rejoin the gatekeeper
# set for person events only — that's deferred, see PLAN §11.32.
# Phase 6B.104 (2026-08-20) demoted that camera from the gatekeeper tier
# per maintainer OOB: "I just want [it] to be like the other cameras. Not a
# vehicle motion gatekeeper anymore." §11.79 (2026-08-28) reverted that
# decision after 3 vehicles entered the property from non-gatekeeper
# cameras and produced no Telegram alerts — only history.jsonl rows.
# maintainer OOB 2026-08-28: "Let's do this for now and see how it works.
# Depending on how it works maybe we'll come up with something
# different." All 6 cameras now fire the full vehicle Telegram stack
# (TG#1 arriving + TG#2 motion composite + TG#3 match/no-match). The
# persistent-RTSP-eligible subset keeps its persistent RTSP reader
# because that's about reliable frame capture, not vehicle handling.
# §11.77 gate_cooldown limits per-camera
# vehicle floods to 1/min/camera (default). Other surfaces that read
# this constant — `_motion_gate_dispatch.GATEKEEPER_CAMERAS` (mirror),
# `vehicle_event_pipeline.match_stage` (gate check), and the tests that
# pin it — were updated together.


# ----------------------------------------------------------------------
# Camera registry helpers (Phase 6B.167 §13.5 Commit 14)
# ----------------------------------------------------------------------
# GATEKEEPER_CAMERAS / PERSON_GATEKEEPER_CAMERAS are now keyed by
# CameraSpec.code (the value stored in spec.code by infra.cameras)
# instead of operator-flavored friendly names. The lookup chain is:
#
#   1. Caller (webhook parser, classify_queue, ...) receives a friendly
#      name from the camera-creds.env-derived `cameras` dict.
#   2. Callers translate name → code via _code_for_camera(name).
#   3. Membership tests run against the code-keyed GATEKEEPER_CAMERAS set.
#
# Today, the operator's env is in the LEGACY schema (FRONT_* / OUTSIDE_*)
# so spec.code is the legacy prefix (e.g. "OUTSIDE_FRONT_SOLAR"). When
# §13.4 migration lands and the operator moves to CAM{N}_IP, the codes
# here will change to CAM1/CAM2/... in lockstep with infra/cameras.py.
# Tests already run against the synthetic CAM1/CAM2/CAM3 fixture.
#
# _zone_for(code) returns the spec.zone for a camera code, falling back
# to "unknown" when the code isn't registered. Used by downstream
# pipelines (vehicle_event_pipeline, person_event_pipeline) once those
# commits land — see PLAN §13.5 Commit 15.

# Phase 6B.167 §13.4 Commit 17 (T3 C17): codes are CAM{N} per
# infra.cameras._LEGACY_PREFIX_TO_CODE. The operator's legacy env
# (FRONT_* / OUTSIDE_*) gets translated to CAM{N} codes by
# infra.cameras._parse_legacy_fallback — that's the §13.4 contract.
# Membership tests run code-keyed:
#     _code_for_camera(name) in GATEKEEPER_CAMERAS
# See also listener/_motion_gate_dispatch.py (mirror constant).
GATEKEEPER_CAMERAS = frozenset({
    "CAM1",   # → FRONT (Front Door Outside)
    "CAM2",   # → BACK (Back Door Inside)
    "CAM3",   # → OUTSIDE_FRONT_GARAGE
    "CAM4",   # → OUTSIDE_FRONT_POWER
    "CAM5",   # → OUTSIDE_FRONT_SOLAR (original gatekeeper §11.79)
    "CAM6",   # → OUTSIDE_BACK_SOLAR
})


# Phase 6B.106 — person-gatekeeper cameras. Phase 6B.140 (2026-08-27):
# switched from one camera (close-mounted, saw only the door panel) to
# another camera (wider-angle, captures the full approach + entry
# sequence). The wider-angle camera has its own persistent RTSP
# reader (boot loop below), same hardware family as the vehicle
# gatekeeper camera, and no pre-buffer bug risk. The close-mounted
# camera is now class_disabled for person events (see
# DISABLED_CAMERA_EVENTS) — it stays in the network for
# vehicle/animal/motion events but no longer routes to the person
# pipeline. Will revisit when §11.36b
# lands (ArcFace threshold tuning + audio clips).
# Mirrors the vehicle gatekeeper concept: person events on these cameras
# route to QUEUE_GATEKEEPER_PERSON + the structured pipeline, separate
# from the legacy person pipeline. Adding more cameras here means their
# person events get the structured Telegram + match-alert flow instead
# of the LLM-prose path.
#
# §11.80 (2026-08-28) — expanded to all 6 active cameras alongside
# GATEKEEPER_CAMERAS expansion. maintainer OOB 2026-08-28: "make every camera
# a person gatekeeper camera and every camera a vehicle gatekeeper
# camera. When I get too many alerts I'll let you know." With all 6
# cameras in both sets, ALL_GATEKEEPER_CAMERAS is now the same 6-camera
# set. Per-camera thresholds in motion_gate_thresholds.json already tune
# individual sensitivity. The §11.77 gate_cooldown at person=120s on
# all 6 cameras caps per-camera person floods to 1/min/camera. Mirrored
# at listener/_motion_gate_dispatch.py:104.
#
# Phase 6B.167 §13.4 Commit 17 (T3 C17): CAM{N} codes per
# infra.cameras._LEGACY_PREFIX_TO_CODE. Same migration contract as
# GATEKEEPER_CAMERAS above.
PERSON_GATEKEEPER_CAMERAS = frozenset({
    "CAM1",   # → FRONT (Front Door Outside)
    "CAM2",   # → BACK (Back Door Inside)
    "CAM3",   # → OUTSIDE_FRONT_GARAGE (original §6B.140)
    "CAM4",   # → OUTSIDE_FRONT_POWER
    "CAM5",   # → OUTSIDE_FRONT_SOLAR
    "CAM6",   # → OUTSIDE_BACK_SOLAR
})

# Phase 6B.167 §13.4 Commit 18 (T3 C18): alias dict is hydrated at
# import-time from cameras.env (operator-private, gitignored) on top
# of the hardcoded fallback. The fallback dict survives so that
# `python3 -c "from listener.listener import _SNAPSHOT_CAMERA_ALIASES"`
# still works in environments without cameras.env (CI, tests,
# bare scripts that don't have the env). Operator-supplied shorthand
# from cameras.env WINS over the hardcoded keys (last-write-wins
# on dict comprehension order).
_SNAPSHOT_CAMERA_ALIASES_FALLBACK: dict[str, str] = {
    # Hardcoded fallback — preserved so the listener boots without
    # cameras.env (e.g. CI runs, smoke tests, fresh checkouts). The
    # operator's cameras.env values override these when present.
    "CAM1": "CAM5",   # Outside Front Solar
    "CAM2": "CAM3",   # Outside Front Garage
    "BACK": "CAM2",  # Back Door Inside
    "CAM1": "CAM1",   # Front Door Outside
    "CAM4": "CAM6",   # Outside Back Solar
    "CAM5": "CAM4",   # Outside Front Power
}

_env_snapshot_aliases, _env_preview_aliases = _load_camera_aliases()
_SNAPSHOT_CAMERA_ALIASES: dict[str, str] = {
    **_SNAPSHOT_CAMERA_ALIASES_FALLBACK,
    **_env_snapshot_aliases,
}


def _zone_for(camera_code: str, env_path: str | None = None) -> str:
    """Return the spec.zone for a camera code, or "unknown" on miss.

    Phase 6B.167 §13.5 Commit 14: zone-based routing for vehicle/person
    event pipelines (Commit 15). Resolves the camera code through the
    registry so callers downstream of the listener get a stable zone
    label without re-parsing cameras.env.

    Args:
        camera_code: A CameraSpec.code value (today: legacy prefix like
            "OUTSIDE_FRONT_SOLAR"; future: CAM{N} after §13.4 migration).
            Pass-through unknown codes → "unknown" rather than raising,
            because the spec.code space grows as the operator enrolls
            new cameras and downstream pipelines must not crash on first
            sight of a fresh code.
        env_path: Optional explicit env file. Defaults to the operator's
            active camera-creds.env (resolved via infra.cameras).

    Returns:
        The matched spec.zone, or "unknown" if no spec matches.
    """
    if not camera_code:
        return "unknown"
    try:
        return _by_camera_code(camera_code, env_path=env_path).zone
    except KeyError:
        return "unknown"


def _resolve_snapshot_camera(arg: str, cameras: dict) -> str | None:
    """Map a /snapshot camera arg to its CameraSpec.code (CAM{N}).

    Accepts:
      - CAM{N} code already in the cameras registry (case-sensitive)
      - shorthand from _SNAPSHOT_CAMERA_ALIASES (e.g. "CAM1" → "CAM5")

    Returns the CAM{N} code, or None if the arg is unknown.
    Does not lowercase CAM{N} codes — typos like 'cam1' return None on
    purpose so the operator sees a clear 400 instead of silent acceptance.
    """
    if arg in _SNAPSHOT_CAMERA_ALIASES:
        return _SNAPSHOT_CAMERA_ALIASES[arg]
    if arg in cameras:
        return arg
    return None

# Origin: 2026-08-03 (one camera only). 2026-08-17 (Phase 6B.87,
# PLAN §11.17) added a second gatekeeper camera — both got the 8s
# deferred capture + pre-event motion trail (frame_offsets
# [0, 30, 60, 90, 120, 150]) and a dedicated persistent RTSP reader
# (QUEUE_GATEKEEPER_VEHICLE, Worker 0). 2026-08-20 (Phase 6B.104)
# demoted the second one back out of the vehicle gatekeeper tier per
# maintainer — "I just want [it] to be like the other cameras. Not a vehicle
# motion gatekeeper anymore." That camera's vehicle events now flow
# through QUEUE_OTHER_VEHICLE (no capture delay, no match-alert
# Telegram stack). It retains its persistent RTSP reader (still in the
# boot loop) because persistent RTSP is about reliable frame capture
# (Reolink pre-buffer-dump fix), not vehicle gatekeeping.

# Phase 6B.62 (2026-08-07) — Deferred capture delay for gatekeeper cameras.
# When a vehicle webhook arrives from a gatekeeper camera, wait this many
# seconds BEFORE pulling frames from the persistent RTSP ring buffer.
#
# Tuned 4s → 6s on 2026-08-07 per maintainer: "I really like the four second
# delay but I think it needs to be a six second delay." The delay was tuned
# again from 6s to 8s on 2026-08-09 after observing live vehicle alerts.
# With 4s wait the
# frame_006 (oldest) still caught the truck just entering the frame at
# T-10s; with 6s wait frame_006 catches it earlier (T-12s) and frame_001
# (newest) was at webhook T+5s, giving the trail a clear view of the
# truck fully arriving + mid-yard + parked.
#
# Rationale: the gatekeeper's smart_vehicle detector fires when the
# truck is already mid-yard (~12s after it first entered the FOV).
# Capturing immediately means the trail only shows the truck already
# parked. Waiting 8s shifts the 6-frame trail forward — frame_006 now
# catches the truck arriving at the
# gate, frame_001 catches it mid-yard.
#
# Implementation: the executor worker submits a threading.Timer instead of
# doing time.sleep() inline, so the worker thread is freed immediately and
# other cameras' alerts aren't blocked. The Timer fires _process_alert_safe
# after the delay. If the listener restarts mid-delay, the Timer is lost
# (acceptable — same as today's behavior on a hung alert).
GATEKEEPER_CAPTURE_DELAY_S = 8.0

def _classify_queue(camera_name: str, event_type: str) -> str | None:
    """
    Map a webhook's (camera_name, event_type) to a queue NAME.

    Returns the queue NAME string (not the queue object), or None if
    the (camera, event_type) combo is in DISABLED_CAMERA_EVENTS (Phase 6B.53).

    Phase 6B.16 routing map (highest to lowest priority):

      vehicle + gatekeeper camera  → QUEUE_GATEKEEPER_VEHICLE
      vehicle + other camera       → QUEUE_OTHER_VEHICLE
      person (Reolink) / people    → QUEUE_PERSON
      animal                       → QUEUE_ANIMAL
      anything else / unknown      → QUEUE_MOTION

    The event_type string is normalised to lowercase. Reolink's
    `alarm.type` is `"person"` (singular), but historical tests use
    `"people"` (plural). Both route to QUEUE_PERSON.

    Phase 6B.53: hard-disable list checked first. If `(camera, event)`
    is in DISABLED_CAMERA_EVENTS, return None → caller drops the
    webhook (no queue, no frame capture, no vision, no Telegram).
    """
    e = (event_type or "").strip().lower()
    c = (camera_name or "").strip()
    # Phase 6B.167 §13.5 Commit 14: GATEKEEPER_CAMERAS and
    # DISABLED_CAMERA_EVENTS are now code-keyed. Translate friendly
    # name → code before membership tests so legacy name-keyed callers
    # and code-keyed callers (post-migration) both work.
    c_code = _code_for_camera(c)
    # Phase 6B.53 — listener-side disable for selected camera/event combos
    if (c_code, e) in DISABLED_CAMERA_EVENTS:
        return None
    if e == "vehicle":
        if c_code in GATEKEEPER_CAMERAS:
            return _ClassedWebhookExecutor.QUEUE_GATEKEEPER_VEHICLE
        return _ClassedWebhookExecutor.QUEUE_OTHER_VEHICLE
    if e in ("people", "person"):
        return _ClassedWebhookExecutor.QUEUE_PERSON
    if e == "animal":
        return _ClassedWebhookExecutor.QUEUE_ANIMAL
    return _ClassedWebhookExecutor.QUEUE_MOTION


# Phase 6B.53 — listener-side disable for selected (camera, event_type)
# combos. All-hours (24/7) suppression. The camera still records to NAS
# 24/7 and the webhook still arrives at the listener, but the listener
# returns HTTP 202 + dropped:class_disabled without capturing frames,
# calling vision, or sending Telegram.
#
# To add a new disable: append (camera_name, event_type) tuple.
# To remove: delete the tuple.
# event_type lookup is case-insensitive (downcased by _classify_queue).
DISABLED_CAMERA_EVENTS: frozenset[tuple[str, str]] = frozenset({
    # §11.80 (2026-08-28) — empty after the "every camera a person
    # gatekeeper" expansion. All 6 cameras now route person events
    # through the structured Telegram pipeline (QUEUE_GATEKEEPER_PERSON).
    # Pre-§11.80 this set contained person+people entries for 5 cameras
    # (all 6 except the original gatekeeper animal disable, which stays).
    # The (gatekeeper, animal) entry is the only survivor and remains in
    # effect. Per maintainer OOB 2026-08-28: "make every camera a person
    # gatekeeper camera. When I get too many alerts I'll let you know."
    # Per-camera thresholds and gate_cooldown.person=120s already
    # suppress the worst false positives for person events; the CAM1
    # animal disable persists as a separate, narrower filter.
    #
    # Phase 6B.167 §13.4 Commit 17 (T3 C17): camera half of each tuple
    # is the CAM{N} code (per infra.cameras._LEGACY_PREFIX_TO_CODE),
    # not the legacy prefix. See §13.4 contract in GATEKEEPER_CAMERAS above.
    # CAM5 = OUTSIDE_FRONT_SOLAR (the original gatekeeper).
    ("CAM5", "animal"),
})


# Priority order for worker dispatch (highest to lowest).
# Iterable used by `_worker_loop()` to pop work in rank order.
QUEUE_PRIORITY_ORDER = (
    "gatekeeper_vehicle",  # rank 1 — vehicle on driveway / gatekeeper
    "other_vehicle",       # rank 2 — vehicle anywhere else
    "person",              # rank 3
    "animal",              # rank 4
    "motion",              # rank 5 — plain MD / unknown
)


class _WorkItem(NamedTuple):
    future: Any
    fn: Any
    args: tuple
    kwargs: dict


class _ClassedWebhookExecutor:
    """Worker pool with one bounded queue per alert class.

    Phase 6B.16. Replaces the single-FIFO `_BoundedWebhookExecutor`
    (kept as a thin alias below for backward compatibility with tests).

    Design:
      - 5 per-class bounded queues, all maxsize=64.
      - max_workers worker threads; each tick walks the priority order
        and pops from the highest-priority non-empty queue.
      - submit() picks the right queue based on (camera_name, event_type)
        via `_classify_queue()`.
      - Each queue has its own accept_total / rejected_full_total counter;
        legacy `accepted_total` / `rejected_full_total` fields aggregate
        all queues for backward compat.

    Phase 2026-08-03: dedicated gatekeeper worker.
      Worker 0 is reserved exclusively for QUEUE_GATEKEEPER_VEHICLE
      (gatekeeper-camera vehicle events). Workers 1..N-1 share the
      remaining queues via the priority walk. Rationale: when a
      non-gatekeeper camera floods the system (cascade of vehicle
      alerts from other sources, etc.), the gatekeeper queue used to sit
      idle behind the backlog because all 4 workers were busy on
      other_vehicle. With one worker always free for the gatekeeper,
      a gatekeeper vehicle alert gets a worker within milliseconds of
      enqueue instead of waiting for a non-gatekeeper task to finish.
      Note: as of Phase 6B.104 (2026-08-20), the gatekeeper set is
      single-camera — the other gatekeeper's vehicle events flow
      through QUEUE_OTHER_VEHICLE.
      Cost: 25% of worker capacity parked on a single lane even when
      the gatekeeper is quiet. Acceptable — per-camera alert rate is low.
    """

    # Queue names — kept as class constants so tests / callers can refer
    # to them by name without typo risk.
    QUEUE_GATEKEEPER_VEHICLE = "gatekeeper_vehicle"
    QUEUE_OTHER_VEHICLE = "other_vehicle"
    QUEUE_PERSON = "person"
    QUEUE_ANIMAL = "animal"
    QUEUE_MOTION = "motion"

    _QUEUE_NAME_TO_ATTR: ClassVar[dict[str, str]] = {
        QUEUE_GATEKEEPER_VEHICLE: "_q_gatekeeper_vehicle",
        QUEUE_OTHER_VEHICLE: "_q_other_vehicle",
        QUEUE_PERSON: "_q_person",
        QUEUE_ANIMAL: "_q_animal",
        QUEUE_MOTION: "_q_motion",
    }

    def __init__(self, max_workers: int = 4, queue_capacity: int = 64) -> None:
        self._max_workers = max_workers
        self._queue_capacity = queue_capacity
        self._queues: dict[str, queue.Queue] = {
            self.QUEUE_GATEKEEPER_VEHICLE: queue.Queue(maxsize=queue_capacity),
            self.QUEUE_OTHER_VEHICLE: queue.Queue(maxsize=queue_capacity),
            self.QUEUE_PERSON: queue.Queue(maxsize=queue_capacity),
            self.QUEUE_ANIMAL: queue.Queue(maxsize=queue_capacity),
            self.QUEUE_MOTION: queue.Queue(maxsize=queue_capacity),
        }
        self._workers: list[threading.Thread] = []
        self._shutdown = False
        self._stats_lock = _threading_mod.Lock()
        self._per_class_lock = _threading_mod.Lock()
        # Per-class accept / reject counters. Initialized to 0 for
        # each queue name so stats() always reports every queue, even
        # before any traffic.
        zero = 0
        self._accepted_per_class: dict[str, int] = {
            self.QUEUE_GATEKEEPER_VEHICLE: zero,
            self.QUEUE_OTHER_VEHICLE: zero,
            self.QUEUE_PERSON: zero,
            self.QUEUE_ANIMAL: zero,
            self.QUEUE_MOTION: zero,
        }
        self._rejected_per_class: dict[str, int] = {
            self.QUEUE_GATEKEEPER_VEHICLE: zero,
            self.QUEUE_OTHER_VEHICLE: zero,
            self.QUEUE_PERSON: zero,
            self.QUEUE_ANIMAL: zero,
            self.QUEUE_MOTION: zero,
            # Phase 6B.53 — webhooks dropped because (camera, event_type)
            # is in DISABLED_CAMERA_EVENTS. Surfaced via /status so the
            # drop count is observable.
            "class_disabled": zero,
        }
        # Legacy aggregate counters (kept for backward compat with
        # tests that inspect `executor.accepted_total` /
        # `executor.rejected_full_total`).
        self.accepted_total = 0
        self.rejected_full_total = 0
        for i in range(max_workers):
            if i == 0:
                # Phase 2026-08-03: worker 0 is reserved for the gatekeeper
                # lane so a gatekeeper vehicle alert never waits behind a
                # non-gatekeeper backlog. See class docstring.
                target = self._gatekeeper_worker_loop
                name_suffix = "gatekeeper"
            else:
                target = self._worker_loop
                name_suffix = "shared"
            t = _threading_mod.Thread(
                target=target,
                name=f"webhook-worker-{i}-{name_suffix}",
                daemon=True,
            )
            t.start()
            self._workers.append(t)

    def submit(self, fn, *args, **kwargs):
        """Submit work to the default queue (motion).

        Phase 6B.16 also exposes `submit_for_class(fn, camera_name,
        event_type, *args, **kwargs)` for routed submits. Direct `.submit`
        keeps the old signature so any existing call sites (and tests)
        don't need to change.
        """
        return self._enqueue(self.QUEUE_MOTION, fn, args, kwargs)

    def submit_for_class(
        self, fn, camera_name: str, event_type: str, *args, **kwargs
    ):
        """Submit work to the queue matching `(camera_name, event_type)`.

        Routes via `_classify_queue()` then enqueues. Returns the Future
        on success, None if the chosen queue is full OR if the
        (camera, event_type) combo is in DISABLED_CAMERA_EVENTS
        (Phase 6B.53). When disabled, logs an audit line and bumps
        the `class_disabled` counter so the drop is visible in /status.
        """
        qname = _classify_queue(camera_name, event_type)
        if qname is None:
            with self._stats_lock:
                self._rejected_per_class["class_disabled"] += 1
            log.info(
                f"webhook_executor class_disabled "
                f"(camera={camera_name!r} event_type={event_type!r})"
            )
            return None
        return self._enqueue(qname, fn, args, kwargs)

    def _enqueue(
        self,
        qname: str,
        fn,
        args: tuple,
        kwargs: dict,
    ):
        fut: Future = Future()
        item = _WorkItem(future=fut, fn=fn, args=args, kwargs=kwargs)
        q = self._queues[qname]
        try:
            q.put_nowait(item)
        except queue.Full:
            with self._per_class_lock:
                self._rejected_per_class[qname] += 1
            with self._stats_lock:
                self.rejected_full_total += 1
            log.warning(
                f"webhook_executor queue_full "
                f"(queue={qname} "
                f"accepted={self._accepted_per_class[qname]} "
                f"rejected_full={self._rejected_per_class[qname]})"
            )
            return None
        with self._per_class_lock:
            self._accepted_per_class[qname] += 1
        with self._stats_lock:
            self.accepted_total += 1
        return fut

    def _gatekeeper_worker_loop(self) -> None:
        """Worker 0 loop: dedicated to QUEUE_GATEKEEPER_VEHICLE only.

        Phase 2026-08-03. Exists so the gatekeeper vehicle lane never blocks
        behind a non-gatekeeper backlog. Only ever dequeues from
        QUEUE_GATEKEEPER_VEHICLE; ignores every other queue. Blocks
        on that single queue with a 1s timeout (so shutdown can
        observe _shutdown without spinning).
        """
        q = self._queues[self.QUEUE_GATEKEEPER_VEHICLE]
        while not self._shutdown:
            try:
                item = q.get(timeout=1.0)
            except queue.Empty:
                continue
            try:
                result = item.fn(*item.args, **item.kwargs)
                if item.future.set_running_or_notify_cancel():
                    item.future.set_result(result)
            except Exception as err:
                item.future.set_exception(err)
            finally:
                q.task_done()

    def _worker_loop(self) -> None:
        while not self._shutdown:
            item = None
            served_qname: str | None = None
            # Walk the priority order; if a higher queue has work,
            # service it before anything lower.
            for qname in QUEUE_PRIORITY_ORDER:
                q = self._queues[qname]
                try:
                    item = q.get_nowait()
                    served_qname = qname
                    break
                except queue.Empty:
                    continue
            if item is None:
                # All queues empty (rare; only if we lost a race between
                # the empty-check and put_nowait). Block on the lowest
                # priority queue so we don't busy-spin.
                try:
                    item = self._queues[self.QUEUE_MOTION].get(timeout=1.0)
                    served_qname = self.QUEUE_MOTION
                except queue.Empty:
                    continue
            try:
                result = item.fn(*item.args, **item.kwargs)
                if item.future.set_running_or_notify_cancel():
                    item.future.set_result(result)
            except Exception as err:
                item.future.set_exception(err)
            finally:
                if served_qname is not None:
                    self._queues[served_qname].task_done()

    def queue_depth(self) -> int:
        """Aggregate depth across all 5 queues (legacy API)."""
        return sum(q.qsize() for q in self._queues.values())

    def queue_depths(self) -> dict[str, int]:
        """Per-queue depth (Phase 6B.16 stats endpoint)."""
        return {name: q.qsize() for name, q in self._queues.items()}

    def stats(self) -> dict:
        with self._stats_lock:
            base: dict[str, Any] = {
                "workers": self._max_workers,
                "queue_capacity": self._queue_capacity,
                "queue_depth": self.queue_depth(),
                "accepted_total": self.accepted_total,
                "rejected_full_total": self.rejected_full_total,
            }
        with self._per_class_lock:
            base["queue_depths"] = dict(self.queue_depths())
            base["accepted_per_class"] = dict(self._accepted_per_class)
            base["rejected_per_class"] = dict(self._rejected_per_class)
        return base

    def shutdown(self, wait: bool = False) -> None:
        """Test helper. Sets the shutdown flag; workers exit on next loop tick."""
        self._shutdown = True


# Backward-compatibility alias. Existing tests reference
# `_BoundedWebhookExecutor` directly. Phase 6B.16 keeps this name pointing
# at the new classed executor so those tests still construct correctly.
_BoundedWebhookExecutor = _ClassedWebhookExecutor


_webhook_executor: _BoundedWebhookExecutor | None = None


def get_webhook_executor() -> _BoundedWebhookExecutor:
    """Module-level singleton webhook executor (lazy-initialized)."""
    global _webhook_executor
    if _webhook_executor is None:
        _webhook_executor = _BoundedWebhookExecutor(max_workers=4, queue_capacity=64)
    return _webhook_executor


def _reset_webhook_executor_for_tests() -> None:
    """Test helper: drop the singleton so the next get_webhook_executor()
    call returns a fresh executor with zero counters."""
    global _webhook_executor
    _webhook_executor = None


# ----------------------------------------------------------------------
# State
# ----------------------------------------------------------------------


# ----------------------------------------------------------------------
# Debounce state
# ----------------------------------------------------------------------
#
# Reolink fires 2-6 webhooks per physical event (motion → AI confirm →
# re-entry state transitions). Without debouncing, the listener kicks off
# the full pipeline 5x for one walk-through. Server-side dedup is the
# only reliable way — the camera's behavior is firmware-level.
#
# _last_alert_at: {camera_name: time.monotonic() of last accepted alert}
# _debounce_lock: serializes updates to _last_alert_at (Flask is threaded)

_last_alert_at: dict = {}
_debounce_lock = threading.Lock()
_debounce_window_seconds: float = 15.0  # default; override via _debounce_set_window


# Per-camera debounce override map. Loaded lazily from config/alert_overrides.json
# at first access (and cached). Cameras in this map use their own window instead
# of the global 15s default. Empty dict = no overrides (everyone uses global).
#
# Why a separate config block instead of re-using _OVERRIDE_CONFIG in alert_generator:
# keeping the listener decoupled from alert_generator's internal state avoids
# circular-import risk and lets the listener boot independently for testing.
_debounce_overrides_cache: dict | None = None
_debounce_overrides_path = (
    pathlib.Path(__file__).parent.parent / "config" / "alert_overrides.json"
)


def _get_debounce_overrides() -> dict:
    """
    Load the per-camera debounce window overrides from config/alert_overrides.json.

    Returns a dict mapping camera_name → seconds. Cameras not in this dict
    use the global 15s default. If the file is missing or malformed, returns
    an empty dict (graceful degradation — listener still functions, just
    without per-camera overrides).

    Result is cached at module level; if you edit the config file at
    runtime you must restart the listener for changes to take effect.
    """
    global _debounce_overrides_cache
    if _debounce_overrides_cache is not None:
        return _debounce_overrides_cache
    try:
        import json

        with open(_debounce_overrides_path) as f:
            cfg = json.load(f)
        raw = cfg.get("debounce_overrides") or {}
        # Filter out the _comment key and validate values are numeric
        out: dict[str, float] = {}
        for cam, seconds in raw.items():
            if cam.startswith("_"):
                continue  # comment keys
            try:
                out[cam] = float(seconds)
            except (TypeError, ValueError):
                log.warning(
                    f"debounce_overrides['{cam}'] = {seconds!r} is not numeric; "
                    f"ignoring (camera will use global window)"
                )
        _debounce_overrides_cache = out
        return out
    except FileNotFoundError:
        log.warning(
            f"alert_overrides.json not found at {_debounce_overrides_path}; "
            f"no per-camera debounce overrides active"
        )
        _debounce_overrides_cache = {}
        return {}
    except Exception as e:
        log.warning(
            f"Failed to load debounce_overrides from {_debounce_overrides_path}: {e}; "
            f"no per-camera debounce overrides active"
        )
        _debounce_overrides_cache = {}
        return {}


def _reset_debounce_overrides_cache() -> None:
    """
    Clear the cached debounce overrides. Used by tests when they monkey-patch
    the config file. Production code does not call this.
    """
    global _debounce_overrides_cache
    _debounce_overrides_cache = None


def _debounce_set_window(seconds: float) -> None:
    """Set the active debounce window. Used by tests."""
    global _debounce_window_seconds
    _debounce_window_seconds = float(seconds)


def _debounce_reset() -> None:
    """Clear all debounce state. Used by tests."""
    with _debounce_lock:
        _last_alert_at.clear()


def _get_debounce_window_seconds(camera_name: str) -> float:
    """Return the effective debounce window for a canonical camera name."""
    per_camera_window = _get_debounce_overrides().get(camera_name)
    if per_camera_window is not None:
        return float(per_camera_window)
    return _debounce_window_seconds


def _should_debounce(camera_name: str, window_seconds: float | None = None) -> bool:
    """
    Decide whether to drop this webhook based on per-camera debounce window.

    Returns True if the previous alert for this camera was within the window
    (i.e. this webhook should be dropped). Returns False if it's been long
    enough, or if there's no prior alert for this camera.

    Side effect: when returning False, records the current time as the last
    alert for this camera.

    Args:
        camera_name: Friendly camera name (e.g. "<FRIENDLY_NAME>")
        window_seconds: Override the active window. Defaults to:
            - The per-camera override from config/alert_overrides.json
              (debounce_overrides block), if camera_name is in that map.
            - Otherwise, the active global window set by
              `_debounce_set_window()` (or 15s if never set).
    """
    if window_seconds is None:
        window_seconds = _get_debounce_window_seconds(camera_name)

    now = time.monotonic()
    with _debounce_lock:
        last = _last_alert_at.get(camera_name)
        if last is not None and (now - last) < window_seconds:
            # Within window — debounce (do NOT update timestamp)
            return True
        # Either no prior alert, or outside the window — accept
        _last_alert_at[camera_name] = now
        return False


def _load_cameras_from_creds() -> dict:
    """
    Load camera config from camera-creds.env.

    Returns a dict keyed by friendly camera name; each value is a
    {"rtsp_url": str, "ip": str} pair loaded from the env file. The
    friendly names are operator-chosen and stored in camera-creds.env.

    `load_camera_creds` already returns this shape, so we delegate to it.
    """
    return load_camera_creds(CAMERA_CREDS_FILE)


# ----------------------------------------------------------------------
# Flask app factory
# ----------------------------------------------------------------------


def create_app(test_config: dict | None = None) -> Flask:
    """
    Create the Flask app.

    Args:
        test_config: Optional dict with 'cameras' key for testing.
    """
    app = Flask(__name__)

    # Load camera configuration
    if test_config and "cameras" in test_config:
        cameras = test_config["cameras"]
    else:
        cameras = _load_cameras_from_creds()

    app.config["CAMERAS"] = cameras

    # --- Routes ---

    @app.route("/health", methods=["GET"])
    def health():
        return jsonify({"status": "ok", "cameras_loaded": len(cameras)})

    @app.route("/status", methods=["GET"])
    def status():
        # Compute uptime from STATE['start_time'].
        try:
            started = datetime.fromisoformat(STATE["start_time"])
            uptime = int((datetime.now(EDT) - started).total_seconds())
        except (KeyError, ValueError):
            uptime = None

        # Cleanup status — read from cleanup module's STATE-equivalent.
        # cleanup.run_all() updates these globals at the end of each pass.
        # Pre-6B.105b: this was `import_module("cleanup")`. Worked only
        # because infra/pipeline_integration.py (imported elsewhere in this
        # module) side-effected `sys.path.insert(0, "infra")` at import
        # time, which made the bare name `cleanup` resolve to infra/cleanup.py.
        # After 6B.105b's import cleanup, that side-effect is gone, so we
        # use the canonical path here.
        cleanup_last_at = getattr(import_module("infra.cleanup"), "_last_cleanup_at", None)
        cleanup_last_result = getattr(
            import_module("infra.cleanup"), "_last_cleanup_result", None
        )

        # Phase 1.2 (2026-08-05) — motion+match cooldown stats.
        # Surfaces the dedup state so operators can see "how many
        # motion alerts are being suppressed right now" via /status.
        try:
            motion_cooldown = MOTION_COOLDOWN.stats()
        except Exception as _cd_err:
            motion_cooldown = {"error": "cooldown_unavailable", "detail": str(_cd_err)}

        # Phase 1.3 (2026-08-05) — matcher failure stats.
        try:
            matcher_failures = MATCHER_FAILURES.stats()
        except Exception as _mf_err:
            matcher_failures = {"error": "matcher_failures_unavailable", "detail": str(_mf_err)}

        # Phase 6B.25 — per-pass matcher telemetry. Read-only snapshot;
        # returns the same dict that get_telemetry_snapshot() produces for
        # disk persistence. Lazily imported to avoid pulling vehicle_state
        # if /status is hit before the matcher module is loaded.
        try:
            from infra.matcher_telemetry import get_telemetry_snapshot
            matcher_telemetry = get_telemetry_snapshot()
        except Exception as _tel_err:
            matcher_telemetry = {
                "error": "telemetry_unavailable",
                "detail": str(_tel_err),
            }

        # Phase 6B.47 — merge scored shadow counts into the same telemetry
        # object so /status surfaces disagreement rate (PRD requirement:
        # "monitor disagreement rate after promotion"). Production code path
        # increments _scored_shadow_* via compare_with_legacy; we expose the
        # raw counts + computed rate.
        try:
            from infra.vehicle_matcher import (
                _shadow_counters_snapshot,  # match-fix-2026-08-18: top-level pkg doesn't re-export; infra does
            )
            shadow = _shadow_counters_snapshot()
            sa = shadow.get("scored_agreements", 0)
            sd = shadow.get("scored_disagreements", 0)
            total = sa + sd
            matcher_telemetry["scored_shadow_agreements"] = sa
            matcher_telemetry["scored_shadow_disagreements"] = sd
            matcher_telemetry["scored_disagreement_rate"] = (
                round(sd / total, 4) if total > 0 else 0.0
            )
        except Exception as _shadow_err:
            matcher_telemetry["scored_shadow_error"] = str(_shadow_err)

        # Phase 6B.102 (2026-08-20) — `prompt_mode` block removed. Phase 6B.78
        # deleted VEHICLE_COMBINED_PROMPT_TEMPLATE (and VEHICLE_MOTION_PROMPT_TEMPLATE)
        # from infra/prompt_templates.py; the listener's lazy import had been
        # silently failing for 5+ days, with /status returning
        # `prompt_mode={"error": "...ImportError..."}`. The
        # FARMSURV_COMBINED_PROMPT plist env var is gone too (see
        # ~/Library/LaunchAgents/ai.farm.surveillance-listener-refactor.plist).
        # The dispatch that uses the templates lives in infra/vision_analyzer.py
        # (select_prompt_template); operators verify prompt dispatch there or
        # via infra/tests/test_prompt_templates.py.

        return jsonify(
            {
                "status": "ok",
                "cameras_loaded": len(cameras),
                "uptime_seconds": uptime,
                "total_alerts": STATE["total_alerts"],
                "by_threat_level": STATE["by_threat_level"],
                "last_alert": STATE["last_alert"],
                "last_webhook_at": STATE["last_webhook_at"],
                "start_time": STATE["start_time"],
                "last_cleanup_at": cleanup_last_at,
                "cleanup_last_result": cleanup_last_result,
                "webhook_executor": get_webhook_executor().stats(),
                "matcher_telemetry": matcher_telemetry,
                "motion_cooldown": motion_cooldown,
                "matcher_failures": matcher_failures,
            }
        )

    @app.route("/snapshot", methods=["GET"])
    def snapshot():
        """Return the latest frame from a camera's persistent RTSP reader
        as a JPEG image. Phase 6B.88 / PLAN §11.19.

        Query params:
          camera (required): shorthand ('CAM1', 'CAM2') or canonical name
                             (e.g. '<FRIENDLY_NAME>').
          max_size (optional): 'WxH' — downscale to fit before serving.
                               Useful for staying under Telegram's photo
                               size cap when the operator wants a quick
                               preview.

        Returns:
          200 image/jpeg — raw JPEG bytes (latest ring-buffer frame).
          400 — missing camera arg, unknown camera, or bad max_size.
          503 — reader not booted (camera not in GATEKEEPER_CAMERAS, or
                persistent reader still coming up) OR no frames yet.
          500 — reader.get_recent_frames raised (RTSP failure, etc.).

        Read-only. Consumes one snapshot from the existing ring buffer.
        Never blocks the alert pipeline.
        """
        cam_arg = (request.args.get("camera") or "").strip()
        if not cam_arg:
            return jsonify({
                "error": "missing_query_param",
                "detail": "?camera=<shorthand|canonical> required",
                "known_aliases": sorted(_SNAPSHOT_CAMERA_ALIASES.keys()),
            }), 400

        camera_name = _resolve_snapshot_camera(cam_arg, cameras)
        if camera_name is None:
            return jsonify({
                "error": "unknown_camera",
                "detail": f"{cam_arg!r} is neither a known shorthand nor a canonical camera name",
                "known_aliases": sorted(_SNAPSHOT_CAMERA_ALIASES.keys()),
                "known_canonical": sorted(cameras.keys()),
            }), 400

        # Validate max_size first so the operator gets a clear 400 instead
        # of a 503 when they have a typo. Input validation before
        # system-state checks.
        max_size: tuple[int, int] | None = None
        if "max_size" in request.args:
            try:
                w_str, h_str = request.args["max_size"].lower().split("x", 1)
                max_size = (int(w_str), int(h_str))
                if max_size[0] <= 0 or max_size[1] <= 0:
                    raise ValueError("dimensions must be positive")
            except (ValueError, AttributeError):
                return jsonify({
                    "error": "bad_max_size",
                    "detail": (
                        "max_size must be 'WxH' with positive integers "
                        "(e.g. 1280x720)"
                    ),
                }), 400

        reader = get_reader(camera_name)
        if reader is None:
            return jsonify({
                "error": "reader_not_booted",
                "detail": (
                    f"{camera_name!r} has no persistent reader registered. "
                    "Either the camera is not in GATEKEEPER_CAMERAS, or the "
                    "listener is still in the middle of its boot sequence."
                ),
            }), 503

        with tempfile.TemporaryDirectory(prefix="listener_snapshot_") as td:
            try:
                paths = reader.get_recent_frames(
                    n=1, output_dir=td, max_size=max_size
                )
            except Exception as e:
                log.exception(
                    "snapshot: get_recent_frames failed for %s", camera_name
                )
                return jsonify({
                    "error": "reader_get_recent_frames_failed",
                    "detail": str(e),
                }), 500

            if not paths:
                return jsonify({
                    "error": "no_frames_in_ring_buffer",
                    "detail": (
                        f"{camera_name!r} persistent reader has decoded "
                        "0 frames so far. Try again after it has run for "
                        "a few seconds."
                    ),
                }), 503

            latest = paths[-1]
            log.info(
                "snapshot: served %s (%d bytes, max_size=%s) -> %s",
                latest, os.path.getsize(latest), max_size, request.remote_addr,
            )
            response = send_file(
                latest,
                mimetype="image/jpeg",
                as_attachment=False,
                download_name=f"{camera_name.replace(' ', '_')}.jpg",
            )
            # Phase 6B.88 — disable caching. Operators hitting
            # /snapshot?camera=CAM1 expect the current frame, not
            # whatever a proxy/browser cached from the last call.
            response.headers["Cache-Control"] = "no-store"
            return response

    @app.route("/preview", methods=["GET"])
    def preview():
        """Return a Synology preview thumbnail for a camera + timestamp.

        Phase 6B.124 (2026-08-22). Use case: post-hoc "did anything
        happen at time X?" lookups when Reolink AI didn't fire a
        webhook (e.g. slow-moving vehicles that don't trip the
        motion-detection threshold). The Synology NAS records
        continuously and keeps ~1 week of 320x180 preview JPEGs
        at 20-second cadence; this endpoint locates the closest one.

        Query params:
          camera (required) — alias ('CAM1') or canonical name.
          ts (required) — timestamp. Accepts:
            - "YYYY-MM-DD HH:MM:SS EDT" (project's canonical format)
            - ISO 8601 with timezone offset
          detail (optional) — 'json' returns metadata instead of image

        Returns:
          200 image/jpeg — the preview thumbnail (320x180, ~13 KB)
          200 application/json (detail=json) — path + ts metadata
          400 — missing camera or ts, or unparseable ts
          404 — no preview found (camera unknown, time outside
                retention, or recording not active at that time)

        Read-only. Returns a Synology file directly via send_file;
        no caching of our own.
        """
        from datetime import datetime, timedelta, timezone

        from infra.synology_preview import find_preview

        EDT = timezone(timedelta(hours=-4))
        cam_arg = (request.args.get("camera") or "").strip()
        ts_arg = (request.args.get("ts") or "").strip()
        if not cam_arg or not ts_arg:
            return jsonify({
                "error": "missing_query_param",
                "detail": "?camera=<shorthand|canonical>&ts=<timestamp> required",
                "known_aliases": sorted(_SNAPSHOT_CAMERA_ALIASES.keys()),
            }), 400

        # Parse ts — accept "YYYY-MM-DD HH:MM:SS EDT" or ISO 8601
        try:
            if ts_arg.endswith(" EDT"):
                dt = datetime.strptime(
                    ts_arg[:-4].strip(), "%Y-%m-%d %H:%M:%S"
                ).replace(tzinfo=EDT)
            else:
                dt = datetime.fromisoformat(ts_arg)
            target_ts = int(dt.timestamp())
        except ValueError:
            return jsonify({
                "error": "invalid_ts",
                "detail": (
                    f'cannot parse ts={ts_arg!r}; expected '
                    f'"YYYY-MM-DD HH:MM:SS EDT" or ISO 8601'
                ),
            }), 400

        result = find_preview(cam_arg, target_ts)
        if result is None:
            return jsonify({
                "error": "no_preview_found",
                "detail": (
                    f"no Synology preview for camera={cam_arg!r} at "
                    f"ts={ts_arg!r} (target_ts={target_ts})"
                ),
            }), 404

        # Optional JSON detail mode — useful for "did anything happen at
        # time X?" debugging without rendering the image inline.
        if request.args.get("detail") == "json":
            return jsonify({
                "camera": cam_arg,
                "ts_requested": ts_arg,
                "target_ts": target_ts,
                "preview_path": result,
                "size_bytes": os.path.getsize(result),
            })

        log.info(
            "preview: served %s (%d bytes) for camera=%s ts=%s -> %s",
            result, os.path.getsize(result), cam_arg, ts_arg, request.remote_addr,
        )
        response = send_file(
            result,
            mimetype="image/jpeg",
            as_attachment=False,
            download_name=f"{cam_arg.replace(' ', '_')}.jpg",
        )
        # Synology files don't change retroactively, but disable
        # caching for consistency with /snapshot and to keep the
        # 404-from-stale-cache footgun from biting.
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.route("/alert", methods=["POST"])
    def receive_alert():
        # Log the raw incoming request so we can see exactly what the camera sends
        raw = request.get_data(as_text=True)
        source_ip = (
            request.headers.get("X-Forwarded-For", request.remote_addr or "")
            .split(",")[0]
            .strip()
        )
        log.info(
            f"[RAW] POST /alert from {source_ip} (Content-Type={request.content_type}): {raw[:500]}"
        )

        # Parse JSON
        payload = request.get_json(silent=True)
        if not payload:
            log.warning(f"Rejecting request from {source_ip}: invalid JSON")
            return jsonify({"error": "Invalid JSON"}), 400

        # Accept BOTH:
        #  (a) Flat shape: {camera, ip, event, timestamp}
        #  (b) Reolink default: {type, secret, alarm: {alarmTime, channel, channelName, device, deviceModel, message, name, time, type}}
        flat = _normalize_payload(payload, source_ip)
        if flat is None:
            return jsonify(
                {"error": "Could not extract camera/event from payload"}
            ), 400

        camera_name = flat["camera"]
        ip = flat["ip"]
        event = flat["event"]
        timestamp = flat["timestamp"]

        # Resolve camera name aliases — Reolink webhook payloads sometimes
        # use a different casing than the canonical OSD name registered in
        # camera-creds.env. The aliases dict (infra.camera_aliases) maps
        # variants to the canonical friendly name; on legacy envs it
        # contains operator-flavored aliases that get scrubbed at
        # enrollment time.
        camera_name = resolve_camera_name(camera_name)

        # Validate camera name is known
        if camera_name not in cameras:
            log.warning(
                f"Unknown camera in payload from {source_ip}: {camera_name!r} (have: {list(cameras.keys())})"
            )
            return jsonify({"error": f"Unknown camera: {camera_name}"}), 400

        # Validate IP matches (prevent spoofing). Phase 6B.167 §13.5
        # Commit 14: resolve via by_ip() rather than the legacy
        # cameras[camera_name]["ip"] dict access. by_ip() returns a
        # CameraSpec; .ip is the canonical address from cameras.env.
        try:
            spec_by_ip = _by_camera_ip(ip)
        except KeyError:
            log.warning(
                f"Unknown IP in payload from {source_ip}: {ip!r} "
                f"(camera_name={camera_name!r})"
            )
            return jsonify({"error": f"Unknown IP: {ip}"}), 400
        expected_ip = spec_by_ip.ip
        if ip != expected_ip:
            log.warning(
                f"IP mismatch for {camera_name}: got {ip}, expected {expected_ip}"
            )
            return jsonify({"error": f"IP mismatch for {camera_name}"}), 400

        # Debounce: drop duplicate webhooks within the per-camera window.
        # Reolink fires 2-6 webhooks per physical event (state transitions);
        # this collapses them to 1 alert.
        #
        # Event-type-aware (2026-07-23):
        # Only MD events share a debounce window with prior MD events. A
        # VEHICLE or PEOPLE webhook that follows an MD webhook is genuinely
        # new information (the camera detected a different semantic class),
        # so it MUST pass through — debouncing it would silently drop alerts.
        # (Bug seen 2026-07-23 10:20:13 EDT: BFC MD at 10:20:13 reset the
        # window, BFC VEHICLE at 10:20:14 was dropped as "within 15s window".)
        if event == "md":
            debounce_window_seconds = _get_debounce_window_seconds(camera_name)
            if _should_debounce(camera_name, debounce_window_seconds):
                log.info(
                    "[DEBOUNCED] camera=%r event=%s window_seconds=%s",
                    camera_name,
                    event,
                    debounce_window_seconds,
                )
                return jsonify(
                    {
                        "status": "debounced",
                        "camera": camera_name,
                        "event": event,
                        "window_seconds": debounce_window_seconds,
                    }
                ), 200

        # Generate alert ID
        alert_id = str(uuid.uuid4())

        # Submit pipeline work to the bounded executor (Phase 1, 2026-07-24).
        # Previously: threading.Thread(target=_process_alert_safe, ...).start()
        # which was unbounded. Now: bounded queue; if full, return 503 and
        # log `queue_full` so the postmortem grep catches it.
        # Phase 6B.167 §13.5 Commit 14: rtsp_url comes from the
        # CameraSpec returned by by_ip() (above) — same registry, no
        # separate cameras[camera_name]["rtsp_url"] dict access.
        rtsp_url = spec_by_ip.rtsp_url
        STATE["last_webhook_at"] = datetime.now(EDT).isoformat()
        executor = get_webhook_executor()
        # Phase 6B.16 — route the alert to its class queue based on
        # the camera + event type, so vehicle events (especially from
        # the gatekeeper camera) jump ahead of
        # person/animal/motion events.
        # Phase 6B.53 — snapshot class_disabled counter so we can
        # detect whether THIS submit bumped it (vs a queue-full drop).
        class_disabled_at_entry = executor._rejected_per_class.get(
            "class_disabled", 0
        )
        future = executor.submit_for_class(
            # Phase 6B.62 — gatekeeper cameras get a configured deferred capture
            # (via threading.Timer) so the 6-frame trail shifts forward to
            # cover the truck arriving rather than already parked. Non-
            # gatekeeper cameras pass through to _process_alert_safe
            # unchanged.
            _process_alert_with_gatekeeper_delay,
            camera_name,
            event,
            alert_id,
            camera_name,
            timestamp,
            event,
            rtsp_url,
        )
        if future is None:
            # submit_for_class returns None for two distinct reasons:
            #   1. Queue is full → 503, tell the camera to retry later.
            #   2. (camera, event_type) is in DISABLED_CAMERA_EVENTS
            #      (Phase 6B.53) → 202, the drop is intentional.
            # Disambiguate by snapshotting the class_disabled counter
            # BEFORE the submit call and comparing to its current value.
            if executor._rejected_per_class.get("class_disabled", 0) > class_disabled_at_entry:
                log.info(
                    f"Alert {alert_id} DROPPED (class disabled) for {camera_name} "
                    f"({event} at {timestamp}) [from {source_ip}]"
                )
                return (
                    jsonify(
                        {
                            "status": "dropped",
                            "dropped": "class_disabled",
                            "camera": camera_name,
                            "event": event,
                        }
                    ),
                    202,
                )
            # Queue is full. Tell the camera to back off. We do NOT write
            # the alert to history — it never reached processing.
            log.warning(
                f"Alert {alert_id} REJECTED (queue full) for {camera_name} "
                f"({event} at {timestamp}) [from {source_ip}]"
            )
            return (
                jsonify(
                    {
                        "status": "queue_full",
                        "error": "webhook executor queue is full — retry later",
                    }
                ),
                503,
            )

        log.info(
            f"Alert {alert_id} queued for {camera_name} ({event} at {timestamp}) [from {source_ip}]"
        )

        return jsonify(
            {
                "status": "accepted",
                "alert_id": alert_id,
                "camera": camera_name,
            }
        ), 202

    return app


def _normalize_payload(payload: dict, source_ip: str) -> dict | None:
    """
    Convert incoming payload to flat {camera, ip, event, timestamp} dict.

    Supports two shapes:
    1. Flat shape (already-normalized client): {camera, ip, event, timestamp}
    2. Reolink default webhook shape:
        {
          "type": "...",                   # event topic like "motion", "ai", etc.
          "secret": "***",                 # camera's secret
          "alarm": {
            "alarmTime": "...",
            "channel": "...",
            "channelName": "...",
            "device": "Reolink device name",
            "deviceModel": "REOLINK_MODEL",
            "message": "...",
            "name": "<FRIENDLY_NAME>",  # this is the camera's friendly name
            "time": "ISO timestamp",
            "type": "person|vehicle|animal|motion|..."   # ← AI category
          }
        }

    Camera identification strategy:
    - For Reolink shape: match `alarm.name` to known camera's friendly name.
      deviceModel is NOT used for identification - after the 2026-07-24
      REOLINK_MODEL swap the fleet is mixed (4x REOLINK_MODEL new + 2x REOLINK_MODEL
      surviving) but every camera has a unique friendly name in
      camera_map, so name-matching is sufficient.
    - IP comes from request source (Reolink doesn't include it in payload).

    Returns None if no camera can be identified.
    """
    # Shape 1: already flat
    if all(k in payload for k in ("camera", "ip", "event", "timestamp")):
        return {
            "camera": payload["camera"],
            "ip": payload["ip"],
            "event": payload["event"],
            "timestamp": payload["timestamp"],
        }

    # Shape 2: Reolink default
    if "alarm" in payload and isinstance(payload["alarm"], dict):
        alarm = payload["alarm"]
        # Reolink puts the camera's friendly name in channelName/device, but
        # also sometimes puts an event description in `name` like
        # "Person Detected from <FRIENDLY_NAME>". Prefer channelName.
        device_name = (
            alarm.get("channelName") or alarm.get("device") or alarm.get("name")
        )
        event_type = alarm.get("type", "unknown")
        outer_type = payload.get("type", "")
        if outer_type and outer_type != event_type:
            event_type = outer_type
        # Reolink uses uppercase tags like "PEOPLE", "MOTION", "VEHICLE" — normalise
        event_type = event_type.lower() if isinstance(event_type, str) else "unknown"
        # Phase 6B.99 (PLAN.md §11.26): every timestamp visible to the user
        # (Telegram body, alert queue log line, audit record, vehicle history
        # JSON) must be EDT, not UTC. Reolink webhooks deliver ISO-8601 in
        # UTC (e.g. "2026-08-19T16:12:33.000+0000"). `to_edt_string` converts
        # to fixed EDT (UTC-4) and formats as "YYYY-MM-DD HH:MM:SS EDT".
        # On parse failure the raw input passes through unchanged — bad input
        # must not abort the alert pipeline (best-effort, never raises).
        raw_ts = (
            alarm.get("time") or alarm.get("alarmTime") or datetime.now(EDT).isoformat()
        )
        timestamp = _to_edt(raw_ts)
        return {
            "camera": device_name or "unknown",
            "ip": source_ip,  # Reolink doesn't include IP — use request source
            "event": event_type,
            "timestamp": timestamp,
        }

    # Unknown shape
    return None


# ----------------------------------------------------------------------
# Pipeline
# ----------------------------------------------------------------------


# Phase 6B.158 (2026-08-28) — §11.81: unified Qwen3.6-35B-A3B server.
# Both vision and text routes now point to the single server on :8093.
# The server handles multimodal (vision) and text via the same model.
# Override via VISION_LLM_URL / TEXT_LLM_URL env vars or llm-creds.env.
from infra.llm_config import load_text_config, load_vision_config

VISION_API_URL = load_vision_config().url
TEXT_API_URL = load_text_config().url

# vision_analyzer.analyze_frames() routes DEFAULT_URL through the pool;
# explicit api_url= overrides still work for one-off scripts/tests.
ALERT_FRAME_DIR = FRAMES_DIR
os.makedirs(ALERT_FRAME_DIR, exist_ok=True)


def _process_alert_safe(
    alert_id: str, camera_name: str, timestamp: str, event: str, rtsp_url: str
) -> None:
    """Background wrapper that catches and logs all exceptions."""
    try:
        _process_alert(alert_id, camera_name, timestamp, event, rtsp_url)
    except Exception:
        log.exception(f"Pipeline failed for alert {alert_id}")


# Phase 6B.62 (2026-08-07) — Deferred capture wrapper for gatekeeper cameras.
# Schedules _process_alert_safe to run after GATEKEEPER_CAPTURE_DELAY_S
# seconds, using threading.Timer so the executor worker returns immediately
# (does NOT block the queue). The Timer fires on a daemon thread, so a
# listener restart mid-delay loses the alert (acceptable — same failure
# mode as today's hung alerts).
#
# For non-gatekeeper cameras, returns _process_alert_safe directly so
# execution is unchanged.
def _process_alert_with_gatekeeper_delay(
    alert_id: str, camera_name: str, timestamp: str, event: str, rtsp_url: str
) -> None:
    """If gatekeeper: schedule a delayed run via threading.Timer. Else: passthrough."""
    if _code_for_camera(camera_name) not in GATEKEEPER_CAMERAS:
        _process_alert_safe(alert_id, camera_name, timestamp, event, rtsp_url)
        return

    log.info(
        f"[{alert_id}] deferred capture: scheduling _process_alert_safe "
        f"in {GATEKEEPER_CAPTURE_DELAY_S:.1f}s for {camera_name} ({event})"
    )

    def _fire():
        log.info(f"[{alert_id}] deferred capture: firing now after {GATEKEEPER_CAPTURE_DELAY_S:.1f}s wait")
        _process_alert_safe(alert_id, camera_name, timestamp, event, rtsp_url)

    timer = threading.Timer(GATEKEEPER_CAPTURE_DELAY_S, _fire)
    timer.daemon = True
    timer.start()


# Phase 2.2 (2026-08-05) — Telegram creds cache.
# Before: every call to _load_telegram_creds() opened the env file and
# re-parsed it. With 50+ alerts/day that's 50 pointless file reads.
# Now: cache parsed creds keyed on (path, mtime) — only re-read when
# the file's mtime changes. Surfaces reloads via log.info so the
# postmortem grep shows when the cache refreshed (helps diagnose
# "did the deploy pick up the new creds?").
_TELEGRAM_CREDS_CACHE: tuple[tuple[str, str], float] | None = None


def _load_telegram_creds() -> tuple[str, str]:
    """Load Telegram bot token + chat ID from file or env.

    Returns ("", "") if neither is available — callers must handle that
    case (notifier.notify() returns False, vehicle send logs a warning).

    Loaded here (instead of only inside the alert-generation block) so the
    Phase 6B.9 "arriving" message can fire right after phase-1 capture,
    before vision has run. Same load order is reused for the main
    pipeline further down — load once, use twice.

    Phase 2.2 (2026-08-05) — cached via mtime invalidation. The first
    call reads the file; subsequent calls with an unchanged mtime
    return the cached (token, chat_id) tuple. When the file's mtime
    changes (e.g., a deploy that touches telegram-creds.env), the
    cache is invalidated and the file is re-read.
    """
    global _TELEGRAM_CREDS_CACHE
    bot_token = ""
    chat_id = ""
    used_cache = False

    if os.path.exists(TELEGRAM_CREDS_FILE):
        mtime = os.path.getmtime(TELEGRAM_CREDS_FILE)
        if (
            _TELEGRAM_CREDS_CACHE is not None
            and _TELEGRAM_CREDS_CACHE[1] == mtime
        ):
            bot_token, chat_id = _TELEGRAM_CREDS_CACHE[0]
            used_cache = True
        else:
            try:
                tg_creds = load_telegram_creds(TELEGRAM_CREDS_FILE)
                bot_token = tg_creds.bot_token
                chat_id = tg_creds.chat_id
                _TELEGRAM_CREDS_CACHE = ((bot_token, chat_id), mtime)
                log.info(
                    f"Telegram creds loaded from {TELEGRAM_CREDS_FILE} "
                    f"(mtime={mtime:.0f})"
                )
            except (FileNotFoundError, ValueError) as e:
                log.warning(f"Failed to load telegram-creds.env: {e}")

    if not bot_token:
        bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not chat_id:
        chat_id = os.environ.get("TELEGRAM_CHAT_ID", "5264050975")

    # Phase 2.2 — at debug level, surface when the cache was used so
    # operators can see "this alert hit the cache" without INFO spam.
    if used_cache:
        log.debug("Telegram creds cache hit")

    return bot_token, chat_id


# ---------------------------------------------------------------------------
# Phase 6B.106 — Telegram body format helpers moved to telegram_formatter/
# ---------------------------------------------------------------------------
# 5 format helpers + 2 constants moved to telegram_formatter/vehicle_alert.py:
#   _format_qwen_confidence_line     → format_qwen_confidence_line
#   _format_detector_metadata_lines  → format_detector_metadata_lines
#   _format_motion_alert_vehicle_line → format_motion_alert_vehicle_line
#   _render_qwen_dict_lines          → render_qwen_dict_lines
#   _annotate_frame_bboxes           → annotate_frame_bboxes
#   _MATCHER_OUTPUT_SKIP_KEYS        → MATCHER_OUTPUT_SKIP_KEYS
#   _QWEN_OUTPUT_SKIP_KEYS           → QWEN_OUTPUT_SKIP_KEYS (back-compat alias)
#
# Extracted 2026-08-21. Verbatim pre-slim copy archived at
# listener/_format_helpers_archive_6B106.py for rollback / archaeology.
#
# Note: these were dead code in the slim listener.py post-6B.105c — the
# production pipeline never called them. The only live callers are
# scripts/probe_enriched_alert.py (ad-hoc operator probe) and the
# existing infra/tests/. They lived at the top of listener.py because
# they were created before the module-purity review was applied to
# listener.py. 6B.106 is the proper extraction.




def _process_alert(
    alert_id: str, camera_name: str, timestamp: str, event: str, rtsp_url: str
) -> None:
    """
    Run the full pipeline via 6-stage context-object driver.

    Phase 6B.105c (2026-08-21): the original 1754-line `_process_alert` block
    was lifted into `listener/vehicle_event_pipeline.py` as 6 named stages
    driven by `process_alert(ctx)`. This function is now a 22-line driver
    that builds the AlertContext and delegates.

    Phase 6B.106 (2026-08-22): person events on PERSON_GATEKEEPER_CAMERAS
    dispatch to `process_person_event(ctx)` instead — mirror of the vehicle
    pipeline but structured body, no LLM prose. See PLAN §11.36.

    Args:
        alert_id: UUID for this alert run.
        camera_name: Human-readable label.
        timestamp: ISO 8601 event timestamp.
        event: Event type ("motion", "person", "vehicle", etc.).
        rtsp_url: Full RTSP URL for this camera.
    """
    # Phase 6B.107 (§11.37) + 6B.108a-rev1 (§11.38.13) — motion gate runs
    # FIRST, ABOVE all routing logic. Gate verdict is suppress → exit; pass →
    # fall through to the existing camera-based routing (CAM1 person events still
    # go to person pipeline; everything else still goes to vehicle pipeline).
    # The gate ONLY adds suppress-on-noise capability; it never overrides
    # camera-based routing. Per maintainer 2026-08-23: keep gate + redundant pipeline
    # for now (safer); pipeline simplification (6B.108 §11.38.3-§11.38.12) is
    # deferred to a follow-on phase.

    # Phase 6B.154 (PLAN §11.77): per-camera × per-event-type cooldown at the
    # gate. Runs BEFORE the motion gate. Reads
    # config/motion_gate_thresholds.json [camera][gate_cooldown][event_type]
    # (seconds). 0 or absent = no cooldown (full backward-compatibility).
    # On a hit: log + return immediately — no frames, no YOLO, no Telegram,
    # no audit row. maintainer OOB 2026-08-28: "Can we do a cool down per camera
    # per event type in the gate? Can this be configurable?"
    # Dual-context import (matches the pattern used for motion_gate_dispatch
    # below + listener/vehicle_event_pipeline). Tested via test_gate_cooldown.py
    # + test_listener_gate_routing.py.
    try:
        from infra.gate_cooldown import is_in_gate_cooldown
    except ImportError:
        from listener.infra.gate_cooldown import is_in_gate_cooldown  # type: ignore[no-redef]
    in_cooldown, _last_seen = is_in_gate_cooldown(camera_name, event)
    if in_cooldown:
        log.info(
            f"[{alert_id}] gate_cooldown: suppressed "
            f"(camera={camera_name} event={event!r}) — "
            "no gate, no pipeline, no Telegram"
        )
        return

    output_dir_for_alert = os.path.join(ALERT_FRAME_DIR, alert_id)
    # Dual-context import (matches the pattern used for vehicle_event_pipeline).
    # When listener.py runs as __main__ (sys.path[0] = listener/), the bare
    # name resolves; when imported as listener.listener (tests), the package
    # form resolves. Tested via test_listener_gate_routing.py.
    try:
        from _motion_gate_dispatch import maybe_run_motion_gate
    except ImportError:
        from listener._motion_gate_dispatch import maybe_run_motion_gate
    gate_verdict = maybe_run_motion_gate(
        alert_id=alert_id,
        camera_name=camera_name,
        rtsp_url=rtsp_url,
        output_dir=output_dir_for_alert,
        timestamp=timestamp,  # Phase 6B.116 timestamp-fix
        event_type=event,     # Phase 6B.152 — per-camera × per-event-type config
    )
    if gate_verdict is not None:
        # Gate ran. If suppress → exit, no Telegram, no pipeline.
        # Exception: when the suppress reason is "high_conf_<class>_not_vehicle_no_pipeline"
        # AND the camera webhook event is "vehicle", override the suppression. The
        # camera's on-AI detected motion; YOLO just saw a person (likely the driver or
        # a bystander near the vehicle). The user's intent is "car drove out, that's the
        # alert" — we route to vehicle pipeline. Phase 6B.161 (2026-08-28).
        suppress_override_reason = (
            gate_verdict.is_suppress
            and event == "vehicle"
            and (gate_verdict.reason or "").endswith("_not_vehicle_no_pipeline")
        )
        if gate_verdict.is_suppress and not suppress_override_reason:
            log.info(
                f"[{alert_id}] motion_gate: suppressed ({gate_verdict.reason}) — "
                "no Telegram, no pipeline"
            )
            return
        if suppress_override_reason:
            log.warning(
                f"[{alert_id}] motion_gate: suppressed-by-gate ({gate_verdict.reason}) "
                f"OVERRIDDEN — camera event=vehicle wins, routing to vehicle pipeline"
            )
        # Otherwise: fall through to the vehicle pipeline dispatch (the
        # catch-all after the person + animal structured pipelines above).
        # Phase 6B.170 (§11.111, 2026-09-02): this was historically called
        # "legacy camera-based routing" because the 1554-line monolith
        # used to own all vehicle pipeline code; after the §11.111
        # extraction it lives in listener.vehicle_pipeline.* and is the
        # one and only vehicle path (no longer legacy). The gate's
        # verdict is logged but does NOT change the routing decision
        # (gate only adds suppress-on-noise; routing is camera+event
        # based, as designed in Phase 6B.106).
        log.info(
            f"[{alert_id}] motion_gate: pass (decision={gate_verdict.decision} "
            f"class={gate_verdict.class_label} "
            f"conf={gate_verdict.confidence:.2f}) — continuing to vehicle pipeline dispatch"
        )

    # Phase 6B.106 — route person events on person-gatekeeper cameras
    # to the structured pipeline BEFORE the vehicle pipeline. Otherwise
    # the legacy path handles them (LLM prose Telegram). UNCHANGED.
    # Phase 6B.108a (§11.38.6) — pass the gate verdict through so the
    # person pipeline can reuse the gate's 4 frames when
    # PIPELINE_USES_GATE_CROPS=1.
    #
    # Phase 6B.145 (§11.67) — promote `event=md` to 'person' when the
    # gate is confident it's a person on a person-gatekeeper camera.
    # Reolink's on-device person classifier misses ~50% of people it
    # sees (slow walkers, partial occlusion, lighting) and sends `md`
    # instead of `people`. The gate's YOLO has a 0.65 confidence floor
    # (THRESHOLDS_BY_CLASS for person). Mirrors the 6B.129a logic
    # that already promotes `event=md → vehicle` when the gate says
    # vehicle. Promotion only fires on PERSON_GATEKEEPER_CAMERAS — off
    # those cameras the gate's person verdict is handled by the
    # vehicle pipeline's `_non_vehicle_first_pass`.
    event_lower = (event or "").strip().lower()
    gate_says_person = (
        gate_verdict is not None
        and not gate_verdict.is_suppress
        and gate_verdict.decision == "person"
    )
    if (
        gate_says_person
        and event_lower not in ("person", "people")
        and _code_for_camera(camera_name) in PERSON_GATEKEEPER_CAMERAS
    ):
        # gate_says_person requires gate_verdict is not None
        try:
            from motion_gate_pipeline import GateVerdict
        except ImportError:
            from listener.motion_gate_pipeline import GateVerdict
        verdict = cast(GateVerdict, gate_verdict)
        log.info(
            f"[{alert_id}] event_promotion: {event_lower!r} → 'people' "
            f"(gate verdict: decision={verdict.decision} "
            f"class={verdict.class_label} conf={verdict.confidence:.2f})"
        )
        event_lower = "people"
    if event_lower in ("person", "people") and _code_for_camera(camera_name) in PERSON_GATEKEEPER_CAMERAS:
        _process_person_alert(
            alert_id=alert_id,
            camera_name=camera_name,
            timestamp=timestamp,
            event=event_lower,
            rtsp_url=rtsp_url,
            gate_verdict=gate_verdict,
        )
        return

    # Phase 6B.165 (PLAN §11.86) — animal event pipeline scaffold.
    # Animal events on any camera route to the animal pipeline before
    # falling through to the vehicle catch-all. Mirrors the person
    # branch above. The scaffold (§11.86.1) is audit-only; Qwen,
    # matching, and Telegram arrive in §11.86.2 through §11.86.6.
    #
    # NOTE: the gate's `event_promotion: 'animal' → 'people'` logic
    # already runs above (lines ~1745-1768). If the gate's YOLO
    # classified a person on an animal webhook, the event was promoted
    # to event_lower='people' and routed to the person branch above.
    # What reaches this branch is an animal event that the gate did
    # NOT promote — i.e., a genuine animal or a false YOLO detection.
    if event_lower == "animal":
        _process_animal_alert(
            alert_id=alert_id,
            camera_name=camera_name,
            timestamp=timestamp,
            event=event_lower,
            rtsp_url=rtsp_url,
            gate_verdict=gate_verdict,
        )
        return

    # Phase 6B.170 (§11.111, 2026-09-02): the 1554-line monolith
    # listener.vehicle_event_pipeline was split into 9 submodules under
    # listener.vehicle_pipeline (one per stage + helpers). This import
    # points at the package now. The legacy file vehicle_event_pipeline.py
    # remains in the tree as a re-export shim until the next cleanup
    # commit (so any test files that still import the old symbol path
    # during the transition have one source of truth).
    try:
        from vehicle_pipeline import AlertContext, process_alert
    except ImportError:
        from listener.vehicle_pipeline import AlertContext, process_alert

    # Load Telegram creds up front — Phase 6B.9 message 1 ("arriving") fires before
    # vision has run, so we can't wait for the post-vision load block. Same creds
    # are reused for the main pipeline further down.
    _bot_token, _chat_id = _load_telegram_creds()

    # Phase 6B.129a (§11.51) — promote non-vehicle events to vehicle when
    # the motion gate's YOLO classifier agrees they're a vehicle. Reolink's
    # built-in classifier mislabels slow-moving or unusual vehicles (e.g.,
    # a parked red tractor — alert 5b8284b3 2026-08-26 13:05:54 returned
    # `type=md` even though the gate's YOLO returned `class=car conf=0.82`).
    # Without the promotion, the pipeline routed `event=md` straight to
    # single-frame vision and got back "SUV and tractor parked on gravel road"
    # instead of an identification. The gate has already applied the
    # per-class confidence threshold (THRESHOLDS_BY_CLASS — car/truck/bus
    # ≥ 0.50, motorcycle/bicycle ≥ 0.45) before emitting the verdict, so no
    # additional promotion floor is required here.
    gate_says_vehicle = (
        gate_verdict is not None
        and not gate_verdict.is_suppress
        and gate_verdict.decision == "vehicle"
    )
    effective_event = "vehicle" if (event == "vehicle" or gate_says_vehicle) else event
    if gate_says_vehicle and event != "vehicle":
        # gate_says_vehicle requires gate_verdict is not None
        try:
            from motion_gate_pipeline import GateVerdict
        except ImportError:
            from listener.motion_gate_pipeline import GateVerdict
        verdict = cast(GateVerdict, gate_verdict)
        log.info(
            f"[{alert_id}] event_promotion: {event!r} → 'vehicle' "
            f"(gate verdict: decision={verdict.decision} "
            f"class={verdict.class_label} conf={verdict.confidence:.2f})"
        )
    ctx = AlertContext(
        alert_id=alert_id,
        camera_name=camera_name,
        # Phase 6B.168 (2026-08-31): translate the friendly camera_name
        # from the webhook payload to its canonical CAM{N} code once, at
        # the boundary. All gatekeeper membership tests inside
        # vehicle_event_pipeline.py compare ctx.camera_code against
        # ctx.gatekeeper_cameras (which is code-keyed per listener.py:282
        # and infra/vision_queue.py:167). The pre-fix path passed
        # camera_name (friendly) into both sides of the `in` test, which
        # silently failed every vehicle event to the
        # 'is not a gatekeeper — skipping match-alert path' branch —
        # see alert jsonl entries for c12ad03e / b85fb18a / 6f098a21 /
        # 22aea5bb on 2026-08-31.
        camera_code=_code_for_camera(camera_name),
        timestamp=timestamp,
        event_type=effective_event,
        rtsp_url=rtsp_url,
        output_dir=os.path.join(ALERT_FRAME_DIR, alert_id),
        is_vehicle_event=(effective_event == "vehicle"),
        known_vehicles=[],
        bot_token=_bot_token,
        chat_id=_chat_id,
        api_url=VISION_API_URL,
        gatekeeper_cameras=GATEKEEPER_CAMERAS,
        # Phase 6B.108a (§11.38.6) — pass the gate verdict onto the
        # AlertContext so gate_aware_vehicle_capture can reuse the gate's
        # 4 frames when PIPELINE_USES_GATE_CROPS=1. gate_verdict is None
        # when the gate didn't run (MOTION_GATE_ENABLED=0 or capture
        # failed mid-capture). capture_source defaults to "rtsp" — the
        # wrapper overwrites it after its decision.
        gate_verdict=gate_verdict if gate_verdict is not None and not gate_verdict.is_suppress else None,
    )
    result = process_alert(ctx)
    log.info(
        f"[{alert_id}] Pipeline complete. "
        f"Telegram: {result.get('telegram_sent')}, "
        f"Alert id: {result.get('alert_id')}, "
        f"capture_source={ctx.capture_source}"
    )


def _process_person_alert(
    alert_id: str, camera_name: str, timestamp: str, event: str, rtsp_url: str,
    gate_verdict=None,
) -> None:
    """Phase 6B.106 — person-gatekeeper pipeline driver.

    Builds a PersonContext and delegates to process_person_event().
    Mirrors _process_alert's structure but is much smaller — person
    events don't need vehicle matching, threat-level LLM, or motion
    composite generation. Single Telegram, single path.

    Phase 6B.108a (§11.38.6) — gate_verdict kwarg added. When the
    gate ran (MOTION_GATE_ENABLED=1) and the verdict was a pass
    (not suppress), the wrapper passes it through so the person
    pipeline's gate_aware_person_capture can reuse the gate's 4 frames.
    Suppress verdicts are filtered to None here (gate already exited
    the flow before we got here; this is defense-in-depth).
    """
    # Dual-context import (matches _process_alert pattern).
    try:
        from person_event_pipeline import PersonContext, process_person_event
    except ImportError:
        from listener.person_event_pipeline import PersonContext, process_person_event

    _bot_token, _chat_id = _load_telegram_creds()

    ctx = PersonContext(
        alert_id=alert_id,
        camera_name=camera_name,
        timestamp=timestamp,
        event_type=event,
        rtsp_url=rtsp_url,
        output_dir=os.path.join(ALERT_FRAME_DIR, alert_id),
        bot_token=_bot_token,
        chat_id=_chat_id,
        api_url=VISION_API_URL,
        # Phase 6B.108a (§11.38.6) — same wiring as AlertContext.
        gate_verdict=gate_verdict if gate_verdict is not None and not gate_verdict.is_suppress else None,
    )
    result = process_person_event(ctx)
    if result.get("suppressed"):
        log.info(
            f"[{alert_id}] Person alert suppressed: "
            f"reason={result.get('suppressed_reason')}"
        )
        return
    log.info(
        f"[{alert_id}] Person pipeline complete. "
        f"matched={result.get('matched_name')!r} "
        f"via={result.get('matched_via')!r} "
        f"telegram_sent={result.get('telegram_sent')} "
        f"capture_source={ctx.capture_source}"
    )


def _process_animal_alert(
    alert_id: str, camera_name: str, timestamp: str, event: str, rtsp_url: str,
    gate_verdict=None,
) -> None:
    """Phase 6B.165 (§11.86.1) — animal event pipeline driver (scaffold).

    Builds an AnimalContext and delegates to process_animal_event().
    Mirrors _process_person_alert above but for animal events. The
    scaffold (§11.86.1) is audit-only — Qwen, matching, and Telegram
    arrive in subsequent sub-phases (§11.86.2 through §11.86.6).

    Args:
        alert_id: UUID for this alert run.
        camera_name: Human-readable camera label.
        timestamp: ISO 8601 event timestamp from the webhook.
        event: Event type (always "animal" lowercased by listener).
        rtsp_url: Full RTSP URL for this camera (reserved for future
            capture — the scaffold does not capture).
        gate_verdict: Optional motion-gate verdict (Phase 6B.108a
            pattern). Reserved for §11.86.2+; the scaffold ignores it.
    """
    # Dual-context import (matches _process_alert + _process_person_alert
    # pattern). Bare 'animal_event_pipeline' resolves when listener.py
    # runs as __main__; 'listener.animal_event_pipeline' resolves when
    # imported as listener.listener (tests).
    try:
        from animal_event_pipeline import AnimalContext, process_animal_event
    except ImportError:
        from listener.animal_event_pipeline import AnimalContext, process_animal_event

    _bot_token, _chat_id = _load_telegram_creds()

    ctx = AnimalContext(
        alert_id=alert_id,
        camera_name=camera_name,
        timestamp=timestamp,
        event_type=event,
        rtsp_url=rtsp_url,
        output_dir=os.path.join(ALERT_FRAME_DIR, alert_id),
        bot_token=_bot_token,
        chat_id=_chat_id,
        api_url=VISION_API_URL,
    )
    result = process_animal_event(ctx)
    if result.get("suppressed"):
        log.info(
            f"[{alert_id}] Animal alert suppressed: "
            f"reason={result.get('suppressed_reason')}"
        )
        return
    log.info(
        f"[{alert_id}] Animal pipeline (scaffold) complete. "
        f"telegram_sent={result.get('telegram_sent')} "
        f"phase={result.get('phase')!r}"
    )


# ----------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------


if __name__ == "__main__":
    # Phase 6B.38 — Test/prod isolation. Defense-in-depth: if a test
    # harness sets FARMSURV_TESTING=1 (as conftest.py does), refuse to
    # start the listener. The logger gate (PRODUCTION_MODE) prevents
    # log-file contamination, but this guard prevents accidental
    # network traffic on port 8090 during tests. If you see this
    # exit, find the test that set FARMSURV_TESTING and unset it.
    if os.environ.get("FARMSURV_TESTING") == "1":
        sys.stderr.write(
            "alert_listener.py refuses to start under FARMSURV_TESTING=1. "
            "Unset the env var and try again.\n"
        )
        sys.exit(78)  # EX_CONFIG — configuration error

    # Run retention cleanup at startup
    log.info("Running retention cleanup...")
    run_cleanup()

    app = create_app()
    log.info("Starting alert_listener on port 8090")
    log.info(f"Loaded {len(app.config['CAMERAS'])} cameras")

    # Phase 6B.150: heartbeat thread removed (maintainer 2026-08-28:
    # "no other identification or activity" — heartbeat was a
    # top-of-hour re-evaluation Telegram, not part of the
    # webhook → gate → vehicle/person pipeline). The archived
    # implementation lives at listener/_heartbeat_archive_6B150.py.

    # Start cleanup thread (hourly retention sweep). Mirrors the
    # heartbeat pattern — daemon thread that keeps disk usage bounded
    # without requiring listener restarts. Without this, data/frames
    # fills until the next process restart (was 564 MB on 2026-07-20).
    start_cleanup_thread()
    log.info("Cleanup thread started (hourly retention sweep)")

    # Start a persistent RTSP reader for each registered camera to avoid
    # the Reolink pre-buffer dump + replay bug. Holding the connection
    # open 24/7 means the camera transitions to "live stream" mode
    # after a ~5s warmup, and the 6-frame alert burst pulls from the
    # in-memory ring buffer instead of opening a fresh RTSP session.
    # Origin: 2026-08-05 (one camera); 2026-08-17 adds a second
    # (Phase 6B.87). 2026-08-20 (Phase 6B.104) — one camera was demoted
    # from the *vehicle* gatekeeper tier (no longer in
    # GATEKEEPER_CAMERAS) but kept on the persistent RTSP list because
    # persistent RTSP is about reliable frame capture, not vehicle
    # gatekeeping. If it later needs to drop persistent RTSP too,
    # remove it from the registry. See PLAN §11.32.
    # The registry is per-camera (keyed by code); each persistent
    # RTSP camera boots independently, so a failure on one doesn't
    # take down the others.
    from infra.persistent_rtsp import (
        PersistentRTSPReader,
        get_reader,
        init_reader_registry,
        set_reader,
    )

    # Phase 6B.167 §13.5 (T3 C19 follow-on): seed the persistent_rtsp
    # name→code lookup so set_reader() can transparently store the
    # reader under both its canonical friendly name AND its CAM{N}
    # code. Without this, set_reader() logs `code=<unresolved>` and
    # only stores under the friendly name, breaking
    # `/snapshot?camera=CAM5` lookups (which arrive post-§13.4 alias
    # resolution as CAM{N} codes, not friendly names).
    init_reader_registry(_load_camera_specs(env_path=None))

    # Phase 6B.151 originally wired persistent RTSP to all 6 cameras.
    # 2026-08-28 — Memory diet: only the two gatekeeper cameras stay on
    # persistent RTSP. Each gatekeeper reader was burning ~1.5 GB of RSS
    # (180-frame ring of 2304x1296 PIL.Image @ 8.54 MB each) and the other
    # 4 readers were idle most of the time. Cutting from 6→2 frees ~6 GB.
    # Non-gatekeepers fall back to on-demand RTSP from frame_capture.
    # See PLAN §11.32, §11.74 (fail-loud contract).
    # 2026-08-28 — Phase 6B.160: reverted to all-6 cameras. All Reolink
    # main streams were lowered from 15fps → 2fps and bitrate 6144→3072 kbps,
    # so each reader is now ~50 MB ring (90 frames × ~560 KB JPEG) instead of
    # 1.5 GB. Six readers × 50 MB = 300 MB total ring memory — well within
    # the 4.71 GB system free RAM headroom. Persistent RTSP now applies to
    # every camera so the on-demand fallback is never used.
    # Phase 6B.167 §13.5 Commit 14: iterate over the registry, not a
    # hardcoded operator-flavored list. All cameras in the registry
    # get a persistent reader; the registry is the single source of
    # truth for which cameras exist + their RTSP URLs. New camera
    # enrollments automatically get persistent RTSP coverage.
    for _camera_code in _all_camera_codes(env_path=None):
        try:
            _spec = _by_camera_code(_camera_code)
        except KeyError:
            log.warning(
                "Camera code %r not in registry — persistent reader NOT started.",
                _camera_code,
            )
            continue
        _url = _spec.rtsp_url
        _camera_name = _spec.name
        if not _url:
            log.warning(
                "%s RTSP URL not found in cameras.env — "
                "persistent reader NOT started.",
                _camera_name,
            )
            continue
        try:
            # Phase 6B.80 (2026-08-16, PLAN §11.13) — scheduled_reconnect_seconds
            # = 1 h (default) breaks the zombie-RTSP cycle that caused the
            # 41h uptime + frozen frames_decoded failure on 2026-08-16.
            # The watchdog fires _scheduled_reconnect_fire() every cadence,
            # which stop+restart the decode thread and close/reopen the
            # av container. Cadence is overridable via the
            # FARMSV_RTSP_RECONNECT_SECONDS env var (see persistent_rtsp.py).
            _reader = PersistentRTSPReader(rtsp_url=_url)
            _reader.start()
            set_reader(_camera_name, _reader)
            # 2026-08-14 — Identity check. set_reader writes to
            # the infra.persistent_rtsp module's _readers dict global.
            # get_reader reads from the SAME module instance.
            # If anything ever creates a second module instance (e.g.
            # another file doing `from persistent_rtsp import ...` as
            # top-level, which Python would resolve via sys.path[0] if
            # infra/ is on the path), the write goes to one global and
            # the read comes from another — silent dual-state bug.
            # Asserting identity here turns that into a boot-time crash
            # instead of a runtime slow-path bug.
            _registered = get_reader(_camera_name)
            if _registered is not _reader:
                raise RuntimeError(
                    f"Persistent reader wiring mismatch for {_camera_name}: "
                    f"set_reader wrote one instance (id={id(_reader)}) but "
                    f"get_reader returns another (id={id(_registered)}). "
                    f"Two module instances of infra.persistent_rtsp exist. "
                    f"Refusing to boot — the gatekeeper capture path would "
                    f"silently fall back to on-demand capture and miss "
                    f"pre-event motion trails."
                )
            log.info(
                "PersistentRTSPReader started for %s "
                "(%s) — identity check passed",
                _camera_name,
                _url.split("@")[-1],
            )
        except Exception as _reader_boot_err:
            log.warning(
                "Failed to start persistent RTSP reader for %s: %s "
                "(capture for that camera will fall back to on-demand)",
                _camera_name,
                _reader_boot_err,
            )

    # Phase 6B.25 — write matcher_telemetry.json every 5 minutes so
    # operators can see per-pass counter and latency without scraping
    # logs. Atomic write (tmp + os.replace). Daemon thread, dies with
    # the listener. Path matches the project layout (data/ at repo root).
    try:
        from infra.matcher_telemetry import start_telemetry_snapshot_thread
        from infra.paths import STATE_DIR
        start_telemetry_snapshot_thread(
            output_path=os.path.join(STATE_DIR, "matcher_telemetry.json"),
            interval_seconds=300,
        )
        log.info(
            "Matcher telemetry snapshot thread started "
            "(every 300s -> data/matcher_telemetry.json)"
        )
    except Exception as _tel_boot_err:
        log.warning(
            "Failed to start matcher telemetry snapshot thread: %s",
            _tel_boot_err,
        )

    # Listener binds 0.0.0.0 so Reolink cameras on the LAN can POST to /alert.
    # This is by design — the LAN is the trust boundary. Bandit B104 flags
    # all-interfaces bind as a hardening concern. Mitigation pending:
    #   - Webhook signature verification (HMAC) for /alert
    #   - IP allowlist (only known camera IPs)
    #   - Rate-limit per source IP
    # Until then, the listener relies on LAN isolation. See PLAN.md open
    # questions for the "webhook auth" item.
    app.run(host="0.0.0.0", port=8090, debug=False, threaded=True)  # nosec B104
