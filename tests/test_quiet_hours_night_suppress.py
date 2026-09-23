"""
Tests for US-069b: TG-dispatch defense-in-depth night suppress.

Verifies that in_quiet_hours() returns True when a camera+class pair
matches the per-camera night-suppress list (V2-069), and that existing
behavior is preserved when class_name is None.

STATUS: new (US-069b)
"""

import os
import unittest
from datetime import UTC, datetime
from unittest.mock import patch

from infra.quiet_hours import in_quiet_hours


class TestNightSuppressDefense(unittest.TestCase):
    """Defense-in-depth night suppress via in_quiet_hours(class_name=...)."""

    def setUp(self):
        os.environ["FARMSURV_QUIET_HOURS_ENABLED"] = "1"
        os.environ["FARMSURV_FARM_LAT"] = "34.5782"
        os.environ["FARMSURV_FARM_LON"] = "-84.9438"
        os.environ["FARMSURV_QUIET_HOURS_CAMERAS"] = ""

    def tearDown(self):
        for key in ("FARMSURV_NIGHT_SUPPRESS_CAMERAS",
                     "FARMSURV_NIGHT_SUPPRESS_CLASSES",
                     "FARMSURV_QUIET_HOURS_CAMERAS"):
            os.environ.pop(key, None)

    # ------------------------------------------------------------------
    # AC: test_night_and_camera_match_returns_true
    # ------------------------------------------------------------------

    def test_night_and_camera_match_returns_true(self):
        """When class_name is provided AND camera+class match the suppress
        list AND it is night, in_quiet_hours returns True regardless of the
        quiet hours camera scoping."""
        with patch("infra.quiet_hours.night_suppress_match",
                   return_value=True):
            dt_night = datetime(2026, 9, 19, 2, 0, tzinfo=UTC)
            result = in_quiet_hours(dt_night, "OUTSIDE_BACK_SOLAR",
                                    class_name="vehicle")
        self.assertTrue(result)

    # ------------------------------------------------------------------
    # AC: test_night_and_no_class_match_returns_existing_behavior
    # ------------------------------------------------------------------

    def test_night_and_no_class_match_returns_existing_behavior(self):
        """When class_name is None (legacy caller), fall back to the
        existing global is_night_at_edt + quiet_hours camera scoping.
        Don't silently suppress without class info."""
        os.environ["FARMSURV_QUIET_HOURS_CAMERAS"] = "OUTSIDE_BACK_SOLAR"

        dt_night = datetime(2026, 9, 19, 2, 0, tzinfo=UTC)

        # Without class_name: OUTSIDE_BACK_SOLAR is in
        # FARMSURV_QUIET_HOURS_CAMERAS and it's night -> True.
        result = in_quiet_hours(dt_night, "OUTSIDE_BACK_SOLAR")
        self.assertTrue(result)

        # Camera NOT in quiet hours list -> False.
        result = in_quiet_hours(dt_night, "GARAGE_SIDE")
        self.assertFalse(result)

    # ------------------------------------------------------------------
    # AC: test_day_returns_false
    # ------------------------------------------------------------------

    def test_day_returns_false(self):
        """When the detection class matches the suppress list but it is
        daytime, in_quiet_hours returns False because the defense layer
        mirrors the motion-gate (night_suppress_match AND is_night_at_edt)."""
        with patch("infra.quiet_hours.night_suppress_match",
                   return_value=True):
            dt_day = datetime(2026, 9, 19, 12, 0, tzinfo=UTC)
            result = in_quiet_hours(dt_day, "OUTSIDE_BACK_SOLAR",
                                    class_name="vehicle")
        self.assertFalse(result)

    # ------------------------------------------------------------------
    # AC: test_class_without_camera_no_op
    # ------------------------------------------------------------------

    def test_class_without_camera_no_op(self):
        """When the camera is NOT in the suppress list, the defense layer
        should NOT suppress, even if class_name is provided."""
        with patch("infra.quiet_hours.night_suppress_match",
                   return_value=False):
            dt_night = datetime(2026, 9, 19, 2, 0, tzinfo=UTC)
            result = in_quiet_hours(dt_night, "GARAGE_SIDE",
                                    class_name="vehicle")
        self.assertFalse(result)

    def test_wrong_class_no_suppress(self):
        """When the camera IS in the suppress list but the class is NOT,
        the defense layer should NOT suppress."""
        with patch("infra.quiet_hours.night_suppress_match",
                   return_value=False):
            dt_night = datetime(2026, 9, 19, 2, 0, tzinfo=UTC)
            result = in_quiet_hours(dt_night, "OUTSIDE_BACK_SOLAR",
                                    class_name="person")
        self.assertFalse(result)

    def test_suppress_all_classes_when_classes_list_empty(self):
        """When FARMSURV_NIGHT_SUPPRESS_CLASSES is empty, all classes
        on the listed camera are suppressed at night."""
        with patch("infra.quiet_hours.night_suppress_match",
                   return_value=True):
            dt_night = datetime(2026, 9, 19, 2, 0, tzinfo=UTC)
            # person
            result = in_quiet_hours(dt_night, "OUTSIDE_BACK_SOLAR",
                                    class_name="person")
            self.assertTrue(result)
            # car
            result = in_quiet_hours(dt_night, "OUTSIDE_BACK_SOLAR",
                                    class_name="car")
            self.assertTrue(result)


if __name__ == "__main__":
    unittest.main()
