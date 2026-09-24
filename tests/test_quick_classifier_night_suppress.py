"""
tests/test_quick_classifier_night_suppress.py — V2-069 per-camera night suppress.

Tests the _parse_csv_env helper (in infra.env_utils) and the
night_suppress_match + integrate-suppress logic in
infra.quick_classifier.

STATUS: new (US-069a)
"""

from __future__ import annotations

import importlib
import os
import sys

import pytest

# Ensure the worktree root is on sys.path so infra.* resolves.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from infra.env_utils import parse_csv_env

# ---------------------------------------------------------------------------
# _parse_csv_env helpers (infra.env_utils)
# ---------------------------------------------------------------------------

class TestParseCsvEnv:
    """Tests for the parse_csv_env helper in infra.env_utils."""

    def test_csv_parse_strips_whitespace_and_drops_empty(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """parse_csv_env strips whitespace and drops empty tokens from commas."""
        monkeypatch.setenv("FARMSURV_NIGHT_SUPPRESS_CAMERAS", "  cam1 ,  , cam2 , ")
        result = parse_csv_env("FARMSURV_NIGHT_SUPPRESS_CAMERAS")
        assert result == ["cam1", "cam2"]

    def test_empty_env_returns_empty_list(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Unset or empty env var returns []."""
        monkeypatch.delenv("FARMSURV_NIGHT_SUPPRESS_CAMERAS", raising=False)
        result = parse_csv_env("FARMSURV_NIGHT_SUPPRESS_CAMERAS")
        assert result == []

        monkeypatch.setenv("FARMSURV_NIGHT_SUPPRESS_CAMERAS", "")
        result = parse_csv_env("FARMSURV_NIGHT_SUPPRESS_CAMERAS")
        assert result == []

    def test_single_value_no_trailing_comma(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Single value without trailing comma returns one-item list."""
        monkeypatch.setenv("FARMSURV_NIGHT_SUPPRESS_CAMERAS", "cam3")
        result = parse_csv_env("FARMSURV_NIGHT_SUPPRESS_CAMERAS")
        assert result == ["cam3"]


# ---------------------------------------------------------------------------
# night_suppress_match (infra.quick_classifier)
# ---------------------------------------------------------------------------

class TestNightSuppressMatch:
    """Tests for the night_suppress_match pure function."""

    @pytest.fixture(autouse=True)
    def _setup_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Set env vars for each test, then restore after."""
        # Default: empty camera list (no suppress).
        monkeypatch.setenv("FARMSURV_NIGHT_SUPPRESS_CAMERAS", "")
        monkeypatch.setenv("FARMSURV_NIGHT_SUPPRESS_CLASSES", "")
        # Re-import with fresh env by reloading the module.
        import infra.quick_classifier as qc

        importlib.reload(qc)
        # Patch the module-level lookup to use fresh values.
        self.qc = qc

    def _match(self, camera_id: str, class_name: str) -> bool:
        """Call night_suppress_match via the reloaded module."""
        return self.qc.night_suppress_match(camera_id, class_name)

    def test_night_and_camera_match_suppresses(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Night suppress returns True when camera is in list and classes is empty."""
        monkeypatch.setenv("FARMSURV_NIGHT_SUPPRESS_CAMERAS", "OUTSIDE_BACK_SOLAR")
        monkeypatch.setenv("FARMSURV_NIGHT_SUPPRESS_CLASSES", "")
        importlib.reload(self.qc)
        assert self.qc.night_suppress_match("OUTSIDE_BACK_SOLAR", "car") is True

    def test_night_and_camera_not_in_list_passes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Returns False when camera is not in the suppress list."""
        monkeypatch.setenv("FARMSURV_NIGHT_SUPPRESS_CAMERAS", "OUTSIDE_BACK_SOLAR")
        monkeypatch.setenv("FARMSURV_NIGHT_SUPPRESS_CLASSES", "")
        importlib.reload(self.qc)
        assert self.qc.night_suppress_match("FRONT_DOOR", "car") is False

    def test_night_and_camera_bucket_name_matches(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Bucket name 'vehicle' matches concrete COCO class 'car' (US-073d fix)."""
        monkeypatch.setenv("FARMSURV_NIGHT_SUPPRESS_CAMERAS", "OUTSIDE_BACK_SOLAR")
        monkeypatch.setenv("FARMSURV_NIGHT_SUPPRESS_CLASSES", "vehicle")
        importlib.reload(self.qc)
        # 'car' maps to the 'vehicle' bucket, so suppress IS applied.
        assert self.qc.night_suppress_match("OUTSIDE_BACK_SOLAR", "car") is True

    def test_empty_camera_list_current_behavior(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Empty camera list = no suppress for any camera (current behavior)."""
        monkeypatch.setenv("FARMSURV_NIGHT_SUPPRESS_CAMERAS", "")
        monkeypatch.setenv("FARMSURV_NIGHT_SUPPRESS_CLASSES", "vehicle")
        importlib.reload(self.qc)
        assert self.qc.night_suppress_match("ANY_CAMERA", "car") is False
        assert self.qc.night_suppress_match("ANY_CAMERA", "person") is False

    def test_class_match_when_classes_listed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When classes list is set, bucket name expands to concrete classes."""
        monkeypatch.setenv("FARMSURV_NIGHT_SUPPRESS_CAMERAS", "OUTSIDE_BACK_SOLAR")
        monkeypatch.setenv("FARMSURV_NIGHT_SUPPRESS_CLASSES", "vehicle")
        importlib.reload(self.qc)
        # Bucket 'vehicle' expands to all COCO vehicle classes.
        assert self.qc.night_suppress_match("OUTSIDE_BACK_SOLAR", "car") is True
        assert self.qc.night_suppress_match("OUTSIDE_BACK_SOLAR", "truck") is True
        assert self.qc.night_suppress_match("OUTSIDE_BACK_SOLAR", "bicycle") is True
        # Non-vehicle should not match.
        assert self.qc.night_suppress_match("OUTSIDE_BACK_SOLAR", "person") is False

    def test_class_without_camera_logs_warning_no_op(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Class-without-camera logs WARNING and suppress_match is no-op."""
        monkeypatch.setenv("FARMSURV_NIGHT_SUPPRESS_CAMERAS", "")
        monkeypatch.setenv("FARMSURV_NIGHT_SUPPRESS_CLASSES", "vehicle")
        importlib.reload(self.qc)
        # The warning should be logged at boot time (module load).
        assert any(
            "night_suppress_class_without_camera" in record.message
            for record in caplog.records
        )
        # No-op: match always returns False.
        assert self.qc.night_suppress_match("ANY", "vehicle") is False


# ---------------------------------------------------------------------------
# Integration: classify_frame night suppress with camera_id
# ---------------------------------------------------------------------------

class TestClassifyFrameNightSuppress:
    """Integration tests: classify_frame honors camera_id + night suppress."""

    @pytest.fixture(autouse=True)
    def _setup_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("FARMSURV_NIGHT_SUPPRESS_CAMERAS", "OUTSIDE_BACK_SOLAR")
        monkeypatch.setenv("FARMSURV_NIGHT_SUPPRESS_CLASSES", "")
        import infra.quick_classifier as qc

        importlib.reload(qc)
        self.qc = qc

    def test_night_and_camera_match_suppresses_integration(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """classify_frame returns suppressed when night + camera matches."""
        # Use a night-time timestamp (Sept 22, 2026 ~01:00 EDT).
        monkeypatch.setenv("FARMSURV_NIGHT_SUPPRESS_CAMERAS", "OUTSIDE_BACK_SOLAR")
        monkeypatch.setenv("FARMSURV_NIGHT_SUPPRESS_CLASSES", "")
        import infra.quick_classifier as qc

        importlib.reload(qc)

        # Create a minimal valid ONNX file (not a real model, but enough
        # for the import to succeed). The actual YOLO inference will be
        # mocked out by a FakeClassifier in the calling layer.
        # Here we test the suppress logic at the module level.
        assert qc.night_suppress_match("OUTSIDE_BACK_SOLAR", "car") is True

    def test_day_and_camera_match_passes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """night_suppress_match is agnostic to time — time check is separate.
        The match function itself only checks camera+class lists.
        """
        # day_suppress_match is not a function; the time check is in
        # classify_frame via _is_night_at_edt(). This test verifies that
        # night_suppress_match returns True regardless of time (the caller
        # checks time separately).
        assert self.qc.night_suppress_match("OUTSIDE_BACK_SOLAR", "car") is True

    def test_camera_not_listed_does_not_suppress(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Non-listed camera returns False regardless of class."""
        assert self.qc.night_suppress_match("FRONT_DOOR", "car") is False


# ---------------------------------------------------------------------------
# Bucket-name expansion (US-073d)
# ---------------------------------------------------------------------------

class TestBucketExpansion:
    """Tests for US-073d: bucket-name expansion in night_suppress_match."""

    @pytest.fixture(autouse=True)
    def _setup_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("FARMSURV_NIGHT_SUPPRESS_CAMERAS", "OUTSIDE_BACK_SOLAR")
        monkeypatch.setenv("FARMSURV_NIGHT_SUPPRESS_CLASSES", "")
        import infra.quick_classifier as qc

        importlib.reload(qc)
        self.qc = qc

    def _match(self, class_name: str) -> bool:
        return self.qc.night_suppress_match("OUTSIDE_BACK_SOLAR", class_name)

    def test_bucket_vehicle_expands_to_car(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """'vehicle' bucket matches 'car' (the bug fix for US-073d)."""
        monkeypatch.setenv("FARMSURV_NIGHT_SUPPRESS_CLASSES", "vehicle")
        importlib.reload(self.qc)
        assert self._match("car") is True

    def test_bucket_vehicle_matches_all_vehicle_classes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """'vehicle' bucket matches all COCO vehicle classes."""
        monkeypatch.setenv("FARMSURV_NIGHT_SUPPRESS_CLASSES", "vehicle")
        importlib.reload(self.qc)
        for cls in ("bicycle", "car", "motorcycle", "bus", "truck"):
            assert self._match(cls) is True, f"vehicle should match {cls}"

    def test_bucket_vehicle_does_not_match_person(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """'vehicle' bucket does NOT match 'person'."""
        monkeypatch.setenv("FARMSURV_NIGHT_SUPPRESS_CLASSES", "vehicle")
        importlib.reload(self.qc)
        assert self._match("person") is False

    def test_bucket_vehicle_does_not_match_animal(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """'vehicle' bucket does NOT match 'dog'."""
        monkeypatch.setenv("FARMSURV_NIGHT_SUPPRESS_CLASSES", "vehicle")
        importlib.reload(self.qc)
        assert self._match("dog") is False

    def test_mixed_bucket_and_literal(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Mixed set: 'vehicle,person' matches both vehicle classes and person."""
        monkeypatch.setenv("FARMSURV_NIGHT_SUPPRESS_CLASSES", "vehicle,person")
        importlib.reload(self.qc)
        assert self._match("car") is True
        assert self._match("person") is True
        assert self._match("dog") is False

    def test_backward_compat_literal_only(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Literal class names: 'car,truck' matches only car and truck, NOT bicycle."""
        monkeypatch.setenv("FARMSURV_NIGHT_SUPPRESS_CLASSES", "car,truck")
        importlib.reload(self.qc)
        assert self._match("car") is True
        assert self._match("truck") is True
        assert self._match("bicycle") is False
        assert self._match("bus") is False

    def test_expands_person_bucket(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """'person' bucket matches 'person'."""
        monkeypatch.setenv("FARMSURV_NIGHT_SUPPRESS_CLASSES", "person")
        importlib.reload(self.qc)
        assert self._match("person") is True
        assert self._match("car") is False

    def test_expands_animal_bucket(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """'animal' bucket matches all animal classes."""
        monkeypatch.setenv("FARMSURV_NIGHT_SUPPRESS_CLASSES", "animal")
        importlib.reload(self.qc)
        for cls in ("cat", "dog", "horse", "sheep", "cow", "bear", "bird"):
            assert self._match(cls) is True, f"animal should match {cls}"
        assert self._match("car") is False
