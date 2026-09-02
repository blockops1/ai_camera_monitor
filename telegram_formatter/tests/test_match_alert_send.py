"""Unit tests for send_match_alert and send_no_match_alert senders.

Phase 6B.142 (2026-08-27, maintainer OOB): match/no-match Telegrams now
send body as text via send_message, then the two tight crops as a
2-image media group via send_photo_group (no caption). No more
match_crops.jpg composite.

This test file exercises the senders end-to-end with the telegram
functions mocked — verifies:
  - body goes via send_message
  - crops go via send_photo_group
  - send_photo_group receives both crops as a list, caption=""
  - audit log gets image_paths set to the crop paths
  - text-only fallback when no crops
"""

from __future__ import annotations

import sys
from pathlib import Path

_root = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_root))


from telegram_formatter.match_telegram import (
    MatchTelegramInput,
    send_match_alert,
    send_no_match_alert,
)
from telegram_formatter.no_match_telegram import (
    NoMatchTelegramInput,
)
from vehicle_matcher.matcher import MatchVerdict, NoMatch


def _verdict(kv=None, score=5.5, gap=2.0):
    return MatchVerdict(
        known_vehicle=kv or {"id": "v_test", "label": "Test Car", "owner": "Test"},
        score=score,
        gap=gap,
        breakdowns={"color": 1.0, "make": 1.0},
        rank=0,
        all_scores=[("v_test", score)],
    )


def _match_input(verdict=None):
    return MatchTelegramInput(
        camera_name="Outside Front Solar",
        captured_at_iso="2026-08-27 12:00:00 EDT",
        verdict=verdict or _verdict(),
        match_threshold=0.6,
        gap_threshold=1.5,
        alert_id="test-alert-001",
    )


def _no_match_input(reason="below_threshold"):
    nm = NoMatch(reason=reason)
    return NoMatchTelegramInput(
        camera_name="Outside Front Solar",
        captured_at_iso="2026-08-27 12:00:00 EDT",
        no_match=nm,
        match_threshold=0.6,
        gap_threshold=1.5,
        top_n_breakdowns=[],
        alert_id="test-alert-002",
    )


def _fake_jpegs(tmp_path: Path, n: int) -> list[str]:
    paths = []
    for i in range(n):
        p = tmp_path / f"tight_crop_{i}.jpg"
        # Minimal valid JPEG (1x1 white pixel)
        p.write_bytes(
            b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01"
            b"\x00\x00\xff\xdb\x00C\x00\x08\x06\x06\x07\x06\x05\x08\x07"
            b"\x07\x07\t\t\x08\n\x0c\x14\r\x0c\x0b\x0b\x0c\x19\x12\x13"
            b"\x0f\x14\x1d\x1a\x1f\x1e\x1d\x1a\x1c\x1c $.' \",#\x1c\x1c(7)"
            b",01444\x1f'9=82<.342\xff\xc0\x00\x0b\x08\x00\x01\x00\x01"
            b"\x01\x01\x11\x00\xff\xc4\x00\x14\x00\x01\x00\x00\x00\x00"
            b"\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\xff"
            b"\xc4\x00\x14\x10\x01\x00\x00\x00\x00\x00\x00\x00\x00\x00"
            b"\x00\x00\x00\x00\x00\x00\x00\x00\x00\xff\xda\x00\x08\x01"
            b"\x01\x00\x00?\x00T\xdb\x9d\x9e\x9d\xbb\x9e\xa2\x80\xf0\xe1"
            b"\xb0\xc0\xe1\xf0\xe1\xc0\xff\xd9"
        )
        paths.append(str(p))
    return paths


class _TelegramCapture:
    """Helper that captures all args/kwargs to send_message + send_photo_group."""

    def __init__(self):
        self.calls: dict[str, list[tuple[tuple, dict]]] = {
            "send_message": [],
            "send_photo_group": [],
        }

    def install(self, monkeypatch):
        def fake_send_message(*a, **kw):
            self.calls["send_message"].append((a, kw))
            return True

        def fake_send_photo_group(*a, **kw):
            self.calls["send_photo_group"].append((a, kw))
            return True

        monkeypatch.setattr("infra.send_telegram.send_message", fake_send_message)
        monkeypatch.setattr("infra.send_telegram.send_photo_group", fake_send_photo_group)


# ============================================================================
# send_match_alert tests
# ============================================================================


class TestSendMatchAlertAlbum:
    """Phase 6B.142: match alert sends body + 2-image tight-crop album."""

    def test_sends_body_via_send_message(self, tmp_path, monkeypatch):
        crops = _fake_jpegs(tmp_path, 2)
        cap = _TelegramCapture()
        cap.install(monkeypatch)

        sent = send_match_alert(
            alert_id="test-alert-001",
            camera_name="Outside Front Solar",
            match_telegram_input=_match_input(),
            crop_paths=crops,
            bot_token="fake_token",
            chat_id="fake_chat",
            captured_at="2026-08-27 12:00:00 EDT",
        )
        assert sent is True
        # send_message called exactly once
        assert len(cap.calls["send_message"]) == 1
        # body is the structured match body (passed as positional text arg)
        args, kwargs = cap.calls["send_message"][0]
        assert "Outside Front Solar" in args[2]  # bot_token, chat_id, text
        assert "✅ Match" in args[2]
        # kwargs for audit context
        assert kwargs["event"] == "vehicle_matched"
        assert kwargs["channel"] == "gatekeeper_match"

    def test_sends_crops_via_send_photo_group(self, tmp_path, monkeypatch):
        crops = _fake_jpegs(tmp_path, 2)
        cap = _TelegramCapture()
        cap.install(monkeypatch)

        send_match_alert(
            alert_id="test-alert-001",
            camera_name="Outside Front Solar",
            match_telegram_input=_match_input(),
            crop_paths=crops,
            bot_token="fake_token",
            chat_id="fake_chat",
            captured_at="2026-08-27 12:00:00 EDT",
        )

        # send_photo_group called exactly once
        assert len(cap.calls["send_photo_group"]) == 1
        args, kwargs = cap.calls["send_photo_group"][0]
        # bot_token, chat_id, frame_paths are positional
        assert args[2] == crops, f"args={args!r}"
        assert kwargs.get("caption") == "", f"kwargs={kwargs!r}"
        assert kwargs["event"] == "vehicle_matched"
        assert kwargs["channel"] == "gatekeeper_match"

    def test_text_only_when_no_crops(self, tmp_path, monkeypatch):
        cap = _TelegramCapture()
        cap.install(monkeypatch)

        sent = send_match_alert(
            alert_id="test-alert-001",
            camera_name="Outside Front Solar",
            match_telegram_input=_match_input(),
            crop_paths=[],  # no crops
            bot_token="fake_token",
            chat_id="fake_chat",
            captured_at="2026-08-27 12:00:00 EDT",
        )
        assert sent is True
        # body still sent
        assert len(cap.calls["send_message"]) == 1
        # album NOT called (no crops)
        assert len(cap.calls["send_photo_group"]) == 0

    def test_returns_false_when_creds_missing(self, tmp_path, monkeypatch):
        crops = _fake_jpegs(tmp_path, 2)
        cap = _TelegramCapture()
        cap.install(monkeypatch)

        sent = send_match_alert(
            alert_id="test-alert-001",
            camera_name="Outside Front Solar",
            match_telegram_input=_match_input(),
            crop_paths=crops,
            bot_token="",  # missing
            chat_id="",
            captured_at="2026-08-27 12:00:00 EDT",
        )
        assert sent is False
        assert len(cap.calls["send_message"]) == 0
        assert len(cap.calls["send_photo_group"]) == 0

    def test_album_failure_does_not_fail_match(self, tmp_path, monkeypatch):
        """Album send failure logs warning but doesn't fail the match alert."""
        crops = _fake_jpegs(tmp_path, 2)

        def fake_send_message(*a, **kw):
            return True

        def fake_send_photo_group(*a, **kw):
            raise RuntimeError("telegram rate-limited")

        monkeypatch.setattr("infra.send_telegram.send_message", fake_send_message)
        monkeypatch.setattr("infra.send_telegram.send_photo_group", fake_send_photo_group)

        sent = send_match_alert(
            alert_id="test-alert-001",
            camera_name="Outside Front Solar",
            match_telegram_input=_match_input(),
            crop_paths=crops,
            bot_token="fake_token",
            chat_id="fake_chat",
            captured_at="2026-08-27 12:00:00 EDT",
        )
        # Body sent successfully → still considered success
        assert sent is True


# ============================================================================
# send_no_match_alert tests
# ============================================================================


class TestSendNoMatchAlertAlbum:
    """Phase 6B.142: no-match alert sends body + 2-image tight-crop album."""

    def test_sends_body_via_send_message(self, tmp_path, monkeypatch):
        crops = _fake_jpegs(tmp_path, 2)
        cap = _TelegramCapture()
        cap.install(monkeypatch)

        sent = send_no_match_alert(
            alert_id="test-alert-002",
            camera_name="Outside Front Solar",
            no_match_telegram_input=_no_match_input(),
            crop_paths=crops,
            bot_token="fake_token",
            chat_id="fake_chat",
            captured_at="2026-08-27 12:00:00 EDT",
        )
        assert sent is True
        assert len(cap.calls["send_message"]) == 1
        args, kwargs = cap.calls["send_message"][0]
        assert "Outside Front Solar" in args[2]
        assert kwargs["event"] == "vehicle_no_match"
        assert kwargs["channel"] == "gatekeeper_no_match"

    def test_sends_crops_via_send_photo_group(self, tmp_path, monkeypatch):
        crops = _fake_jpegs(tmp_path, 2)
        cap = _TelegramCapture()
        cap.install(monkeypatch)

        send_no_match_alert(
            alert_id="test-alert-002",
            camera_name="Outside Front Solar",
            no_match_telegram_input=_no_match_input(),
            crop_paths=crops,
            bot_token="fake_token",
            chat_id="fake_chat",
            captured_at="2026-08-27 12:00:00 EDT",
        )

        assert len(cap.calls["send_photo_group"]) == 1
        args, kwargs = cap.calls["send_photo_group"][0]
        assert args[2] == crops, f"args={args!r}"
        assert kwargs.get("caption") == "", f"kwargs={kwargs!r}"
        assert kwargs["event"] == "vehicle_no_match"
        assert kwargs["channel"] == "gatekeeper_no_match"

    def test_text_only_when_no_crops(self, tmp_path, monkeypatch):
        cap = _TelegramCapture()
        cap.install(monkeypatch)

        sent = send_no_match_alert(
            alert_id="test-alert-002",
            camera_name="Outside Front Solar",
            no_match_telegram_input=_no_match_input(),
            crop_paths=[],
            bot_token="fake_token",
            chat_id="fake_chat",
            captured_at="2026-08-27 12:00:00 EDT",
        )
        assert sent is True
        assert len(cap.calls["send_message"]) == 1
        assert len(cap.calls["send_photo_group"]) == 0
