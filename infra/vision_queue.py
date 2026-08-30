"""
vision_queue.py — Single-flight priority queue for vision LLM calls.

STATUS: stable
THREAD SAFETY: thread-safe (heap guarded by threading.Lock; multiple
    callers can enqueue concurrently)

INPUTS:
    - function arg payload: dict (required) — OpenAI-compatible chat
      completion payload
    - function arg priority: int (required) — lower = higher priority
      (PRIORITY_GATEKEEPER=0, PRIORITY_OUTSIDE=1, PRIORITY_INSIDE=5)
    - function arg camera_name: str (required)
    - function arg callback: callable (required) — invoked with the
      response dict on success

OUTPUTS:
    - return value: enqueue() returns None; VisionQueue.enqueue is fire-
      and-forget (result delivered via the callback)
    - side effect: callback is invoked on the worker thread with the
      Qwen response dict (or error info on failure)
    - log line per enqueue + completion (debug level)

PUBLIC API:
    QueueOverflowError — raised when MAX_QUEUE_DEPTH exceeded for an
      INSIDE-priority job (inside cameras fail fast instead of blocking)
    VisionQueue(worker_count=1, max_depth=MAX_QUEUE_DEPTH) -> VisionQueue
        .enqueue(payload, priority, camera_name, callback) -> None
        .start() -> None — start the worker thread
        .stop() -> None — drain + stop (idempotent)
        .depth() -> int — current heap size
        .stats() -> dict — counters (enqueued, completed, overflowed)
    get_queue() -> VisionQueue
        Process-wide singleton (None until set).
    PRIORITY_GATEKEEPER / PRIORITY_OUTSIDE / PRIORITY_INSIDE — int constants

DOES NOT DO:
    - Call Qwen — that's infra.vision_client's job (callback invokes it)
    - Persist queue state across restarts — process-local only
    - Retry on Qwen errors — caller handles via callback

WHY HERE:
    Qwen3-VL runs with --parallel 1 (full 8192 ctx per request, no
    contention). Multiple camera webhooks fire concurrently, so we
    need a queue to serialize vision calls AND prioritize them.

    Burned 2026-07-20: with --parallel 4 llama-server crashed under
    4K image bursts (each parallel slot only got 2048 ctx). Dropping
    to --parallel 1 means we MUST serialize.

    Burned 2026-08-07: 41 person alerts queued behind 1 gatekeeper
    vehicle alert — every gatekeeper call timed out. Queue overflow
    policy added so inside cameras fail fast instead of starving
    the perimeter.

    Priority rules (lower = higher priority):
        PRIORITY_GATEKEEPER = 0  Outside Front Solar ONLY
                                — gatekeeper must clear first so a
                                vehicle arrival isn't blocked
        PRIORITY_OUTSIDE    = 1  4 outside perimeter cameras
        PRIORITY_INSIDE     = 5  Back Door Inside (descriptive only)

    Overflow policy (Phase.61): if heap size >= MAX_QUEUE_DEPTH
    when an INSIDE job arrives, the job is dropped with
    QueueOverflowError. Gatekeeper + outside jobs NEVER drop.

CALLED BY:
    - infra.vision_analyzer: enqueue() — the only producer
    - listener.listener.bootstrap: get_queue() / start()

CALLS INTO:
    - heapq: priority queue implementation
    - threading.Thread + Event: worker loop + drain
    - infra.vision_client (via the callback) — actual HTTP transport

RELATED:
    - infra.vision_client — sits BELOW this queue (queue serializes,
      client does the HTTP)
    - infra.pipeline_integration — defines PHASE6A_ELIGIBLE_CAMERAS,
      which decides per-camera priority
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
PRIORITY_GATEKEEPER = 0  # Outside Front Solar ONLY — see GATEKEEPER_CAMERAS
PRIORITY_OUTSIDE = 1     # Outside Front Garage, Outside Front Power, Outside Back Solar, Front Door Outside
PRIORITY_INSIDE = 5      # Back Door Inside
# Phase.61 — MAX_QUEUE_DEPTH for overflow drop policy. When the heap
# already has this many jobs and a low-priority (inside) job arrives,
# drop it immediately rather than letting it block forever.
MAX_QUEUE_DEPTH = 10

# Cameras that get PRIORITY_GATEKEEPER (jumps the queue above all other
# outside cameras). Currently OFS only — the gatekeeper for vehicle
# arrival alerts. Adding more cameras here means those vehicle alerts
# will be processed before any other outside camera's alerts.
GATEKEEPER_CAMERAS: frozenset[str] = frozenset({
    "Outside Front Solar",
})
# Phase.24 Fix C — heartbeat was originally planned as a separate
# priority tier, but the heartbeat loop was reworked to read cached
# vision results instead of calling the vision queue directly
# (heartbeat.py uses get_last_vision_result, not analyze_frames).
# Removed the unused constant.

# Cameras eligible for Phase 6A face recognition. ONLY these get
# PRIORITY_OUTSIDE; everything else is PRIORITY_INSIDE.
#
# Source of truth matches the cameras the listener loads from
# camera-creds.env that are outdoor / outside-facing. Inside
# cameras (Back Door Inside today) are deprioritized.
#
# 2026-07-24 (Phase C of RLC-510A swap): the two retired RLC-833As
# (Building Front Corner, Building Back Solar) are dropped. The four
# new RLC-510A outdoor cameras are added:
#   - Outside Front Garage (192.168.1.73)
#   - Outside Front Power  (192.168.1.108)
#   - Outside Front Solar  (192.168.1.103) — also the new gatekeeper
#   - Outside Back Solar   (192.168.1.113)
# Existing Front Door Outside stays. Back Door Inside is inside and
# stays deprioritized (not in this set).
# 2026-07-29 (cleanup): historical note above confirmed accurate.
# Building Front Corner and Building Back Solar are physically gone
# (see docs/CLEANUP-2026-07-29-RETIRED-CAMERAS.md); this set reflects
# the post-retirement state. Outside Front Solar now acts as both a
# perimeter camera AND the gatekeeper.
PHASE6A_ELIGIBLE_CAMERAS: frozenset[str] = frozenset({
    "Outside Front Garage",
    "Outside Front Power",
    "Outside Front Solar",
    "Outside Back Solar",
    "Front Door Outside",
})


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
        """
        import concurrent.futures

        if priority is None:
            if camera in GATEKEEPER_CAMERAS:
                priority = PRIORITY_GATEKEEPER
            elif camera in PHASE6A_ELIGIBLE_CAMERAS:
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
