"""test_alert_message_caption.py — Tests for caption composition in alert.py.

Covers US-067a acceptance criteria for the new Markdown format:
  (a) header line with camera + time
  (b) class line with emoji + class + confidence
  (c) TG position marker
  (d) short alert ID (first 8 chars)
  (e) MarkdownV2-escaped caption, truncated to 1024 chars
"""

from __future__ import annotations

from unittest.mock import MagicMock

from infra.alert_artifacts import AlertArtifacts
from infra.gate import GateVerdict
from telegram_formatter.alert import _CAPTION_MAX_LENGTH, build_alert_message


def _make_artifacts(
    composite_path: str | None = "/mock/composite.png",
) -> AlertArtifacts:
    """Build an AlertArtifacts dataclass for testing."""
    return AlertArtifacts(
        crop_a_path="/mock/crop_a.png",
        crop_b_path="/mock/crop_b.png",
        composite_path=composite_path,
    )


def _make_vm1_result(
    cls: str = "vehicle",
    subclass: str | None = "sedan",
    confidence: str = "likely",
    notes: str | None = None,
    count: int | None = 1,
    face_visible: bool | None = False,
) -> dict:
    """Build a VM1 result dict with the new schema."""
    return {
        "class": cls,
        "subclass": subclass,
        "confidence": confidence,
        "notes": notes,
        "count": count,
        "face_visible": face_visible,
    }


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
    """AC: new Markdown caption format with bbox."""

    def test_caption_includes_classification(self):
        """Caption includes classification from vm1_result."""
        verdict = _make_verdict_with_bbox(classification="person")
        result = build_alert_message(
            verdict=verdict,
            vm1_result=_make_vm1_result(cls="person"),
            artifacts=_make_artifacts(),
            camera_label="Front Gate",
            position=1,
            alert=_make_alert(),
        )
        assert "person" in result["caption"]

    def test_caption_includes_vm1_confidence(self):
        """Caption includes confidence from vm1_result."""
        verdict = _make_verdict_with_bbox(top_class="car", top_confidence=0.87)
        result = build_alert_message(
            verdict=verdict,
            vm1_result=_make_vm1_result(confidence="87"),
            artifacts=_make_artifacts(),
            camera_label="Front Gate",
            position=1,
            alert=_make_alert(),
        )
        # Confidence appears in class line.
        assert "87%" in result["caption"]

    def test_caption_has_no_old_labels(self):
        """Caption does NOT have old YOLO:/Vision:/Classification: labels."""
        verdict = _make_verdict_with_bbox(bbox=(100, 200, 50, 80))
        result = build_alert_message(
            verdict=verdict,
            vm1_result=_make_vm1_result(),
            artifacts=_make_artifacts(),
            camera_label="Front Gate",
            position=1,
            alert=_make_alert(),
        )
        caption = result["caption"]
        assert "YOLO:" not in caption
        assert "Vision:" not in caption
        assert "Classification:" not in caption
        assert "Camera:" not in caption

    def test_caption_includes_short_alert_id(self):
        """Caption includes first 8 chars of alert_id."""
        result = build_alert_message(
            verdict=_make_verdict_with_bbox(),
            vm1_result=_make_vm1_result(),
            artifacts=_make_artifacts(),
            camera_label="Front Gate",
            position=1,
            alert=_make_alert(alert_id="evt-abc-123-xxxx"),
        )
        assert "evt-abc-" in result["caption"]

    def test_caption_includes_timestamp(self):
        """Caption includes timestamp when provided in alert dict."""
        result = build_alert_message(
            verdict=_make_verdict_with_bbox(),
            vm1_result=_make_vm1_result(),
            artifacts=_make_artifacts(),
            camera_label="Front Gate",
            position=1,
            alert=_make_alert(timestamp="2026-09-07T12:30:00Z"),
        )
        # Timestamp is escaped; check for hour portion.
        assert "08" in result["caption"] and "30" in result["caption"]

    def test_caption_no_alert_dict_still_works(self):
        """Caption works when alert is None (no id/timestamp lines)."""
        result = build_alert_message(
            verdict=_make_verdict_with_bbox(),
            vm1_result=_make_vm1_result(),
            artifacts=_make_artifacts(),
            camera_label="Front Gate",
            position=1,
            alert=None,
        )
        caption = result["caption"]
        lines = [l for l in caption.split("\n") if l]
        # 3 lines: header, class, TG marker (no short ID, no timestamp)
        assert len(lines) == 3
        # No old labels
        assert "YOLO:" not in caption
        assert "Vision:" not in caption
        assert "frames[t" not in caption


class TestCaptionNoBbox:
    """AC: no bbox -> caption still renders correctly."""

    def test_caption_without_bbox(self):
        """Caption renders correctly when bbox is None."""
        verdict = _make_verdict_no_bbox()
        result = build_alert_message(
            verdict=verdict,
            vm1_result=_make_vm1_result(),
            artifacts=_make_artifacts(),
            camera_label="Back Yard",
            position=1,
            alert=_make_alert(),
        )
        assert "frames[t-3]: " not in result["caption"]
        # Still has header, class, TG marker, short ID
        lines = [l for l in result["caption"].split("\n") if l]
        assert len(lines) >= 4


class TestCaptionTruncation:
    """AC: long caption -> truncated to <= 1024 chars with ellipsis."""

    def test_long_notes_truncated_to_1024_with_ellipsis(self):
        """Caption truncated at 1024 chars with ellipsis for very long notes."""
        long_notes = "N" * 1000
        result = build_alert_message(
            verdict=_make_verdict_with_bbox(),
            vm1_result=_make_vm1_result(notes=long_notes),
            artifacts=_make_artifacts(),
            camera_label="Front Gate",
            position=1,
            alert=_make_alert(),
        )
        caption = result["caption"]
        assert len(caption) <= _CAPTION_MAX_LENGTH
        assert "..." in caption

    def test_normal_caption_not_truncated(self):
        """Normal-length caption is NOT truncated."""
        verdict = _make_verdict_with_bbox()
        result = build_alert_message(
            verdict=verdict,
            vm1_result=_make_vm1_result(),
            artifacts=_make_artifacts(),
            camera_label="Front Gate",
            position=1,
            alert=_make_alert(),
        )
        caption = result["caption"]
        assert len(caption) <= _CAPTION_MAX_LENGTH
        assert "..." not in caption

    def test_truncation_ends_with_ellipsis(self):
        """Truncated caption ends with '...' (3 dots)."""
        long_notes = "Y" * 1000
        verdict = _make_verdict_with_bbox()
        result = build_alert_message(
            verdict=verdict,
            vm1_result=_make_vm1_result(notes=long_notes),
            artifacts=_make_artifacts(),
            camera_label="Front Gate",
            position=1,
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
        assert "Timestamp: 2026-09-12 04:00:00 EDT" in caption
        # Verify order: Camera -> Mode -> Class -> Alert lines
        parts = caption.split("\n")
        idx_alert2 = parts.index("Alert 2 of 3")
        idx_alert_id = parts.index("Alert ID: evt-xyz-789")
        idx_ts = parts.index("Timestamp: 2026-09-12 04:00:00 EDT")
        assert idx_alert2 < idx_alert_id < idx_ts
        # Camera line comes first
        assert parts[0] == "Camera: Front Gate"
        # Curated vehicle fields rendered as bullet list (US-067b)
        assert "\u2022 Make: Ford" in caption
        assert "\u2022 Wheel style: alloy" in caption

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
        # TG#2 caption should NOT include "Class confirmed: person" — instead it
        # renders the attribute that triggered classification (here: "black hoodie").
        # Updated for v0.6.3 (2026-09-18): detail.py now echoes the confirmed
        # attribute name when the classifier returns one, not just the class label.
        assert "Class confirmed: black hoodie" in caption


# ---------------------------------------------------------------------------
# TG#3 caption alert metadata tests (US-033b)
# ---------------------------------------------------------------------------


class TestTg3CaptionAlertMetadata:
    """US-033b: TG#3 caption includes/excludes alert metadata."""

    def test_tg3_caption_includes_alert_metadata_when_alert_provided(self):
        """TG#3 caption includes Alert 3 of 3 / Alert ID / Timestamp."""
        from telegram_formatter.match_alert import build_match_message

        alert = _make_alert(alert_id="evt-ghi-456", timestamp="2026-09-12T09:00:00Z")
        result = build_match_message(
            match_result={
                "matched": True,
                "known_vehicle": {
                    "id": "v1",
                    "label": "Test vehicle",
                    "color": "white",
                    "make": "Ford",
                    "model": "F-150",
                },
                "score": 8.5,
                "all_scores": [("v1", 8.5)],
            },
            vm2_result={
                "distinctive_features": ["white"],
            },
            camera_label="Front Gate",
            alert=alert,
        )
        caption = result["caption"]
        assert "Alert 3 of 3" in caption
        assert "Alert ID: evt-ghi-456" in caption
        assert "Timestamp: 2026-09-12 05:00:00 EDT" in caption
        # Verify order: Camera -> Match block -> Alert lines
        parts = caption.split("\n")
        idx_camera = parts.index("Camera: Front Gate")
        idx_match = next(i for i, p in enumerate(parts) if "Match" in p and "✅" in p)
        idx_alert3 = parts.index("Alert 3 of 3")
        idx_alert_id = parts.index("Alert ID: evt-ghi-456")
        idx_ts = parts.index("Timestamp: 2026-09-12 05:00:00 EDT")
        assert idx_camera < idx_match < idx_alert3 < idx_alert_id < idx_ts

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
        # Curated vehicle fields as bullet list (US-067b)
        assert "\u2022 Make: Ford" in caption
        assert "\u2022 Model: F-150" in caption
        assert "\u2022 Wheel style: alloy" in caption
        assert "\u2022 Headlight signature: LED" in caption
        assert "\u2022 Rear lights signature: strip" in caption
        # No nested vehicle_features: parent header
        assert "vehicle_features:" not in caption

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
        # Curated person fields as bullet list (US-067b)
        assert "\u2022 Clothing upper: black hoodie" in caption
        assert "\u2022 Clothing lower: blue jeans" in caption
        assert "\u2022 Action: walking" in caption
        # No nested attributes:/signature: parent headers
        assert "attributes:" not in caption
        assert "signature:" not in caption
        # Non-curated fields omitted (carrying, confidence, notable_details)
        assert "carrying" not in caption
        assert "confidence" not in caption
        assert "notable_details" not in caption

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
        # Animal mode: all top-level scalars as bullets (US-067b)
        assert "\u2022 Species: dog" in caption
        assert "\u2022 Breed: labrador" in caption
        assert "\u2022 Size: large" in caption
        assert "\u2022 Action: walking" in caption
        # Lists and dicts skipped
        assert "distinctive_features" not in caption
        assert "confidence" not in caption
