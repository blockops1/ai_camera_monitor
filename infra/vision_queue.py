"""
vision_queue.py — Single-flight priority queue for vision LLM calls.

STATUS: stable
THREAD SAFETY: thread-safe (heap guarded by threading.Condition; multiple
    callers can enqueue concurrently)

INPUTS:
    - function arg `fn`: Callable (required) — function to run on the
      worker thread (the queue is generic; the caller supplies fn)
    - function arg `camera`: str (required) — CameraSpec.code OR
      CameraSpec.name (the queue translates via infra.cameras before
      membership tests against the eligible set)
    - function arg `priority`: int | None (optional) — lower = higher
      priority (PRIORITY_GATEKEEPER=0, PRIORITY_OUTSIDE=1, PRIORITY_INSIDE=5);
      None means "derive from camera eligibility"
    - function arg `timeout_s`: float (optional, default 60.0) — how
      long the CALLER is willing to wait before its future times out
    - function arg `*args`, `**kwargs` — forwarded to `fn` on dispatch

OUTPUTS:
    - return value: VisionQueue.submit() returns concurrent.futures.Future
      (caller awaits it for the result; future fires TimeoutError if
      queue depth or caller-side timeout elapses first)
    - log line per enqueue + completion (debug level)
    - on overflow: returns a Future already in failed state with
      QueueOverflowError

PUBLIC API:
    QueueOverflowError — raised when MAX_QUEUE_DEPTH exceeded for an
      INSIDE-priority job (inside cameras fail fast instead of blocking)
    VisionQueue() -> VisionQueue
        .submit(fn, *args, camera, priority=None, timeout_s=60.0, **kwargs)
            -> concurrent.futures.Future
        .start() -> None — start the worker thread (idempotent)
        .stop() -> None — drain + stop (idempotent)
        .stats() -> dict — counters (queue_depth, processed, dropped,
            max_wait_ms)
    get_queue() -> VisionQueue
        Process-wide singleton (creates + starts on first call).
    PRIORITY_GATEKEEPER / PRIORITY_OUTSIDE / PRIORITY_INSIDE — int constants
    GATEKEEPER_CAMERAS — frozenset[str] of CameraSpec.code values
    PHASE6A_ELIGIBLE_CAMERAS — frozenset[str] of CameraSpec.code values
    code_for(camera) -> str
        Translate a CameraSpec.name OR CameraSpec.code to its canonical
        CameraSpec.code via infra.cameras.load_cameras(). Falls back to
        the input string unchanged when no spec matches (lets tests pass
        synthetic identifiers without a real cameras.env).

DOES NOT DO:
    - Call the vision LLM — the caller supplies `fn` (usually
      infra.vision_analyzer.analyze_frames)
    - Persist queue state across restarts — process-local only
    - Retry on errors — caller handles via the Future

WHY HERE:
    The local Qwen3-VL runs with --parallel 1 (full 8192 ctx per
    request, no contention). Multiple camera webhooks fire concurrently,
    so we need a queue to serialize vision calls AND prioritize them.

    Burned 2026-07-20: with --parallel 4 llama-server crashed under
    4K image bursts (each parallel slot only got 2048 ctx). Dropping
    to --parallel 1 means we MUST serialize.

    Burned 2026-08-07: 41 person alerts queued behind 1 gatekeeper
    vehicle alert — every gatekeeper call timed out. Queue overflow
    policy added so inside cameras fail fast instead of starving
    the perimeter.

    Priority rules (lower = higher priority):
        PRIORITY_GATEKEEPER = 0  cameras in GATEKEEPER_CAMERAS
                                — gatekeeper must clear first so a
                                vehicle arrival isn't blocked
        PRIORITY_OUTSIDE    = 1  cameras in PHASE6A_ELIGIBLE_CAMERAS
                                (security perimeter)
        PRIORITY_INSIDE     = 5  everything else (descriptive only)

    Overflow policy (Phase.61): if heap size >= MAX_QUEUE_DEPTH
    when an INSIDE job arrives, the job is dropped with
    QueueOverflowError. Gatekeeper + outside jobs NEVER drop.

    Code-keyed eligibility sets (Phase.167 §13.5 Commit 12, 2026-08-30;
    updated Phase.168 2026-08-31 to CAM{N} identifiers per the NEW
    cameras.env schema):
    GATEKEEPER_CAMERAS and PHASE6A_ELIGIBLE_CAMERAS contain
    CameraSpec.code values (e.g. "CAM5" for the outside-front-solar
    camera under NEW schema). The translation from a caller-passed name
    to the canonical code happens via code_for() inside submit().

CALLED BY:
    - infra.vision_analyzer: queue.submit(...) — the only producer
    - infra.pipeline_integration: PHASE6A_ELIGIBLE_CAMERAS membership
      (uses code_for() to translate a friendly name first)
    - listener.listener.bootstrap: get_queue() / start()

CALLS INTO:
    - heapq: priority queue implementation
    - threading.Condition + threading.Thread: worker loop + drain
    - infra.cameras (Phase.167 Commit 12): load_cameras() inside
      code_for() to translate a CameraSpec.name to its CameraSpec.code

RELATED:
    - infra.cameras — source of CameraSpec.code/name/ip triples
    - infra.vision_analyzer — the function typically passed to submit()
    - infra.pipeline_integration — second consumer of
      PHASE6A_ELIGIBLE_CAMERAS

HISTORY:
    2026-08-30 — Phase.167 §13.5 Commit 12: replaced name-keyed
        GATEKEEPER_CAMERAS and PHASE6A_ELIGIBLE_CAMERAS with code-
        keyed frozensets. submit() now translates caller-passed names
        to codes via infra.cameras.load_cameras().
    2026-08-31 — Phase.168: switched the literal code values from
        legacy identifiers ("OUTSIDE_FRONT_SOLAR") to CAM{N} codes
        ("CAM5") to match the NEW cameras.env schema deployed 2026-08-30.
        Pre-fix the membership test in submit() never matched any camera
        and every vehicle vision call silently fell to PRIORITY_INSIDE,
        starving the matcher. Also fixed the same camera_name-vs-code
        mismatch in listener/vehicle_event_pipeline.py match_stage
        (5 sites) by introducing ctx.camera_code populated once at the
        boundary in listener.py:driver.
"""

from __future__ import annotations

import concurrent.futures
import heapq
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any


class QueueOverflowError(Exception):
    """Raised when a low-priority vision job is dropped because the queue is full.

    Phase.61 (2026-08-07): inside-tier jobs are dropped on overflow rather
    than blocking gatekeeper/perimeter work forever. The caller can catch
    this and produce a 'queue overflow' error sentinel for the alert path.
    """

log = logging.getLogger("vision_queue")

# Priority tiers — lower number = processed first
PRIORITY_GATEKEEPER = 0  # cameras in GATEKEEPER_CAMERAS (jump the queue)
PRIORITY_OUTSIDE = 1     # cameras in PHASE6A_ELIGIBLE_CAMERAS (security perimeter)
PRIORITY_INSIDE = 5      # everything else (descriptive only)
# Phase.61 — MAX_QUEUE_DEPTH for overflow drop policy. When the heap
# already has this many jobs and a low-priority (inside) job arrives,
# drop it immediately rather than letting it block forever.
MAX_QUEUE_DEPTH = 10

# Phase.167 §13.5 Commit 12 — code-keyed eligibility sets.
#
# GATEKEEPER_CAMERAS contains CameraSpec.code values (CAM{N} under the
# NEW schema) for cameras whose vision calls should jump the queue
# (priority 0). Submitting a job for any code in this set routes it
# ahead of all outside-tier work.
#
# PHASE6A_ELIGIBLE_CAMERAS contains CameraSpec.code values for cameras
# that participate in Phase 6A face recognition. These get
# PRIORITY_OUTSIDE; everything else falls through to PRIORITY_INSIDE.
#
# Both sets are intentionally operator-agnostic identifiers (the codes
# come from infra.cameras). No friendly names appear in this source.
# Callers may pass either a code or a name to submit(); the translation
# happens inside code_for() below.
#
# Future direction: these eligibility sets are policy data — long term
# they belong in cameras.env per-camera (one boolean per CameraSpec),
# not hardcoded here. For Commit 12 the literal code sets stay in this
# file; the move to cameras.env is tracked as a follow-up.

# Phase.168 (2026-08-31): under the NEW cameras.env schema (Phase
# 6B.167 §13.4) each spec's `.code` is a CAM{N} identifier (CAM1..CAM6),
# and code_for() (L186) returns spec.code for both name and code
# callers. So GATEKEEPER_CAMERAS and PHASE6A_ELIGIBLE_CAMERAS must
# also be CAM{N} codes — not the legacy friendly names they previously
# held. The pre-fix sets were keyed by legacy codes (e.g.
# "OUTSIDE_FRONT_SOLAR") which never appear under NEW schema, so the
# membership test in submit() (L295) was always False for every gatekeeper
# camera — every vehicle vision call silently fell to priority 5
# (inside-tier), causing the backlog that starved vehicle events and
# triggered the matcher-doesn't-run failure mode fixed at
# listener/vehicle_event_pipeline.py:652 (Phase.168).
#
# These six cameras match the gatekeeper set in listener.py:282 and
# listener/_motion_gate_dispatch.py:112. Future direction is to
# express this as per-camera booleans in cameras.env and load it
# dynamically — tracked as Phase.169 follow-up.
GATEKEEPER_CAMERAS: frozenset[str] = frozenset({
    "CAM1",
    "CAM2",
    "CAM3",
    "CAM4",
    "CAM5",
    "CAM6",
})

# Phase.24 Fix C — heartbeat was originally planned as a separate
# priority tier, but the heartbeat loop was reworked to read cached
# vision results instead of calling the vision queue directly
# (heartbeat.py uses get_last_vision_result, not analyze_frames).
# Removed the unused constant.

# Phase.168 (2026-08-31): same NEW-schema migration as
# GATEKEEPER_CAMERAS above — must be CAM{N} codes under the
# current cameras.env format.
PHASE6A_ELIGIBLE_CAMERAS: frozenset[str] = frozenset({
    "CAM1",
    "CAM3",
    "CAM4",
    "CAM5",
    "CAM6",
})


def code_for(camera: str) -> str:
    """Translate a CameraSpec.name OR CameraSpec.code to its canonical code.

    Phase.167 §13.5 Commit 12: callers historically passed friendly
    names; the queue's eligibility sets are code-keyed, so we need
    a translation step.

    Args:
        camera: CameraSpec.code OR CameraSpec.name (or any string the
            caller chooses — see "fallback" below).

    Returns:
        The CameraSpec.code that matches `camera`. If `camera` is
        already a known code, returns it unchanged. If it's a known
        name, returns the spec's code. If no spec matches, returns
        `camera` unchanged as a best-effort fallback (lets tests pass
        synthetic identifiers without standing up a cameras.env).
    """
    from infra.cameras import load_cameras

    for spec in load_cameras():
        if spec.code == camera:
            return spec.code
        if spec.name == camera:
            return spec.code
    return camera


@dataclass(order=True)
class _Job:
    """One queued vision call. Ordered by (priority, seq) so ties are FIFO."""
    priority: int
    seq: int
    camera: str = field(compare=False)
    fn: Callable[..., Any] = field(compare=False)
    args: tuple = field(compare=False)
    kwargs: dict = field(compare=False)
    future: Any = field(compare=False, repr=False)
    enqueued_at: float = field(compare=False, default=0.0)


class VisionQueue:
    """Single-flight priority queue for vision LLM calls.

    One background worker thread pulls jobs in priority order and runs
    them serially. Callers submit with `submit(...)` and get back a
    concurrent.futures.Future that resolves to the function's return.

    Designed to be a module-level singleton — there is exactly one
    vision pipeline in the listener process.
    """

    def __init__(self) -> None:
        self._heap: list[_Job] = []
        self._cv = threading.Condition()
        self._seq = 0
        self._worker: threading.Thread | None = None
        self._stop = False
        self._processed = 0
        self._dropped = 0  # jobs whose caller had already given up
        self._lock = threading.Lock()
        # Stats for debugging
        self._max_wait_ms = 0.0

    def start(self) -> None:
        """Start the worker thread. Idempotent."""
        if self._worker is not None and self._worker.is_alive():
            return
        self._stop = False
        self._worker = threading.Thread(
            target=self._run, name="vision-queue", daemon=True
        )
        self._worker.start()
        log.info("VisionQueue worker started")

    def stop(self) -> None:
        """Signal worker to exit after current job. Pending jobs get
        their futures cancelled."""
        with self._cv:
            self._stop = True
            self._cv.notify_all()

    def submit(
        self,
        fn: Callable[..., Any],
        *args: Any,
        camera: str,
        priority: int | None = None,
        timeout_s: float = 60.0,
        **kwargs: Any,
    ) -> concurrent.futures.Future:
        """Enqueue a vision call. Returns a Future.

        `timeout_s` is how long the CALLER is willing to wait for the
        result. If the queue is so backed up that the job hasn't started
        within `timeout_s`, the future is cancelled with TimeoutError.

        If `priority` is None, it is derived from `camera` using
        PHASE6A_ELIGIBLE_CAMERAS.

        Phase.167 §13.5 Commit 12: `camera` may be either a
        CameraSpec.code OR a CameraSpec.name. The eligibility sets are
        code-keyed, so submit() translates via code_for() before the
        membership tests.
        """
        import concurrent.futures

        if priority is None:
            camera_code = code_for(camera)
            if camera_code in GATEKEEPER_CAMERAS:
                priority = PRIORITY_GATEKEEPER
            elif camera_code in PHASE6A_ELIGIBLE_CAMERAS:
                priority = PRIORITY_OUTSIDE
            else:
                priority = PRIORITY_INSIDE

        future: concurrent.futures.Future = concurrent.futures.Future()

        with self._cv:
            # Phase.61: drop low-priority (inside) jobs on overflow rather
            # than letting them block gatekeeper/outside jobs forever.
            # Gatekeeper + outside jobs are NEVER dropped — they always queue.
            if priority > PRIORITY_OUTSIDE and len(self._heap) >= MAX_QUEUE_DEPTH:
                self._dropped += 1
                future.set_exception(QueueOverflowError(
                    f"VisionQueue overflow: heap_size={len(self._heap)} >= "
                    f"MAX_QUEUE_DEPTH={MAX_QUEUE_DEPTH}, camera={camera!r} "
                    f"priority={priority} dropped (inside-tier work does not "
                    f"block gatekeeper/perimeter)"
                ))
                log.warning(
                    f"VisionQueue DROP seq=pending camera={camera!r} "
                    f"priority={priority} heap_size={len(self._heap)} — "
                    f"overflow policy"
                )
                return future

            self._seq += 1
            job = _Job(
                priority=priority,
                seq=self._seq,
                camera=camera,
                fn=fn,
                args=args,
                kwargs=kwargs,
                future=future,
                enqueued_at=time.monotonic(),
                # Optional deadline — waiters will skip past timed-out jobs
            )
            job.deadline = time.monotonic() + timeout_s  # type: ignore[attr-defined]
            heapq.heappush(self._heap, job)
            self._cv.notify()

        log.debug(
            f"VisionQueue enqueued seq={job.seq} camera={camera!r} "
            f"priority={priority} heap_size={len(self._heap)}"
        )
        return future

    def _run(self) -> None:
        """Worker loop: pull next job, run it, mark future done."""
        while True:
            with self._cv:
                while self._heap == [] and not self._stop:
                    self._cv.wait()
                if self._stop and self._heap == []:
                    return
                job = heapq.heappop(self._heap)

            now = time.monotonic()
            deadline = getattr(job, "deadline", now)
            if now > deadline:
                # Caller already gave up
                self._dropped += 1
                if not job.future.done():
                    job.future.set_exception(TimeoutError(
                        f"VisionQueue job for {job.camera!r} timed out "
                        f"in queue after {now - job.enqueued_at:.1f}s"
                    ))
                log.warning(
                    f"VisionQueue dropped seq={job.seq} camera={job.camera!r} "
                    f"— caller timed out after {now - job.enqueued_at:.1f}s"
                )
                continue

            wait_ms = (now - job.enqueued_at) * 1000.0
            with self._lock:
                self._max_wait_ms = max(self._max_wait_ms, wait_ms)

            log.info(
                f"VisionQueue processing seq={job.seq} camera={job.camera!r} "
                f"priority={job.priority} (waited {wait_ms:.0f}ms, "
                f"queue_size={len(self._heap)})"
            )
            try:
                result = job.fn(*job.args, **job.kwargs)
                if not job.future.done():
                    job.future.set_result(result)
            except Exception as exc:
                log.exception(
                    f"VisionQueue job seq={job.seq} camera={job.camera!r} raised"
                )
                if not job.future.done():
                    job.future.set_exception(exc)
            finally:
                with self._lock:
                    self._processed += 1

    def stats(self) -> dict:
        with self._lock:
            return {
                "queue_depth": len(self._heap),
                "processed": self._processed,
                "dropped": self._dropped,
                "max_wait_ms": round(self._max_wait_ms, 1),
            }


# Module-level singleton — the listener process has exactly one vision pipeline.
_queue: VisionQueue | None = None
_queue_lock = threading.Lock()


def get_queue() -> VisionQueue:
    """Return the module-level VisionQueue, creating + starting it on first call."""
    global _queue
    if _queue is None:
        with _queue_lock:
            if _queue is None:
                _queue = VisionQueue()
                _queue.start()
    return _queue
