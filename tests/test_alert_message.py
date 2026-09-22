"""test_alert_message.py — Tests for telegram_formatter.alert.build_alert_message.

Covers US-067a acceptance criteria:
  (a) 4-6 line Markdown body with camera header + class line + TG marker + short ID
  (b) TG X of 3 position marker
  (c) No old-style labels (Classification:/YOLO:/Vision:)
  (d) Emoji per class type (person, vehicle, animal, other)
  (e) MarkdownV2 escaping
  (f) Optional notes line
  (g) photos=[composite] / photos=[] construction
"""

from __future__ import annotations

from unittest.mock import MagicMock

from infra.alert_artifacts import AlertArtifacts
from infra.gate import GateVerdict
from telegram_formatter.alert import (
    _emoji_for_class,
    _escape_markdown_v2,
    build_alert_message,
)


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
    return {
        "class": cls,
        "subclass": subclass,
        "confidence": confidence,
        "notes": notes,
        "count": count,
        "face_visible": face_visible,
    }


def _make_verdict() -> GateVerdict:
    """Build a minimal GateVerdict mock."""
    v = MagicMock(spec=GateVerdict)
    v.classification = "vehicle"
    v.class_label = "car"
    v.confidence = 0.95
    v.top_class = "car"
    v.top_confidence = 0.95
    v.reason = "high_conf_vehicle"
    v.crop_bbox_a = (100, 200, 50, 80)
    return v


def _make_alert(
    alert_id: str = "72c70d5f-e9ce-40da-885d-d74471a22259",
    timestamp: str = "2026-09-22 12:21:00",
) -> dict:
    return {"id": alert_id, "timestamp": timestamp}


class TestBuildAlertMessage:
    """AC: build_alert_message photo-list construction."""

    def test_full_artifacts_returns_composite_only(self):
        """AC(a): full artifacts -> photos=[composite]."""
        artifacts = _make_artifacts()
        result = build_alert_message(
            verdict=_make_verdict(),
            vm1_result=_make_vm1_result(),
            artifacts=artifacts,
        )

        assert len(result["photos"]) == 1
        assert result["photos"][0] == "/mock/composite.png"

    def test_composite_none_returns_empty_photos(self):
        """AC(b): composite=None -> photos=[] only."""
        artifacts = _make_artifacts(composite_path=None)
        result = build_alert_message(
            verdict=_make_verdict(),
            vm1_result=_make_vm1_result(),
            artifacts=artifacts,
        )

        assert len(result["photos"]) == 0

    def test_caption_includes_classification(self):
        """Caption contains classification and confidence."""
        result = build_alert_message(
            verdict=_make_verdict(),
            vm1_result=_make_vm1_result(cls="person", confidence="likely"),
            artifacts=_make_artifacts(),
        )

        assert "person" in result["caption"]
        assert "likely" in result["caption"]

    def test_caption_includes_notes(self):
        """Caption includes notes if provided."""
        result = build_alert_message(
            verdict=_make_verdict(),
            vm1_result=_make_vm1_result(notes="Additional detail here"),
            artifacts=_make_artifacts(),
        )

        assert "Additional detail here" in result["caption"]


class TestMarkdownFormat:
    """AC: new 4-6 line Markdown body format (US-067a)."""

    def test_header_line_camera_and_time(self):
        """Header: camera emoji + bold camera name + time."""
        result = build_alert_message(
            verdict=_make_verdict(),
            vm1_result=_make_vm1_result(cls="person"),
            artifacts=_make_artifacts(),
            camera_label="Front Door",
            position=1,
            alert=_make_alert(),
        )
        lines = result["caption"].split("\n")
        assert len(lines) >= 1
        # First line has camera emoji (0x1f3a5) and camera name
        assert "Front Door" in lines[0]
        assert "\U0001f3a5" in lines[0]  # camera emoji
        # Timestamp is MarkdownV2-escaped, so : and - are backslash-escaped
        assert "08" in lines[0] and "21" in lines[0]  # time portion (EDT = UTC-4)

    def test_class_line_with_emoji_and_confidence(self):
        """Class line: emoji + bold class name + confidence percentage."""
        result = build_alert_message(
            verdict=_make_verdict(),
            vm1_result=_make_vm1_result(cls="person", confidence="92"),
            artifacts=_make_artifacts(),
            position=2,
        )
        lines = result["caption"].split("\n")
        # Second line has person emoji and class name
        assert "\U0001f464" in lines[1]  # person emoji
        assert "person" in lines[1]
        assert "92%" in lines[1]

    def test_tg_position_marker(self):
        """TG marker line: 'TG X of 3'."""
        for pos in [1, 2, 3]:
            result = build_alert_message(
                verdict=_make_verdict(),
                vm1_result=_make_vm1_result(),
                artifacts=_make_artifacts(),
                position=pos,
            )
            assert f"TG {pos} of 3" in result["caption"]

    def test_short_alert_id(self):
        """Short ID: first 8 chars of alert_id."""
        result = build_alert_message(
            verdict=_make_verdict(),
            vm1_result=_make_vm1_result(),
            artifacts=_make_artifacts(),
            alert=_make_alert(alert_id="72c70d5f-xxxx-xxxx-xxxx-xxxxxxxxxxxx"),
        )
        # One line should be just the 8-char short ID
        assert "72c70d5f" in result["caption"]
        # Full UUID should NOT be in the caption
        assert "xxxx-xxxx-xxxx-xxxxxxxxxxxx" not in result["caption"]

    def test_no_full_alert_id_in_caption(self):
        """Full UUID stays in JSON only, not in TG body."""
        full_id = "72c70d5f-e9ce-40da-885d-d74471a22259"
        result = build_alert_message(
            verdict=_make_verdict(),
            vm1_result=_make_vm1_result(),
            artifacts=_make_artifacts(),
            alert=_make_alert(alert_id=full_id),
        )
        assert full_id not in result["caption"]

    def test_notes_optional_line(self):
        """Notes line only when notes is non-empty."""
        # With notes
        result = build_alert_message(
            verdict=_make_verdict(),
            vm1_result=_make_vm1_result(notes="Dog barking loudly"),
            artifacts=_make_artifacts(),
        )
        assert "Dog barking loudly" in result["caption"]

        # Without notes — caption should not have an empty notes line
        result_no_notes = build_alert_message(
            verdict=_make_verdict(),
            vm1_result=_make_vm1_result(notes=None),
            artifacts=_make_artifacts(),
        )
        lines = [l for l in result_no_notes["caption"].split("\n") if l]
        # Should have 4 lines: header, class, TG marker, short ID (or 3 if no alert)
        assert len(lines) == 3  # no alert_id, so header + class + TG marker

    def test_no_old_style_labels(self):
        """AC(c): no Classification:/YOLO:/Vision: labels."""
        result = build_alert_message(
            verdict=_make_verdict(),
            vm1_result=_make_vm1_result(cls="person"),
            artifacts=_make_artifacts(),
            alert=_make_alert(),
        )
        caption = result["caption"]
        assert "Classification:" not in caption
        assert "YOLO:" not in caption
        assert "Vision:" not in caption
        assert "Camera:" not in caption
        assert "Alert ID:" not in caption


class TestEmojiMapping:
    """AC: emoji icons per class type."""

    def test_person_emoji(self):
        assert _emoji_for_class("person") == "\U0001f464"  # person

    def test_vehicle_emoji(self):
        assert _emoji_for_class("vehicle") == "\U0001f697"  # vehicle

    def test_animal_emoji(self):
        assert _emoji_for_class("animal") == "\U0001f415"  # animal

    def test_unknown_emoji(self):
        assert _emoji_for_class("unknown") == "\U0001f4e6"  # box


class TestMarkdownVEscaping:
    """AC: MarkdownV2 special chars are escaped."""

    def test_asterisks_escaped(self):
        """Asterisks in user data should be escaped."""
        escaped = _escape_markdown_v2("hello*world")
        assert "\\*hello\\*world" == escaped or "\\*world" in escaped

    def test_underscore_escaped(self):
        escaped = _escape_markdown_v2("hello_world")
        assert "\\_" in escaped

    def test_brackets_escaped(self):
        escaped = _escape_markdown_v2("hello[world]")
        assert "\\[" in escaped and "\\]" in escaped

    def test_backtick_escaped(self):
        escaped = _escape_markdown_v2("hello`world`")
        assert "\\`" in escaped

    def test_backslash_escaped(self):
        escaped = _escape_markdown_v2("hello\\world")
        assert escaped == "hello\\\\world"

    def test_safe_chars_preserved(self):
        """Safe characters should pass through unchanged."""
        safe = "abc123"
        assert _escape_markdown_v2(safe) == safe

    def test_caption_uses_escaped_values(self):
        """Camera label with special chars should be escaped in caption."""
        result = build_alert_message(
            verdict=_make_verdict(),
            vm1_result=_make_vm1_result(cls="person"),
            artifacts=_make_artifacts(),
            camera_label="Front [Door]",
            position=1,
            alert=_make_alert(alert_id="test_id1"),
        )
        assert "[Front" not in result["caption"]  # brackets escaped
        assert "\\[" in result["caption"]


class TestCaptionTruncation:
    """AC: caption is truncated to 1024 chars."""

    def test_truncation(self):
        """Caption > 1024 chars gets truncated with ellipsis."""
        # Inject a very long notes field
        long_notes = "A" * 2000
        result = build_alert_message(
            verdict=_make_verdict(),
            vm1_result=_make_vm1_result(notes=long_notes),
            artifacts=_make_artifacts(),
            alert=_make_alert(),
        )
        assert len(result["caption"]) <= 1024
        assert "..." in result["caption"]
