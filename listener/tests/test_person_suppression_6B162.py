"""Phase 6B.162 — person alert suppression for verified false positives.

Tests the suppression path: gate=person, Qwen=no_person_in_frame →
suppressed, no Telegram, audit logged, cooldown not consumed.
"""

import sys

sys.path.insert(0, "ai_camera_monitor")

from unittest.mock import patch


class TestPersonSuppression6B162:
    """Suppression logic for no_person_in_frame verdicts."""

    def _make_verdict(self, tmp_path):
        """Create a GateVerdict with 4 PIL frames (mirrors test fixture)."""
        from PIL import Image as PILImage

        from listener.motion_gate_pipeline import GateVerdict

        colors = [(255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 0)]
        pil_frames = []
        for i, color in enumerate(colors, start=1):
            img = PILImage.new("RGB", (640, 480), color=color)
            pil_frames.append(img)
        crop_a_pil = pil_frames[2].copy()
        crop_b_pil = pil_frames[3].copy()

        out_dir = tmp_path / "alert"
        out_dir.mkdir()
        for i in range(4):
            pil_frames[i].save(str(out_dir / f"frame_{i+1:03d}.jpg"), "JPEG")

        return GateVerdict(
            decision="person",
            class_label="person",
            confidence=0.58,
            frames=pil_frames,
            crop_a=crop_a_pil,
            crop_b=crop_b_pil,
            frame_paths=[str(out_dir / f"frame_{i+1:03d}.jpg") for i in range(4)],
            reason="high_conf_person",
        )

    def _make_ctx(self, output_dir, gate_verdict=None):
        from listener.person_event_pipeline import PersonContext

        ctx = PersonContext(
            alert_id="test-suppress-1",
            camera_name="CAM4",
            timestamp="2026-08-29T02:09:10",
            event_type="person",
            rtsp_url="rtsp://test/cam",
            output_dir=output_dir,
            bot_token="token",
            chat_id="chat",
            api_url="http://127.0.0.1:8093/v1",
        )
        if gate_verdict is not None:
            ctx.gate_verdict = gate_verdict
        return ctx

    def test_suppression_no_person_in_frame(self, tmp_path):
        """Gate=person, Qwen=no person → suppressed, no Telegram."""
        from listener.person_event_pipeline import process_person_event

        verdict = self._make_verdict(tmp_path)
        ctx = self._make_ctx(str(tmp_path / "output"), gate_verdict=verdict)

        with patch(
            "infra.vision_analyzer.analyze_frames_queued",
            return_value={"persons": [], "scene_description": "empty scene"},
        ), patch(
            "listener.person_event_pipeline._load_known_persons_for_matching",
            return_value=[{"name": "<owner-name>", "clothing_upper_color": "red"}],
        ), patch(
            "infra.send_telegram.send_message"
        ), patch(
            "infra.send_telegram.send_photo_group"
        ), patch(
            "infra.alert_history.append_alert"
        ):
            result = process_person_event(ctx)

        assert result["suppressed"] is True
        assert result["suppressed_reason"] == "no_person_in_frame"
        assert result["matched_name"] is None
        assert result["telegram_sent"] is False
        assert result["structured_body"] == ""

    def test_no_suppression_no_known_persons(self, tmp_path):
        """Gate=person, Qwen=person (unknown) → alert sent (not suppressed)."""
        from listener.person_event_pipeline import process_person_event

        verdict = self._make_verdict(tmp_path)
        ctx = self._make_ctx(str(tmp_path / "output"), gate_verdict=verdict)

        with patch(
            "infra.vision_analyzer.analyze_frames_queued",
            return_value={
                "persons": [{
                    "clothing_upper": {"color": "red", "type": "jacket"},
                    "face_visible": False,
                }],
                "primary_person_index": 0,
            },
        ), patch(
            "listener.person_event_pipeline._load_known_persons_for_matching",
            return_value=[],  # no known persons → NoMatch(reason="no_known_persons")
        ), patch(
            "infra.send_telegram.send_message", return_value=True
        ), patch(
            "infra.send_telegram.send_photo_group", return_value=True
        ), patch(
            "infra.alert_history.append_alert"
        ):
            result = process_person_event(ctx)

        assert result["suppressed"] is False
        assert result["matched_name"] is None
        assert result["telegram_sent"] is True

    def test_suppression_flag_behavior(self):
        """NoMatch suppress field defaults to False, set True only for no_person_in_frame."""
        from infra.person_matcher import NoMatch

        no_match = NoMatch(reason="no_person_in_frame", suppress=True)
        assert no_match.suppress is True
        assert no_match.reason == "no_person_in_frame"

        other_match = NoMatch(reason="no_known_persons")
        assert other_match.suppress is False
        assert other_match.reason == "no_known_persons"
