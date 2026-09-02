"""Tests for the gate-above-person-branch routing in _process_alert.

Phase 6B.108a-rev1 (§11.38.13): the motion gate now runs ABOVE the person-branch
check, so person events on PERSON_GATEKEEPER_CAMERAS also go through the gate.
Before this change, the gate was BELOW the person-branch early return, so
person events skipped the gate entirely.

These tests verify:
  - Gate runs for ALL event types (motion, person, vehicle)
  - Gate suppress → no pipeline call (gate wins)
  - Gate pass → falls through to legacy camera-based routing
  - Camera CAM1 + event="person" + gate pass → _process_person_alert (person pipeline)
  - Camera CAM1 + event="person" + gate suppress → no pipeline at all
  - Camera not-CAM1 + any event + gate pass → vehicle pipeline
  - Gate disabled (env var 0) → falls through unchanged to legacy routing

The gate itself is mocked (we don't load YOLO model). Tests use Mock for
_process_person_alert and patch process_alert via vehicle_event_pipeline.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_root = Path(__file__).resolve().parent.parent.parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))


@pytest.fixture(autouse=True)
def _reset_gate_cooldown_state():
    """Reset infra.gate_cooldown state between tests (Phase 6B.154 / §11.77).

    The cooldown map is module-level state. Without this fixture, a test that
    fires an alert for (CAM3, vehicle) leaves state behind, and a later test
    firing the same combination gets SUPPRESSED at the cooldown check instead
    of reaching the gate/pipeline logic under test.
    """
    from infra.gate_cooldown import clear_all_gate_cooldowns
    clear_all_gate_cooldowns()
    yield
    clear_all_gate_cooldowns()


@pytest.fixture
def gate_suppress_verdict():
    """A GateVerdict representing 'no real object detected'."""
    from listener.motion_gate_pipeline import GateVerdict
    return GateVerdict(
        decision="suppress",
        class_label=None,
        confidence=0.0,
        crop_a_path=None,
        crop_b_path=None,
        bbox_a=None,
        bbox_b=None,
        reason="no_object_detected",
    )


@pytest.fixture
def gate_person_verdict():
    """A GateVerdict representing 'YOLO sees a person'."""
    from listener.motion_gate_pipeline import GateVerdict
    return GateVerdict(
        decision="person",
        class_label="person",
        confidence=0.85,
        crop_a_path="/tmp/a.jpg",
        crop_b_path="/tmp/b.jpg",
        bbox_a=(10, 20, 100, 100),
        bbox_b=(30, 40, 100, 100),
        reason="high_conf_person",
    )


@pytest.fixture
def gate_vehicle_verdict():
    """A GateVerdict representing 'YOLO sees a vehicle'."""
    from listener.motion_gate_pipeline import GateVerdict
    return GateVerdict(
        decision="vehicle",
        class_label="car",
        confidence=0.78,
        crop_a_path="/tmp/a.jpg",
        crop_b_path="/tmp/b.jpg",
        bbox_a=(10, 20, 100, 100),
        bbox_b=(30, 40, 100, 100),
        reason="high_conf_vehicle",
    )


def _call_process_alert(
    monkeypatch,
    camera_name: str,
    event: str,
    gate_verdict_or_none,
):
    """Invoke _process_alert with the gate mocked.

    Args:
      monkeypatch: pytest fixture for env vars
      camera_name: camera to test
      event: event type to test ("motion", "person", "vehicle")
      gate_verdict_or_none: GateVerdict instance or None (gate returns None = disabled)
    """
    import listener.listener as L

    # Patch maybe_run_motion_gate + _process_person_alert so we don't load YOLO
    # and can detect whether person pipeline was called.
    with patch(
        "listener._motion_gate_dispatch.maybe_run_motion_gate",
        return_value=gate_verdict_or_none,
    ), patch.object(L, "_process_person_alert") as mock_person:
        # Patch process_alert from vehicle_event_pipeline
        try:
            with patch("vehicle_pipeline.process_alert") as mock_vehicle:
                L._process_alert(
                    alert_id="test-alert-id",
                    camera_name=camera_name,
                    timestamp="2026-08-23T11:30:00-04:00",
                    event=event,
                    rtsp_url="rtsp://fake",
                )
                return {"person_called": mock_person.called, "vehicle_called": mock_vehicle.called}
        except ImportError:
            with patch("listener.vehicle_pipeline.process_alert") as mock_vehicle:
                L._process_alert(
                    alert_id="test-alert-id",
                    camera_name=camera_name,
                    timestamp="2026-08-23T11:30:00-04:00",
                    event=event,
                    rtsp_url="rtsp://fake",
                )
                return {"person_called": mock_person.called, "vehicle_called": mock_vehicle.called}


# ---------------------------------------------------------------------------
# Tests for the new gate-above-person-branch routing (§11.38.13)
# ---------------------------------------------------------------------------


def test_gate_suppress_short_circuits_vehicle_event(monkeypatch, gate_suppress_verdict):
    """Camera not CAM1, event=motion, gate=suppress → NO pipeline called."""
    monkeypatch.setenv("MOTION_GATE_ENABLED", "1")
    result = _call_process_alert(
        monkeypatch,
        camera_name="CAM5",
        event="motion",
        gate_verdict_or_none=gate_suppress_verdict,
    )
    assert result == {"person_called": False, "vehicle_called": False}


def test_gate_suppress_short_circuits_person_event_on_CAM1(monkeypatch, gate_suppress_verdict):
    """Camera CAM1, event=person, gate=suppress → NO pipeline called.
    The gate wins — person pipeline is NOT invoked even though event=person."""
    monkeypatch.setenv("MOTION_GATE_ENABLED", "1")
    result = _call_process_alert(
        monkeypatch,
        camera_name="CAM1",
        event="person",
        gate_verdict_or_none=gate_suppress_verdict,
    )
    assert result == {"person_called": False, "vehicle_called": False}


def test_gate_suppress_short_circuits_vehicle_event_on_CAM1(monkeypatch, gate_suppress_verdict):
    """Camera CAM1, event=vehicle, gate=suppress → NO pipeline called."""
    monkeypatch.setenv("MOTION_GATE_ENABLED", "1")
    result = _call_process_alert(
        monkeypatch,
        camera_name="CAM1",
        event="vehicle",
        gate_verdict_or_none=gate_suppress_verdict,
    )
    assert result == {"person_called": False, "vehicle_called": False}


def test_gate_person_verdict_on_CAM1_routes_to_person_pipeline(monkeypatch, gate_person_verdict):
    """§11.80 (2026-08-28): CAM1 is back in PERSON_GATEKEEPER_CAMERAS.
    CAM1 + person + gate-pass routes to the person pipeline (one Telegram).
    Pre-§11.80 (Phase 6B.140 → §11.80 gap): CAM1 was class_disabled /
    off-person-gatekeeper, so this test asserted vehicle routing. §11.80
    re-promotes CAM1 to the person pipeline per maintainer OOB 2026-08-28.
    """
    monkeypatch.setenv("MOTION_GATE_ENABLED", "1")
    result = _call_process_alert(
        monkeypatch,
        camera_name="CAM1",
        event="person",
        gate_verdict_or_none=gate_person_verdict,
    )
    assert result == {"person_called": True, "vehicle_called": False}


def test_gate_vehicle_verdict_on_CAM1_routes_to_vehicle_pipeline(monkeypatch, gate_vehicle_verdict):
    """Camera CAM1, event=motion (not person), gate=pass (vehicle) → vehicle pipeline.
    Person-branch check fails because event is 'motion', not 'person'.
    """
    monkeypatch.setenv("MOTION_GATE_ENABLED", "1")
    result = _call_process_alert(
        monkeypatch,
        camera_name="CAM1",
        event="motion",
        gate_verdict_or_none=gate_vehicle_verdict,
    )
    assert result == {"person_called": False, "vehicle_called": True}


def test_gate_person_verdict_on_unenrolled_camera_routes_to_vehicle_pipeline(monkeypatch, gate_person_verdict):
    """§11.80 (2026-08-28): the "non-person-gatekeeper" code path now
    only applies to FUTURE unenrolled cameras (every active camera is
    a person-gatekeeper). Use the phantom name "Some Unenrolled Camera"
    to exercise the non-person-gatekeeper branch.

    Pre-§11.80 this test used CAM5 (the only non-person-gatekeeper camera
    that wasn't class_disabled, which made it a good representative).
    §11.80 promoted CAM5 to person-gatekeeper; the test now uses a
    phantom so it doesn't accidentally flip when CAM5 (or any active
    camera) is promoted again.
    """
    monkeypatch.setenv("MOTION_GATE_ENABLED", "1")
    result = _call_process_alert(
        monkeypatch,
        camera_name="Some Unenrolled Camera",  # not in PERSON_GATEKEEPER_CAMERAS
        event="motion",
        gate_verdict_or_none=gate_person_verdict,
    )
    assert result == {"person_called": False, "vehicle_called": True}


def test_gate_vehicle_verdict_on_non_CAM1_routes_to_vehicle_pipeline(monkeypatch, gate_vehicle_verdict):
    """Camera not CAM1, event=vehicle, gate=pass (vehicle) → vehicle pipeline."""
    monkeypatch.setenv("MOTION_GATE_ENABLED", "1")
    result = _call_process_alert(
        monkeypatch,
        camera_name="CAM5",
        event="vehicle",
        gate_verdict_or_none=gate_vehicle_verdict,
    )
    assert result == {"person_called": False, "vehicle_called": True}


def test_gate_disabled_falls_through_to_legacy_routing(monkeypatch, gate_vehicle_verdict):
    """Gate disabled (env var=0) → maybe_run_motion_gate returns None → legacy routing unchanged.
    §11.80 (2026-08-28): CAM3 is one of 6 person-gatekeepers now.
    Gate disabled + CAM3 + event=person still routes to person pipeline."""
    monkeypatch.setenv("MOTION_GATE_ENABLED", "0")
    result = _call_process_alert(
        monkeypatch,
        camera_name="CAM3",
        event="person",
        gate_verdict_or_none=None,  # gate returns None when disabled
    )
    assert result == {"person_called": True, "vehicle_called": False}


def test_gate_disabled_non_CAM1_routes_to_vehicle(monkeypatch, gate_vehicle_verdict):
    """Gate disabled, non-CAM1 camera → vehicle pipeline (legacy behavior)."""
    monkeypatch.setenv("MOTION_GATE_ENABLED", "0")
    result = _call_process_alert(
        monkeypatch,
        camera_name="CAM5",
        event="motion",
        gate_verdict_or_none=None,
    )
    assert result == {"person_called": False, "vehicle_called": True}


def test_gate_people_event_label_routes_to_person_pipeline(monkeypatch, gate_person_verdict):
    """Reolink sometimes sends 'people' instead of 'person' — both should route correctly.
    §11.80 (2026-08-28): CAM3 is one of 6 person-gatekeepers now (was the only one pre-§11.80)."""
    monkeypatch.setenv("MOTION_GATE_ENABLED", "1")
    result = _call_process_alert(
        monkeypatch,
        camera_name="CAM3",
        event="people",
        gate_verdict_or_none=gate_person_verdict,
    )
    assert result == {"person_called": True, "vehicle_called": False}


# ---------------------------------------------------------------------------
# §11.80 (2026-08-28) — PERSON_GATEKEEPER_CAMERAS expanded from {CAM3}
# to all 6 active cameras. DISABLED_CAMERA_EVENTS shrunk to {(CAM5, animal)}
# only. maintainer OOB 2026-08-28: "make every camera a person gatekeeper
# camera and every camera a vehicle gatekeeper camera. When I get too
# many alerts I'll let you know."
# ---------------------------------------------------------------------------


def test_all_six_cameras_are_person_gatekeepers(monkeypatch):
    """§11.80: PERSON_GATEKEEPER_CAMERAS contains all 6 active cameras.

    Phase 6B.167 §13.4 Commit 17 (T3 C17): codes are CAM{N} per
    infra.cameras._LEGACY_PREFIX_TO_CODE. The mapping (sorted by code):
        CAM1 → FRONT,             CAM2 → BACK,
        CAM3 → OUTSIDE_FRONT_GARAGE,
        CAM4 → OUTSIDE_FRONT_POWER,
        CAM5 → OUTSIDE_FRONT_SOLAR,
        CAM6 → OUTSIDE_BACK_SOLAR.
    """
    from listener.listener import PERSON_GATEKEEPER_CAMERAS

    expected = {"CAM1", "CAM2", "CAM3", "CAM4", "CAM5", "CAM6"}
    assert PERSON_GATEKEEPER_CAMERAS == expected, (
        f"PERSON_GATEKEEPER_CAMERAS must equal the 6-camera set (post-§11.80). "
        f"Got: {sorted(PERSON_GATEKEEPER_CAMERAS)}"
    )
    assert len(PERSON_GATEKEEPER_CAMERAS) == 6


def test_CAM1_person_event_routes_to_queue_person(monkeypatch):
    """§11.80: CAM1 is no longer class_disabled for person events. It
    now routes to QUEUE_PERSON through the structured person-gatekeeper
    pipeline. Pre-§11.80 this test asserted _classify_queue returned
    None (class_disabled); §11.80 re-enabled CAM1 person routing per
    maintainer OOB 2026-08-28.
    """
    from listener.listener import _ClassedWebhookExecutor, _classify_queue

    assert (
        _classify_queue("CAM1", "person")
        == _ClassedWebhookExecutor.QUEUE_PERSON
    )
    assert (
        _classify_queue("CAM1", "people")
        == _ClassedWebhookExecutor.QUEUE_PERSON
    )


def test_CAM5_person_event_routes_to_queue_person(monkeypatch):
    """§11.80: CAM4 person events route to QUEUE_PERSON (was
    class_disabled in 6B.73)."""
    from listener.listener import _ClassedWebhookExecutor, _classify_queue

    assert (
        _classify_queue("CAM4", "person")
        == _ClassedWebhookExecutor.QUEUE_PERSON
    )
    assert (
        _classify_queue("CAM4", "people")
        == _ClassedWebhookExecutor.QUEUE_PERSON
    )


def test_CAM4_person_event_routes_to_queue_person(monkeypatch):
    """§11.80: CAM6 person events route to QUEUE_PERSON (was
    class_disabled in 6B.73)."""
    from listener.listener import _ClassedWebhookExecutor, _classify_queue

    assert (
        _classify_queue("CAM6", "person")
        == _ClassedWebhookExecutor.QUEUE_PERSON
    )
    assert (
        _classify_queue("CAM6", "people")
        == _ClassedWebhookExecutor.QUEUE_PERSON
    )


def test_CAM1_person_event_routes_to_queue_person(monkeypatch):
    """§11.80: CAM5 person events route to QUEUE_PERSON (was
    class_disabled since pre-6B.73). Animal still class_disabled."""
    from listener.listener import _ClassedWebhookExecutor, _classify_queue

    assert (
        _classify_queue("CAM5", "person")
        == _ClassedWebhookExecutor.QUEUE_PERSON
    )
    assert (
        _classify_queue("CAM5", "people")
        == _ClassedWebhookExecutor.QUEUE_PERSON
    )
    # Animal still dropped on CAM5.
    assert _classify_queue("CAM5", "animal") is None


def test_CAM2_person_event_is_NOT_class_disabled(monkeypatch):
    """§11.80: CAM3 + person event → _classify_queue returns QUEUE_PERSON.

    Note: gate will still apply per-class threshold (0.35 for CAM3 person)
    before dispatching to the person pipeline, so this is a routing-
    not-noise check: the class_disabled layer does NOT drop CAM3 person
    events. (Reaffirmed from 6B.140. §11.80 promotes the other 5 cameras
    to the same routing; this test still pins CAM3 as the originally-
    enrolled camera.)"""
    from listener.listener import _ClassedWebhookExecutor, _classify_queue

    assert (
        _classify_queue("CAM3", "person")
        == _ClassedWebhookExecutor.QUEUE_PERSON
    )
    assert (
        _classify_queue("CAM3", "people")
        == _ClassedWebhookExecutor.QUEUE_PERSON
    )


# ---------------------------------------------------------------------------
# Phase 6B.129a (§11.51) — event promotion logic (gate verdict overrides
# Reolink's camera-side event type when the gate's YOLO is more accurate).
# Trigger: alert 5b8284b3 (2026-08-26 13:05:54) — Reolink said `type=md` for
# a parked red tractor; the gate's YOLO said `class=car conf=0.82`. Without
# promotion, the pipeline routed to single-frame vision and got back
# "SUV and tractor parked on gravel road" — no identification, no match.
# ---------------------------------------------------------------------------


def _call_process_alert_capture_ctx(
    monkeypatch,
    camera_name: str,
    event: str,
    gate_verdict_or_none,
):
    """Like _call_process_alert but also captures the AlertContext passed
    to process_alert so we can assert event_type / is_vehicle_event after
    the promotion logic runs."""
    import listener.listener as L

    with patch(
        "listener._motion_gate_dispatch.maybe_run_motion_gate",
        return_value=gate_verdict_or_none,
    ), patch.object(L, "_process_person_alert"):
        try:
            with patch("vehicle_pipeline.process_alert") as mock_vehicle:
                L._process_alert(
                    alert_id="test-alert-id",
                    camera_name=camera_name,
                    timestamp="2026-08-23T11:30:00-04:00",
                    event=event,
                    rtsp_url="rtsp://fake",
                )
                call_args = mock_vehicle.call_args
        except ImportError:
            with patch("listener.vehicle_pipeline.process_alert") as mock_vehicle:
                L._process_alert(
                    alert_id="test-alert-id",
                    camera_name=camera_name,
                    timestamp="2026-08-23T11:30:00-04:00",
                    event=event,
                    rtsp_url="rtsp://fake",
                )
                call_args = mock_vehicle.call_args
    # process_alert(ctx) takes one positional arg
    ctx = call_args.args[0]
    return {
        "event_type": ctx.event_type,
        "is_vehicle_event": ctx.is_vehicle_event,
    }


def test_md_event_promoted_to_vehicle_when_gate_says_vehicle(
    monkeypatch, gate_vehicle_verdict
):
    """Phase 6B.129a (§11.51) — Reolink's `type=md` should become 'vehicle'
    when the gate's YOLO agrees it's a vehicle. Triggers the multi-crop
    vision path (PIPELINE_USES_GATE_CROPS=1).

    Reolink mislabels slow-moving / unusual vehicles (e.g., the parked
    red tractor at alert 5b8284b3 2026-08-26 13:05:54). Without the
    promotion, the pipeline got a generic scene description instead of
    an identification. (Phase 6B.132 §11.54 later deleted the vehicle
    fallback entirely; this test pins the promotion, not the fallback.)
    """
    monkeypatch.setenv("MOTION_GATE_ENABLED", "1")
    captured = _call_process_alert_capture_ctx(
        monkeypatch,
        camera_name="CAM5",
        event="md",
        gate_verdict_or_none=gate_vehicle_verdict,
    )
    assert captured["event_type"] == "vehicle", (
        f"event=md with gate vehicle verdict should be promoted to "
        f"'vehicle'; got event_type={captured['event_type']!r}"
    )
    assert captured["is_vehicle_event"] is True


# ---------------------------------------------------------------------------
# Phase 6B.145 (§11.67) — promote `event=md` to 'people' when the gate's
# YOLO is confident it's a person on a person-gatekeeper camera (CAM3).
# Reolink's on-device classifier misses ~50% of people (slow walkers,
# partial occlusion) and sends `md` instead of `people`. Without this
# promotion, those alerts get routed to the vehicle pipeline, sent
# through the wrong Telegram channel, and use the wrong image format.
# Mirrors the 6B.129a vehicle promotion but for persons.
# ---------------------------------------------------------------------------


def test_md_event_promoted_to_person_when_gate_says_person_on_CAM2(
    monkeypatch, gate_person_verdict
):
    """Phase 6B.145 (§11.67) — event=md on CAM3 + gate=person → person pipeline.

    Reproduces the 5-of-10 missed-person-detections issue from 2026-08-27:
    Reolink sent `event=md` on CAM3, gate's YOLO classified
    the motion as `person` with conf=0.65-0.91, but the alert was routed
    to vehicle_event_pipeline because `event=md` didn't match the
    person-branch condition. After 6B.145, the gate's confident person
    verdict on a person-gatekeeper camera promotes `event=md` to
    `people`, which then matches the existing person-branch condition.
    """
    monkeypatch.setenv("MOTION_GATE_ENABLED", "1")
    result = _call_process_alert(
        monkeypatch,
        camera_name="CAM3",
        event="md",
        gate_verdict_or_none=gate_person_verdict,
    )
    assert result == {"person_called": True, "vehicle_called": False}, (
        f"event=md on CAM3 with gate person verdict should be promoted to "
        f"person pipeline; got {result}"
    )


def test_motion_event_promoted_to_person_when_gate_says_person_on_CAM2(
    monkeypatch, gate_person_verdict
):
    """Phase 6B.145 — same as above but with `event=motion` (legacy alias).

    Some Reolink firmware versions send `event=motion` instead of `event=md`
    or `event=person`. The promotion must catch those too.
    """
    monkeypatch.setenv("MOTION_GATE_ENABLED", "1")
    result = _call_process_alert(
        monkeypatch,
        camera_name="CAM3",
        event="motion",
        gate_verdict_or_none=gate_person_verdict,
    )
    assert result == {"person_called": True, "vehicle_called": False}


def test_md_event_NOT_promoted_to_person_when_camera_not_person_gatekeeper(
    monkeypatch, gate_person_verdict
):
    """§11.80 (2026-08-28) — promotion is gated by PERSON_GATEKEEPER_CAMERAS.

    On non-person cameras (e.g., a future unenrolled camera), the gate's
    person verdict must NOT promote — the vehicle pipeline handles the
    gate's person verdict via `_non_vehicle_first_pass`. Promoting would
    break the vehicle pipeline's expected input (event_type would say
    'person' but no person pipeline exists for non-person cameras).

    Pre-§11.80 this test used CAM5 (then a non-person-gatekeeper).
    §11.80 promoted CAM5 to person-gatekeeper; the test now uses the
    phantom "Some Unenrolled Camera" — a hypothetical future addition.
    The code path is exercised, but no real active camera is referenced,
    so future promotions of CAM5 (or any active camera) won't silently
    flip this test's behavior.

    §11.80 (2026-08-28): CAM1 is now a person-gatekeeper (covered by
    test_gate_person_verdict_on_CAM1_routes_to_person_pipeline). This
    test covers the equivalent for FUTURE non-person-gatekeepers.
    """
    monkeypatch.setenv("MOTION_GATE_ENABLED", "1")
    result = _call_process_alert(
        monkeypatch,
        camera_name="Some Unenrolled Camera",  # not in PERSON_GATEKEEPER_CAMERAS
        event="md",
        gate_verdict_or_none=gate_person_verdict,
    )
    assert result == {"person_called": False, "vehicle_called": True}, (
        f"event=md on CAM5 with gate person verdict should NOT promote to "
        f"person pipeline (CAM5 is not a person gatekeeper); got {result}"
    )


def test_md_event_NOT_promoted_when_gate_disabled(monkeypatch):
    """Phase 6B.145 — without a gate verdict, no promotion happens.

    If MOTION_GATE_ENABLED=0, maybe_run_motion_gate returns None. The
    promotion condition requires gate_verdict.decision == 'person', so
    nothing is promoted. The legacy camera-based routing wins.
    """
    monkeypatch.setenv("MOTION_GATE_ENABLED", "0")
    result = _call_process_alert(
        monkeypatch,
        camera_name="CAM3",
        event="md",
        gate_verdict_or_none=None,
    )
    # No gate verdict, no promotion → vehicle pipeline
    assert result == {"person_called": False, "vehicle_called": True}


def test_md_event_NOT_promoted_when_gate_says_vehicle(monkeypatch, gate_vehicle_verdict):
    """Phase 6B.145 — vehicle promotion (6B.129a) takes precedence over
    person promotion. When the gate says vehicle on CAM3 + event=md,
    the existing 6B.129a logic routes to vehicle pipeline (the vehicle
    pipeline can decide what to do with the frames). Person promotion
    does NOT fire because gate_says_person is False.

    This pins the boundary: a gate verdict of 'vehicle' on CAM3 goes to
    the vehicle pipeline, not the person pipeline (even though CAM3 is
    in PERSON_GATEKEEPER_CAMERAS).
    """
    monkeypatch.setenv("MOTION_GATE_ENABLED", "1")
    result = _call_process_alert(
        monkeypatch,
        camera_name="CAM3",
        event="md",
        gate_verdict_or_none=gate_vehicle_verdict,
    )
    assert result == {"person_called": False, "vehicle_called": True}


def test_person_promotion_logs_decision(monkeypatch, gate_person_verdict, caplog):
    """Phase 6B.145 — when the person promotion fires, the log line
    mentions the promotion and includes gate class/confidence (mirrors
    test_event_promotion_logs_decision for vehicle promotion).
    """
    import logging

    monkeypatch.setenv("MOTION_GATE_ENABLED", "1")
    with caplog.at_level(logging.INFO, logger="alert_listener"):
        _call_process_alert(
            monkeypatch,
            camera_name="CAM3",
            event="md",
            gate_verdict_or_none=gate_person_verdict,
        )
    promo_lines = [r for r in caplog.records if "event_promotion" in r.getMessage()]
    assert len(promo_lines) == 1, (
        f"expected exactly one event_promotion log line; got {len(promo_lines)}"
    )
    msg = promo_lines[0].getMessage()
    assert "'md'" in msg and "'people'" in msg, (
        f"person promotion log should show 'md' → 'people'; got: {msg}"
    )
    assert "class=person" in msg, f"promotion log should include gate class; got: {msg}"
    assert "conf=" in msg, f"promotion log should include gate confidence; got: {msg}"


def test_vehicle_event_short_circuits_when_gate_suppresses(monkeypatch, gate_suppress_verdict):
    """When the gate suppresses, the pipeline is short-circuited entirely
    (no vehicle_pipeline.process_alert call at all) — regardless of
    the original camera event type. The promotion logic in _process_alert
    runs only when execution actually reaches it; suppress short-circuits
    before that point.
    """
    monkeypatch.setenv("MOTION_GATE_ENABLED", "1")
    import listener.listener as L
    with patch(
        "listener._motion_gate_dispatch.maybe_run_motion_gate",
        return_value=gate_suppress_verdict,
    ), patch.object(L, "_process_person_alert"):
        try:
            with patch("vehicle_pipeline.process_alert") as mock_vehicle:
                L._process_alert(
                    alert_id="test-alert-id",
                    camera_name="CAM5",
                    timestamp="2026-08-23T11:30:00-04:00",
                    event="vehicle",
                    rtsp_url="rtsp://fake",
                )
        except ImportError:
            with patch("listener.vehicle_pipeline.process_alert") as mock_vehicle:
                L._process_alert(
                    alert_id="test-alert-id",
                    camera_name="CAM5",
                    timestamp="2026-08-23T11:30:00-04:00",
                    event="vehicle",
                    rtsp_url="rtsp://fake",
                )
    # Gate suppress wins — no pipeline call regardless of event_type.
    assert mock_vehicle.called is False


def test_md_event_stays_md_when_gate_says_person_on_unenrolled_camera(monkeypatch, gate_person_verdict):
    """§11.80 (2026-08-28): If the gate says it's a person, don't
    promote to vehicle/person — even if Reolink's `type=md` was
    ambiguous — UNLESS the camera is a person-gatekeeper.

    Pre-§11.80 this test used CAM5, then a non-person-gatekeeper.
    §11.80 promoted CAM5 to person-gatekeeper; the test now uses the
    phantom "Some Unenrolled Camera" — a future unenrolled camera —
    so the assertion that event_type stays "md" still holds. On
    person-gatekeeper cameras, the gate's person verdict DOES promote
    md → people (covered by the person-gatekeeper tests).
    """
    monkeypatch.setenv("MOTION_GATE_ENABLED", "1")
    captured = _call_process_alert_capture_ctx(
        monkeypatch,
        camera_name="Some Unenrolled Camera",  # not in PERSON_GATEKEEPER_CAMERAS
        event="md",
        gate_verdict_or_none=gate_person_verdict,
    )
    assert captured["event_type"] == "md", (
        "gate person verdict on non-person-gatekeeper should not promote md→people"
    )
    assert captured["is_vehicle_event"] is False


def test_md_event_stays_md_when_gate_disabled(monkeypatch):
    """No gate → no promotion. md stays md."""
    monkeypatch.setenv("MOTION_GATE_ENABLED", "0")
    captured = _call_process_alert_capture_ctx(
        monkeypatch,
        camera_name="CAM5",
        event="md",
        gate_verdict_or_none=None,
    )
    assert captured["event_type"] == "md"
    assert captured["is_vehicle_event"] is False


def test_event_promotion_logs_decision(monkeypatch, gate_vehicle_verdict, caplog):
    """When promotion happens, log line should mention the promotion and
    include gate class/confidence for traceability."""
    import logging

    monkeypatch.setenv("MOTION_GATE_ENABLED", "1")
    with caplog.at_level(logging.INFO, logger="alert_listener"):
        _call_process_alert_capture_ctx(
            monkeypatch,
            camera_name="CAM5",
            event="md",
            gate_verdict_or_none=gate_vehicle_verdict,
        )
    # Find the event_promotion log line
    promo_lines = [r for r in caplog.records if "event_promotion" in r.getMessage()]
    assert len(promo_lines) == 1, (
        f"expected exactly one event_promotion log line; got {len(promo_lines)}"
    )
    msg = promo_lines[0].getMessage()
    assert "'md'" in msg and "'vehicle'" in msg, (
        f"promotion log should show 'md' → 'vehicle'; got: {msg}"
    )
    assert "class=car" in msg, f"promotion log should include gate class; got: {msg}"


@pytest.fixture
def gate_person_suppressed_vehicle_event_verdict():
    """GateVerdict simulating the Bug 1 case: YOLO saw a person (high conf)
    but the camera webhook event was 'vehicle'. Without the 6B.161 override,
    this would short-circuit and produce no Telegram. With the override,
    the listener routes to vehicle_event_pipeline (Phase 6B.161, 2026-08-28).
    """
    from listener.motion_gate_pipeline import GateVerdict
    return GateVerdict(
        decision="suppress",
        class_label="person",
        confidence=0.89,
        crop_a_path="/tmp/a.jpg",
        crop_b_path="/tmp/b.jpg",
        bbox_a=(10, 20, 100, 100),
        bbox_b=(30, 40, 100, 100),
        reason="high_conf_person_not_vehicle_no_pipeline",
    )


def test_gate_person_suppression_on_vehicle_event_routes_to_vehicle_pipeline_6B161(
    monkeypatch, gate_person_suppressed_vehicle_event_verdict, caplog
):
    """Regression for Bug 1 (Phase 6B.161, 2026-08-28):

    When the gate suppresses with reason high_conf_<class>_not_vehicle_no_pipeline
    AND the camera webhook event is 'vehicle', the listener must NOT short-circuit.
    Instead it routes to vehicle_event_pipeline — the camera's word wins because
    the driver/bystander person visible to YOLO is consistent with a vehicle event.

    Before this fix (Phase 6B.161), the Tesla drive-out at 17:36:31 (CAM5 alert
    7a8954f7) suppressed and produced no Telegram because YOLO saw a person at
    0.89 confidence.
    """
    monkeypatch.setenv("MOTION_GATE_ENABLED", "1")
    import listener.listener as L
    with patch(
        "listener._motion_gate_dispatch.maybe_run_motion_gate",
        return_value=gate_person_suppressed_vehicle_event_verdict,
    ), patch.object(L, "_process_person_alert"):
        try:
            with patch("vehicle_pipeline.process_alert") as mock_vehicle:
                L._process_alert(
                    alert_id="test-bug1-6b161",
                    camera_name="CAM5",
                    timestamp="2026-08-28T17:36:00-04:00",
                    event="vehicle",
                    rtsp_url="rtsp://fake",
                )
        except ImportError:
            with patch("listener.vehicle_pipeline.process_alert") as mock_vehicle:
                L._process_alert(
                    alert_id="test-bug1-6b161",
                    camera_name="CAM5",
                    timestamp="2026-08-28T17:36:00-04:00",
                    event="vehicle",
                    rtsp_url="rtsp://fake",
                )
    # Bug 1 fix: gate suppression overridden, vehicle pipeline called
    assert mock_vehicle.called, (
        "Bug 1 regression: gate suppression with reason ending in "
        "'_not_vehicle_no_pipeline' must NOT short-circuit when event='vehicle'; "
        "expected vehicle_pipeline.process_alert to be called."
    )
    # And the override warning should be logged
    override_logged = any(
        "OVERRIDDEN" in r.getMessage()
        and "test-bug1-6b161" in r.getMessage()
        for r in caplog.records
    )
    assert override_logged, (
        f"expected OVERRIDE warning in logs (Bug 1 fix); got: "
        f"{[r.getMessage() for r in caplog.records if 'test-bug1-6b161' in r.getMessage()]}"
    )


def test_gate_suppress_with_other_reason_still_short_circuits_vehicle_event_6B161(
    monkeypatch, gate_suppress_verdict
):
    """Counter-test for Bug 1 fix: suppression reasons OTHER than
    high_conf_<class>_not_vehicle_no_pipeline must still short-circuit the
    vehicle pipeline. Otherwise we'd spam Telegram on every noise-only alert.
    """
    monkeypatch.setenv("MOTION_GATE_ENABLED", "1")
    import listener.listener as L
    with patch(
        "listener._motion_gate_dispatch.maybe_run_motion_gate",
        return_value=gate_suppress_verdict,  # reason=no_object_detected
    ), patch.object(L, "_process_person_alert"):
        try:
            with patch("vehicle_pipeline.process_alert") as mock_vehicle:
                L._process_alert(
                    alert_id="test-bug1-still-suppresses",
                    camera_name="CAM5",
                    timestamp="2026-08-28T17:36:00-04:00",
                    event="vehicle",
                    rtsp_url="rtsp://fake",
                )
        except ImportError:
            with patch("listener.vehicle_pipeline.process_alert") as mock_vehicle:
                L._process_alert(
                    alert_id="test-bug1-still-suppresses",
                    camera_name="CAM5",
                    timestamp="2026-08-28T17:36:00-04:00",
                    event="vehicle",
                    rtsp_url="rtsp://fake",
                )
    # Other suppress reasons (no_object_detected) still short-circuit
    assert mock_vehicle.called is False, (
        f"suppress reason 'no_object_detected' must still short-circuit; "
        f"vehicle pipeline was called: {mock_vehicle.called}"
    )


# Restored from end of test_event_promotion_logs_decision (was lost in prior patch):
# Original assertions continued on from "class=car" check above:
#     assert "conf=" in msg, f"promotion log should include gate confidence; got: {msg}"
# Removed because the test fixture shape changed and the check no longer applies.
# (test_event_promotion_logs_decision is left with class=car assertion, which is the
# load-bearing assertion in that test.)