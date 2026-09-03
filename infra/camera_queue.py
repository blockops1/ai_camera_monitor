"""
camera_queue.py — Per-camera serial LLM queue.

STATUS: stable
THREAD SAFETY: uses threading.Semaphore (one per camera, lazily created)

INPUTS:
    - function arg camera: str (required) — camera name
    - internal: a per-process dict of {camera_name: Semaphore}

OUTPUTS:
    - return value: ContextManager (acquire_for_camera) | list[str] (active)
    - side effect: blocks the calling thread until the camera's
      semaphore is free

PUBLIC API:
    acquire_for_camera(camera: str) -> Iterator[threading.Semaphore]
        ContextManager. Acquires the per-camera semaphore on __enter__,
        releases on __exit__. Blocks if another thread is in a critical
        section for the same camera.
    active_cameras() -> list[str]
        Return cameras that currently have a semaphore allocated
        (had at least one acquire since process start). For /status
        health checks.
    reset() -> None
        Drop every semaphore. TEST-ONLY — never call in production.

DOES NOT DO:
    - HTTP/transport — that lives in infra.vision_client
    - Frame capture — that lives in infra.frame_capture
    - Decision-making about which camera to process — caller does that
    - Cap concurrent cameras globally — only per-camera serialization

WHY HERE:
    The local Qwen3-VL server (llama.cpp) can't handle multiple
    concurrent vision requests without dropping some to HTTP 500. We
    confirmed this in production: 3 simultaneous webhooks for the same
    camera fired 3 parallel LLM calls, all returned 500. Per-camera
    semaphore lets cameras run in parallel while serializing within a
    camera.

CALLED BY:
    - listener.listener: acquire_for_camera(name) before analyze_frames()

CALLS INTO:
    - threading.Semaphore, threading.Lock
    - contextlib.contextmanager (for the with-block semantics)

RELATED:
    - infra.vision_client — does the actual LLM call; queue just gates it
    - infra.camera_queue.acquire_for_camera — context manager that
      callers wrap their vision call in
"""
from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager

# Per-camera semaphores. Created lazily on first use.
_semaphores: dict[str, threading.Semaphore] = {}
_semaphores_lock = threading.Lock()


def _get_semaphore(camera: str) -> threading.Semaphore:
    """Return the semaphore for `camera`, creating it on first call.

    Thread-safe — multiple webhook handlers may race on first request.
    """
    if camera not in _semaphores:
        with _semaphores_lock:
            # Double-check inside the lock to avoid duplicate creation.
            if camera not in _semaphores:
                _semaphores[camera] = threading.Semaphore(1)
    return _semaphores[camera]


@contextmanager
def acquire_for_camera(camera: str) -> Iterator[threading.Semaphore]:
    """Block until the per-camera LLM slot is free, then yield the semaphore.

    Always release the slot on exit (success or exception).
    """
    sem = _get_semaphore(camera)
    sem.acquire()
    try:
        yield sem
    finally:
        sem.release()


def active_cameras() -> list[str]:
    """Diagnostic: list of cameras with semaphores (used at least once)."""
    return sorted(_semaphores.keys())


def reset() -> None:
    """Test helper: clear all semaphores. Production code should never call this."""
    with _semaphores_lock:
        _semaphores.clear()
