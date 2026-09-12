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
        """Caption includes bbox position when bbox exists."""
        verdict = _make_verdict_with_bbox(bbox=(100, 200, 50, 80))
        result = build_alert_message(
            verdict=verdict,
            vm1_result=_make_vm1_result(),
            artifacts=_make_artifacts(),
            camera_label="Front Gate",
            alert=_make_alert(),
        )
        caption = result["caption"]
        # Position is in composite viz (TG#1 photo), not caption.
        # Caption should NOT spam the same bbox 4 times.
        assert "frames[t-3]:" not in caption
        assert "frames[t0]:" not in caption
        # YOLO + Vision lines should both be present.
        assert "YOLO:" in caption
        assert "Vision:" in caption

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
        # No bbox-spam from old position line.
        assert "frames[t" not in caption
        assert "no subject" not in caption
        # Position line is gone; YOLO/Vision lines remain.
        assert "YOLO:" in caption
        assert "Vision:" in caption
        # id/timestamp lines absent (no alert dict)
        assert "Alert ID:" not in caption
        assert "Timestamp:" not in caption


class TestCaptionNoBbox:
    """AC(b): no bbox -> caption says 'no subject bbox detected'."""

    def test_caption_says_no_bbox_when_crop_bbox_a_is_none(self):
        """Caption is rendered without bbox spam when bbox is None.

        Old behavior said 'no subject bbox detected'; the position line
        is gone entirely, so we just assert no frames[t-3] spam.
        """
        verdict = _make_verdict_no_bbox()
        result = build_alert_message(
            verdict=verdict,
            vm1_result=_make_vm1_result(),
            artifacts=_make_artifacts(),
            camera_label="Back Yard",
            alert=_make_alert(),
        )
        assert "frames[t-3]:" not in result["caption"]
        # Caption still has YOLO + Vision lines.
        assert "YOLO:" in result["caption"]
        assert "Vision:" in result["caption"]


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


# ---------------------------------------------------------------------------
# TG#2 caption alert metadata tests (US-033a)
# ---------------------------------------------------------------------------

from pathlib import Path


class TestTg2CaptionAlertMetadata:
    """US-033a: TG#2 caption includes/excludes alert metadata."""

    def test_tg2_caption_includes_alert_metadata_when_alert_provided(self):
        """TG#2 caption includes Alert 2 of 3 / Alert ID / Timestamp."""
        from telegram_formatter.detail import build_detail_message

        alert = _make_alert(alert_id="evt-xyz-789", timestamp="2026-09-12T08:00:00Z")
        result = build_detail_message(
            mode="vehicle",
            vm2_result={
                "class": "vehicle",
                "color": "white",
                "make": "Ford",
                "model": "F-150",
                "body_style_hint": "pickup",
                "vehicle_features": {
                    "wheel_style": "alloy",
                    "headlight_signature": "LED",
                },
                "confidence": 0.92,
                "notable_details": [],
            },
            crop_a=Path("/mock/crop_a.png"),
            crop_b=Path("/mock/crop_b.png"),
            camera_label="Front Gate",
            alert=alert,
        )
        caption = result["caption"]
        assert "Alert 2 of 3" in caption
        assert "Alert ID: evt-xyz-789" in caption
        assert "Timestamp: 2026-09-12T08:00:00Z" in caption
        # Verify order: Camera -> Mode -> Class -> Alert lines
        parts = caption.split("\n")
        idx_alert2 = parts.index("Alert 2 of 3")
        idx_alert_id = parts.index("Alert ID: evt-xyz-789")
        idx_ts = parts.index("Timestamp: 2026-09-12T08:00:00Z")
        assert idx_alert2 < idx_alert_id < idx_ts
        # Camera line comes first
        assert parts[0] == "Camera: Front Gate"
        # Nested vehicle_features expanded on own lines (indent +3 per level)
        assert "vehicle_features:" in caption
        assert "   headlight_signature: LED" in caption

    def test_tg2_caption_omits_alert_metadata_when_alert_none(self):
        """TG#2 caption omits Alert lines when alert is None."""
        from telegram_formatter.detail import build_detail_message

        result = build_detail_message(
            mode="person",
            vm2_result={
                "class": "person",
                "better_crop": "crop_a",
                "attributes": {
                    "clothing_upper": "black hoodie",
                    "clothing_lower": "blue jeans",
                },
                "confidence": 0.88,
                "notable_details": [],
            },
            crop_a=Path("/mock/crop_a.png"),
            crop_b=Path("/mock/crop_b.png"),
            camera_label="Back Yard",
            alert=None,
        )
        caption = result["caption"]
        assert "Alert 2 of 3" not in caption
        assert "Alert ID:" not in caption
        assert "Timestamp:" not in caption
        # Basic lines still present
        assert "Camera: Back Yard" in caption
        assert "Mode: person" in caption
        assert "Class confirmed: person" in caption


# ---------------------------------------------------------------------------
# TG#3 caption alert metadata tests (US-033b)
# ---------------------------------------------------------------------------


class TestTg3CaptionAlertMetadata:
    """US-033b: TG#3 caption includes/excludes alert metadata."""

    def test_tg3_caption_includes_alert_metadata_when_alert_provided(self):
        """TG#3 caption includes Alert 3 of 3 / Alert ID / Timestamp."""
        from telegram_formatter.match_alert import build_match_message

        alert = _make_alert(alert_id="evt-ghi-456", timestamp="2026-09-12T09:00:00Z")
        # match_alert.py still uses vm2_result['license_plate'] and
        # ['distinctive_features'] — these are TG#3 concerns, not TG#2.
        result = build_match_message(
            match_result={"matched": True},
            vm2_result={
                "make": "Ford",
                "model": "F-150",
                "color": "white",
                "distinctive_features": ["white"],
            },
            camera_label="Front Gate",
            alert=alert,
        )
        caption = result["caption"]
        assert "Alert 3 of 3" in caption
        assert "Alert ID: evt-ghi-456" in caption
        assert "Timestamp: 2026-09-12T09:00:00Z" in caption
        # Verify order: Camera -> Status -> Alert lines
        parts = caption.split("\n")
        idx_camera = parts.index("Camera: Front Gate")
        idx_status = next(i for i, p in enumerate(parts) if p.startswith("Status:"))
        idx_alert3 = parts.index("Alert 3 of 3")
        idx_alert_id = parts.index("Alert ID: evt-ghi-456")
        idx_ts = parts.index("Timestamp: 2026-09-12T09:00:00Z")
        assert idx_camera < idx_status < idx_alert3 < idx_alert_id < idx_ts

    def test_tg3_caption_camera_line_is_first(self):
        """Camera: is the FIRST line of the caption (before Status)."""
        from telegram_formatter.match_alert import build_match_message

        result = build_match_message(
            match_result={"matched": False},
            vm2_result={"distinctive_features": []},
            camera_label="Back Yard",
            alert=None,
        )
        caption = result["caption"]
        first_line = caption.split("\n")[0]
        assert first_line == "Camera: Back Yard"

    def test_tg3_caption_no_alert_metadata_when_alert_none(self):
        """TG#3 caption omits Alert lines when alert is None."""
        from telegram_formatter.match_alert import build_match_message

        result = build_match_message(
            match_result={"matched": True},
            vm2_result={"make": "Ford", "model": "F-150", "distinctive_features": []},
            camera_label="Gate A",
            alert=None,
        )
        caption = result["caption"]
        assert "Alert 3 of 3" not in caption
        assert "Alert ID:" not in caption
        assert "Timestamp:" not in caption
        assert "Camera: Gate A" in caption


# ---------------------------------------------------------------------------
# TG#2 nested-dict expansion tests (US-034d)
# ---------------------------------------------------------------------------


class TestTg2NestedDictExpansion:
    """US-034d: nested dicts expand onto indented sub-lines."""

    def test_vehicle_mode_expands_vehicle_features(self):
        """vehicle mode: vehicle_features sub-fields each on their own line."""
        from telegram_formatter.detail import build_detail_message

        result = build_detail_message(
            mode="vehicle",
            vm2_result={
                "class": "vehicle",
                "color": "white",
                "make": "Ford",
                "model": "F-150",
                "body_style_hint": "pickup",
                "vehicle_features": {
                    "wheel_style": "alloy",
                    "headlight_signature": "LED",
                    "rear_lights_signature": "strip",
                },
                "description": "white Ford F-150 pickup",
                "confidence": 0.92,
                "notable_details": [],
            },
            crop_a=Path("/mock/crop_a.png"),
            crop_b=Path("/mock/crop_b.png"),
            camera_label="Front Gate",
        )
        caption = result["caption"]
        assert "vehicle_features:" in caption
        assert "   wheel_style: alloy" in caption
        assert "   headlight_signature: LED" in caption
        assert "   rear_lights_signature: strip" in caption
        # vehicle_features lines come AFTER top-level fields
        assert caption.index("body_style_hint:") < caption.index("vehicle_features:")

    def test_person_mode_expands_attributes_and_signature(self):
        """person mode: attributes + signature.stable/transient each on own line."""
        from telegram_formatter.detail import build_detail_message

        result = build_detail_message(
            mode="person",
            vm2_result={
                "class": "person",
                "better_crop": "crop_a",
                "attributes": {
                    "clothing_upper": "black hoodie",
                    "clothing_lower": "blue jeans",
                    "carrying": ["backpack", "umbrella"],
                    "action": "walking",
                },
                "signature": {
                    "stable": ["tattoo on right forearm"],
                    "transient": ["carrying a backpack"],
                },
                "confidence": 0.88,
                "notable_details": ["approaching the gate"],
            },
            crop_a=Path("/mock/crop_a.png"),
            crop_b=Path("/mock/crop_b.png"),
            camera_label="Back Yard",
        )
        caption = result["caption"]
        assert "attributes:" in caption
        assert "   clothing_upper: black hoodie" in caption
        assert "   carrying: [backpack, umbrella]" in caption
        assert "signature:" in caption
        assert "   stable: [tattoo on right forearm]" in caption
        assert "   transient: [carrying a backpack]" in caption

    def test_animal_mode_expands_distinctive_features_as_inline_list(self):
        """animal mode: distinctive_features rendered as inline list."""
        from telegram_formatter.detail import build_detail_message

        result = build_detail_message(
            mode="animal",
            vm2_result={
                "class": "animal",
                "species": "dog",
                "breed": "labrador",
                "size": "large",
                "color_pattern": "golden",
                "distinctive_features": ["blue collar", "tag with name"],
                "action": "walking",
                "confidence": 0.95,
                "notable_details": ["wagging tail"],
            },
            crop_a=Path("/mock/crop_a.png"),
            crop_b=Path("/mock/crop_b.png"),
            camera_label="Farm Entrance",
        )
        caption = result["caption"]
        assert "species: dog" in caption
        assert "breed: labrador" in caption
        assert "distinctive_features: [blue collar, tag with name]" in caption
        assert "action: walking" in caption
        assert "confidence: 0.95" in caption
