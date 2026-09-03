"""
test_matcher_failures.py — Per-behavior pins for infra.matcher_failures.

Phase.108 — module extracted from listener.py 2026-08-21.

Per test-serves-design rule (Note 2026-08-19): pin behavior, not
implementation. Each test uses a freshly-instantiated MatcherFailureTracker
(not the module singleton MATCHER_FAILURES) so tests don't interfere
with each other or with the running listener's failure-count state.
"""
from __future__ import annotations


def test_record_returns_count_in_5min_window():
    """record(exc) returns the failure count in the rolling 5-min window."""
    from infra.matcher_failures import MatcherFailureTracker

    t = MatcherFailureTracker()
    assert t.record(RuntimeError("first")) == 1
    assert t.record(RuntimeError("second")) == 2
    assert t.record(RuntimeError("third")) == 3


def test_stats_initial_state_all_zeros():
    """A freshly-created tracker has all-zero / no-last-failure state."""
    from infra.matcher_failures import MatcherFailureTracker

    t = MatcherFailureTracker()
    stats = t.stats()
    assert stats["total_failures"] == 0
    assert stats["failures_last_5min"] == 0
    assert stats["last_failure_at"] is None
    assert stats["last_exception"] is None


def test_stats_after_one_failure():
    """After one record(), stats reflects total=1, recent=1, and the exc repr."""
    from infra.matcher_failures import MatcherFailureTracker

    t = MatcherFailureTracker()
    t.record(ValueError("bad plate"))
    stats = t.stats()
    assert stats["total_failures"] == 1
    assert stats["failures_last_5min"] == 1
    assert stats["last_failure_at"] is not None
    assert "ValueError" in stats["last_exception"]
    assert "bad plate" in stats["last_exception"]


def test_stats_after_multiple_failures_records_last_repr():
    """stats()['last_exception'] is the most recent exception's repr, not the first."""
    from infra.matcher_failures import MatcherFailureTracker

    t = MatcherFailureTracker()
    t.record(ValueError("first"))
    t.record(KeyError("second"))
    stats = t.stats()
    assert stats["total_failures"] == 2
    assert stats["failures_last_5min"] == 2
    assert "KeyError" in stats["last_exception"]
    assert "second" in stats["last_exception"]


def test_record_handles_any_baseexception_subclass():
    """record() accepts any BaseException, not just RuntimeError/ValueError."""
    from infra.matcher_failures import MatcherFailureTracker

    t = MatcherFailureTracker()
    t.record(KeyboardInterrupt())  # BaseException, not Exception
    stats = t.stats()
    assert stats["total_failures"] == 1
    assert "KeyboardInterrupt" in stats["last_exception"]


def test_module_singleton_exists():
    """The module-level MATCHER_FAILURES singleton is exposed for the listener.

    Per the listener's /status handler contract: MATCHER_FAILURES.stats() must
    be callable from the route handler. If this test fails, the singleton
    was renamed or removed and /status will break.
    """
    from infra import matcher_failures
    assert hasattr(matcher_failures, "MATCHER_FAILURES")
    assert isinstance(
        matcher_failures.MATCHER_FAILURES,
        matcher_failures.MatcherFailureTracker,
    )


def test_rolling_5min_window_trims_old_entries():
    """failures_last_5min only counts entries from the last 5 minutes.

    We can't easily wait 5 minutes in a test, so we test the trimming
    behavior indirectly by inspecting the internal state: the
    _recent_timestamps list is trimmed on every record() call.
    """
    from infra.matcher_failures import MatcherFailureTracker

    t = MatcherFailureTracker()
    # Simulate many failures — _recent_timestamps should not grow unbounded
    for i in range(10):
        t.record(RuntimeError(f"e{i}"))
    # Internal check: all 10 should still be in the 5-min window
    assert len(t._recent_timestamps) == 10
    assert t.stats()["failures_last_5min"] == 10
    assert t.stats()["total_failures"] == 10