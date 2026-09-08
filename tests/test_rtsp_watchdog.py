"""
test_rtsp_watchdog.py — US-017d.

Verifies the proactive scheduled_reconnect_watchdog ported from
v1-refactor (PLAN §11.13): every `scheduled_reconnect_seconds` the
reader closes its av container and spawns a fresh decode thread.

AC4 (PRD US-017d card_body_fallback): "A unit test stubs
PersistentRTSPReader with mocked av.container, calls start(), waits
until scheduled_reconnect_seconds elapses (mock the sleep), and
asserts container.close() was called and a fresh decode thread was
spawned (frames_decoded_total reset OR thread id changed)."
"""

from __future__ import annotations

import threading
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
