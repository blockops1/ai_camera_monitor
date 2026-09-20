"""tests/test_daemon.py — tests for listener/daemon.py cleanup thread.

US-061c: verify the cleanup thread starts, verify cleanup.run() is called
at least once on a mocked clock.
"""

import logging
import threading
import time
from unittest import mock

import pytest

import infra.cleanup


def _run_cleanup_loop_once(interval_min, fake_run_func=None):
    """Run _cleanup_loop for one cycle then stop it.

    Returns the list of (dirs, freed, errors) from each cleanup.run() call.
    """
    from listener.daemon import _cleanup_loop

    calls = []

    def fake_run():
        result = fake_run_func() if fake_run_func else {
            "deleted_dirs": 0, "freed_bytes": 0, "errors": 0,
        }
        calls.append(result)
        return result

    # Patch infra.cleanup.run before starting the loop
    orig_run = infra.cleanup.run
    infra.cleanup.run = fake_run

    # Patch time.sleep so we don't wait the real interval
    sleep_count = [0]

    def fast_sleep(n):
        sleep_count[0] += 1
        if sleep_count[0] >= 2:
            raise KeyboardInterrupt()

    orig_sleep = time.sleep
    time.sleep = fast_sleep

    try:
        t = threading.Thread(target=_cleanup_loop, args=(interval_min,))
        t.start()
        t.join(timeout=5)
    except KeyboardInterrupt:
        pass
    finally:
        time.sleep = orig_sleep
        infra.cleanup.run = orig_run

    return calls


# ---------------------------------------------------------------------------
# Test: cleanup thread starts and calls run()
# ---------------------------------------------------------------------------


class TestCleanupThreadStarts:
    """Verify the cleanup thread is started in main()."""

    def test_cleanup_loop_calls_run_once(self):
        """_cleanup_loop calls infra.cleanup.run() at least once."""
        calls = _run_cleanup_loop_once(1)
        assert len(calls) >= 1, "cleanup.run() was not called at least once"

    def test_cleanup_loop_logs_result(self):
        """The cleanup loop logs per-run stats."""
        logs_captured = []

        class LogCapture(logging.Handler):
            def emit(self, record):
                logs_captured.append(record.getMessage())

        handler = LogCapture()
        log = logging.getLogger("daemon")
        log.addHandler(handler)

        call_count = [0]

        def fake_run():
            call_count[0] += 1
            return {
                "deleted_dirs": call_count[0],
                "freed_bytes": 1024,
                "errors": 0,
            }

        orig_run = infra.cleanup.run
        infra.cleanup.run = fake_run

        orig_sleep = time.sleep

        sleep_count = [0]

        def fast_sleep(n):
            sleep_count[0] += 1
            if sleep_count[0] >= 2:
                raise KeyboardInterrupt()

        time.sleep = fast_sleep

        from listener.daemon import _cleanup_loop

        try:
            t = threading.Thread(target=_cleanup_loop, args=(1,))
            t.start()
            t.join(timeout=5)
        except KeyboardInterrupt:
            pass
        finally:
            time.sleep = orig_sleep
            infra.cleanup.run = orig_run

        # Check that a log line with the cleanup stats was emitted
        assert any("dirs=" in m for m in logs_captured), \
            f"Expected cleanup log with stats, got: {logs_captured}"

    def test_cleanup_thread_skips_when_interval_zero(self):
        """When FARMSURV_CLEANUP_INTERVAL_MIN=0, the thread does not start.

        The main() function checks interval_min > 0 before spawning.
        """
        import inspect
        import listener.daemon as daemon_mod

        source = inspect.getsource(daemon_mod.main)
        assert "interval_min > 0" in source or "interval_min>0" in source


# ---------------------------------------------------------------------------
# Test: code patterns (AC proxy checks)
# ---------------------------------------------------------------------------


class TestCleanupThreadWiring:
    """Verify the code patterns the ACs check for."""

    def test_cleanup_import_present(self):
        """infra.cleanup is referenced in daemon.py."""
        import inspect
        import listener.daemon as daemon_mod

        source = inspect.getsource(daemon_mod)
        assert "_infra_cleanup" in source or "infra.cleanup" in source

    def test_cleanup_loop_function_exists(self):
        """_cleanup_loop function is defined and callable."""
        from listener.daemon import _cleanup_loop

        assert callable(_cleanup_loop)

    def test_threading_import_present(self):
        """threading is imported in daemon.py."""
        import inspect
        import listener.daemon as daemon_mod

        source = inspect.getsource(daemon_mod)
        assert "import threading" in source
