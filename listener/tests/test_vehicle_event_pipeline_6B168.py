"""
test_vehicle_event_pipeline_6B168.py — Phase.168 regression test.

Bug fixed: match_stage() (and 4 sibling gatekeeper checks) compared
ctx.camera_name (the friendly webhook label, e.g. "Test Camera One")
against ctx.gatekeeper_cameras (a code-keyed frozenset, e.g. {"CAM5"}).
The membership test was always False, so every vehicle event silently
fell to "is not a gatekeeper — skipping match-alert path" and the
matcher never ran. Live symptom: vehicle events repeatedly triggered
zero alerts during a single in-field test, with every event landing
on the L0 fallback path.

Fix: AlertContext.camera_code is populated once at the listener driver
boundary (listener.py:_process_alert) via _code_for_camera(camera_name),
and all 4 gatekeeper membership checks in this module compare
camera_code (not camera_name) against gatekeeper_cameras.

This test pins the new contract:
  1. AlertContext carries a camera_code field populated at the boundary.
  2. match_stage uses camera_code, not camera_name, for the gatekeeper
     membership test — and the "is not a gatekeeper" log surfaces
     both fields so future drift is visible in production logs.
  3. infra/vision_queue.GATEKEEPER_CAMERAS + PHASE6A_ELIGIBLE_CAMERAS
     hold CAM{N} codes (NEW schema), not legacy friendly codes.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

_root = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_root))

import pytest

import vehicle_matcher  # noqa: F401
from listener.vehicle_event_pipeline import (
    AlertContext,
    match_stage,
)


# --- helpers ---------------------------------------------------------------

GATEKEEPER_FRIENDLY = "Test Camera One"
GATEKEEPER_CODE = "CAM5"
GATEKEEPER_SET = frozenset({"CAM1", "CAM2", "CAM3", "CAM4", "CAM5", "CAM6"})


def _make_ctx(
    *,
    camera_name: str,
    camera_code: str,
    is_vehicle_event: bool = True,
) -> AlertContext:
    """Build the smallest AlertContext that match_stage inspects."""
    return AlertContext(
        alert_id="6B168-test",
        camera_name=camera_name,
        camera_code=camera_code,
        timestamp="2026-08-31 14:00:00 EDT",
        event_type="vehicle" if is_vehicle_event else "person",
        rtsp_url="rtsp://test/ofs",
        output_dir="/tmp/6B168-test",
        is_vehicle_event=is_vehicle_event,
        known_vehicles=[],
        bot_token="test-token",
        chat_id="test-chat",
        api_url="http://127.0.0.1:8093/v1/chat/completions",
        gatekeeper_cameras=GATEKEEPER_SET,
    )


# --- Test 1: AlertContext carries camera_code -----------------------------


class TestAlertContextCameraCode:
    """AlertContext must accept and surface camera_code."""

    def test_camera_code_default_is_empty_string(self):
        """Phase.168: when camera_code is not explicitly set, the
        __post_init__ hook derives it from camera_name. Empty string
        only persists when camera_name is also empty."""
        ctx = AlertContext(
            alert_id="x",
            camera_name="",  # no name → no derivation possible
            timestamp="t",
            event_type="vehicle",
            rtsp_url="r",
            output_dir="/tmp",
            is_vehicle_event=True,
            known_vehicles=[],
            bot_token="t",
            chat_id="c",
            api_url="u",
            gatekeeper_cameras=frozenset(),
        )
        assert ctx.camera_code == ""

    def test_camera_code_not_derived_without_post_init(self):
        """Phase.168 (final): AlertContext deliberately does NOT
        auto-derive camera_code in __post_init__. The listener driver
        is the only legitimate construction site in production, and
        it always passes both camera_name AND camera_code explicitly.

        If a test fixture (or future code path) constructs AlertContext
        without camera_code, it stays empty — the caller is
        responsible for setting it. This prevents the silent-mutation
        bug class where a test mutates ctx.camera_name after
        construction and forgets to refresh ctx.camera_code.
        """
        ctx = AlertContext(
            alert_id="x",
            camera_name=GATEKEEPER_FRIENDLY,  # friendly from webhook
            timestamp="t",
            event_type="vehicle",
            rtsp_url="r",
            output_dir="/tmp",
            is_vehicle_event=True,
            known_vehicles=[],
            bot_token="t",
            chat_id="c",
            api_url="u",
            gatekeeper_cameras=frozenset(),
        )
        assert ctx.camera_code == "", (
            "without __post_init__ derivation, camera_code must stay empty"
        )

    def test_explicit_camera_code_wins(self):
        """If the caller passes camera_code explicitly, it must be used
        as-is (no auto-derivation to overwrite it)."""
        ctx = _make_ctx(
            camera_name=GATEKEEPER_FRIENDLY,
            camera_code="CAM_EXPLICIT",
        )
        assert ctx.camera_code == "CAM_EXPLICIT"

    def test_camera_code_populated_when_constructor_passes_it(self):
        ctx = _make_ctx(
            camera_name=GATEKEEPER_FRIENDLY,
            camera_code=GATEKEEPER_CODE,
        )
        assert ctx.camera_code == GATEKEEPER_CODE


# --- Test 2: match_stage uses camera_code (THE bug regression) -----------


class TestMatchStageUsesCameraCode:
    """The pre-fix bug: match_stage compared friendly name against
    code-keyed set → always False → "is not a gatekeeper" path."""

    def test_match_stage_does_not_log_skip_when_code_in_gatekeeper(self, caplog):
        """camera_code=CAM5 in gatekeeper set must NOT trigger the
        'is not a gatekeeper' log branch."""
        ctx = _make_ctx(
            camera_name=GATEKEEPER_FRIENDLY,  # friendly (the webhook shape)
            camera_code=GATEKEEPER_CODE,      # code (the membership key)
        )
        ctx.vision_result = None
        with caplog.at_level(logging.INFO):  # capture from all loggers
            try:
                match_stage(ctx)
            except Exception:
                pass

        skip_logs = [
            r for r in caplog.records
            if "is not a gatekeeper" in r.getMessage()
        ]
        assert skip_logs == [], (
            f"match_stage must not log 'is not a gatekeeper' when "
            f"camera_code={GATEKEEPER_CODE} is in gatekeeper_cameras; "
            f"got: {[r.getMessage() for r in skip_logs]}"
        )

    def test_match_stage_logs_skip_when_code_not_in_gatekeeper(self, caplog):
        """camera_code='CAM99' not in gatekeeper set → 'is not a
        gatekeeper' log must fire (proves the check still works for
        non-gatekeepers)."""
        ctx = _make_ctx(
            camera_name="Some Unknown Camera",
            camera_code="CAM99",  # not in GATEKEEPER_SET
        )
        with caplog.at_level(logging.INFO):  # capture from all loggers
            match_stage(ctx)

        skip_logs = [
            r for r in caplog.records
            if "is not a gatekeeper" in r.getMessage()
        ]
        assert len(skip_logs) == 1, (
            "Expected exactly one 'is not a gatekeeper' log when "
            "camera_code is not in gatekeeper set; "
            f"got {len(skip_logs)}"
        )
        msg = skip_logs[0].getMessage()
        assert "Some Unknown Camera" in msg, (
            f"Log must include camera_name (got: {msg!r})"
        )
        assert "CAM99" in msg, (
            f"Log must include camera_code (got: {msg!r})"
        )

    def test_non_vehicle_event_does_not_match(self):
        """The first guard in match_stage is is_vehicle_event; a
        non-vehicle event must early-return before the gatekeeper
        check. This is the pre-existing behavior, pinned here so
        the refactor doesn't break it."""
        ctx = _make_ctx(
            camera_name=GATEKEEPER_FRIENDLY,
            camera_code=GATEKEEPER_CODE,
            is_vehicle_event=False,
        )
        # Should silently return — no exception, no log.
        match_stage(ctx)


# --- Test 3: vision_queue sets use CAM{N} codes --------------------------


class TestVisionQueueEligibilitySets:
    """infra/vision_queue.GATEKEEPER_CAMERAS and PHASE6A_ELIGIBLE_CAMERAS
    must contain CAM{N} codes (matching the NEW cameras.env schema),
    not legacy friendly codes like 'OUTSIDE_FRONT_SOLAR'."""

    def test_gatekeeper_cameras_contains_cam5(self):
        from infra.vision_queue import GATEKEEPER_CAMERAS
        assert "CAM5" in GATEKEEPER_CAMERAS, (
            "Phase.168: GATEKEEPER_CAMERAS must contain CAM5 under "
            "the NEW schema. Pre-fix it contained only a single "
            "code, so submit()'s membership test never matched "
            f"multi-camera deployments. Actual: {sorted(GATEKEEPER_CAMERAS)}"
        )

    def test_gatekeeper_cameras_no_legacy_friendly_codes(self):
        from infra.vision_queue import GATEKEEPER_CAMERAS
        for legacy in ("OUTSIDE_FRONT_SOLAR", "FRONT", "BACK"):
            assert legacy not in GATEKEEPER_CAMERAS, (
                f"Phase.168: legacy friendly code {legacy!r} leaked "
                "into vision_queue.GATEKEEPER_CAMERAS — sets must be "
                "CAM{N} codes only."
            )

    def test_phase6a_eligible_cameras_no_legacy_friendly_codes(self):
        from infra.vision_queue import PHASE6A_ELIGIBLE_CAMERAS
        for legacy in ("OUTSIDE_FRONT_SOLAR", "FRONT", "BACK",
                       "OUTSIDE_FRONT_GARAGE", "OUTSIDE_FRONT_POWER",
                       "OUTSIDE_BACK_SOLAR"):
            assert legacy not in PHASE6A_ELIGIBLE_CAMERAS, (
                f"Phase.168: legacy friendly code {legacy!r} leaked "
                "into vision_queue.PHASE6A_ELIGIBLE_CAMERAS — sets must "
                "be CAM{N} codes only."
            )

    def test_gatekeeper_and_listener_sets_match(self):
        """All CAM{N} codes in listener.GATEKEEPER_CAMERAS must also be
        in vision_queue.GATEKEEPER_CAMERAS — no drift allowed."""
        try:
            from listener.listener import GATEKEEPER_CAMERAS as L
        except ImportError as e:
            pytest.skip(f"listener module not importable: {e}")
        from infra.vision_queue import GATEKEEPER_CAMERAS as Q
        assert L == Q, (
            f"Drift between listener.GATEKEEPER_CAMERAS ({sorted(L)}) "
            f"and vision_queue.GATEKEEPER_CAMERAS ({sorted(Q)}). "
            "These must stay in sync — see Phase.167 follow-up."
        )