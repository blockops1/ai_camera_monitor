"""test_detail_message.py — Tests for telegram_formatter.detail.build_detail_message.

Covers US-067b acceptance criteria:
  (a) Curated attribute list replaces recursive VM2 dict dump
  (b) Single bullet per attribute (• Label: value)
  (c) Empty/null attributes skipped
  (d) No nested attributes:/signature:/vehicle_features: parent-object lines
  (e) *Attributes:* header present
  (f) TG X of 3 marker present
"""

from __future__ import annotations

from pathlib import Path

import pytest

from telegram_formatter.detail import build_detail_message


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _make_alert(
    alert_id: str = "72c70d5f",
    timestamp: str = "2026-09-22T16:21:00Z",
) -> dict:
    return {"id": alert_id, "timestamp": timestamp}


# ---------------------------------------------------------------------------
# AC(e): *Attributes:* header
# ---------------------------------------------------------------------------


class TestAttributesHeader:
    """AC: *Attributes:* bold header is present when attributes exist."""

    def test_person_has_attributes_header(self):
        result = build_detail_message(
            mode="person",
            vm2_result={
                "attributes": {
                    "clothing_upper": "black hoodie",
                    "clothing_lower": "blue jeans",
                },
                "confidence": 0.88,
            },
            crop_a=Path("/mock/a.png"),
            crop_b=Path("/mock/b.png"),
        )
        assert "*Attributes:*" in result["caption"]

    def test_vehicle_has_attributes_header(self):
        result = build_detail_message(
            mode="vehicle",
            vm2_result={
                "make": "Ford",
                "model": "F-150",
                "color": "white",
                "body_style_hint": "pickup",
            },
            crop_a=Path("/mock/a.png"),
            crop_b=Path("/mock/b.png"),
        )
        assert "*Attributes:*" in result["caption"]

    def test_empty_attrs_no_header(self):
        """No attributes → no *Attributes:* line."""
        result = build_detail_message(
            mode="person",
            vm2_result={"attributes": {}, "confidence": 0.5},
            crop_a=Path("/mock/a.png"),
            crop_b=Path("/mock/b.png"),
        )
        assert "*Attributes:*" not in result["caption"]


# ---------------------------------------------------------------------------
# AC(f): TG X of 3 marker
# ---------------------------------------------------------------------------


class TestTGMarker:
    """AC: TG {position} of 3 marker on every message."""

    def test_default_position(self):
        result = build_detail_message(
            mode="vehicle",
            vm2_result={"make": "Toyota"},
            crop_a=Path("/mock/a.png"),
            crop_b=Path("/mock/b.png"),
        )
        assert "TG 2 of 3" in result["caption"]

    def test_custom_position(self):
        result = build_detail_message(
            mode="person",
            vm2_result={"attributes": {"clothing_upper": "red shirt"}},
            crop_a=Path("/mock/a.png"),
            crop_b=Path("/mock/b.png"),
            position=3,
        )
        assert "TG 3 of 3" in result["caption"]
        assert "TG 2 of 3" not in result["caption"]

    def test_position_one(self):
        result = build_detail_message(
            mode="vehicle",
            vm2_result={"color": "red"},
            crop_a=Path("/mock/a.png"),
            crop_b=Path("/mock/b.png"),
            position=1,
        )
        assert "TG 1 of 3" in result["caption"]


# ---------------------------------------------------------------------------
# AC(a)-(b): Curated fields, bullet format
# ---------------------------------------------------------------------------


class TestCuratedPersonFields:
    """AC: person mode renders only curated fields as bullets."""

    def test_clothing_upper(self):
        result = build_detail_message(
            mode="person",
            vm2_result={
                "attributes": {"clothing_upper": "white t-shirt"},
            },
            crop_a=Path("/mock/a.png"),
            crop_b=Path("/mock/b.png"),
        )
        assert "\u2022 Clothing upper: white t-shirt" in result["caption"]

    def test_clothing_lower(self):
        result = build_detail_message(
            mode="person",
            vm2_result={
                "attributes": {"clothing_lower": "dark jeans"},
            },
            crop_a=Path("/mock/a.png"),
            crop_b=Path("/mock/b.png"),
        )
        assert "\u2022 Clothing lower: dark jeans" in result["caption"]

    def test_hat(self):
        result = build_detail_message(
            mode="person",
            vm2_result={
                "attributes": {
                    "hat": "baseball cap",
                    "hat_color": "camo",  # not curated -> omitted
                },
            },
            crop_a=Path("/mock/a.png"),
            crop_b=Path("/mock/b.png"),
        )
        assert "\u2022 Hat: baseball cap" in result["caption"]
        assert "hat_color" not in result["caption"]

    def test_action(self):
        result = build_detail_message(
            mode="person",
            vm2_result={
                "attributes": {"action": "walking"},
            },
            crop_a=Path("/mock/a.png"),
            crop_b=Path("/mock/b.png"),
        )
        assert "\u2022 Action: walking" in result["caption"]

    def test_posture(self):
        result = build_detail_message(
            mode="person",
            vm2_result={
                "attributes": {"posture": "standing"},
            },
            crop_a=Path("/mock/a.png"),
            crop_b=Path("/mock/b.png"),
        )
        assert "\u2022 Posture: standing" in result["caption"]

    def test_footwear(self):
        result = build_detail_message(
            mode="person",
            vm2_result={
                "attributes": {"footwear": "boots"},
            },
            crop_a=Path("/mock/a.png"),
            crop_b=Path("/mock/b.png"),
        )
        assert "\u2022 Footwear: boots" in result["caption"]

    def test_accessories(self):
        result = build_detail_message(
            mode="person",
            vm2_result={
                "attributes": {"accessories": "sunglasses"},
            },
            crop_a=Path("/mock/a.png"),
            crop_b=Path("/mock/b.png"),
        )
        assert "\u2022 Accessories: sunglasses" in result["caption"]

    def test_multiple_person_fields(self):
        """Several curated person fields rendered together."""
        result = build_detail_message(
            mode="person",
            vm2_result={
                "attributes": {
                    "clothing_upper": "white t-shirt",
                    "clothing_lower": "dark jeans",
                    "hat": "baseball cap",
                    "action": "walking",
                },
            },
            crop_a=Path("/mock/a.png"),
            crop_b=Path("/mock/b.png"),
        )
        caption = result["caption"]
        assert "\u2022 Clothing upper: white t-shirt" in caption
        assert "\u2022 Clothing lower: dark jeans" in caption
        assert "\u2022 Hat: baseball cap" in caption
        assert "\u2022 Action: walking" in caption

    def test_non_curated_fields_omitted(self):
        """Non-curated person attributes are NOT in the output."""
        result = build_detail_message(
            mode="person",
            vm2_result={
                "attributes": {
                    "clothing_upper": "red hoodie",
                    "silhouette": {"build": "slim"},
                    "skin_tone": "medium",
                    "age_range": "young_adult",
                    "confidence": 0.85,
                },
            },
            crop_a=Path("/mock/a.png"),
            crop_b=Path("/mock/b.png"),
        )
        caption = result["caption"]
        assert "\u2022 Clothing upper: red hoodie" in caption
        # These should NOT appear
        assert "silhouette" not in caption
        assert "skin_tone" not in caption
        assert "age_range" not in caption
        assert "confidence" not in caption


class TestCuratedVehicleFields:
    """AC: vehicle mode renders only curated fields as bullets."""

    def test_make(self):
        result = build_detail_message(
            mode="vehicle",
            vm2_result={"make": "Ford"},
            crop_a=Path("/mock/a.png"),
            crop_b=Path("/mock/b.png"),
        )
        assert "\u2022 Make: Ford" in result["caption"]

    def test_model(self):
        result = build_detail_message(
            mode="vehicle",
            vm2_result={"model": "F-150"},
            crop_a=Path("/mock/a.png"),
            crop_b=Path("/mock/b.png"),
        )
        assert "\u2022 Model: F-150" in result["caption"]

    def test_color(self):
        result = build_detail_message(
            mode="vehicle",
            vm2_result={"color": "white"},
            crop_a=Path("/mock/a.png"),
            crop_b=Path("/mock/b.png"),
        )
        assert "\u2022 Color: white" in result["caption"]

    def test_type(self):
        result = build_detail_message(
            mode="vehicle",
            vm2_result={"body_style_hint": "pickup"},
            crop_a=Path("/mock/a.png"),
            crop_b=Path("/mock/b.png"),
        )
        assert "\u2022 Type: pickup" in result["caption"]

    def test_all_vehicle_curated(self):
        result = build_detail_message(
            mode="vehicle",
            vm2_result={
                "make": "Toyota",
                "model": "Camry",
                "color": "silver",
                "body_style_hint": "sedan",
            },
            crop_a=Path("/mock/a.png"),
            crop_b=Path("/mock/b.png"),
        )
        caption = result["caption"]
        assert "\u2022 Make: Toyota" in caption
        assert "\u2022 Model: Camry" in caption
        assert "\u2022 Color: silver" in caption
        assert "\u2022 Type: sedan" in caption

    def test_distinguishing_features_from_vehicle_features(self):
        """vehicle_features sub-fields rendered as distinguishing feature bullets."""
        result = build_detail_message(
            mode="vehicle",
            vm2_result={
                "make": "Ford",
                "model": "F-150",
                "color": "black",
                "body_style_hint": "pickup",
                "vehicle_features": {
                    "wheel_style": "alloy",
                    "headlight_signature": "LED",
                    "window_tint": "dark",
                    "bed_cover": "tonneau",
                },
            },
            crop_a=Path("/mock/a.png"),
            crop_b=Path("/mock/b.png"),
        )
        caption = result["caption"]
        assert "\u2022 Wheel style: alloy" in caption
        assert "\u2022 Headlight signature: LED" in caption
        assert "\u2022 Window tint: dark" in caption
        assert "\u2022 Bed cover: tonneau" in caption

    def test_vehicle_features_not_rendered_as_nested_block(self):
        """AC(d): vehicle_features: parent header is NOT present."""
        result = build_detail_message(
            mode="vehicle",
            vm2_result={
                "make": "Tesla",
                "color": "red",
                "vehicle_features": {
                    "wheel_style": "turbo",
                },
            },
            crop_a=Path("/mock/a.png"),
            crop_b=Path("/mock/b.png"),
        )
        caption = result["caption"]
        assert "vehicle_features:" not in caption
        assert "Wheel style: turbo" in caption


# ---------------------------------------------------------------------------
# AC(c): Skip empty/null attributes
# ---------------------------------------------------------------------------


class TestSkipEmpty:
    """AC: empty/null/empty-string attributes are skipped."""

    def test_null_attribute_skipped(self):
        result = build_detail_message(
            mode="person",
            vm2_result={
                "attributes": {
                    "clothing_upper": "red shirt",
                    "clothing_lower": None,
                },
            },
            crop_a=Path("/mock/a.png"),
            crop_b=Path("/mock/b.png"),
        )
        assert "\u2022 Clothing upper: red shirt" in result["caption"]
        assert "Clothing lower" not in result["caption"]

    def test_empty_string_skipped(self):
        result = build_detail_message(
            mode="person",
            vm2_result={
                "attributes": {
                    "clothing_upper": "blue hoodie",
                    "clothing_lower": "",
                },
            },
            crop_a=Path("/mock/a.png"),
            crop_b=Path("/mock/b.png"),
        )
        assert "Clothing upper" in result["caption"]
        assert "Clothing lower" not in result["caption"]

    def test_null_null_string_skipped(self):
        """String 'null' (lowercase) should be treated as null."""
        result = build_detail_message(
            mode="person",
            vm2_result={
                "attributes": {
                    "clothing_upper": "black tee",
                    "action": "null",
                },
            },
            crop_a=Path("/mock/a.png"),
            crop_b=Path("/mock/b.png"),
        )
        assert "Clothing upper" in result["caption"]
        assert "Action" not in result["caption"]

    def test_all_null_no_attributes_line(self):
        result = build_detail_message(
            mode="person",
            vm2_result={
                "attributes": {
                    "clothing_upper": None,
                    "clothing_lower": "",
                    "action": "null",
                },
            },
            crop_a=Path("/mock/a.png"),
            crop_b=Path("/mock/b.png"),
        )
        assert "*Attributes:*" not in result["caption"]


# ---------------------------------------------------------------------------
# AC(d): No nested parent-object dump
# ---------------------------------------------------------------------------


class TestNoNestedDump:
    """AC(d): nested attributes:/signature:/confidence: parent-object keys absent."""

    def test_no_attributes_nested_header(self):
        result = build_detail_message(
            mode="person",
            vm2_result={
                "attributes": {"clothing_upper": "green jacket"},
                "confidence": 0.88,
            },
            crop_a=Path("/mock/a.png"),
            crop_b=Path("/mock/b.png"),
        )
        caption = result["caption"]
        assert "attributes:" not in caption
        assert "confidence:" not in caption
        assert "\u2022 Clothing upper: green jacket" in caption

    def test_no_signature_nested_header(self):
        result = build_detail_message(
            mode="person",
            vm2_result={
                "attributes": {"action": "running"},
                "signature": {"stable": ["scar on left cheek"]},
            },
            crop_a=Path("/mock/a.png"),
            crop_b=Path("/mock/b.png"),
        )
        caption = result["caption"]
        assert "signature:" not in caption
        assert "scar on left cheek" not in caption

    def test_no_vehicle_features_nested_header(self):
        result = build_detail_message(
            mode="vehicle",
            vm2_result={
                "make": "Honda",
                "color": "blue",
                "vehicle_features": {
                    "window_tint": "light",
                },
            },
            crop_a=Path("/mock/a.png"),
            crop_b=Path("/mock/b.png"),
        )
        caption = result["caption"]
        assert "vehicle_features:" not in caption


# ---------------------------------------------------------------------------
# General behavior
# ---------------------------------------------------------------------------


class TestGeneralBehavior:
    """General behavior tests: caption structure, photos, edge cases."""

    def test_caption_structure(self):
        """Caption has Camera, Mode, Class confirmed, attributes, TG marker."""
        result = build_detail_message(
            mode="person",
            vm2_result={
                "attributes": {
                    "clothing_upper": "white shirt",
                    "action": "walking",
                },
            },
            crop_a=Path("/mock/a.png"),
            crop_b=Path("/mock/b.png"),
            camera_label="Front Gate",
        )
        lines = result["caption"].split("\n")
        assert lines[0] == "Camera: Front Gate"
        assert lines[1] == "Mode: person"
        assert lines[2] == "Class confirmed: white shirt"
        # TG marker should be the last non-empty line
        assert lines[-1] == "TG 2 of 3"

    def test_photos_list(self):
        result = build_detail_message(
            mode="vehicle",
            vm2_result={"make": "Ford"},
            crop_a=Path("/mock/crop_a.png"),
            crop_b=Path("/mock/crop_b.png"),
        )
        assert result["photos"] == ["/mock/crop_a.png", "/mock/crop_b.png"]

    def test_photos_with_none_crops(self):
        result = build_detail_message(
            mode="vehicle",
            vm2_result={"make": "Ford"},
            crop_a=None,
            crop_b=None,
        )
        assert result["photos"] == ["", ""]

    def test_alert_metadata_included(self):
        alert = _make_alert(
            alert_id="72c70d5f-e9ce-40da-885d-d74471a22259",
            timestamp="2026-09-22T16:21:00Z",
        )
        result = build_detail_message(
            mode="person",
            vm2_result={"attributes": {"clothing_upper": "black jacket"}},
            crop_a=Path("/mock/a.png"),
            crop_b=Path("/mock/b.png"),
            alert=alert,
        )
        caption = result["caption"]
        assert "Alert 2 of 3" in caption
        assert "Alert ID: 72c70d5f-e9ce-40da-885d-d74471a22259" in caption
        assert "Timestamp:" in caption

    def test_alert_none_omits_metadata(self):
        result = build_detail_message(
            mode="vehicle",
            vm2_result={"make": "Toyota"},
            crop_a=Path("/mock/a.png"),
            crop_b=Path("/mock/b.png"),
            alert=None,
        )
        caption = result["caption"]
        assert "Alert" not in caption
        assert "Alert ID" not in caption
        assert "Timestamp" not in caption

    def test_unsure_mode(self):
        result = build_detail_message(
            mode="unsure",
            vm2_result={"reason": "too blurry to classify"},
            crop_a=Path("/mock/a.png"),
            crop_b=Path("/mock/b.png"),
        )
        assert "Class: unsure" in result["caption"]

    def test_invalid_mode_raises(self):
        with pytest.raises(ValueError, match="mode must be one of"):
            build_detail_message(
                mode="unknown",
                vm2_result={},
                crop_a=Path("/mock/a.png"),
                crop_b=Path("/mock/b.png"),
            )

    def test_bullet_format_unicode(self):
        """Verify bullets use Unicode bullet character (U+2022)."""
        result = build_detail_message(
            mode="person",
            vm2_result={"attributes": {"clothing_upper": "red shirt"}},
            crop_a=Path("/mock/a.png"),
            crop_b=Path("/mock/b.png"),
        )
        assert "\u2022" in result["caption"]
        assert "\u2022 Clothing upper: red shirt" in result["caption"]
