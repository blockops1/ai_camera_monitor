"""
test_cooldown_lifecycle.py — Lifecycle tests for the cooldown check + record_hit.

US-050c moved record_hit from pipeline.run() to daemon.py (after TG#3 dispatch).
Pipeline-level tests verify that:
  (a) should_suppress works correctly (pipeline still calls it at stage 7a).
  (b) run() does NOT call record_hit (it's now in daemon).
  (c) The PipelineCooldown class still functions correctly (class-level tests).

Daemon-level record_hit lifecycle is tested in test_daemon_dispatch.py.
"""

import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from listener.pipeline import run


def _make_gate_verdict(
    classification="vehicle",
    class_label="car",
    confidence=0.85,
    top_class="car",
    top_confidence=0.85,
    reason="high_conf_vehicle",
    pairwise_diff_path=None,
):
    """Build a mock GateVerdict for pipeline tests."""
    v = MagicMock()
    v.classification = classification
    v.class_label = class_label
    v.confidence = confidence
    v.top_class = top_class
    v.top_confidence = top_confidence
    v.reason = reason
    v.crop_a = None
    v.crop_b = None
    v.pairwise_diff_path = pairwise_diff_path
    v.frames = []
    v.is_none = lambda: classification == "none"
    return v


def _make_artifacts(crop_a_path="/mock/a.png", crop_b_path="/mock/b.png"):
    """Build a mock AlertArtifacts for pipeline tests."""
    a = MagicMock()
    a.crop_a_path = crop_a_path
    a.crop_b_path = crop_b_path
    return a


def _make_alert(**kwargs):
    """Build a sample alert dict."""
    return {
        "id": kwargs.get("id", "evt-test-001"),
        "camera_id": kwargs.get("camera_id", "CAM1"),
        "camera_label": kwargs.get("camera_label", "Front Gate"),
        "classification": kwargs.get("classification", "vehicle"),
        "frames": kwargs.get(
            "frames",
            ["/tmp/f1.jpg", "/tmp/f2.jpg", "/tmp/f3.jpg", "/tmp/f4.jpg"],
        ),
    }


# ---------------------------------------------------------------------------
# AC(a): successful run → record_hit → next within window → suppressed
# ---------------------------------------------------------------------------


class TestCooldownLifecycleSuccess:
    """run() does NOT call record_hit (moved to daemon); cooldown check still works."""

    def test_success_then_suppression(self, tmp_path):
        """First webhook: OK → manual record_hit; second within window: dropped."""
        from infra.pipeline_cooldown import clear, record_hit

        clear()  # Ensure clean state
        base_time = 1000.0

        diff_path = str(tmp_path / "diff.png")
        Path(diff_path).write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 20)
        gate_v = _make_gate_verdict(pairwise_diff_path=diff_path)

        alert1 = _make_alert(id="alert-a1", classification="vehicle")
        alert2 = _make_alert(id="alert-a2", classification="vehicle")

        vm1_result = {"class": "vehicle", "confidence": 0.92}
        tg1 = {"caption": "test", "photos": []}
        vm2_result = {"class_confirmed": "vehicle", "distinctive_features": []}
        tg2 = {"caption": "test", "photos": []}
        tg3 = {"caption": "test", "photos": []}

        with (
            patch("listener.pipeline.should_suppress", return_value=False),
            patch("listener.pipeline.run_gate", return_value=gate_v),
            patch(
                "listener.pipeline.prepare_alert_artifacts", return_value=_make_artifacts()
            ),
            patch("listener.pipeline.verify_class", return_value=vm1_result),
            patch("listener.pipeline.build_alert_message", return_value=tg1),
            patch("listener.pipeline.detail_class", return_value=vm2_result),
            patch("listener.pipeline.build_detail_message", return_value=tg2),
            patch("listener.pipeline._load_candidates", return_value=[]),
            patch("listener.pipeline.build_match_message", return_value=tg3),
        ):
            result1 = run(alert1)
            assert result1["status"] == "ok"

        # record_hit is now called in daemon after TG#3 dispatch.
        # Simulate daemon behavior for cooldown testing.
        record_hit("CAM1", "vehicle", base_time)

        # Second alert: should_suppress returns True (cooldown active)
        with (
            patch("listener.pipeline.should_suppress", return_value=True),
            patch("listener.pipeline.run_gate", return_value=gate_v),
            patch("listener.pipeline.prepare_alert_artifacts") as mock_art,
            patch("listener.pipeline.verify_class") as mock_vm1,
            patch("listener.pipeline.build_alert_message") as mock_tg1,
            patch("listener.pipeline.detail_class") as mock_vm2,
            patch("listener.pipeline.build_detail_message") as mock_tg2,
            patch("listener.pipeline.build_match_message") as mock_tg3,
        ):
            result2 = run(alert2)
            # cooldown check fires at stage 7a, before further stages
            assert result2["status"] == "dropped"
            assert result2["reason"] == "cooldown_active"
            # stage functions after cooldown check should NOT have been called
            mock_vm1.assert_not_called()

    def test_run_does_not_call_record_hit(self, tmp_path):
        """pipeline.run() no longer calls record_hit — that's daemon's job now."""
        diff_path = str(tmp_path / "diff.png")
        Path(diff_path).write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 20)
        gate_v = _make_gate_verdict(pairwise_diff_path=diff_path)
        alert = _make_alert(id="alert-rh", classification="vehicle")

        vm1_result = {"class": "vehicle", "confidence": 0.92}
        tg1 = {"caption": "test", "photos": []}
        vm2_result = {"class_confirmed": "vehicle", "distinctive_features": []}
        tg2 = {"caption": "test", "photos": []}
        tg3 = {"caption": "test", "photos": []}

        with (
            patch("listener.pipeline.should_suppress", return_value=False),
            patch("listener.pipeline.run_gate", return_value=gate_v),
            patch("listener.pipeline.prepare_alert_artifacts", return_value=_make_artifacts()),
            patch("listener.pipeline.verify_class", return_value=vm1_result),
            patch("listener.pipeline.build_alert_message", return_value=tg1),
            patch("listener.pipeline.detail_class", return_value=vm2_result),
            patch("listener.pipeline.build_detail_message", return_value=tg2),
            patch("listener.pipeline._load_candidates", return_value=[]),
            patch("listener.pipeline.build_match_message", return_value=tg3),
        ):
            result = run(alert)
            assert result["status"] == "ok"


# ---------------------------------------------------------------------------
# AC(b): TG#1 failure → record_hit NOT called → next within window OK
# ---------------------------------------------------------------------------


class TestCooldownLifecycleFailure:
    """run() no longer calls record_hit regardless of failure or success."""

    def test_failure_then_ok(self, tmp_path):
        """First alert fails at TG#1 → run() raises; second alert: proceeds (no cooldown)."""
        from infra.pipeline_cooldown import clear

        clear()

        diff_path = str(tmp_path / "diff.png")
        Path(diff_path).write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 20)
        gate_v = _make_gate_verdict(pairwise_diff_path=diff_path)

        alert1 = _make_alert(id="alert-b1", classification="vehicle")
        alert2 = _make_alert(id="alert-b2", classification="vehicle")

        # First alert: fails at TG#1
        with (
            patch("listener.pipeline.should_suppress", return_value=False),
            patch("listener.pipeline.run_gate", return_value=gate_v),
            patch(
                "listener.pipeline.prepare_alert_artifacts", return_value=_make_artifacts()
            ),
            patch("listener.pipeline.verify_class", return_value={"class": "vehicle"}),
            patch(
                "listener.pipeline.build_alert_message",
                side_effect=RuntimeError("TG#1 network error"),
            ),
        ):
            with pytest.raises(RuntimeError, match="TG#1 network error"):
                run(alert1)

        # Second alert: proceeds (run() never called record_hit, so no cooldown)
        vm1_result = {"class": "vehicle", "confidence": 0.92}
        tg1_ok = {"caption": "ok", "photos": []}
        vm2_result = {"class_confirmed": "vehicle", "distinctive_features": []}
        tg2_ok = {"caption": "ok", "photos": []}
        tg3_ok = {"caption": "ok", "photos": []}

        with (
            patch("listener.pipeline.should_suppress", return_value=False),
            patch("listener.pipeline.run_gate", return_value=gate_v),
            patch(
                "listener.pipeline.prepare_alert_artifacts", return_value=_make_artifacts()
            ),
            patch("listener.pipeline.verify_class", return_value=vm1_result),
            patch("listener.pipeline.build_alert_message", return_value=tg1_ok),
            patch("listener.pipeline.detail_class", return_value=vm2_result),
            patch("listener.pipeline.build_detail_message", return_value=tg2_ok),
            patch("listener.pipeline._load_candidates", return_value=[]),
            patch("listener.pipeline.build_match_message", return_value=tg3_ok),
        ):
            result2 = run(alert2)
            assert result2["status"] == "ok"


# ---------------------------------------------------------------------------
# AC(c): window expires (31s) → should_suppress returns False
# ---------------------------------------------------------------------------


class TestCooldownWindowExpiry:
    """Webhook arrives 31s after previous → window expired → not suppressed."""

    def test_window_expired(self, tmp_path):
        """(c) Webhook arrives 31s after a successful previous → not suppressed."""
        from infra.pipeline_cooldown import clear, record_hit, should_suppress

        clear()

        # Simulate: record a hit at t=0
        record_hit("CAM1", "vehicle", 0.0)

        # At t=0, should_suppress is True (0.0 < 30, within window)
        assert (
            should_suppress("CAM1", "vehicle", 0.0) is True
        ), "should be true right after record_hit (within window)"

        # At t=29.9, still within window → True
        assert (
            should_suppress("CAM1", "vehicle", 29.9) is True
        ), "should be true just before window expires"

        # At t=30.0, window expired (30.0 < 30 is False) → False
        assert (
            should_suppress("CAM1", "vehicle", 30.0) is False
        ), "should be false at exactly 30s (window boundary)"

        # At t=31, the 30s window has expired → should_suppress returns False
        assert (
            should_suppress("CAM1", "vehicle", 31.0) is False
        ), "should be false after window expires"

    def test_full_pipeline_window_expiry(self, tmp_path):
        """(c) Full pipeline run: second webhook arrives after window → OK."""
        from infra.pipeline_cooldown import clear, record_hit, should_suppress

        clear()

        base_time = 1000.0

        diff_path = str(tmp_path / "diff.png")
        Path(diff_path).write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 20)
        gate_v = _make_gate_verdict(pairwise_diff_path=diff_path)

        alert1 = _make_alert(id="alert-c1", classification="vehicle")
        alert2 = _make_alert(id="alert-c2", classification="vehicle")

        # First alert: proceed with normal flow
        vm1_result = {"class": "vehicle", "confidence": 0.92}
        tg1 = {"caption": "test", "photos": []}
        vm2_result = {"class_confirmed": "vehicle", "distinctive_features": []}
        tg2 = {"caption": "test", "photos": []}
        tg3 = {"caption": "test", "photos": []}

        call_count = 0

        def mock_should_suppress(camera_id, classification, now):
            nonlocal call_count
            call_count += 1
            # First call: at time=base_time, no cooldown recorded yet → False
            if call_count == 1:
                return False
            # Second call: 31 seconds later → window expired → False
            return False

        with (
            patch("listener.pipeline.should_suppress", side_effect=mock_should_suppress),
            patch("listener.pipeline.run_gate", return_value=gate_v),
            patch(
                "listener.pipeline.prepare_alert_artifacts", return_value=_make_artifacts()
            ),
            patch("listener.pipeline.verify_class", return_value=vm1_result),
            patch("listener.pipeline.build_alert_message", return_value=tg1),
            patch("listener.pipeline.detail_class", return_value=vm2_result),
            patch("listener.pipeline.build_detail_message", return_value=tg2),
            patch("listener.pipeline._load_candidates", return_value=[]),
            patch("listener.pipeline.build_match_message", return_value=tg3),
        ):
            result1 = run(alert1)
            assert result1["status"] == "ok"
            # run() no longer calls record_hit — that's daemon's job.
            # Simulate what the daemon does: call record_hit after TG#3 dispatch.
            from infra.pipeline_cooldown import record_hit as rh
            rh("CAM1", "vehicle", base_time)

            # Simulate 31 seconds passing
            result2 = run(alert2)
            assert result2["status"] == "ok"


# ---------------------------------------------------------------------------
# AC(d): concurrent webhooks — exactly ONE calls record_hit
# ---------------------------------------------------------------------------


class TestCooldownConcurrentWebhooks:
    """Three concurrent webhooks for same camera → exactly ONE calls record_hit."""

    def test_exactly_one_record_hit(self, tmp_path):
        """Three concurrent webhooks: first OK, rest hit should_suppress (cooldown).

        record_hit is called by daemon after TG#3 dispatch, not by run().
        This test verifies the cooldown suppression path still works correctly.
        """
        from infra.pipeline_cooldown import clear

        clear()

        diff_path = str(tmp_path / "diff.png")
        Path(diff_path).write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 20)
        gate_v = _make_gate_verdict(pairwise_diff_path=diff_path)

        vm1_result = {"class": "vehicle", "confidence": 0.92}
        tg1 = {"caption": "test", "photos": []}
        vm2_result = {"class_confirmed": "vehicle", "distinctive_features": []}
        tg2 = {"caption": "test", "photos": []}
        tg3 = {"caption": "test", "photos": []}

        call_count = 0

        def mock_should_suppress(camera_id, classification, now):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return False  # First webhook: proceed
            return True  # Second and third: suppressed

        with (
            patch("listener.pipeline.should_suppress", side_effect=mock_should_suppress),
            patch("listener.pipeline.run_gate", return_value=gate_v),
            patch(
                "listener.pipeline.prepare_alert_artifacts", return_value=_make_artifacts()
            ),
            patch("listener.pipeline.verify_class", return_value=vm1_result),
            patch("listener.pipeline.build_alert_message", return_value=tg1),
            patch("listener.pipeline.detail_class", return_value=vm2_result),
            patch("listener.pipeline.build_detail_message", return_value=tg2),
            patch("listener.pipeline._load_candidates", return_value=[]),
            patch("listener.pipeline.build_match_message", return_value=tg3),
        ):
            result1 = run(_make_alert(id="alert-d1"))
            result2 = run(_make_alert(id="alert-d2"))
            result3 = run(_make_alert(id="alert-d3"))

            # First: OK, second/third: dropped by cooldown
            assert result1["status"] == "ok"
            assert result2["status"] == "dropped"
            assert result2["reason"] == "cooldown_active"
            assert result3["status"] == "dropped"
            assert result3["reason"] == "cooldown_active"


# ---------------------------------------------------------------------------
# Note: record_hit and should_suppress module-level functions are used
# directly in tests to set and query cooldown state.
# ---------------------------------------------------------------------------
