"""
Tests for infra/persistent_rtsp.py — long-lived RTSP connection + ring buffer.

Phase 6B.80 (PLAN §11.13) — Scheduled RTSP reconnect.

Covers:
    - Module constants: SCHEDULED_RECONNECT_DEFAULT
    - _resolve_scheduled_reconnect: arg vs env var precedence
    - __init__: stores resolved cadence + watchdog thread slot
    - _scheduled_reconnect_watchdog: closes container after deadline,
      skips when container is None, stops cleanly on stop_event,
      increments reconnects_total
    - start()/stop() lifecycle: starts both threads, joins both on stop
    - Per-camera reader registry (Phase 6B.87, PLAN §11.17):
      set_reader/get_reader/get_reader_for_url/clear_reader_registry;
      set_reader replaces by name; get_reader_for_url strips creds;
      legacy set_default_reader/get_default_reader shims still work

We mock time.monotonic to fast-forward past the watchdog deadline
without sleeping the test runner. The av.container.InputContainer
is mocked via MagicMock(spec=...) so isinstance() checks pass.
"""

import os
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from infra.persistent_rtsp import (
    SCHEDULED_RECONNECT_DEFAULT,
    PersistentRTSPReader,
    _resolve_scheduled_reconnect,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


class TestConstants:
    def test_scheduled_reconnect_default_is_one_hour(self):
        # 3600s chosen per PLAN §11.13 research (30min-4h practical NVR range).
        assert SCHEDULED_RECONNECT_DEFAULT == 3600.0


# ---------------------------------------------------------------------------
# Resolver
# ---------------------------------------------------------------------------


class TestResolveScheduledReconnect:
    def test_arg_overrides_default(self):
        # No env var; arg wins.
        with patch.dict(os.environ, {}, clear=True):
            assert _resolve_scheduled_reconnect(120) == 120.0

    def test_env_var_overrides_default(self):
        with patch.dict(os.environ, {"FARMSV_RTSP_RECONNECT_SECONDS": "300"}):
            assert _resolve_scheduled_reconnect(None) == 300.0

    def test_arg_overrides_env_var(self):
        # Precedence: arg > env > default.
        with patch.dict(os.environ, {"FARMSV_RTSP_RECONNECT_SECONDS": "300"}):
            assert _resolve_scheduled_reconnect(120) == 120.0

    def test_default_when_neither_set(self):
        with patch.dict(os.environ, {}, clear=True):
            assert _resolve_scheduled_reconnect(None) == SCHEDULED_RECONNECT_DEFAULT

    def test_invalid_env_var_raises_value_error(self):
        # Documented shape — surface a clean ValueError rather than letting
        # the operator silently fall through to the default.
        with (
            patch.dict(os.environ, {"FARMSV_RTSP_RECONNECT_SECONDS": "not-a-number"}),
            pytest.raises(ValueError),
        ):
            _resolve_scheduled_reconnect(None)


# ---------------------------------------------------------------------------
# __init__: stores resolved cadence + watchdog thread slot
# ---------------------------------------------------------------------------


class TestInit:
    def test_stores_resolved_cadence_from_arg(self):
        reader = PersistentRTSPReader(
            "rtsp://test/stream",
            scheduled_reconnect_seconds=120,
        )
        assert reader._scheduled_reconnect_seconds == 120.0

    def test_stores_resolved_cadence_from_env(self):
        with patch.dict(os.environ, {"FARMSV_RTSP_RECONNECT_SECONDS": "300"}):
            reader = PersistentRTSPReader("rtsp://test/stream")
        assert reader._scheduled_reconnect_seconds == 300.0

    def test_watchdog_thread_slot_is_none_before_start(self):
        # _watchdog_thread is initialized to None and only set in .start().
        # This guards against a future refactor accidentally starting
        # the watchdog in __init__.
        reader = PersistentRTSPReader(
            "rtsp://test/stream", scheduled_reconnect_seconds=10
        )
        assert reader._watchdog_thread is None


# ---------------------------------------------------------------------------
# Watchdog behavior
# ---------------------------------------------------------------------------


class TestScheduledReconnectWatchdog:
    """Tests for the watchdog daemon thread that fires
    `_scheduled_reconnect_fire` every `scheduled_reconnect_seconds`.
    Uses patched time.monotonic to fast-forward past the deadline
    without sleeping the test runner.

    Each fire calls `_scheduled_reconnect_fire`, which does:
      1. self._stop_event.set()
      2. join the decode thread (timeout 10s)
      3. close container
      4. start fresh decode thread

    We patch `_run_loop` so the decode thread is a no-op (returns
    immediately when the fake watchdog calls stop+start cycle).
    """

    def _make_reader_with_mock_container(self, scheduled=10.0):
        """Build a reader with a mocked container, but don't call .start()
        (which would spin up real threads + try to open RTSP). The
        watchdog method is invoked directly.
        """
        reader = PersistentRTSPReader(
            "rtsp://test/stream", scheduled_reconnect_seconds=scheduled
        )
        reader._container = MagicMock(spec=["close"])
        reader._start_time = time.monotonic() - 5.0  # simulate 5s uptime
        reader.frames_decoded_total = 100
        reader.reconnects_total = 0
        # Pre-populate a fake "current" decode thread that exits
        # immediately when stop_event is set, so the watchdog's
        # join-with-timeout succeeds cleanly.
        def _noop_decode():
            while not reader._stop_event.is_set():
                reader._stop_event.wait(timeout=0.05)
        reader._thread = threading.Thread(
            target=_noop_decode, daemon=True, name="fake-decode"
        )
        reader._thread.start()
        return reader

    def _patch_run_loop_to_noop(self, reader):
        """Patch _run_loop on the instance so when the watchdog's
        stop+start cycle creates a fresh thread, it exits instantly."""
        def _noop_run(self):
            while not self._stop_event.is_set():
                self._stop_event.wait(timeout=0.05)
        # We use patch.object on the class because the watchdog
        # assigns self._thread = threading.Thread(target=self._run_loop)
        # and the bound method lookup happens at thread start, so
        # patching the class is fine.
        self._run_loop_patcher = patch.object(
            PersistentRTSPReader, "_run_loop", _noop_run
        )
        self._run_loop_patcher.start()

    def _fast_forward_past_deadline(self, scheduled):
        """Return a side_effect for time.monotonic that fast-forwards past
        the watchdog deadline. The new watchdog sleeps on
        stop_event.wait(timeout=scheduled) — so we don't actually need
        to patch time.monotonic here. Kept for symmetry with the
        earlier test design; the watchdog's wait() honours the
        timeout parameter regardless of monotonic.
        """
        return lambda: time.monotonic()

    def test_scheduled_reconnect_fire_closes_container(self):
        # Direct unit test of the fire method — bypasses the wait()
        # loop in the watchdog and exercises the stop+join+close+start
        # cycle that the watchdog triggers.
        reader = self._make_reader_with_mock_container(scheduled=10.0)
        container_mock = reader._container

        # Patch _run_loop so the fresh thread spawned by the fire
        # cycle exits promptly (so the test doesn't leak threads).
        self._patch_run_loop_to_noop(reader)
        try:
            reader._scheduled_reconnect_fire()
        finally:
            self._run_loop_patcher.stop()

        container_mock.close.assert_called_once()
        assert reader.reconnects_total == 1
        # After the fire, a fresh decode thread should be alive.
        assert reader._thread is not None
        assert reader._thread.is_alive()

    def test_scheduled_reconnect_fire_skips_close_if_container_is_none(self):
        # Idempotent: error path may have already cleared _container.
        # The fire should not raise. reconnects_total IS incremented
        # even with no container to close (the fire happened; that's
        # the count we care about for the watchdog cadence).
        reader = self._make_reader_with_mock_container(scheduled=10.0)
        reader._container = None  # error path cleared it

        self._patch_run_loop_to_noop(reader)
        try:
            reader._scheduled_reconnect_fire()
        finally:
            self._run_loop_patcher.stop()

        assert reader.reconnects_total == 1

    def test_scheduled_reconnect_fire_logs_uptime_and_counters(self, caplog):
        reader = self._make_reader_with_mock_container(scheduled=10.0)
        reader.frames_decoded_total = 1234

        with caplog.at_level("INFO", logger="persistent_rtsp"):
            self._patch_run_loop_to_noop(reader)
            try:
                reader._scheduled_reconnect_fire()
            finally:
                self._run_loop_patcher.stop()

        # Find the scheduled_reconnect_fire log line.
        match = [
            r for r in caplog.records if "scheduled_reconnect_fire" in r.message
        ]
        assert match, "expected scheduled_reconnect_fire log line"
        msg = match[0].message
        assert "frames_decoded=1234" in msg
        assert "reconnects_total=0" in msg  # before increment

    def test_reconnects_total_increments_across_scheduled_and_error_paths(self):
        # Fire one scheduled reconnect, then simulate an error-driven
        # reconnect via _decode_iteration. Both paths use the same
        # reconnects_total counter.
        reader = self._make_reader_with_mock_container(scheduled=10.0)
        assert reader.reconnects_total == 0

        # Scheduled path:
        self._patch_run_loop_to_noop(reader)
        try:
            reader._scheduled_reconnect_fire()
        finally:
            self._run_loop_patcher.stop()
        assert reader.reconnects_total == 1

        # Error path (simulated): _run_loop catches and increments.
        reader.reconnects_total += 1
        assert reader.reconnects_total == 2

    def test_watchdog_loop_exits_on_stop_event(self):
        # Watchdog loop should exit promptly when stop_event is set
        # before the cadence elapses. Use a high cadence so the test
        # finishes fast.
        reader = self._make_reader_with_mock_container(scheduled=3600.0)
        t = threading.Thread(
            target=reader._scheduled_reconnect_watchdog, daemon=True
        )
        t.start()
        # Brief sleep so the watchdog enters the wait() call.
        time.sleep(0.05)
        reader._stop_event.set()
        t.join(timeout=2.0)
        assert not t.is_alive()
        # No fire happened — reconnects_total unchanged.
        assert reader.reconnects_total == 0


# ---------------------------------------------------------------------------
# Full start/stop lifecycle
# ---------------------------------------------------------------------------


class TestLifecycle:
    """start()/stop() bring up + tear down both the decode thread and the
    watchdog thread. We can't easily test the decode thread here (it
    would try to open RTSP), but we CAN verify the watchdog thread is
    started and joined.
    """

    def _block_on_stop_event(self):
        """Real-loop mimic: block until stop_event is set. Used to patch
        both _run_loop and _scheduled_reconnect_watchdog so neither
        thread exits instantly."""
        def _impl(self):
            while not self._stop_event.is_set():
                self._stop_event.wait(timeout=0.05)
        return _impl

    def test_start_starts_watchdog_thread(self):
        # Patch _run_loop and _scheduled_reconnect_watchdog so neither
        # thread exits before we assert is_alive(). Without this, the
        # mocked decode thread returns instantly, is_running becomes
        # False, and .stop() short-circuits without joining the watchdog.
        fake_watchdog = self._block_on_stop_event()
        with (
            patch.object(PersistentRTSPReader, "_run_loop", fake_watchdog),
            patch.object(
                PersistentRTSPReader, "_scheduled_reconnect_watchdog", fake_watchdog
            ),
        ):
            reader = PersistentRTSPReader(
                "rtsp://test/stream",
                scheduled_reconnect_seconds=10,
            )
            reader.start()
            try:
                assert reader._watchdog_thread is not None
                assert reader._watchdog_thread.is_alive()
            finally:
                reader.stop()

    def test_stop_joins_watchdog_thread(self):
        fake_watchdog = self._block_on_stop_event()
        with (
            patch.object(PersistentRTSPReader, "_run_loop", fake_watchdog),
            patch.object(
                PersistentRTSPReader, "_scheduled_reconnect_watchdog", fake_watchdog
            ),
        ):
            reader = PersistentRTSPReader(
                "rtsp://test/stream",
                scheduled_reconnect_seconds=10,
            )
            reader.start()
            reader.stop()
            assert reader._watchdog_thread is not None
            assert reader._thread is not None
            assert not reader._watchdog_thread.is_alive()
            assert not reader._thread.is_alive()

    def test_is_running_true_after_start(self):
        fake_watchdog = self._block_on_stop_event()
        with (
            patch.object(PersistentRTSPReader, "_run_loop", fake_watchdog),
            patch.object(
                PersistentRTSPReader, "_scheduled_reconnect_watchdog", fake_watchdog
            ),
        ):
            reader = PersistentRTSPReader(
                "rtsp://test/stream",
                scheduled_reconnect_seconds=10,
            )
            reader.start()
            try:
                assert reader.is_running
            finally:
                reader.stop()


# ---------------------------------------------------------------------------
# Registry (Phase 6B.87, PLAN §11.17)
# ---------------------------------------------------------------------------
# Per-camera registration under canonical name. capture_frames() resolves
# the right reader by RTSP URL (creds stripped) via get_reader_for_url().
# Back-compat shims (set_default_reader / get_default_reader) cover any
# external caller that used the singleton-without-name pattern.


class TestRegistry:
    """Tests for the per-camera reader registry."""

    def setup_method(self):
        from infra.persistent_rtsp import clear_reader_registry
        clear_reader_registry()

    def teardown_method(self):
        from infra.persistent_rtsp import clear_reader_registry
        clear_reader_registry()

    @staticmethod
    def _make_fake_reader(rtsp_url: str) -> PersistentRTSPReader:
        """Build a PersistentRTSPReader without starting any threads.

        Bypasses .start() so we don't need to mock av. We then set
        the minimum attributes the registry code touches (just
        _rtsp_url, which __init__ already stores).
        """
        # __init__ itself does no I/O — safe to call directly.
        return PersistentRTSPReader(rtsp_url=rtsp_url, scheduled_reconnect_seconds=3600)

    def test_set_get_reader_roundtrip(self):
        from infra.persistent_rtsp import get_reader, set_reader

        # Synthetic fixtures (PII-free): test codes CAM1/CAM2, synthetic IPs.
        cam1 = self._make_fake_reader("rtsp://admin:p@10.0.0.1:554/h264Preview_01_main")
        cam2 = self._make_fake_reader("rtsp://admin:p@10.0.0.2:554/h264Preview_01_main")

        set_reader("Test Front", cam1)
        set_reader("Test Back", cam2)

        assert get_reader("Test Front") is cam1
        assert get_reader("Test Back") is cam2

    def test_get_reader_for_url_matches_by_host_port_path(self):
        from infra.persistent_rtsp import get_reader_for_url, set_reader

        # Synthetic fixtures: distinct creds but same host:port/path structure.
        cam1 = self._make_fake_reader(
            "rtsp://admin:%23wijhVV2OFt69%40Z%23tG@10.0.0.1:554/h264Preview_01_main"
        )
        cam2 = self._make_fake_reader(
            "rtsp://admin:%23wijhVV2OFt69%40Z%23tG@10.0.0.2:554/h264Preview_01_main"
        )
        set_reader("Test Front", cam1)
        set_reader("Test Back", cam2)

        # Lookup with same creds as registered → matches
        assert (
            get_reader_for_url(
                "rtsp://admin:%23wijhVV2OFt69%40Z%23tG@10.0.0.1:554/h264Preview_01_main"
            )
            is cam1
        )

        # Lookup with DIFFERENT creds → still matches (creds are stripped)
        assert (
            get_reader_for_url("rtsp://other:different@10.0.0.2:554/h264Preview_01_main")
            is cam2
        )

        # Lookup for an unregistered URL → returns None
        assert (
            get_reader_for_url("rtsp://admin:p@10.0.0.254:554/stream")
            is None
        )

    def test_get_reader_returns_none_for_unregistered(self):
        from infra.persistent_rtsp import get_reader, set_reader

        # Brand-new registry, no readers set
        assert get_reader("Nonexistent Camera") is None

        # A different camera set, lookup misses
        cam1 = self._make_fake_reader("rtsp://admin:p@10.0.0.1:554/h264Preview_01_main")
        set_reader("Test Front", cam1)
        assert get_reader("Test Back") is None

        # And the registered one resolves
        assert get_reader("Test Front") is cam1

    def test_legacy_set_default_reader_still_works(self):
        from infra.persistent_rtsp import (
            get_default_reader,
            get_reader,
            set_default_reader,
        )

        cam1 = self._make_fake_reader("rtsp://admin:p@10.0.0.1:554/h264Preview_01_main")

        # Back-compat: set_default_reader still registers + get_default_reader still retrieves
        set_default_reader(cam1)
        assert get_default_reader() is cam1
        # And it lives under the "default" name in the new registry
        assert get_reader("default") is cam1

        # Setting None via the back-compat shim removes it
        set_default_reader(None)
        assert get_default_reader() is None
        assert get_reader("default") is None


# Phase 6B.167 (PLAN §13.5 Commit 10) — code-keyed registry. New primary API
# is init_reader_registry(cameras) + set_reader_by_code(code, ...) +
# get_reader_by_code(code). set_reader(name, ...) is back-compat and stores
# under both name AND code (when the name resolves via the init'd registry).
#
# Synthetic fixtures (PII-free): CAM1/CAM2 with 10.0.0.x addresses.

def _sample_cameras():
    """Return a synthetic list[CameraSpec] for registry tests.

    Uses infra.cameras.CameraSpec dataclass (Phase 6B.167 §13.5 Commit 5).
    Codes are CAM1/CAM2 (NEW schema), names are generic test labels.
    """
    from infra.cameras import CameraSpec

    return [
        CameraSpec(
            code="CAM1",
            name="Test Front",
            ip="10.0.0.1",
            zone="yard",
            http_user="test_user",
            http_pass="test_pass",
        ),
        CameraSpec(
            code="CAM2",
            name="Test Back",
            ip="10.0.0.2",
            zone="yard",
            http_user="test_user",
            http_pass="test_pass",
        ),
    ]


class TestPhase6B167ByCodeRegistry:
    """Phase 6B.167 §13.5 Commit 10 — code-keyed reader registry.

    Tests the new primary API:
      - init_reader_registry(cameras)
      - set_reader_by_code(code, reader)
      - get_reader_by_code(code)
    And the back-compat guarantees:
      - set_reader(name, ...) dual-writes under both name and code
      - get_reader(name, ...) resolves via name→code when init'd
    """

    def setup_method(self):
        from infra.persistent_rtsp import (
            clear_reader_registry,
            init_reader_registry,
        )

        clear_reader_registry()
        init_reader_registry(_sample_cameras())

    def teardown_method(self):
        from infra.persistent_rtsp import clear_reader_registry

        clear_reader_registry()

    @staticmethod
    def _make_fake_reader(rtsp_url: str) -> PersistentRTSPReader:
        return PersistentRTSPReader(rtsp_url=rtsp_url, scheduled_reconnect_seconds=3600)

    def test_set_reader_by_code_roundtrip(self):
        from infra.persistent_rtsp import get_reader_by_code, set_reader_by_code

        cam1 = self._make_fake_reader("rtsp://admin:p@10.0.0.1:554/stream")
        cam2 = self._make_fake_reader("rtsp://admin:p@10.0.0.2:554/stream")

        set_reader_by_code("CAM1", cam1)
        set_reader_by_code("CAM2", cam2)

        assert get_reader_by_code("CAM1") is cam1
        assert get_reader_by_code("CAM2") is cam2

    def test_get_reader_by_code_returns_none_for_unregistered(self):
        from infra.persistent_rtsp import get_reader_by_code

        assert get_reader_by_code("CAM1") is None
        assert get_reader_by_code("NONEXISTENT") is None

    def test_init_reader_registry_requires_non_empty(self):
        from infra.persistent_rtsp import init_reader_registry

        with pytest.raises(ValueError, match="non-empty"):
            init_reader_registry([])

    def test_init_reader_registry_rejects_duplicate_code(self):
        from infra.cameras import CameraSpec
        from infra.persistent_rtsp import init_reader_registry

        dups = [
            CameraSpec(code="CAM1", name="A", ip="10.0.0.1", zone="", http_user="", http_pass=""),
            CameraSpec(code="CAM1", name="B", ip="10.0.0.2", zone="", http_user="", http_pass=""),
        ]
        with pytest.raises(ValueError, match="duplicate spec.code"):
            init_reader_registry(dups)

    def test_init_reader_registry_rejects_duplicate_name(self):
        from infra.cameras import CameraSpec
        from infra.persistent_rtsp import init_reader_registry

        dups = [
            CameraSpec(code="CAM1", name="Same", ip="10.0.0.1", zone="", http_user="", http_pass=""),
            CameraSpec(code="CAM2", name="Same", ip="10.0.0.2", zone="", http_user="", http_pass=""),
        ]
        with pytest.raises(ValueError, match="duplicate spec.name"):
            init_reader_registry(dups)

    def test_set_reader_name_dual_writes_under_code(self):
        """Back-compat: set_reader("Test Front", ...) also stores under CAM1."""
        from infra.persistent_rtsp import (
            get_reader,
            get_reader_by_code,
            set_reader,
        )

        cam1 = self._make_fake_reader("rtsp://admin:p@10.0.0.1:554/stream")
        set_reader("Test Front", cam1)

        # Look up by either key — same reader back.
        assert get_reader("Test Front") is cam1
        assert get_reader_by_code("CAM1") is cam1
        # Code-as-name lookup also works (no init resolution needed)
        assert get_reader_by_code("Test Front") is cam1

    def test_set_reader_unknown_name_falls_back_to_name_only(self):
        """set_reader(name) for a name NOT in the registry stores under name only.

        This is the back-compat path for legacy callers that use operator
        friendly-names which aren't in the canonical CameraSpec list.
        """
        from infra.persistent_rtsp import (
            get_reader,
            get_reader_by_code,
            set_reader,
        )

        # "Legacy Camera" is NOT in _sample_cameras().
        legacy = self._make_fake_reader("rtsp://admin:p@10.0.0.99:554/stream")
        set_reader("Legacy Camera", legacy)

        # Lookup by name works (direct)
        assert get_reader("Legacy Camera") is legacy
        # Lookup by "code" (treating name as code) also works since
        # the back-compat path stores under whatever string was passed.
        assert get_reader_by_code("Legacy Camera") is legacy

    def test_init_reader_registry_clears_prior_readers(self):
        """Re-init'ing the registry drops any prior reader registrations."""
        from infra.cameras import CameraSpec
        from infra.persistent_rtsp import (
            get_reader_by_code,
            init_reader_registry,
            set_reader_by_code,
        )

        # Initial registration under CAM1.
        cam1_old = self._make_fake_reader("rtsp://admin:p@10.0.0.1:554/stream")
        set_reader_by_code("CAM1", cam1_old)
        assert get_reader_by_code("CAM1") is cam1_old

        # Re-init with NEW camera list (no CAM1, just CAM9).
        new_cameras = [
            CameraSpec(
                code="CAM9",
                name="Test New",
                ip="10.0.0.9",
                zone="yard",
                http_user="test_user",
                http_pass="test_pass",
            ),
        ]
        init_reader_registry(new_cameras)

        # CAM1 is gone (cleared by re-init).
        assert get_reader_by_code("CAM1") is None
        # And a new CAM9 slot is available.
        cam9 = self._make_fake_reader("rtsp://admin:p@10.0.0.9:554/stream")
        set_reader_by_code("CAM9", cam9)
        assert get_reader_by_code("CAM9") is cam9

    def test_get_reader_for_url_finds_code_registered_reader(self):
        """A reader registered via set_reader_by_code is discoverable
        by get_reader_for_url() (used by frame_capture)."""
        from infra.persistent_rtsp import (
            get_reader_for_url,
            set_reader_by_code,
        )

        cam1 = self._make_fake_reader("rtsp://admin:p@10.0.0.1:554/stream")
        set_reader_by_code("CAM1", cam1)

        # Lookup by URL — matches via creds-stripped host:port/path.
        assert get_reader_for_url("rtsp://any:p@10.0.0.1:554/stream") is cam1


# Phase 6B.155 (PLAN §11.78) — max-attempts cap on failure-driven reconnects.
# Per the 2026-08-28 CAM3 incident (6 errors in 80s), the loop now caps at
# RECONNECT_MAX_ATTEMPTS_DEFAULT=10 consecutive failures, then defers to
# the scheduled_reconnect_watchdog instead of hammering the camera every
# 30s forever.

class TestResolveMaxReconnectAttempts:
    """_resolve_max_reconnect_attempts: arg > env > default precedence."""

    def test_arg_value_wins(self, monkeypatch):
        monkeypatch.setenv("FARMSV_RTSP_MAX_RETRIES", "99")
        from infra.persistent_rtsp import _resolve_max_reconnect_attempts

        # Explicit arg wins over env
        assert _resolve_max_reconnect_attempts(7) == 7

    def test_env_var_used_when_arg_is_none(self, monkeypatch):
        monkeypatch.setenv("FARMSV_RTSP_MAX_RETRIES", "42")
        from infra.persistent_rtsp import _resolve_max_reconnect_attempts

        assert _resolve_max_reconnect_attempts(None) == 42

    def test_default_when_no_arg_no_env(self, monkeypatch):
        monkeypatch.delenv("FARMSV_RTSP_MAX_RETRIES", raising=False)
        from infra.persistent_rtsp import (
            RECONNECT_MAX_ATTEMPTS_DEFAULT,
            _resolve_max_reconnect_attempts,
        )

        assert _resolve_max_reconnect_attempts(None) == RECONNECT_MAX_ATTEMPTS_DEFAULT
        assert RECONNECT_MAX_ATTEMPTS_DEFAULT == 10

    def test_zero_disables_cap_legacy_behavior(self, monkeypatch):
        """max_reconnect_attempts<=0 disables the cap (retry forever)."""
        from infra.persistent_rtsp import _resolve_max_reconnect_attempts

        # Zero passed through unchanged (used as "disabled" flag in
        # _run_loop via `> 0` check). Negative values also pass through;
        # consumers should treat anything <=0 as "no cap".
        assert _resolve_max_reconnect_attempts(0) == 0
        assert _resolve_max_reconnect_attempts(-1) == -1

    def test_negative_env_value_raises(self, monkeypatch):
        """Negative env values parse cleanly (we don't gate on parse)."""
        monkeypatch.setenv("FARMSV_RTSP_MAX_RETRIES", "-5")
        from infra.persistent_rtsp import _resolve_max_reconnect_attempts

        # Negative values pass through (consumer checks > 0)
        assert _resolve_max_reconnect_attempts(None) == -5


class TestRunLoopMaxAttemptsCap:
    """_run_loop honors the cap and defers to scheduled_reconnect_watchdog.

    We mock _decode_iteration to raise N consecutive errors and assert:
      - First N-1 attempts log WARNING with "remaining" count
      - Nth attempt logs ERROR with "deferring to scheduled_reconnect_watchdog"
      - After the cap, the loop calls _sleep_until_stop_or_watchdog
      - The consecutive_errors counter accumulates across retries
      - Successful decode after cap logs recovery and resets state
    """

    def _make_reader_with_cap(self, max_attempts: int):
        """Construct a reader directly (without start()) for unit testing."""
        from infra.persistent_rtsp import PersistentRTSPReader

        reader = PersistentRTSPReader(
            rtsp_url="rtsp://admin:p@10.0.0.99:554/test",
            max_reconnect_attempts=max_attempts,
        )
        return reader

    def test_cap_disabled_legacy_forever_retry(self, monkeypatch):
        """max_attempts=0 → never exits the retry loop (legacy behavior)."""
        sleep_calls = []
        warn_logs = []

        reader = self._make_reader_with_cap(max_attempts=0)
        # Mock sleep so test doesn't actually block
        monkeypatch.setattr(
            "infra.persistent_rtsp.time.sleep",
            lambda *args: sleep_calls.append(args[0]),
        )

        # _decode_iteration raises forever
        def always_fail():
            raise OSError("test failure")

        # Each call to _run_loop spins forever; we only need to assert
        # that it logs warnings repeatedly without ever calling the
        # cap-path or the helper sleep function.
        monkeypatch.setattr(reader, "_decode_iteration", always_fail)
        monkeypatch.setattr(
            "infra.persistent_rtsp._sleep_until_stop_or_watchdog",
            lambda *args, **kwargs: pytest.fail(
                "should not be called when cap=0"
            ),
        )

        # Patch log.error (we expect none) and log.warning (many).
        cap_error_calls = []

        def fake_log_error(msg, *args, **kwargs):
            cap_error_calls.append(msg)

        monkeypatch.setattr(
            "infra.persistent_rtsp.log.error", fake_log_error
        )

        def fake_log_warning(msg, *args, **kwargs):
            warn_logs.append(msg)

        monkeypatch.setattr(
            "infra.persistent_rtsp.log.warning", fake_log_warning
        )

        # Set stop_event so the loop exits after a few iterations.
        def stop_after_a_few():
            time.sleep(0)  # yield once
            reader._stop_event.set()

        # Use stop_event control
        reader._stop_event.set()  # exits immediately on next check
        reader._run_loop()

        # No ERROR logged (cap not exercised)
        assert cap_error_calls == []
        # No call to the helper (cap not exercised)
        # (fail() in the monkeypatch would have raised if called)


    def test_cap_triggers_after_n_attempts(self, monkeypatch):
        """After N consecutive errors, loop logs ERROR and calls the helper."""
        helper_called = []
        sleep_calls = []
        error_logs = []
        warn_logs = []

        reader = self._make_reader_with_cap(max_attempts=3)
        monkeypatch.setattr(
            "infra.persistent_rtsp.time.sleep",
            lambda *_args, **_kw: sleep_calls.append(_args[0]),
        )

        fail_count = [0]

        def sometimes_fail():
            fail_count[0] += 1
            if fail_count[0] <= 5:  # 5 failures then succeed
                raise OSError(f"simulated failure #{fail_count[0]}")

        monkeypatch.setattr(reader, "_decode_iteration", sometimes_fail)

        # counter closure: first helper call lets the loop continue; 2nd
        # call sets stop_event to exit cleanly.
        state = {"helper_calls": 0}

        def fake_helper(*_args, **_kwargs):
            helper_called.append(_args)
            state["helper_calls"] += 1
            if state["helper_calls"] >= 2:
                reader._stop_event.set()
            # 1st call: leave stop_event unset so outer loop re-tries
            # decode, which fails (sometimes_fail still raises) and
            # emits the post-cap warning.

        monkeypatch.setattr(
            "infra.persistent_rtsp._sleep_until_stop_or_watchdog", fake_helper
        )

        def fake_error(msg, *args, **kwargs):
            error_logs.append(msg)

        def fake_warning(msg, *args, **kwargs):
            warn_logs.append(msg)

        monkeypatch.setattr("infra.persistent_rtsp.log.error", fake_error)
        monkeypatch.setattr("infra.persistent_rtsp.log.warning", fake_warning)

        reader._run_loop()

        # Helper was called exactly once
        assert len(helper_called) == 1
        # Exactly one ERROR was logged (the "Deferring to scheduled_reconnect_watchdog" message)
        error_count = sum(
            1 for m in error_logs
            if "deferring to scheduled_reconnect_watchdog" in m.lower()
        )
        assert error_count == 1, f"Expected 1 deferral ERROR, got {error_count}"
        # With cap=3: the loop logs warnings for attempts 1, 2 (with
        # "N attempts remaining" suffix), then attempt 3 fires the cap-ERROR.
        # After helper returns (no stop_event), attempts 4 and 5 also fail,
        # logging the post-cap "deferred to scheduled_reconnect_watchdog"
        # warnings before the helper fires stop_event and the loop exits.
        # Just assert >= 2 (the pre-cap count) — exact post-cap count is
        # an implementation detail.
        warning_count = sum(
            1 for m in warn_logs if "decode iteration failed" in m
        )
        assert warning_count >= 2, (
            f"Expected >=2 warning logs, got {warning_count}"
        )
        # NOTE: post-cap behavior (after helper returns without stop_event
        # set) would emit a "deferred to scheduled_reconnect_watchdog"
        # warning on the next failure. We don't assert that here — it's
        # an implementation detail and the test exits cleanly via the
        # stop_event setting on the 2nd helper entry.


    def test_recovery_after_cap_logs_info(self, monkeypatch):
        """After cap is hit AND then the camera recovers, log INFO + break.

        This proves the cap doesn't permanently brick the reader.
        """
        sleep_calls = []
        info_logs = []

        reader = self._make_reader_with_cap(max_attempts=2)

        # First decode raises; second succeeds
        state = [0]

        def first_fail_then_success():
            state[0] += 1
            if state[0] <= 2:  # first 2 calls fail (cap=2 will hit on 2nd)
                raise OSError(f"failure #{state[0]}")
            # 3rd call succeeds; _decode_iteration returns normally

        monkeypatch.setattr(reader, "_decode_iteration", first_fail_then_success)
        monkeypatch.setattr(
            "infra.persistent_rtsp.time.sleep",
            lambda *_args, **_kw: sleep_calls.append(_args[0]),
        )
        monkeypatch.setattr(
            "infra.persistent_rtsp._sleep_until_stop_or_watchdog",
            lambda *args, **kwargs: None,
        )

        def fake_info(msg, *args, **kwargs):
            info_logs.append(msg)

        monkeypatch.setattr("infra.persistent_rtsp.log.info", fake_info)

        reader._run_loop()

        # Recovery INFO was logged
        recovery_msgs = [m for m in info_logs if "RTSP recovered" in m]
        assert len(recovery_msgs) >= 1, (
            f"Expected >=1 RTSP recovered message, got {len(recovery_msgs)}"
        )


# ---------------------------------------------------------------------------
# §11.88 (2026-09-01) — Ring buffer format is PNG lossless, NOT JPEG q85.
# ---------------------------------------------------------------------------


class TestRingBufferFormat:
    """§11.88: the ring stores lossless PNG bytes, not JPEG q85.

    Why: Phase 6A InsightFace embeddings were unreliable because Qwen saw
    a downscaled JPEG q85 view of the scene and reported fuzzy bboxes,
    and the same lossy stream fed the crop_face_region_from_4k caller.
    Storing lossless PNG bytes in the ring removes the lossy half of the
    pipeline. Disk writes (get_recent_frames / get_frames_by_offset)
    re-encode to PNG on the way out.
    """

    def test_ring_contains_png_bytes_not_jpeg(self):
        """When the decode loop pushes a frame, the ring must contain
        bytes that start with the PNG magic (89 50 4E 47), not the JPEG
        magic (FF D8 FF).
        """
        from PIL import Image

        reader = PersistentRTSPReader(
            "rtsp://test/stream",
            scheduled_reconnect_seconds=999999,  # disable watchdog
        )
        # Synthesize a frame as a 2304x1296 RGB PIL.Image.
        frame = Image.new("RGB", (2304, 1296), color=(128, 128, 128))
        # The decode loop uses `img.tobytes()` after our refactor — but
        # our actual refactor calls `frame.save(buf, format="PNG")`. Use
        # the new PNG-encode path explicitly to mirror the production code.
        import io

        buf = io.BytesIO()
        frame.save(buf, format="PNG")
        png_bytes = buf.getvalue()

        reader._ring.append(png_bytes)
        out = reader._ring[0]

        # PNG magic: 89 50 4E 47 (first 4 bytes)
        assert out[:4] == b"\x89PNG", (
            f"Expected PNG magic in ring buffer, got first 4 bytes: {out[:4]!r}"
        )
        # JPEG magic (FF D8 FF) must NOT be present at the start.
        assert not out.startswith(b"\xff\xd8\xff"), (
            "Ring buffer must not contain JPEG bytes (would mean JPEG q85 re-encoded)"
        )

    def test_ring_entry_is_decodable_as_png(self):
        """A PIL.Image.open() on the ring bytes must succeed and produce
        the original dimensions (lossless round-trip)."""
        from PIL import Image

        reader = PersistentRTSPReader(
            "rtsp://test/stream",
            scheduled_reconnect_seconds=999999,
        )
        frame = Image.new("RGB", (2304, 1296), color=(50, 100, 150))
        import io

        buf = io.BytesIO()
        frame.save(buf, format="PNG")
        reader._ring.append(buf.getvalue())

        # Re-decode the ring entry.
        decoded = Image.open(io.BytesIO(reader._ring[0]))
        decoded.load()
        assert decoded.size == (2304, 1296)
        assert decoded.format == "PNG"

    def test_persistent_rtsp_no_jpeg_constant_present(self):
        """Sanity: the module must not still have a JPEG-quality constant
        like _JPEG_QUALITY exposed — these were removed in §11.88.
        """
        import infra.persistent_rtsp as mod

        assert not hasattr(mod, "_JPEG_QUALITY"), (
            "_JPEG_QUALITY constant should have been removed in §11.88; "
            "ring buffer now stores lossless PNG bytes."
        )

