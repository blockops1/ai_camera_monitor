"""
test_rtsp_watchdog.py — US-017d + US-017e.

US-017d verifies the proactive scheduled_reconnect_watchdog ported from
v1-refactor (PLAN §11.13): every `scheduled_reconnect_seconds` the
reader closes its av container and spawns a fresh decode thread.

US-017e verifies the max_reconnect_attempts cap-and-defer ported from
v1-refactor (PLAN §11.78): after N consecutive decode failures the
in-loop reconnect retries stop and the loop defers to the watchdog.

AC4 (PRD US-017d card_body_fallback): "A unit test stubs
PersistentRTSPReader with mocked av.container, calls start(), waits
until scheduled_reconnect_seconds elapses (mock the sleep), and
asserts container.close() was called and a fresh decode thread was
spawned (frames_decoded_total reset OR thread id changed)."

AC3 (PRD US-017e card_body_fallback): "A unit test stubs a
PersistentRTSPReader whose _decode_iteration always raises
av.error.EOFError, calls start(), and asserts that after N+1 fires
the failure loop logs 'consecutive_reconnect_cap_reached' and enters
the defer sleep (no further _decode_iteration calls until watchdog
fires)."
"""

from __future__ import annotations

import logging
import threading
import time as _time
from unittest.mock import MagicMock, patch

import pytest

from infra.frame_capture import PersistentRTSPReader


class TestScheduledReconnectWatchdog:
    """US-017d AC4: watchdog closes container + spawns fresh decode thread."""

    def test_watchdog_closes_container_and_respawns_decode_thread(
        self, monkeypatch
    ):
        """After start(), advancing past scheduled_reconnect_seconds causes
        the watchdog to:
          1. close() the av container it held, and
          2. spawn a fresh decode thread (Thread identity changes).
        """
        # Short cadence so we can fire the watchdog without waiting an hour.
        cadence = 0.05

        # Track every Thread.start() call. The test asserts that the
        # watchdog fired (>=2 decode-thread starts: the initial one from
        # .start() + at least one fresh spawn from _scheduled_reconnect_fire)
        # and that consecutive decode starts are distinct Thread objects
        # (proving a NEW decode thread was spawned, not the same one
        # re-started).
        starts: list[threading.Thread] = []
        real_start = threading.Thread.start

        def track_start(self):
            starts.append(self)
            return real_start(self)

        # Controlled demux: yields one packet, then blocks until released
        # so the decode thread is parked inside demux() (mirrors the live
        # "stuck in C" scenario the watchdog handles — except here the
        # demux generator cooperatively checks the stop_event on each
        # yield so the join in _scheduled_reconnect_fire returns promptly).
        packet = MagicMock()
        packet.dts = 0
        frame = MagicMock()
        frame.dts = 0
        frame.to_image.return_value = MagicMock()  # PIL substitute
        packet.decode.return_value = [frame]

        # Yield once then loop empty so demux is "open but quiet" — the
        # decode thread sits inside the for-loop waiting for the next
        # packet, which is exactly the zombie surface the watchdog fires on.
        def controlled_demux(stream_arg):
            yield packet
            while True:
                if container._stop_event_for_test.is_set():
                    return
                # Yield an empty packet to keep the loop alive without
                # consuming more mock frames.
                yield MagicMock(dts=None, decode=MagicMock(return_value=[]))

        container = MagicMock()
        container.streams.video = [MagicMock()]
        # Expose the stop_event so the demux generator can check it
        # (set later in the test body, before threads run).
        container._stop_event_for_test = threading.Event()
        container.demux = controlled_demux

        with (
            patch("av.open", return_value=container),
            patch.object(threading.Thread, "start", track_start),
        ):
            reader = PersistentRTSPReader(
                "rtsp://u:p@h:554/h",
                scheduled_reconnect_seconds=cadence,
            )
            reader.start()

            # Wire the demux generator's stop-check ref to the reader's
            # _stop_event. Must happen after start() (which clears it)
            # but before the demux loops observe it.
            container._stop_event_for_test = reader._stop_event

            # Wait long enough for: decode thread to enter demux, watchdog
            # to wait `cadence`, fire _scheduled_reconnect_fire, and the
            # fresh decode thread to be spawned. 1s is generous.
            deadline = threading.Event()
            deadline.wait(timeout=1.0)
            # Sleep gives the threads time to actually run.
            import time as _time

            _time.sleep(0.5)

            # 1. container.close() was called by the watchdog fire
            assert container.close.called, (
                "watchdog did not call container.close() after cadence"
            )

            # 2. A fresh decode thread was spawned — at least 2 decode
            # thread starts (the initial one from .start() + at least one
            # fresh spawn from the watchdog fire). All decode-thread starts
            # must be distinct objects (proving new threads, not the same
            # thread re-started).
            decode_starts = [t for t in starts if t.name.startswith("RTSP[")]
            assert len(decode_starts) >= 2, (
                f"Expected >=2 decode-thread starts (initial + >=1 fresh "
                f"from watchdog), got {len(decode_starts)}: "
                f"{[t.name for t in decode_starts]}"
            )
            # Every consecutive pair must be a distinct Thread object.
            for i in range(1, len(decode_starts)):
                assert decode_starts[i] is not decode_starts[i - 1], (
                    "Fresh decode thread must be a different Thread object"
                )
            assert reader.reconnects_total >= 1, (
                f"reconnects_total should advance after watchdog fire, "
                f"got {reader.reconnects_total}"
            )

            reader.stop(timeout=2.0)


class TestMaxReconnectAttemptsCap:
    """US-017e AC3: max-attempts cap logs + defers to watchdog."""

    def test_cap_reached_logged_and_decode_iteration_halted(
        self, monkeypatch, caplog
    ):
        """After N consecutive decode failures, the loop:
        1. logs 'consecutive_reconnect_cap_reached',
        2. enters the defer sleep (no further _decode_iteration calls
           until the watchdog closes _container from its own thread).

        Strategy:
          - max_reconnect_attempts=3 (small for fast test)
          - _decode_iteration always raises av.error.EOFError
          - Patch _sleep_until_stop_or_watchdog to: (a) record that the
            defer sleep was entered, (b) close the watch-dog-visible
            container after a short delay so the demux-side of the loop
            would surface an exception if it were still running, then
            (c) set the stop_event so the defer sleep returns and the
            test ends. (v1 calls _sleep_until_stop_or_watchdog with a
            real timed sleep; the watchdog closes _container from its
            own thread in production. Here we shortcut that for speed.)
          - Patch time.sleep to a no-op so backoff doesn't slow the
            test (the cap fires before backoff matters).
          - Count _decode_iteration invocations: should be exactly
            (N + 1) — once per warning, once on the N+1th that tips
            the cap — and then stop.
        """
        from infra import frame_capture as fc

        # Track every _decode_iteration call.
        decode_calls = {"n": 0}
        sleep_until_calls = {"n": 0}

        def always_fail_decode() -> None:
            decode_calls["n"] += 1
            # conftest.py stubs the `av` module with a MagicMock, so we
            # can't import av.error.EOFError here — the production
            # exception is irrelevant to this test, only the cap
            # behavior matters. RuntimeError is caught by the broad
            # `except Exception` branch in _run_loop just like EOFError
            # would be.
            raise RuntimeError("simulated RTSP decode failure")

        # Replace the defer sleep with a recordable shim. Returns
        # promptly after marking the call so the test can observe the
        # "no further _decode_iteration" invariant while we still hold
        # the cap_exhausted pause.
        def fake_sleep_until_stop_or_watchdog(stop_event, scheduled_reconnect_seconds):
            sleep_until_calls["n"] += 1
            # Mark the cap_exhausted window "entered" by snapshotting
            # the current decode-call count for the assertion below.
            sleep_until_calls["decode_calls_at_entry"] = decode_calls["n"]
            # Yield briefly so a watchdog-thread tick could have
            # arrived. The test asserts no further decode happens
            # during this window.
            stop_event.wait(timeout=0.05)
            # Test ends after the defer sleep returns; stop() in the
            # test body sets the event.

        monkeypatch.setattr(
            fc, "_sleep_until_stop_or_watchdog", fake_sleep_until_stop_or_watchdog
        )
        # Backoff sleeps in the warning branch — make them no-ops.
        monkeypatch.setattr(fc.time, "sleep", lambda _s: None)

        # Watchdog cadence: irrelevant to the cap-and-defer path because
        # we shim the sleep helper. Set short for clarity.
        watchdog_cadence = 0.1

        reader = PersistentRTSPReader(
            "rtsp://u:p@h:554/h",
            scheduled_reconnect_seconds=watchdog_cadence,
            max_reconnect_attempts=3,
        )
        # Stub the per-iteration decode to always raise. Use setattr
        # so the bound method is replaced on the instance.
        reader._decode_iteration = always_fail_decode  # type: ignore[method-assign]

        with caplog.at_level(logging.ERROR, logger="frame_capture"):
            reader.start()
            # Wait for the cap to fire (decode thread runs
            # ~instantly because sleeps are no-ops) + the fake defer
            # sleep to return.
            deadline = _time.monotonic() + 2.0
            while _time.monotonic() < deadline and sleep_until_calls["n"] < 1:
                _time.sleep(0.01)
            # Give the decode thread an extra beat to settle.
            _time.sleep(0.1)
            reader.stop(timeout=2.0)

        # AC3.1 — cap_reached log line emitted.
        cap_logs = [
            r for r in caplog.records
            if "consecutive_reconnect_cap_reached" in r.getMessage()
        ]
        assert len(cap_logs) == 1, (
            f"Expected exactly one consecutive_reconnect_cap_reached log, "
            f"got {len(cap_logs)}: {[r.getMessage() for r in cap_logs]}"
        )
        # The log line should identify the defer target (watchdog).
        assert "scheduled_reconnect_watchdog" in cap_logs[0].getMessage()

        # AC3.2 — the defer sleep was entered exactly once.
        assert sleep_until_calls["n"] == 1, (
            f"Expected exactly one _sleep_until_stop_or_watchdog call, "
            f"got {sleep_until_calls['n']}"
        )

        # AC3.3 — _decode_iteration was called N times (the cap fires
        # when _consecutive_errors == N, i.e. the Nth failure tips the
        # cap). Crucially NO FURTHER calls happened during the defer
        # sleep window. Snapshot the count when defer sleep was
        # entered and assert no new calls were made after — that's
        # the "no further _decode_iteration calls until watchdog
        # fires" invariant from AC3.
        assert decode_calls["n"] == 3, (
            f"Expected 3 _decode_iteration calls (cap tipped on 3rd "
            f"failure when max_reconnect_attempts=3), got "
            f"{decode_calls['n']}"
        )
        calls_at_entry = sleep_until_calls["decode_calls_at_entry"]
        assert decode_calls["n"] == calls_at_entry, (
            f"_decode_iteration kept firing after the cap fired: "
            f"{calls_at_entry} at defer-entry vs {decode_calls['n']} now"
        )

        # AC3.4 — _consecutive_errors counter advanced exactly N
        # (matches decode-call count).
        assert reader._consecutive_errors == 3, (
            f"_consecutive_errors should be 3 (cap tipped on 3rd "
            f"failure), got {reader._consecutive_errors}"
        )


import infra.frame_capture as _frame_capture_module


class TestWatchdogAbortsOnZombieDecodeThread:
    """US-017d follow-up: when the decode thread is stuck in
    container.demux() C-land and the join times out, the watchdog MUST
    NOT close _container or start a new thread. Doing so tears down
    the AVFormatContext while the orphaned thread is still blocked in
    libavformat, causing SIGSEGV when the orphan finally returns
    (offset 0x20 deref, observed in *.ips crash dumps).

    Recovery path: abort the reconnect, leave container + thread
    untouched, and let launchd restart the whole process.
    """

    def test_watchdog_aborts_when_decode_thread_is_stuck(self, monkeypatch):
        """When the decode thread is alive after join() times out, the
        watchdog must:
          1. NOT call _container.close()
          2. NOT start a fresh decode thread
          3. Clear _watchdog_thread so it doesn't fire again
        """
        # Short cadence so we can drive the watchdog.
        cadence = 0.05
        reader = PersistentRTSPReader(
            "rtsp://test@example.com/stream",
            scheduled_reconnect_seconds=cadence,
        )

        # Sentinel container — close() must NOT be called on this.
        sentinel_container = MagicMock(name="zombie_container")

        # A Thread that pretends to be alive forever (join() always
        # times out). This simulates a decode thread stuck in
        # libavformat C-land.
        stuck_thread = MagicMock(spec=threading.Thread)
        stuck_thread.is_alive.return_value = True  # always alive
        # join() is a no-op — we're already past timeout.

        # Wire up the reader's state to look like a stuck decode
        # iteration.
        reader._container = sentinel_container
        reader._thread = stuck_thread
        reader._watchdog_thread = MagicMock(spec=threading.Thread)

        # Patch out _sleep_until_stop_or_watchdog so the watchdog
        # thread returns quickly (we just want to invoke the fire).
        with patch.object(_frame_capture_module,
                          "_sleep_until_stop_or_watchdog",
                          return_value=None):
            # Patch Thread constructor so the watchdog itself doesn't
            # try to spawn a real daemon thread.
            original_thread = threading.Thread
            with patch(
                "infra.frame_capture.threading.Thread",
                side_effect=lambda *a, **kw: original_thread(
                    target=lambda: None, daemon=True,
                ),
            ):
                reader._scheduled_reconnect_fire()

        # 1. container.close() must NOT have been called.
        assert not sentinel_container.close.called, (
            "watchdog called _container.close() while decode thread "
            "was still alive — this is the SIGSEGV trigger."
        )

        # 2. _thread must NOT have been replaced with a new live thread.
        # _thread is still the stuck_thread reference (not overwritten).
        assert reader._thread is stuck_thread, (
            "watchdog replaced stuck decode thread with a fresh one — "
            "two threads now racing on the same AVFormatContext."
        )

        # 3. _watchdog_thread must be cleared so the loop doesn't
        # fire again on this doomed container.
        assert reader._watchdog_thread is None, (
            "watchdog did not clear _watchdog_thread after abort — "
            "it would fire again and trigger another SIGSEGV."
        )

    def test_watchdog_aborts_logs_warning(self, monkeypatch, caplog):
        """The abort path must log a warning explaining why we didn't
        proceed — so operators see the recovery reasoning in the logs.
        """
        cadence = 0.05
        reader = PersistentRTSPReader(
            "rtsp://test@example.com/stream",
            scheduled_reconnect_seconds=cadence,
        )
        sentinel_container = MagicMock(name="zombie_container")
        stuck_thread = MagicMock(spec=threading.Thread)
        stuck_thread.is_alive.return_value = True
        reader._container = sentinel_container
        reader._thread = stuck_thread
        reader._watchdog_thread = MagicMock(spec=threading.Thread)

        with caplog.at_level(logging.WARNING, logger="infra.frame_capture"):
            with patch.object(_frame_capture_module,
                              "_sleep_until_stop_or_watchdog",
                              return_value=None):
                original_thread = threading.Thread
                with patch(
                    "infra.frame_capture.threading.Thread",
                    side_effect=lambda *a, **kw: original_thread(
                        target=lambda: None, daemon=True,
                    ),
                ):
                    reader._scheduled_reconnect_fire()

        warning_msgs = [r.message for r in caplog.records
                        if r.levelno == logging.WARNING]
        assert any("Aborting reconnect" in m for m in warning_msgs), (
            f"Expected abort warning, got: {warning_msgs}"
        )

    def test_watchdog_normal_path_still_works(self, monkeypatch):
        """Regression guard: when the decode thread exits cleanly
        within the join timeout, the watchdog must STILL close the
        container and respawn a fresh thread (happy path preserved).
        """
        cadence = 0.05
        reader = PersistentRTSPReader(
            "rtsp://test@example.com/stream",
            scheduled_reconnect_seconds=cadence,
        )
        healthy_container = MagicMock(name="healthy_container")
        healthy_container.close.return_value = None
        exited_thread = MagicMock(spec=threading.Thread)
        exited_thread.is_alive.return_value = False  # already exited
        reader._container = healthy_container
        reader._thread = exited_thread

        original_thread = threading.Thread
        with patch(
            "infra.frame_capture.threading.Thread",
            side_effect=lambda *a, **kw: original_thread(
                target=lambda: None, daemon=True,
            ),
        ):
            reader._scheduled_reconnect_fire()

        assert healthy_container.close.called, (
            "happy path: container.close() should have been called "
            "when decode thread exited cleanly."
        )
        assert reader._thread is not exited_thread, (
            "happy path: fresh decode thread should have been spawned."
        )
