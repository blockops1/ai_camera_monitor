"""
test_vision_queue.py — Tests for infra.vision_queue.

Phase.167 §13.5 (Commit 12) — eligibility sets are now code-keyed
(CameraSpec.code values), not name-keyed. submit() accepts either a
code or a friendly name and translates via code_for() before the
membership tests. Tests mock infra.cameras.load_cameras() to provide a
synthetic fleet; no operator-flavored naming leaks into this file.

Test inventory:
  TestCodeFor:
    - code passed in → code returned (identity)
    - name passed in → spec.code returned (translation)
    - unknown identifier → fallback (input echoed unchanged)
    - empty spec list → fallback (input echoed unchanged)
    - first-match wins on duplicates
  TestPhase6B167ByCodeKeyedSets:
    - GATEKEEPER_CAMERAS contains codes, not names
    - PHASE6A_ELIGIBLE_CAMERAS contains codes, not names
    - legacy friendly names are NOT in the sets anymore
  TestSubmitPriorityDerivation:
    - gatekeeper code → PRIORITY_GATEKEEPER
    - outside-eligible code → PRIORITY_OUTSIDE
    - unknown code → PRIORITY_INSIDE
    - gatekeeper NAME → PRIORITY_GATEKEEPER (via code_for())
    - outside-eligible NAME → PRIORITY_OUTSIDE (via code_for())
    - unknown NAME → PRIORITY_INSIDE
  TestSubmitExplicitPriority:
    - explicit priority=PRIORITY_INSIDE for a gatekeeper code bypasses code_for()
  TestSubmitOverflowPolicy:
    - inside-tier job dropped when heap full (Phase.61 unchanged)
    - gatekeeper job NEVER dropped
  TestGetQueueSingleton:
    - get_queue() returns the same instance on repeat calls
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path
from unittest.mock import patch

_root = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_root))

import pytest

from infra.cameras import CameraSpec
from infra.vision_queue import (
    GATEKEEPER_CAMERAS,
    PHASE6A_ELIGIBLE_CAMERAS,
    PRIORITY_GATEKEEPER,
    PRIORITY_INSIDE,
    PRIORITY_OUTSIDE,
    QueueOverflowError,
    VisionQueue,
    code_for,
    get_queue,
)


# Synthetic test fleet used by every test in this module. IPs are in
# the test-only 10.x range (matches the public-repo test fixtures).
#
# Naming:
#     TEST_GK = "Test Gatekeeper" — code is in GATEKEEPER_CAMERAS
#     TEST_OUT = "Test Outside" — code is in PHASE6A_ELIGIBLE_CAMERAS (not gatekeeper)
#     TEST_IN  = "Test Inside" — code is in NEITHER set
#
# Codes here are operator-agnostic identifiers; names are generic. No
# operator-flavored camera naming leaks into this file.
_TEST_GK_IP = "10.0.0.1"
_TEST_OUT_IP = "10.0.0.2"
_TEST_IN_IP = "10.0.0.3"

_FLEET = [
    CameraSpec(code="TEST_GK", name="Test Gatekeeper", ip=_TEST_GK_IP, zone="yard"),
    CameraSpec(code="TEST_OUT", name="Test Outside", ip=_TEST_OUT_IP, zone="yard"),
    CameraSpec(code="TEST_IN", name="Test Inside", ip=_TEST_IN_IP, zone="inside"),
]


@pytest.fixture(autouse=True)
def _patch_load_cameras(monkeypatch):
    """Mock infra.cameras.load_cameras() to return _FLEET for every test."""
    monkeypatch.setattr(
        "infra.cameras.load_cameras", lambda env_path=None: list(_FLEET)
    )


@pytest.fixture
def _patch_eligibility_sets(monkeypatch):
    """Replace GATEKEEPER_CAMERAS and PHASE6A_ELIGIBLE_CAMERAS with synthetic
    code-keyed sets so the synthetic test fleet maps cleanly to policy.

    Without this, the production sets (containing "CAM1" etc.)
    would never match the test fleet's codes ("TEST_GK", "TEST_OUT"), and
    every camera would fall through to PRIORITY_INSIDE.

    NOT autouse — TestPhase6B167ByCodeKeyedSets asserts against the
    production (un-mocked) sets to verify they're code-keyed.
    """
    monkeypatch.setattr(
        "infra.vision_queue.GATEKEEPER_CAMERAS", frozenset({"TEST_GK"})
    )
    monkeypatch.setattr(
        "infra.vision_queue.PHASE6A_ELIGIBLE_CAMERAS",
        frozenset({"TEST_GK", "TEST_OUT"}),
    )


# ---------------------------------------------------------------------------
# code_for()
# ---------------------------------------------------------------------------


class TestCodeFor:
    def test_code_passed_in_returns_code(self):
        """If camera is already a known code, return it unchanged."""
        assert code_for("TEST_GK") == "TEST_GK"
        assert code_for("TEST_OUT") == "TEST_OUT"
        assert code_for("TEST_IN") == "TEST_IN"

    def test_name_translates_to_code(self):
        """If camera is a known name, return the spec's code."""
        assert code_for("Test Gatekeeper") == "TEST_GK"
        assert code_for("Test Outside") == "TEST_OUT"
        assert code_for("Test Inside") == "TEST_IN"

    def test_unknown_identifier_falls_through(self):
        """If no spec matches, return the input unchanged."""
        assert code_for("Nonexistent Camera") == "Nonexistent Camera"
        assert code_for("ANOTHER_CODE") == "ANOTHER_CODE"
        # Empty string also falls through (no spec has empty code or name).
        assert code_for("") == ""

    def test_empty_spec_list_falls_through(self, monkeypatch):
        """When load_cameras() returns [], code_for() echoes input unchanged."""
        monkeypatch.setattr(
            "infra.cameras.load_cameras", lambda env_path=None: []
        )
        assert code_for("ANYTHING") == "ANYTHING"

    def test_first_match_wins_on_duplicates(self, monkeypatch):
        """If two specs match the same name, the first wins (stable)."""
        fleet_dup = [
            CameraSpec(code="FIRST", name="Dup Name", ip="10.0.0.10", zone="z"),
            CameraSpec(code="SECOND", name="Dup Name", ip="10.0.0.11", zone="z"),
        ]
        monkeypatch.setattr(
            "infra.cameras.load_cameras", lambda env_path=None: list(fleet_dup)
        )
        assert code_for("Dup Name") == "FIRST"


# ---------------------------------------------------------------------------
# Phase.167 §13.5 Commit 12: code-keyed eligibility sets
# ---------------------------------------------------------------------------


class TestPhase6B167ByCodeKeyedSets:
    """Verify the eligibility sets use codes (not operator names)."""

    def test_gatekeeper_set_is_code_keyed(self):
        # Synthetic codes (TEST_GK) — should NOT appear in the production set.
        assert "TEST_GK" not in GATEKEEPER_CAMERAS
        # Production code is the legacy prefix from camera-creds.env.
        assert "CAM1" in GATEKEEPER_CAMERAS

    def test_phase6a_set_is_code_keyed(self):
        # Friendly names (any name-shaped string with spaces) must NOT
        # appear — the set is exclusively CameraSpec.code identifiers.
        for fake_name in (
            "Fake Front Door",
            "Fake Back Yard",
            "Fake Garage Cam",
            "Fake Outside North",
            "Fake Outside South",
        ):
            assert fake_name not in PHASE6A_ELIGIBLE_CAMERAS, (
                f"name-shaped string {fake_name!r} should not be in "
                f"PHASE6A_ELIGIBLE_CAMERAS — set is code-keyed"
            )
        # Codes (legacy prefixes from the env file) — must be present.
        for code in (
            "FRONT",
            "CAM2",
            "CAM3",
            "CAM1",
            "CAM4",
        ):
            assert code in PHASE6A_ELIGIBLE_CAMERAS, (
                f"code {code!r} should be in PHASE6A_ELIGIBLE_CAMERAS"
            )

    def test_gatekeeper_is_a_phase6a_member(self):
        """Sanity: gatekeeper code is also in the outside-eligible set."""
        # GATEKEEPER ⊂ PHASE6A_ELIGIBLE so priority logic stays consistent.
        assert GATEKEEPER_CAMERAS.issubset(PHASE6A_ELIGIBLE_CAMERAS)


# ---------------------------------------------------------------------------
# submit() — priority derivation from camera (name OR code)
# ---------------------------------------------------------------------------


def _identity_fn(*args, **kwargs):
    """A no-op function passed as `fn` to submit().

    Returns whatever it was called with so tests can introspect kwargs.
    """
    return ("identity_fn_result", args, kwargs)


def _drain_queue(q: VisionQueue, timeout_s: float = 5.0) -> None:
    """Process every pending job on the queue so future.set_result() fires.

    `submit()` returns a Future. We need to wait for the worker to
    finish each job so the future resolves. We can't call the worker's
    _run() directly (it loops forever), so we just sleep briefly and
    then check future.done().
    """
    import time

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        with q._cv:  # noqa: SLF001 — internal but acceptable in tests
            pending = len(q._heap)  # noqa: SLF001
        if pending == 0:
            return
        time.sleep(0.05)


class TestSubmitPriorityDerivation:
    """submit() derives priority from camera via code_for()."""

    pytestmark = pytest.mark.usefixtures("_patch_eligibility_sets")

    def _submit_and_get_priority(self, camera: str) -> int:
        """Helper: submit one job, drain, return the priority used."""
        q = VisionQueue()
        try:
            q.start()
            # priority=None forces derivation. Use a future-only flow so
            # we can read priority back without race conditions.
            future = q.submit(_identity_fn, camera=camera, priority=None)
            # The future's set_exception may fire (overflow path), but
            # priority is captured in job.priority on the heap BEFORE
            # overflow check. Inspect the heap directly.
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                with q._cv:  # noqa: SLF001 — internal but acceptable in tests
                    if q._heap:  # noqa: SLF001
                        return q._heap[0].priority  # noqa: SLF001
                time.sleep(0.01)
            pytest.fail(f"job for camera={camera!r} did not enqueue within 2s")
            return PRIORITY_INSIDE  # unreachable; satisfies type checker
        finally:
            q.stop()
            _drain_queue(q)

    def test_gatekeeper_code_yields_gatekeeper_priority(self):
        assert self._submit_and_get_priority("TEST_GK") == PRIORITY_GATEKEEPER

    def test_outside_eligible_code_yields_outside_priority(self):
        assert self._submit_and_get_priority("TEST_OUT") == PRIORITY_OUTSIDE

    def test_unknown_code_yields_inside_priority(self):
        assert self._submit_and_get_priority("UNKNOWN_CODE") == PRIORITY_INSIDE

    def test_gatekeeper_name_yields_gatekeeper_priority(self):
        """Caller passes a friendly name — code_for() translates to code."""
        assert self._submit_and_get_priority("Test Gatekeeper") == PRIORITY_GATEKEEPER

    def test_outside_eligible_name_yields_outside_priority(self):
        assert self._submit_and_get_priority("Test Outside") == PRIORITY_OUTSIDE

    def test_unknown_name_yields_inside_priority(self):
        assert self._submit_and_get_priority("Nonexistent Camera") == PRIORITY_INSIDE


# ---------------------------------------------------------------------------
# submit() — explicit priority bypasses code_for()
# ---------------------------------------------------------------------------


class TestSubmitExplicitPriority:
    """An explicit priority= argument short-circuits code_for() lookup."""

    pytestmark = pytest.mark.usefixtures("_patch_eligibility_sets")

    def test_explicit_priority_bypasses_gatekeeper_membership(self):
        """Even a gatekeeper code yields INSIDE priority when caller says so."""
        q = VisionQueue()
        try:
            q.start()
            future = q.submit(
                _identity_fn,
                camera="TEST_GK",
                priority=PRIORITY_INSIDE,
            )
            import time

            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                with q._cv:  # noqa: SLF001
                    if q._heap:  # noqa: SLF001
                        assert q._heap[0].priority == PRIORITY_INSIDE  # noqa: SLF001
                        break
                time.sleep(0.01)
            else:
                pytest.fail("job did not enqueue within 2s")
            _drain_queue(q)
        finally:
            q.stop()

    def test_explicit_priority_zero_is_respected(self):
        """A non-default priority value is preserved verbatim."""
        q = VisionQueue()
        try:
            q.start()
            future = q.submit(
                _identity_fn,
                camera="TEST_OUT",
                priority=PRIORITY_GATEKEEPER,  # gatekeeper priority for a non-gatekeeper code
            )
            import time

            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                with q._cv:  # noqa: SLF001
                    if q._heap:  # noqa: SLF001
                        assert q._heap[0].priority == PRIORITY_GATEKEEPER  # noqa: SLF001
                        break
                time.sleep(0.01)
            else:
                pytest.fail("job did not enqueue within 2s")
            _drain_queue(q)
        finally:
            q.stop()


# ---------------------------------------------------------------------------
# submit() — overflow policy unchanged (Phase.61)
# ---------------------------------------------------------------------------


class TestSubmitOverflowPolicy:
    """Inside-tier jobs are dropped on overflow; gatekeeper/outside never drop."""

    pytestmark = pytest.mark.usefixtures("_patch_eligibility_sets")

    def test_inside_tier_job_dropped_when_heap_full(self):
        """Fill the queue with inside-tier jobs, then submit one more."""
        from infra.vision_queue import MAX_QUEUE_DEPTH

        q = VisionQueue()
        try:
            q.start()
            # Fill the heap with inside-tier jobs that block until the
            # gatekeeper event is set, so the worker stays busy on the
            # first job and never pops another.
            slow_finished = threading.Event()

            def slow_fn(*args, **kwargs):
                # Block indefinitely (no timeout) until released.
                # The worker thread is pinned on this job, so the heap
                # stays full across the loop.
                slow_finished.wait()
                return "slow"

            # MAX_QUEUE_DEPTH jobs enqueue the heap to MAX_QUEUE_DEPTH
            # (worker is busy on the first, so no pop during the loop).
            # The next submit then triggers the overflow path.
            for _ in range(MAX_QUEUE_DEPTH):
                q.submit(
                    slow_fn,
                    camera="TEST_IN",  # inside-tier (NOT in PHASE6A_ELIGIBLE_CAMERAS)
                    priority=None,
                )

            # Heap is now MAX_QUEUE_DEPTH; the final inside-tier submit drops.
            future = q.submit(
                _identity_fn,
                camera="TEST_IN",
                priority=None,
            )
            assert future.done()
            with pytest.raises(QueueOverflowError):
                future.result()

            # Cleanup: release the slow fn so the queue can drain.
            slow_finished.set()
            _drain_queue(q)
        finally:
            q.stop()

    def test_gatekeeper_job_never_dropped(self):
        """A gatekeeper job is enqueued even when heap is at MAX_QUEUE_DEPTH."""
        from infra.vision_queue import MAX_QUEUE_DEPTH

        q = VisionQueue()
        try:
            q.start()
            slow_finished = threading.Event()

            def slow_fn(*args, **kwargs):
                slow_finished.wait()
                return "slow"

            # Fill the heap with inside-tier jobs. Worker is pinned on
            # the first (slow_fn blocks forever).
            for _ in range(MAX_QUEUE_DEPTH):
                q.submit(
                    slow_fn,
                    camera="TEST_IN",
                    priority=None,
                )

            # Heap is at MAX_QUEUE_DEPTH; a gatekeeper submit must NOT drop.
            future = q.submit(
                _identity_fn,
                camera="TEST_GK",
                priority=None,
            )
            # The future should NOT be in a failed state from overflow.
            assert not future.done(), (
                "gatekeeper job should not be dropped on overflow"
            )

            # Cleanup.
            slow_finished.set()
            _drain_queue(q)
            # Drain the gatekeeper future too.
            future.result(timeout=5.0)
        finally:
            q.stop()


# ---------------------------------------------------------------------------
# get_queue() — module-level singleton
# ---------------------------------------------------------------------------


class TestGetQueueSingleton:
    def test_returns_same_instance(self):
        # Reset the singleton between calls (only safe in tests).
        import infra.vision_queue as vq_mod

        original = vq_mod._queue  # noqa: SLF001
        q1 = None
        try:
            vq_mod._queue = None  # noqa: SLF001
            q1 = get_queue()
            q2 = get_queue()
            assert q1 is q2
        finally:
            vq_mod._queue = original  # noqa: SLF001
            # Best-effort stop so the listener isn't affected.
            if q1 is not None:
                try:
                    q1.stop()
                except Exception:
                    pass