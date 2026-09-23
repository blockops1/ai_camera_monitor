"""test_gate_global_change_suppress.py -- regression net for global_change gate.

Tests the global_change suppression logic introduced in US-063a:
  - _read_global_change_max_pct() env-var defaults and overrides
  - _bbox_area_pct() geometry math (including d54d3b29 precedent)
  - Full run() integration: oversize bbox suppresses, localized bbox passes

Uses the FakeClassifier pattern from test_gate_routing_classification.py.
NO llama-server, NO real YOLO. Regression net for US-063a.

Acceptance criteria (US-063b):
  1. 13 unit tests covering all three test classes.
  2. Full test suite still green; net +1 test (64 -> 65).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import pytest
from PIL import Image

from infra.gate import _bbox_area_pct, _read_global_change_max_pct, run
from infra.quick_classifier import QuickVerdict

# ---------------------------------------------------------------------------
# FakeClassifier — reused from test_gate_routing_classification.py
# ---------------------------------------------------------------------------

@dataclass
class FakeVerdict:
    """Test helper to construct a QuickVerdict with desired properties."""
    top_class: str = "person"
    top_confidence: float = 0.85
    decision: str = "pass_with_hint"
    reason: str | None = None


class FakeClassifier:
    """A classifier that returns a deterministic QuickVerdict.

    Tests set `verdict` before calling classify_frame(). The classifier
    ignores the frame content and always returns the same verdict.
    """
    verdict: FakeVerdict = FakeVerdict()

    def classify_frame(
        self,
        frame,
        timestamp: datetime | None = None,
        camera_id: str | None = None,
    ) -> QuickVerdict:
        """Return a deterministic QuickVerdict. Ignores frame, timestamp, camera_id."""
        return QuickVerdict(
            top_class=self.verdict.top_class,
            top_confidence=self.verdict.top_confidence,
            decision=self.verdict.decision,
            n_detections=1,
            raw_predictions=[],
            reason=self.verdict.reason,
        )


@pytest.fixture(autouse=True)
def _reset_fake_classifier():
    """Reset FakeClassifier.verdict before each test."""
    FakeClassifier.verdict = FakeVerdict()
    yield


# ---------------------------------------------------------------------------
# Helpers — reused from test_gate_routing_classification.py
# ---------------------------------------------------------------------------

def _make_tiny_jpeg(path, size=(64, 64), color=(100, 100, 100)):
    """Write a tiny valid JPEG image to `path`."""
    img = Image.new("RGB", size, color)
    img.save(path, format="JPEG")
    return str(path)


def _make_fake_frames(tmp_path, offset=0, base_color=(0, 0, 0)):
    """Create 4 fake JPEG frames on disk. Each frame differs enough for diff to detect motion.

    Uses a 640x480 size so cv2 diff works reliably. Frame 0 is black;
    frames 1-3 paint a small 80x80 patch in the center with a progressive
    color shift so pairwise diff finds a compact motion bbox (~3% of frame
    area, well below the global_change 60% threshold).
    """
    frame_paths = []
    for i in range(4):
        fp = tmp_path / f"frame_{i + offset:04d}.jpg"
        # Frame 0 = black; frames 1-3 = black with a small colored patch
        # in the center. Patch is 80x80 on a 640x480 frame = ~3% area.
        img = Image.new("RGB", (640, 480), (0, 0, 0))
        if i > 0:
            color = (i * 80, i * 80, i * 80)
            img.crop((0, 0, 0, 0))  # no-op to keep structure
            from PIL import ImageDraw
            draw = ImageDraw.Draw(img)
            draw.rectangle((280, 200, 360, 280), fill=color)
        img.save(fp, format="JPEG")
        frame_paths.append(str(fp))
    return frame_paths


def _make_fake_frames_oversize(tmp_path, offset=0):
    """Create 4 fake JPEG frames where motion diff covers most of the frame.

    Uses a 640x480 size. Frame 0 is black; frames 1-3 paint a large
    500x400 patch in the upper-left corner, covering ~78% of frame area.
    This triggers global_change suppression at a low threshold.
    """
    frame_paths = []
    for i in range(4):
        fp = tmp_path / f"frame_{i + offset:04d}.jpg"
        img = Image.new("RGB", (640, 480), (0, 0, 0))
        if i > 0:
            color = (i * 80, i * 80, i * 80)
            img.crop((0, 0, 0, 0))  # no-op
            from PIL import ImageDraw
            draw = ImageDraw.Draw(img)
            draw.rectangle((0, 0, 500, 400), fill=color)
        img.save(fp, format="JPEG")
        frame_paths.append(str(fp))
    return frame_paths


# ---------------------------------------------------------------------------
# TestReadGlobalChangeMaxPct (5 tests)
# ---------------------------------------------------------------------------

class TestReadGlobalChangeMaxPct:
    """Test _read_global_change_max_pct() defaults and env overrides."""

    def test_default_is_60(self, monkeypatch):
        """Default (env unset) returns 60.0."""
        monkeypatch.delenv("FARMSURV_GLOBAL_CHANGE_MAX_PCT", raising=False)
        assert _read_global_change_max_pct() == 60.0

    def test_env_override_respected(self, monkeypatch):
        """Env '75.5' returns 75.5."""
        monkeypatch.setenv("FARMSURV_GLOBAL_CHANGE_MAX_PCT", "75.5")
        assert _read_global_change_max_pct() == 75.5

    def test_zero_disables(self, monkeypatch):
        """Env '0' returns 0.0 (disables the gate)."""
        monkeypatch.setenv("FARMSURV_GLOBAL_CHANGE_MAX_PCT", "0")
        assert _read_global_change_max_pct() == 0.0

    def test_negative_disables(self, monkeypatch):
        """Env '-1' returns -1.0 (>0 gate in run() catches it)."""
        monkeypatch.setenv("FARMSURV_GLOBAL_CHANGE_MAX_PCT", "-1")
        assert _read_global_change_max_pct() == -1.0

    def test_garbage_falls_back_to_60(self, monkeypatch):
        """Env 'abc' (non-numeric) falls back to 60.0."""
        monkeypatch.setenv("FARMSURV_GLOBAL_CHANGE_MAX_PCT", "abc")
        assert _read_global_change_max_pct() == 60.0


# ---------------------------------------------------------------------------
# TestBboxAreaPct (5 tests)
# ---------------------------------------------------------------------------

class TestBboxAreaPct:
    """Test _bbox_area_pct() geometry calculations."""

    def test_none_bbox_returns_zero(self):
        """bbox=None returns 0.0 regardless of frame dimensions."""
        assert _bbox_area_pct(None, 640, 480) == 0.0

    def test_zero_dimensions_returns_zero(self):
        """w=0 or h=0 returns 0.0."""
        assert _bbox_area_pct((10, 10, 0, 100), 640, 480) == 0.0
        assert _bbox_area_pct((10, 10, 100, 0), 640, 480) == 0.0

    def test_full_frame_returns_100(self):
        """bbox covering the entire frame returns 100.0."""
        assert _bbox_area_pct((0, 0, 640, 480), 640, 480) == pytest.approx(100.0, abs=0.01)

    def test_d54d3b29_precedent_a(self):
        """d54d3b29 precedent A: bbox=(0,0,2304,864) on 2304x1296 returns 66.67%."""
        # 2304*864 / (2304*1296) = 1990656 / 2985984 = 0.66666... = 66.666...%
        result = _bbox_area_pct((0, 0, 2304, 864), 2304, 1296)
        assert result == pytest.approx(66.67, abs=0.01)

    def test_d54d3b29_precedent_b(self):
        """d54d3b29 precedent B: bbox=(0,0,2304,1120) returns 86.42%."""
        # 2304*1120 / (2304*1296) = 2580480 / 2985984 = 0.86419... = 86.419...%
        result = _bbox_area_pct((0, 0, 2304, 1120), 2304, 1296)
        assert result == pytest.approx(86.42, abs=0.01)


# ---------------------------------------------------------------------------
# TestGlobalChangeSuppression (3 tests, full run() integration)
# ---------------------------------------------------------------------------

class TestGlobalChangeSuppression:
    """Full run() integration tests for global_change suppression."""

    def test_oversize_bbox_triggers_suppression(self, tmp_path, monkeypatch):
        """Oversize bbox (covers most of frame) triggers global_change suppression."""
        # Set a low threshold so the oversize patch triggers it
        monkeypatch.setenv("FARMSURV_GLOBAL_CHANGE_MAX_PCT", "10")

        frame_paths = _make_fake_frames_oversize(tmp_path)
        classifier = FakeClassifier()

        verdict = run(
            frame_paths=frame_paths,
            camera_name="TEST_CAM",
            alert_id="test-oversize",
            output_dir=str(tmp_path),
            classifier=classifier,
        )

        assert verdict.classification == "none"
        assert verdict.reason == "global_change"

    def test_localized_bbox_passes(self, tmp_path, monkeypatch):
        """Localized bbox (small patch) does NOT trigger suppression even at high threshold."""
        # threshold=99 means only a near-full-frame diff would suppress
        monkeypatch.setenv("FARMSURV_GLOBAL_CHANGE_MAX_PCT", "99")

        frame_paths = _make_fake_frames(tmp_path)  # small 80x80 patch (~3%)
        classifier = FakeClassifier()

        verdict = run(
            frame_paths=frame_paths,
            camera_name="TEST_CAM",
            alert_id="test-localized",
            output_dir=str(tmp_path),
            classifier=classifier,
        )

        assert verdict.classification != "none"
        assert verdict.reason != "global_change"

    def test_threshold_zero_disables_suppression(self, tmp_path, monkeypatch):
        """threshold=0 disables global_change gate — verdict passes through."""
        monkeypatch.setenv("FARMSURV_GLOBAL_CHANGE_MAX_PCT", "0")

        # Even with an oversize patch, zero threshold disables the gate
        frame_paths = _make_fake_frames_oversize(tmp_path)
        classifier = FakeClassifier()

        verdict = run(
            frame_paths=frame_paths,
            camera_name="TEST_CAM",
            alert_id="test-zero-disable",
            output_dir=str(tmp_path),
            classifier=classifier,
        )

        # The FakeClassifier sees person@0.85; with global_change disabled,
        # that verdict should pass through (classification != "none")
        assert verdict.classification != "none"
        assert verdict.reason != "global_change"
