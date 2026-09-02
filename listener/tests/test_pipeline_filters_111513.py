"""Tests for listener.pipeline_filters — §11.115.13.

Two filters, both property-wide, both pre-cascade:

1. PipelineCooldown — 15-minute throttle per class.
   - Person cooldown arms ONLY on a matcher hit (face match).
   - Animal cooldown arms on ANY matcher result (matched OR unmatched).
   - Vehicle + other classes pass through (no cooldown state for them).
   - Window is 15 minutes from the last hit per class.

2. is_vehicle_allowed + VEHICLE_CAMERAS_ALLOWLIST — camera allowlist.
   - Vehicle class events are matched ONLY on cameras in the
     allowlist. All other cameras drop silently after Qwen call 1.
   - Other classes are unaffected by the allowlist.
"""
from __future__ import annotations

import time

import pytest

from listener.pipeline_filters import (
    ANIMAL_COOLDOWN_SECONDS,
    PERSON_COOLDOWN_SECONDS,
    VEHICLE_CAMERAS_ALLOWLIST,
    VEHICLE_CAMERAS_ALLOWLIST_FRIENDLY,
    PipelineCooldown,
    is_vehicle_allowed,
)


class TestPipelineCooldownVehicleOtherPassthrough:
    """Vehicle and other classes are NEVER cooldowned.

    Reason: vehicle motion is short-lived (park/leave); vehicle matching
    is also scoped to 2 cameras, not property-wide. `other` class sends
    no Telegram anyway. Neither needs the property-wide throttle.
    """

    def test_vehicle_never_suppressed(self) -> None:
        cd = PipelineCooldown()
        # Record many hits; vehicle should never be in cooldown state
        # and should_suppress should always return False.
        for _ in range(5):
            cd.record_hit("vehicle", matcher_hit=True)
        suppress, reason = cd.should_suppress("vehicle")
        assert suppress is False
        assert reason == "no_cooldown_for_class"

    def test_other_never_suppressed(self) -> None:
        cd = PipelineCooldown()
        for _ in range(5):
            cd.record_hit("other", matcher_hit=True)
        suppress, reason = cd.should_suppress("other")
        assert suppress is False
        assert reason == "no_cooldown_for_class"

    def test_unknown_class_never_suppressed(self) -> None:
        """Defensive: any class outside {person, animal} passes through."""
        cd = PipelineCooldown()
        cd.record_hit("person", matcher_hit=True)
        # Now check a bogus class
        suppress, reason = cd.should_suppress("bogus")
        assert suppress is False
        assert reason == "no_cooldown_for_class"


class TestPipelineCooldownPerson:
    """Person cooldown is property-wide AND gated on matcher_hit."""

    def test_first_event_passes(self) -> None:
        cd = PipelineCooldown()
        suppress, reason = cd.should_suppress("person")
        assert suppress is False
        assert reason == "no_prior_hit"

    def test_hit_arms_cooldown(self) -> None:
        cd = PipelineCooldown()
        # Matcher hit (face match) → cooldown arms
        cd.record_hit("person", matcher_hit=True)
        suppress, reason = cd.should_suppress("person")
        assert suppress is True
        assert reason.startswith("cooldown_active_")

    def test_no_hit_does_not_arm(self) -> None:
        """Person cooldown must NOT arm on no-face (matcher_hit=False)."""
        cd = PipelineCooldown()
        cd.record_hit("person", matcher_hit=False)
        suppress, reason = cd.should_suppress("person")
        assert suppress is False
        assert reason == "no_prior_hit"

    def test_window_15_minutes(self) -> None:
        """Window is exactly 15 minutes (900 seconds)."""
        assert PERSON_COOLDOWN_SECONDS == 15 * 60

    def test_expires_after_15_minutes(self) -> None:
        """After the window passes, the class is no longer cooldowned."""
        cd = PipelineCooldown()
        t0 = 1_000_000.0
        cd.record_hit("person", matcher_hit=True, now_epoch=t0)
        # Still active at t + 14:59
        suppress, _ = cd.should_suppress("person", now_epoch=t0 + 14 * 60 + 59)
        assert suppress is True
        # Expires at t + 15:00 (exclusive boundary — > window)
        suppress, reason = cd.should_suppress(
            "person", now_epoch=t0 + 15 * 60 + 1
        )
        assert suppress is False
        assert reason == "cooldown_expired"


class TestPipelineCooldownAnimal:
    """Animal cooldown is property-wide, arms on ANY matcher result."""

    def test_first_event_passes(self) -> None:
        cd = PipelineCooldown()
        suppress, reason = cd.should_suppress("animal")
        assert suppress is False
        assert reason == "no_prior_hit"

    def test_match_verdict_arms(self) -> None:
        """AnimalMatchVerdict (known animal identified)."""
        cd = PipelineCooldown()
        cd.record_hit("animal", matcher_hit=True)
        suppress, _ = cd.should_suppress("animal")
        assert suppress is True

    def test_animal_no_match_arms(self) -> None:
        """AnimalNoMatch (saw an animal, but not in registry).

        Per maintainer 2026-09-02 PM "once I see an animal": any detection
        arms the cooldown, even if the matcher returned NoMatch.
        """
        cd = PipelineCooldown()
        # Note matcher_hit=False, but record_hit should still arm
        cd.record_hit("animal", matcher_hit=False)
        suppress, reason = cd.should_suppress("animal")
        assert suppress is True
        assert reason.startswith("cooldown_active_")

    def test_window_15_minutes(self) -> None:
        """Animal uses same 15-minute window as person."""
        assert ANIMAL_COOLDOWN_SECONDS == 15 * 60


class TestPipelineCooldownPropertyWide:
    """The cooldown is property-wide, NOT per-camera.

    Per maintainer 2026-09-02 PM: "I want to cool down on all cameras
    during that 15 minute period." A hit on camera A suppresses a
    follow-up event on camera B.
    """

    def test_hit_on_one_camera_suppresses_all(self) -> None:
        cd = PipelineCooldown()
        cd.record_hit("person", matcher_hit=True)
        # Same instance, different "cameras" — should still suppress
        for cam in ["Front Porch", "Driveway", "Back Yard"]:
            suppress, _ = cd.should_suppress("person")  # no per-cam arg
            assert suppress is True

    def test_class_independence(self) -> None:
        """Person hit does not suppress animal, and vice versa."""
        cd = PipelineCooldown()
        cd.record_hit("person", matcher_hit=True)
        suppress_person, _ = cd.should_suppress("person")
        suppress_animal, _ = cd.should_suppress("animal")
        assert suppress_person is True
        assert suppress_animal is False


class TestPipelineCooldownTimeInjection:
    """Time is injectable via now_epoch for deterministic testing."""

    def test_time_injection_now(self) -> None:
        """Default now_epoch=time.time() is used when not injected."""
        cd = PipelineCooldown()
        # Hit recorded NOW
        cd.record_hit("person", matcher_hit=True)
        # Immediately queried — should suppress
        suppress, _ = cd.should_suppress("person")
        assert suppress is True

    def test_backdated_hit_does_not_suppress_future(self) -> None:
        """A hit 1 hour ago does NOT suppress right now."""
        cd = PipelineCooldown()
        old = time.time() - 3600
        cd.record_hit("person", matcher_hit=True, now_epoch=old)
        # Query at "now" — should NOT suppress
        suppress, reason = cd.should_suppress("person", now_epoch=time.time())
        assert suppress is False
        assert reason == "cooldown_expired"


class TestVehicleCameraAllowlist:
    """is_vehicle_allowed + VEHICLE_CAMERAS_ALLOWLIST contract."""

    def test_allowlist_is_frozenset_of_friendly_names(self) -> None:
        """Allowlist is a frozenset of camera friendly names."""
        assert isinstance(VEHICLE_CAMERAS_ALLOWLIST, frozenset)
        # Each member should be a string
        for cam in VEHICLE_CAMERAS_ALLOWLIST:
            assert isinstance(cam, str)

    def test_allowlist_has_exactly_two_cameras(self) -> None:
        """Per design (2026-09-02): vehicle matching on exactly 2 cameras."""
        assert len(VEHICLE_CAMERAS_ALLOWLIST) == 2

    def test_allowlist_contains_front_and_back_solar(self) -> None:
        """Allowlist resolves to the two solar cameras (either as CAM codes
        when infra.cameras.code_for() can resolve, or as friendly names when
        cameras.env is missing — public-build fallback).
        """
        codes = VEHICLE_CAMERAS_ALLOWLIST
        friendly = VEHICLE_CAMERAS_ALLOWLIST_FRIENDLY
        # Two-camera invariant
        assert len(codes) == 2
        # Each friendly name must appear in the resolved set OR map to it
        # (CAM5 ↔ "Outside Front Solar", CAM6 ↔ "Outside Back Solar").
        solar_cams = {"CAM5", "Outside Front Solar"}
        solar_back = {"CAM6", "Outside Back Solar"}
        assert codes & solar_cams, f"OFS not in {codes}"
        assert codes & solar_back, f"OBS not in {codes}"
        # Friendly set is always the source of truth
        assert "Outside Front Solar" in friendly
        assert "Outside Back Solar" in friendly

    def test_allowlist_does_not_contain_other_cameras(self) -> None:
        names = VEHICLE_CAMERAS_ALLOWLIST
        # The other 4 cameras must NOT be in the allowlist
        for cam in ["Front Porch", "Driveway", "Back Yard", "Garage"]:
            assert cam not in names

    @pytest.mark.parametrize(
        "camera_name, expected",
        [
            ("Outside Front Solar", True),
            ("Outside Back Solar", True),
            ("Front Porch", False),
            ("Driveway", False),
            ("Back Yard", False),
            ("Garage", False),
        ],
    )
    def test_is_vehicle_allowed_parametrized(
        self, camera_name: str, expected: bool
    ) -> None:
        assert is_vehicle_allowed(camera_name) is expected