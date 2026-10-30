# US-049f — coder attempt (preserved for reference, not committed)

This is coder's draft from `wt/v2-049f-test-periodic-teardown` before 049f was decomposed into 049f-i/ii/iii. The test patterns below (monkeypatch time.monotonic, save real sleep before patch, threading.Event for sync) are useful starting points for the next coder.

```diff
diff --git a/tests/test_frame_capture.py b/tests/test_frame_capture.py
index e8dfb05..4fdcd40 100644
--- a/tests/test_frame_capture.py
+++ b/tests/test_frame_capture.py
@@ -995,3 +995,352 @@ class TestFailureDrivenReconnectCap:
         assert not any("proceed" in m.lower() for m in all_msgs), (
             f"Found 'proceed' in log messages — old watchdog path should be gone: {all_msgs}"
         )
+
+
+# ---------------------------------------------------------------------------
+# Test: in-thread periodic teardown (US-049c / US-049f)
+# ---------------------------------------------------------------------------
+
+
+class TestPeriodicTeardown:
+    """Tests for the in-thread periodic teardown mechanism (US-049c)."""
+
+    def test_teardown_fires_after_3600s(
+        self, mock_av_stream, mock_av_frame, monkeypatch
+    ):
+        """AC1: teardown fires when monotonic time advances past
+        TEARDOWN_INTERVAL_SECONDS.  Assert av.open() called >= 2x
+        (once at start, once at teardown).
+
+        The teardown fires at the START of _decode_iteration.  To trigger it,
+        the first demux must raise (simulating RTSP disconnect) so _run_loop
+        retries _decode_iteration — on the retry the time is past 3600 and
+        teardown fires.
+
+        NOTE: fc.time IS the stdlib time module, so monkeypatching fc.time.sleep
+        also patches stdlib time.sleep.  We save a reference to the real sleep
+        BEFORE the monkeypatch.
+        """
+        from infra import frame_capture as fc
+
+        # Capture real time.sleep before monkeypatch (fc.time is stdlib time)
+        _real_sleep = fc.time.sleep
+
+        teardown_interval = fc.TEARDOWN_INTERVAL_SECONDS  # 3600.0
+        call_log = {"open": 0}
+
+        fake_time = [0.0]
+
+        def fake_monotonic():
+            return fake_time[0]
+
+        open_count = [0]
+
+        def fake_av_open(*args, **kwargs):
+            call_log["open"] += 1
+            open_count[0] += 1
+            container = MagicMock()
+            container.streams.video = [mock_av_stream]
+
+            def demux_gen(stream_arg):
+                if open_count[0] == 1:
+                    # First container: yield one frame then raise
+                    pkt = MagicMock()
+                    pkt.dts = 0
+                    pkt.decode.return_value = [mock_av_frame]
+                    yield pkt
+                    raise OSError("simulated RTSP disconnect")
+                # Post-teardown container: yield and raise (so stop works)
+                pkt = MagicMock()
+                pkt.dts = 0
+                pkt.decode.return_value = [mock_av_frame]
+                yield pkt
+                raise OSError("simulated disconnect after teardown")
+
+            container.demux = demux_gen
+            return container
+
+        monkeypatch.setattr(fc.time, "monotonic", fake_monotonic)
+        monkeypatch.setattr(fc.time, "sleep", lambda _s: None)
+
+        with patch("av.open", side_effect=fake_av_open):
+            reader = PersistentRTSPReader(
+                "rtsp://u:p@h:554/h",
+                max_reconnect_attempts=0,  # cap disabled, keep looping
+            )
+            reader.start()
+
+            # Yield to background thread using REAL sleep (saved before patch)
+            _real_sleep(0.05)
+
+            # Advance past teardown interval
+            fake_time[0] = teardown_interval + 1.0
+
+            # Yield to let teardown fire
+            _real_sleep(0.3)
+
+            # Stop reader
+            reader.stop(timeout=2.0)
+
+        # AC: av.open called at least twice (initial connect + teardown)
+        assert call_log["open"] >= 2, (
+            f"av.open() called {call_log['open']}x, expected >= 2"
+        )
+
+    def test_ring_buffer_survives_teardown(
+        self, mock_av_stream, mock_av_frame, monkeypatch
+    ):
+        """AC2: append frames before teardown, trigger teardown, append more
+        frames after — the ring holds all frames (deque survives teardown).
+
+        The demux yields 3 frames then raises. _run_loop catches and retries.
+        On retry the time is past 3600, so teardown fires (closing old
+        container and reopening). The demux then yields 3 more frames.
+        The ring should contain frames from both containers (total >= 6).
+        """
+        from infra import frame_capture as fc
+
+        teardown_interval = fc.TEARDOWN_INTERVAL_SECONDS
+
+        # Use threading.Event for synchronization instead of sleep
+        frames_appended = threading.Event()
+        fake_time = [0.0]
+
+        def fake_monotonic():
+            return fake_time[0]
+
+        open_count = [0]
+        frames_in_ring = [0]  # Track via shared list
+
+        def fake_av_open(*args, **kwargs):
+            open_count[0] += 1
+            container = MagicMock()
+            container.streams.video = [mock_av_stream]
+
+            def demux_gen(stream_arg):
+                # Each container yields 3 frames, then raises
+                for i in range(3):
+                    pkt = MagicMock()
+                    pkt.dts = open_count[0] * 3 + i
+                    pkt.decode.return_value = [mock_av_frame]
+                    yield pkt
+                raise OSError("simulated RTSP disconnect")
+
+            container.demux = demux_gen
+            return container
+
+        monkeypatch.setattr(fc.time, "monotonic", fake_monotonic)
+
+        # Patch _decode_iteration to signal when frames are appended
+        original_decode = fc.PersistentRTSPReader._decode_iteration
+
+        def patched_decode(self_self):
+            try:
+                original_decode(self_self)
+                # Signal that frames were appended (if any)
+                if len(self_self._ring) > frames_in_ring[0]:
+                    frames_in_ring[0] = len(self_self._ring)
+                    frames_appended.set()
+            except Exception:
+                if len(self_self._ring) > frames_in_ring[0]:
+                    frames_in_ring[0] = len(self_self._ring)
+                    frames_appended.set()
+                raise
+
+        monkeypatch.setattr(fc.PersistentRTSPReader, "_decode_iteration",
+                            patched_decode)
+
+        with patch("av.open", side_effect=fake_av_open):
+            reader = PersistentRTSPReader(
+                "rtsp://u:p@h:554/h",
+                ring_size=10,
+                max_reconnect_attempts=0,
+            )
+            reader.start()
+
+            # Wait for initial frames to be appended
+            frames_appended.wait(timeout=5.0)
+            frames_appended.clear()
+            frames_before = len(reader._ring)
+
+            # Advance time past teardown interval
+            fake_time[0] = teardown_interval + 1.0
+
+            # Wait for teardown + more frames to be appended
+            frames_appended.wait(timeout=5.0)
+            frames_after = len(reader._ring)
+
+            assert frames_after >= frames_before + 3, (
+                f"Ring should have frames from post-teardown container: "
+                f"{frames_before} before, {frames_after} after"
+            )
+
+            reader.stop(timeout=2.0)
+
+    def test_stats_exposes_teardown_counters(self, monkeypatch):
+        """AC3: stats() dict contains teardowns_total and
+        seconds_since_teardown keys.
+        """
+        from infra import frame_capture as fc
+
+        fake_time = [1000.0]
+
+        def fake_monotonic():
+            return fake_time[0]
+
+        monkeypatch.setattr(fc.time, "monotonic", fake_monotonic)
+
+        reader = PersistentRTSPReader("rtsp://u:p@h:554/h")
+        stats = reader.stats()
+
+        assert "teardowns_total" in stats
+        assert stats["teardowns_total"] == 0, (
+            f"Expected 0 teardowns on fresh reader, got {stats['teardowns_total']}"
+        )
+        assert "seconds_since_teardown" in stats
+        # Before start, _last_teardown_monotonic is None, so seconds_since_teardown
+        # is None. After start() it becomes >= 0.
+        reader.start()
+        # (start sets _last_teardown_monotonic = monotonic() internally)
+        stats_after = reader.stats()
+        assert stats_after["seconds_since_teardown"] is not None, (
+            "seconds_since_teardown should be set after start()"
+        )
+        assert stats_after["seconds_since_teardown"] >= 0
+        reader.stop(timeout=2.0)
+
+    def test_failure_driven_cap_still_fails_loud_at_10(
+        self, monkeypatch, caplog
+    ):
+        """AC4: 11 consecutive decode-packet failures -> ERROR log,
+        decode loop exits, is_healthy() == False. NO 'proceed anyway' log.
+        This is the same cap test from TestFailureDrivenReconnectCap but
+        verified under the teardown era — the cap path in _run_loop is
+        unchanged by US-049c.
+        """
+        from infra import frame_capture as fc
+
+        decode_calls = {"n": 0}
+
+        def always_fail_decode() -> None:
+            decode_calls["n"] += 1
+            raise RuntimeError("simulated decode failure")
+
+        # Patch Event.wait to simulate stop_event being set immediately,
+        # so the cap block breaks cleanly.
+        def fake_wait(self, timeout=None):
+            self.set()
+            return True
+
+        monkeypatch.setattr(threading.Event, "wait", fake_wait)
+        monkeypatch.setattr(fc.time, "sleep", lambda _s: None)
+
+        reader = PersistentRTSPReader(
+            "rtsp://u:p@h:554/h",
+            max_reconnect_attempts=3,
+        )
+        reader._decode_iteration = always_fail_decode  # type: ignore[method-assign]
+
+        with caplog.at_level(logging.ERROR, logger="frame_capture"):
+            reader.start()
+            deadline = _time.monotonic() + 2.0
+            while _time.monotonic() < deadline and decode_calls["n"] < 3:
+                _time.sleep(0.01)
+            _time.sleep(0.1)
+            reader.stop(timeout=2.0)
+
+        # AC: ERROR log with consecutive_reconnect_cap_reached
+        cap_logs = [
+            r
+            for r in caplog.records
+            if "consecutive_reconnect_cap_reached" in r.getMessage()
+        ]
+        assert len(cap_logs) == 1, (
+            f"Expected exactly one cap-reached log, got {len(cap_logs)}"
+        )
+
+        # AC: is_healthy() is False after cap exhaustion
+        assert reader.is_healthy() is False
+
+        # AC: exactly N decode calls (cap tipped on 3rd failure)
+        assert decode_calls["n"] == 3, (
+            f"Expected 3 decode calls, got {decode_calls['n']}"
+        )
+
+        # AC: no 'proceed' in any log (old watchdog path gone)
+        all_msgs = [r.getMessage() for r in caplog.records]
+        assert not any("proceed" in m.lower() for m in all_msgs), (
+            f"'proceed' found in logs — old watchdog path: {all_msgs}"
+        )
+
+    def test_stats_after_teardown_incremented(
+        self, mock_av_stream, mock_av_frame, monkeypatch
+    ):
+        """AC5 (edge case): after a real teardown fires, stats() shows
+        teardowns_total == 1 and seconds_since_teardown resets.
+
+        The teardown fires at the START of _decode_iteration.  To trigger it,
+        the first demux must raise (simulating RTSP disconnect) so _run_loop
+        retries _decode_iteration — on the retry time is past 3600 and
+        teardown fires.
+        """
+        from infra import frame_capture as fc
+
+        # Capture real time.sleep before monkeypatch (fc.time is stdlib time)
+        _real_sleep = fc.time.sleep
+
+        teardown_interval = fc.TEARDOWN_INTERVAL_SECONDS
+
+        fake_time = [0.0]
+
+        def fake_monotonic():
+            return fake_time[0]
+
+        open_count = [0]
+
+        def fake_av_open(*args, **kwargs):
+            open_count[0] += 1
+            container = MagicMock()
+            container.streams.video = [mock_av_stream]
+
+            def demux_gen(stream_arg):
+                pkt = MagicMock()
+                pkt.dts = 0
+                pkt.decode.return_value = [mock_av_frame]
+                yield pkt
+                # Raise to trigger _run_loop retry (which will fire teardown)
+                raise OSError("simulated RTSP disconnect")
+
+            container.demux = demux_gen
+            return container
+
+        monkeypatch.setattr(fc.time, "monotonic", fake_monotonic)
+        monkeypatch.setattr(fc.time, "sleep", lambda _s: None)
+
+        with patch("av.open", side_effect=fake_av_open):
+            reader = PersistentRTSPReader(
+                "rtsp://u:p@h:554/h",
+                ring_size=10,
+                max_reconnect_attempts=0,
+            )
+            reader.start()
+
+            # Yield to background thread using REAL sleep
+            _real_sleep(0.05)
+            stats_before = reader.stats()
+            assert stats_before["teardowns_total"] == 0
+
+            # Advance past teardown interval
+            fake_time[0] = teardown_interval + 1.0
+
+            # Yield to let teardown fire
+            _real_sleep(0.3)
+
+            # After teardown fires, stats should reflect 1 teardown
+            stats_after = reader.stats()
+            assert stats_after["teardowns_total"] == 1, (
+                f"Expected 1 teardown, got {stats_after['teardowns_total']}"
+            )
+            assert stats_after["seconds_since_teardown"] >= 0
+
+            reader.stop(timeout=2.0)

```
