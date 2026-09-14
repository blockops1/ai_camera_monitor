"""test_match_alert_send.py — Tests for TG#3 image attachment via better_crop.

Covers pick_alert_image_path (4 cases) and send_match_alert (photo send).

AC3: at least 4 test cases (crop_a, crop_b, neither, missing).
"""

import sys
from pathlib import Path
from unittest.mock import ANY as mock_ANY, patch

# Ensure the repo root is on sys.path so we can import telegram_formatter
REPO_ROOT = str(Path(__file__).resolve().parent.parent)
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)


# ---------------------------------------------------------------------------
# pick_alert_image_path — pure function, 4 cases
# ---------------------------------------------------------------------------


class TestPickAlertImagePath:
    """Verify the 4 better_crop decision paths."""

    def setup_method(self) -> None:
        from telegram_formatter.match_alert import pick_alert_image_path

        self.pick = pick_alert_image_path

    def test_crop_a(self) -> None:
        result = self.pick(
            {"better_crop": "crop_a"},
            "/path/to/crop_a.jpg",
            "/path/to/crop_b.jpg",
        )
        assert result == "/path/to/crop_a.jpg"

    def test_crop_b(self) -> None:
        result = self.pick(
            {"better_crop": "crop_b"},
            "/path/to/crop_a.jpg",
            "/path/to/crop_b.jpg",
        )
        assert result == "/path/to/crop_b.jpg"

    def test_neither(self) -> None:
        result = self.pick(
            {"better_crop": "neither"},
            "/path/to/crop_a.jpg",
            "/path/to/crop_b.jpg",
        )
        assert result == "/path/to/crop_a.jpg"

    def test_missing(self) -> None:
        result = self.pick({}, "/path/to/crop_a.jpg", "/path/to/crop_b.jpg")
        assert result == "/path/to/crop_a.jpg"

    def test_none_vm2_result(self) -> None:
        result = self.pick(None, "/path/to/crop_a.jpg", "/path/to/crop_b.jpg")
        assert result == "/path/to/crop_a.jpg"

    def test_crop_b_neither(self) -> None:
        """Edge: crop_b_path is None, better_crop=crop_b -> returns None."""
        result = self.pick(
            {"better_crop": "crop_b"},
            "/path/to/crop_a.jpg",
            None,
        )
        assert result is None

    def test_both_none(self) -> None:
        """Both paths None -> returns None (crop_a fallback is None)."""
        result = self.pick(
            {"better_crop": "crop_a"},
            None,
            None,
        )
        assert result is None


# ---------------------------------------------------------------------------
# send_match_alert — verify send_photo is called with picked path
# ---------------------------------------------------------------------------


class TestSendMatchAlert:
    """Verify send_match_alert calls send_photo with the right path."""

    def _make_vm2(self, better_crop=None) -> dict:
        vm2 = {
            "license_plate": "ABC123",
            "distinctive_features": ["red"],
            "class": "vehicle",
        }
        if better_crop is not None:
            vm2["better_crop"] = better_crop
        return vm2

    def test_sends_photo_for_crop_a(self) -> None:
        vm2 = self._make_vm2("crop_a")
        with patch(
            "telegram_formatter.match_alert.send_photo"
        ) as mock_send_photo, patch(
            "telegram_formatter.match_alert._send_message"
        ) as mock_send_msg:
            from telegram_formatter.match_alert import send_match_alert

            send_match_alert(
                bot_token="fake_token",
                chat_id="fake_chat",
                match_result={"matched": True},
                vm2_result=vm2,
                crop_a_path="/a.jpg",
                crop_b_path="/b.jpg",
                camera_label="Gate",
            )

            mock_send_msg.assert_called_once()
            mock_send_photo.assert_called_once_with(
                "fake_token", "fake_chat", "/a.jpg", caption=mock_ANY, base_url="https://api.telegram.org"
            )

    def test_sends_photo_for_crop_b(self) -> None:
        vm2 = self._make_vm2("crop_b")
        with patch(
            "telegram_formatter.match_alert.send_photo"
        ) as mock_send_photo, patch(
            "telegram_formatter.match_alert._send_message"
        ) as mock_send_msg:
            from telegram_formatter.match_alert import send_match_alert

            send_match_alert(
                bot_token="fake_token",
                chat_id="fake_chat",
                match_result={"matched": True},
                vm2_result=vm2,
                crop_a_path="/a.jpg",
                crop_b_path="/b.jpg",
                camera_label="Gate",
            )

            mock_send_msg.assert_called_once()
            mock_send_photo.assert_called_once_with(
                "fake_token", "fake_chat", "/b.jpg", caption=mock_ANY, base_url="https://api.telegram.org"
            )

    def test_sends_photo_for_neither(self) -> None:
        vm2 = self._make_vm2("neither")
        with patch(
            "telegram_formatter.match_alert.send_photo"
        ) as mock_send_photo, patch(
            "telegram_formatter.match_alert._send_message"
        ) as mock_send_msg:
            from telegram_formatter.match_alert import send_match_alert

            send_match_alert(
                bot_token="fake_token",
                chat_id="fake_chat",
                match_result={"matched": True},
                vm2_result=vm2,
                crop_a_path="/a.jpg",
                crop_b_path="/b.jpg",
                camera_label="Gate",
            )

            mock_send_msg.assert_called_once()
            mock_send_photo.assert_called_once_with(
                "fake_token", "fake_chat", "/a.jpg", caption=mock_ANY, base_url="https://api.telegram.org"
            )

    def test_skips_photo_when_no_path(self) -> None:
        vm2 = self._make_vm2("crop_b")
        with patch(
            "telegram_formatter.match_alert.send_photo"
        ) as mock_send_photo, patch(
            "telegram_formatter.match_alert._send_message"
        ) as mock_send_msg:
            from telegram_formatter.match_alert import send_match_alert

            send_match_alert(
                bot_token="fake_token",
                chat_id="fake_chat",
                match_result={"matched": True},
                vm2_result=vm2,
                crop_a_path=None,
                crop_b_path=None,
                camera_label="Gate",
            )

            mock_send_msg.assert_called_once()
            mock_send_photo.assert_not_called()

    def test_photo_send_does_not_block_text(self) -> None:
        """If send_photo raises, _send_message was still called."""
        vm2 = self._make_vm2("crop_a")
        with patch(
            "telegram_formatter.match_alert.send_photo",
            side_effect=RuntimeError("photo fail"),
        ), patch(
            "telegram_formatter.match_alert._send_message"
        ) as mock_send_msg:
            from telegram_formatter.match_alert import send_match_alert

            # Should not raise — send_photo is try/except'd
            send_match_alert(
                bot_token="fake_token",
                chat_id="fake_chat",
                match_result={"matched": True},
                vm2_result=vm2,
                crop_a_path="/a.jpg",
                crop_b_path="/b.jpg",
                camera_label="Gate",
            )

            mock_send_msg.assert_called_once()
