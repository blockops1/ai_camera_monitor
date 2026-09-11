"""test_alert_message_caption.py — Tests for caption composition in alert.py.

Covers US-027c acceptance criteria:
  (a) full bbox -> caption includes 4-frame positions
  (b) no bbox -> caption says 'no subject bbox detected'
  (c) long top_class string -> caption truncated to <= 1024 chars
"""

from __future__ import annotations

from unittest.mock import MagicMock

from infra.alert_artifacts import AlertArtifacts
from infra.gate import GateVerdict
from telegram_formatter.alert import _CAPTION_MAX_LENGTH, build_alert_message


def _make_artifacts(
    composite_path: str | None = "/mock/composite.png",
    full_frame_path: str = "/mock/frame004.jpg",
) -> AlertArtifacts:
    """Build an AlertArtifacts dataclass for testing."""
    return AlertArtifacts(
        crop_a_path="/mock/crop_a.png",
        crop_b_path="/mock/crop_b.png",
        composite_path=composite_path,
        full_frame_path=full_frame_path,
    )


def _make_vm1_result(
    cls: str = "vehicle",
    confidence: float = 0.92,
    notes: str | None = None,
) -> dict:
    """Build a VM1 result dict."""
    return {"class": cls, "confidence": confidence, "notes": notes}


def _make_verdict_with_bbox(
    classification: str = "vehicle",
    top_class: str = "car",
    top_confidence: float = 0.95,
    bbox: tuple[int, int, int, int] | None = (100, 200, 50, 80),
) -> MagicMock:
    """Build a GateVerdict mock with a subject bbox."""
    v = MagicMock(spec=GateVerdict)
    v.classification = classification
    v.class_label = "car"
    v.confidence = 0.95
    v.top_class = top_class
    v.top_confidence = top_confidence
    v.reason = "high_conf_vehicle"
    v.crop_bbox_a = bbox
    return v


def _make_verdict_no_bbox() -> MagicMock:
    """Build a GateVerdict mock with no subject bbox."""
    v = MagicMock(spec=GateVerdict)
    v.classification = "none"
    v.class_label = None
    v.confidence = 0.0
    v.top_class = ""
    v.top_confidence = 0.0
    v.reason = "no_subject_detected"
    v.crop_bbox_a = None
    return v


def _make_alert(
    alert_id: str = "evt-test-001",
    timestamp: str = "2026-09-07T10:00:00Z",
) -> dict:
    """Build an alert dict with id and timestamp."""
    return {"id": alert_id, "timestamp": timestamp}


class TestCaptionWithBbox:
    """AC(a): full bbox -> caption includes 4-frame positions."""

    def test_caption_includes_classification(self):
        """Caption includes classification from verdict."""
        verdict = _make_verdict_with_bbox(classification="person")
        result = build_alert_message(
            verdict=verdict,
            vm1_result=_make_vm1_result(),
            artifacts=_make_artifacts(),
            camera_label="Front Gate",
            alert=_make_alert(),
        )
        assert "person" in result["caption"]

    def test_caption_includes_top_class_and_confidence(self):
        """Caption includes top class and confidence from verdict."""
        verdict = _make_verdict_with_bbox(top_class="car", top_confidence=0.87)
        result = build_alert_message(
            verdict=verdict,
            vm1_result=_make_vm1_result(),
            artifacts=_make_artifacts(),
            camera_label="Front Gate",
            alert=_make_alert(),
        )
        assert "car" in result["caption"]
        assert "0.87" in result["caption"]

    def test_caption_includes_4_frame_positions(self):
        """Caption includes 4-frame position line when bbox exists."""
        verdict = _make_verdict_with_bbox(bbox=(100, 200, 50, 80))
        result = build_alert_message(
            verdict=verdict,
            vm1_result=_make_vm1_result(),
            artifacts=_make_artifacts(),
            camera_label="Front Gate",
            alert=_make_alert(),
        )
        caption = result["caption"]
        assert "frames[t-3]: (100,200,50,80)" in caption
        assert "frames[t-2]: (100,200,50,80)" in caption
        assert "frames[t-1]: (100,200,50,80)" in caption
        assert "frames[t0]: (100,200,50,80)" in caption

    def test_caption_includes_alert_id(self):
        """Caption includes alert_id when provided in alert dict."""
        result = build_alert_message(
            verdict=_make_verdict_with_bbox(),
            vm1_result=_make_vm1_result(),
            artifacts=_make_artifacts(),
            camera_label="Front Gate",
            alert=_make_alert(alert_id="evt-abc-123"),
        )
        assert "evt-abc-123" in result["caption"]

    def test_caption_includes_timestamp(self):
        """Caption includes timestamp when provided in alert dict."""
        result = build_alert_message(
            verdict=_make_verdict_with_bbox(),
            vm1_result=_make_vm1_result(),
            artifacts=_make_artifacts(),
            camera_label="Front Gate",
            alert=_make_alert(timestamp="2026-09-07T12:30:00Z"),
        )
        assert "2026-09-07T12:30:00Z" in result["caption"]

    def test_caption_no_alert_dict_still_works(self):
        """Caption works when alert is None (no id/timestamp lines)."""
        result = build_alert_message(
            verdict=_make_verdict_with_bbox(),
            vm1_result=_make_vm1_result(),
            artifacts=_make_artifacts(),
            camera_label="Front Gate",
            alert=None,
        )
        caption = result["caption"]
        assert "Frames[t" not in caption  # position line still present
        assert "no subject" not in caption  # not the no-bbox path
        # Verify position line is present
        assert "frames[t-3]:" in caption
        assert "frames[t0]:" in caption
        # Verify id/timestamp lines absent
        assert "Alert ID:" not in caption
        assert "Timestamp:" not in caption


class TestCaptionNoBbox:
    """AC(b): no bbox -> caption says 'no subject bbox detected'."""

    def test_caption_says_no_bbox_when_crop_bbox_a_is_none(self):
        """Caption contains 'no subject bbox detected' when bbox is None."""
        verdict = _make_verdict_no_bbox()
        result = build_alert_message(
            verdict=verdict,
            vm1_result=_make_vm1_result(),
            artifacts=_make_artifacts(),
            camera_label="Back Yard",
            alert=_make_alert(),
        )
        assert "no subject bbox detected" in result["caption"]
        # Position line should NOT contain frame coordinates
        assert "frames[t-3]:" not in result["caption"]


class TestCaptionTruncation:
    """AC(c): long top_class string -> caption is truncated to <= 1024 chars."""

    def test_long_top_class_truncated_to_1024_with_ellipsis(self):
        """Caption truncated at 1024 chars with ellipsis for very long top_class."""
        long_class = "A" * 1100  # Very long class name
        verdict = _make_verdict_with_bbox(top_class=long_class)
        result = build_alert_message(
            verdict=verdict,
            vm1_result=_make_vm1_result(
                cls="vehicle",
                notes="N" * 500,  # Extra-long notes
            ),
            artifacts=_make_artifacts(),
            camera_label="Front Gate",
            alert=_make_alert(),
        )
        caption = result["caption"]
        assert len(caption) <= _CAPTION_MAX_LENGTH
        assert caption.endswith("...")

    def test_normal_caption_not_truncated(self):
        """Normal-length caption is NOT truncated."""
        verdict = _make_verdict_with_bbox()
        result = build_alert_message(
            verdict=verdict,
            vm1_result=_make_vm1_result(),
            artifacts=_make_artifacts(),
            camera_label="Front Gate",
            alert=_make_alert(),
        )
        caption = result["caption"]
        assert len(caption) <= _CAPTION_MAX_LENGTH
        assert not caption.endswith("...")

    def test_truncation_preserves_ellipsis_suffix(self):
        """Truncated caption ends with '...' (3 dots)."""
        verdict = _make_verdict_with_bbox(top_class="X" * 1200)
        result = build_alert_message(
            verdict=verdict,
            vm1_result=_make_vm1_result(notes="Y" * 600),
            artifacts=_make_artifacts(),
            camera_label="Front Gate",
            alert=_make_alert(),
        )
        assert result["caption"].endswith("...")
