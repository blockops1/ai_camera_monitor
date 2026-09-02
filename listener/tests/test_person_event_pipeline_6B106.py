"""
test_person_event_pipeline.py — Tests for listener.person_event_pipeline.

Phase 6B.106 (2026-08-22). Tests the 4 stages (capture → identify → match
→ emit) and the process_person_event orchestrator. All I/O is mocked:
- gate_aware_person_capture reads from a GateVerdict fixture
  (Phase 6B.139, §11.60); the legacy `capture_frames` RTSP pull
  was REMOVED.
- analyze_frames_queued returns a fixed vision_result dict
- recognize_faces returns a fixed face_recognition dict
- list_identities returns a fixed known_persons list
- send_photo_with_caption / send_message / send_photo_group record their
  args instead of sending
  (Phase 6B.141: person_emit now uses send_message + send_photo_group
  instead of send_photo_with_caption — text body + 6-image album)
- append_alert records its payload instead of writing

Tests pin:
  - Stage sequencing (each stage populates ctx.* correctly)
  - Frame capture failure short-circuits the pipeline gracefully
  - Vision failure populates error sentinel, match still runs, NoMatch
  - Face recognition: MatchVerdict when confident, fall-through when not
  - Clothing match: takes over when no face recognition result
  - Telegram body: structured, includes all Qwen attributes
  - Audit log: append_alert called with correct payload
  - Audio: env-gated (off by default), no-op when disabled
  - Result dict: includes matched_name, matched_via, telegram_sent

Phase 6B.139 (§11.60): tests now provide a GateVerdict via the
`gate_verdict_with_person_frames` fixture (from test_gate_aware_capture.py)
instead of mocking `infra.frame_capture.capture_frames`. The 3 direct
`person_capture_stage` tests pin the deprecation stub (returns empty
frame_paths).
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

_root = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_root))


import pytest

from listener.person_event_pipeline import (
    PERSON_CAPTURE_FRAME_COUNT,
    PersonContext,
    _build_structured_body,
    _extract_primary_person,
    person_capture_stage,
    person_emit_stage,
    person_identify_stage,
    person_match_stage,
    process_person_event,
)

# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


def _make_ctx(**overrides) -> PersonContext:
    """Build a PersonContext with sensible defaults for tests."""
    defaults: dict = {
        "alert_id": "test-alert-123",
        "camera_name": "CAM1",
        "timestamp": "2026-08-22 14:00:00 EDT",
        "event_type": "person",
        "rtsp_url": "rtsp://test/front-door-outside",
        "output_dir": "/tmp/test-frames/test-alert-123",
        "bot_token": "test-token",
        "chat_id": "test-chat",
        "api_url": "http://test-vision:8080",
    }
    defaults.update(overrides)
    return PersonContext(**defaults)


@pytest.fixture
def captured_frame_paths(tmp_path: Path) -> list[str]:
    """Two real (minimal) JPEG frame paths written to tmp.

    Uses PIL to write actual decodable JPEG bytes so that
    _run_face_recognition's Image.open() doesn't fail.
    """
    from PIL import Image

    paths = []
    for i in range(PERSON_CAPTURE_FRAME_COUNT):
        p = tmp_path / f"frame_{i}.jpg"
        # Create a minimal 64x48 RGB image (small but valid JPEG)
        img = Image.new("RGB", (64, 48), color=(100, 150, 200))
        img.save(str(p), format="JPEG", quality=50)
        paths.append(str(p))
    return paths


@pytest.fixture
def vision_result_person_visible() -> dict:
    """Qwen output for a person with face visible."""
    return {
        "persons": [{
            "person_id": "p1",
            "clothing_upper": {"color": "blue", "type": "jacket"},
            "clothing_lower": {"color": "black", "type": "pants"},
            "carrying": ["red backpack"],
            "action": "walking",
            "face_visible": True,
            "face_bbox": [100, 100, 200, 200],
        }],
        "primary_person_index": 0,
        "scene_description": "A person in a blue jacket walks toward the door carrying a red backpack.",
        "confidence": 0.85,
        "notable_details": [],
        "frame_positions": [],
    }


@pytest.fixture
def vision_result_no_face() -> dict:
    """Qwen output for a person with no face visible."""
    return {
        "persons": [{
            "person_id": "p1",
            "clothing_upper": {"color": "red", "type": "shirt"},
            "clothing_lower": {"color": "blue", "type": "jeans"},
            "carrying": [],
            "action": "approaching",
            "face_visible": False,
            "face_bbox": None,
        }],
        "primary_person_index": 0,
        "scene_description": "A person in red walks toward the door.",
        "confidence": 0.9,
    }


@pytest.fixture
def gate_verdict_with_person_frames(tmp_path):
    """GateVerdict with 4 PIL frames for person path (Phase 6B.139, §11.60).

    Mirror of gate_verdict_with_frames in test_gate_aware_capture.py, kept
    local so this test file stays self-contained. 4 distinct colors so
    tests can assert which frames were selected (green + blue for
    indices 1 and 2). Phase 6B.140 (2026-08-27): camera_id = CAM3.
    """
    from PIL import Image as _PILImage

    from listener.motion_gate_pipeline import GateVerdict

    out_dir = tmp_path / "alert"
    out_dir.mkdir()
    colors = [(255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 0)]
    pil_frames = []
    for i, color in enumerate(colors, start=1):
        path = out_dir / f"frame_{i:03d}.jpg"
        img = _PILImage.new("RGB", (640, 480), color=color)
        img.save(str(path), "JPEG")
        pil_frames.append(img)
    crop_a_pil = pil_frames[2].copy()
    crop_b_pil = pil_frames[3].copy()

    return GateVerdict(
        decision="person",
        class_label="person",
        confidence=0.82,
        crop_a_path=str(out_dir / "frame_003.jpg"),
        crop_b_path=str(out_dir / "frame_004.jpg"),
        bbox_a=(180, 200, 320, 480),
        bbox_b=(200, 220, 300, 460),
        frames=pil_frames,
        crop_a=crop_a_pil,
        crop_b=crop_b_pil,
        frame_paths=[str(out_dir / f"frame_{i:03d}.jpg") for i in (1, 2, 3, 4)],
        raw_verdicts=[],
        reason="high_conf_person",
    )


# ---------------------------------------------------------------------------
# Stage 1: capture (Phase 6B.139 stub — gate is the sole frame producer)
# ---------------------------------------------------------------------------


class TestPersonCaptureStage:
    """Phase 6B.139: person_capture_stage is a deprecation stub.

    The motion gate is now the sole producer of frames for person events;
    gate_aware_person_capture (in listener/_gate_aware_capture.py) reads
    PIL frames + crops from the gate verdict. This test class pins the
    deprecation behavior so future refactors don't accidentally
    re-introduce the second RTSP pull.
    """

    def test_capture_stage_returns_empty_frame_paths(self, tmp_path):
        """person_capture_stage stub sets frame_paths = [] (no RTSP pull)."""

        ctx = _make_ctx(output_dir=str(tmp_path / "output"))

        person_capture_stage(ctx)

        assert ctx.frame_paths == []

    def test_capture_stage_does_not_call_capture_frames(self, tmp_path):
        """person_capture_stage does NOT call infra.frame_capture.capture_frames."""
        from unittest.mock import patch


        ctx = _make_ctx(output_dir=str(tmp_path / "output"))

        with patch("infra.frame_capture.capture_frames") as mock_capture:
            person_capture_stage(ctx)
            mock_capture.assert_not_called()

    def test_capture_stage_logs_deprecation_warning(self, tmp_path, caplog):
        """Stub logs a deprecation warning with the 6B.139 marker."""
        import logging


        ctx = _make_ctx(output_dir=str(tmp_path / "output"))

        with caplog.at_level(logging.WARNING, logger="listener.person_event_pipeline"):
            person_capture_stage(ctx)

        assert any("DEPRECATED 2026-08-27" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# Stage 2: identify
# ---------------------------------------------------------------------------


class TestPersonIdentifyStage:
    def test_identify_with_face_visible(self, captured_frame_paths, vision_result_person_visible):
        ctx = _make_ctx(frame_paths=captured_frame_paths)
        with patch(
            "infra.vision_analyzer.analyze_frames_queued",
            return_value=vision_result_person_visible,
        ), patch(
            "infra.face_recognition.recognize_faces",
            return_value={
                "faces": [{
                    "bbox": [100, 100, 200, 200],
                    "embedding": [0.1] * 512,
                    "identified_name": "employee_b",
                    "confidence": 0.7,
                    "is_known": True,
                }],
                "identified_person": "employee_b",
                "best_confidence": 0.7,
            },
        ):
            person_identify_stage(ctx)
        assert ctx.vision_result == vision_result_person_visible
        assert ctx.face_recognition is not None
        assert ctx.face_recognition["identified_person"] == "employee_b"

    def test_identify_no_face_skips_arcface(self, captured_frame_paths, vision_result_no_face):
        ctx = _make_ctx(frame_paths=captured_frame_paths)
        with patch(
            "infra.vision_analyzer.analyze_frames_queued",
            return_value=vision_result_no_face,
        ):
            person_identify_stage(ctx)
        assert ctx.face_recognition is None

    def test_identify_vision_failure_records_error(self, captured_frame_paths):
        ctx = _make_ctx(frame_paths=captured_frame_paths)
        with patch(
            "infra.vision_analyzer.analyze_frames_queued",
            side_effect=Exception("Qwen down"),
        ):
            person_identify_stage(ctx)
        assert ctx.vision_result == {"persons": [], "error": "Qwen down"}


# ---------------------------------------------------------------------------
# Stage 3: match
# ---------------------------------------------------------------------------


class TestPersonMatchStage:
    def test_match_face_recognition_wins(
        self, captured_frame_paths, vision_result_person_visible
    ):
        ctx = _make_ctx(
            frame_paths=captured_frame_paths,
            vision_result=vision_result_person_visible,
            face_recognition={
                "faces": [{
                    "bbox": [100, 100, 200, 200],
                    "embedding": [0.1] * 512,
                    "identified_name": "maintainer",
                    "confidence": 0.85,
                    "is_known": True,
                }],
                "identified_person": "maintainer",
                "best_confidence": 0.85,
            },
        )
        with patch(
            "listener.person_event_pipeline._load_known_persons_for_matching",
            return_value=[],
        ):
            person_match_stage(ctx)
        assert ctx.matched_name == "maintainer"
        assert ctx.matched_via == "face_recognition"

    def test_match_clothing_when_face_no_match(
        self, captured_frame_paths, vision_result_no_face
    ):
        ctx = _make_ctx(
            frame_paths=captured_frame_paths,
            vision_result=vision_result_no_face,
            face_recognition=None,
        )
        with patch(
            "listener.person_event_pipeline._load_known_persons_for_matching",
            return_value=[{"name": "employee_b", "clothing_upper_color": "red", "role": "resident"}],
        ):
            person_match_stage(ctx)
        assert ctx.matched_name == "employee_b"
        assert ctx.matched_via == "clothing_color"

    def test_match_no_known_persons_returns_no_match(
        self, captured_frame_paths, vision_result_no_face
    ):
        ctx = _make_ctx(
            frame_paths=captured_frame_paths,
            vision_result=vision_result_no_face,
            face_recognition=None,
        )
        with patch(
            "listener.person_event_pipeline._load_known_persons_for_matching",
            return_value=[],
        ):
            person_match_stage(ctx)
        assert ctx.matched_name is None

    def test_match_empty_vision_returns_no_match(self, captured_frame_paths):
        ctx = _make_ctx(
            frame_paths=captured_frame_paths,
            vision_result={"persons": []},
            face_recognition=None,
        )
        with patch(
            "listener.person_event_pipeline._load_known_persons_for_matching",
            return_value=[{"name": "maintainer", "clothing_upper_color": "red"}],
        ):
            person_match_stage(ctx)
        assert ctx.matched_name is None


# ---------------------------------------------------------------------------
# Stage 4: emit
# ---------------------------------------------------------------------------


class TestPersonEmitStage:
    def test_emit_sends_telegram_with_structured_body(
        self, captured_frame_paths, vision_result_person_visible
    ):
        from infra.person_matcher import MatchVerdict

        ctx = _make_ctx(
            camera_name="CAM3",  # Phase 6B.140
            frame_paths=captured_frame_paths,
            vision_result=vision_result_person_visible,
            face_recognition=None,
            person_match=MatchVerdict(
                matched_name="maintainer",
                matched_via="face_recognition",
                confidence=0.85,
                face_bbox=[100, 100, 200, 200],
            ),
            matched_name="maintainer",
            matched_via="face_recognition",
        )
        with patch(
            "infra.send_telegram.send_message"
        ) as mock_send_text, patch(
            "infra.send_telegram.send_photo_group"
        ), patch(
            "infra.alert_history.append_alert"
        ):
            person_emit_stage(ctx)

        assert ctx.telegram_sent is True
        # Phase 6B.141 (2026-08-27): body now goes via send_message
        # (no caption on the album). See _collect_person_album_paths.
        assert mock_send_text.called
        sent_text = mock_send_text.call_args.kwargs["text"]
        # Body should include the matched name
        assert "maintainer" in sent_text
        assert "🚶" in sent_text
        # Phase 6B.140 (2026-08-27): camera is CAM3, not CAM1
        assert "CAM3" in sent_text
        # Phase 6B.141 (2026-08-27): album-skip path runs here
        # because the test fixture's output_dir
        # (/tmp/test-frames/test-alert-123) has no frame_001..frame_004.jpg.
        # In production with CAM3 + GATE_KEEP_DISK_ARTIFACTS=true,
        # the album WOULD be sent with 4-6 images. See probe6B141.

    def test_emit_includes_clothing_attributes(
        self, captured_frame_paths, vision_result_person_visible
    ):
        ctx = _make_ctx(
            frame_paths=captured_frame_paths,
            vision_result=vision_result_person_visible,
            face_recognition=None,
            person_match=None,
            matched_name=None,
        )
        body = _build_structured_body(ctx)
        # Should include clothing upper, lower, carrying, action
        assert "blue" in body
        assert "jacket" in body
        assert "red backpack" in body
        assert "walking" in body
        assert "Face: visible" in body

    def test_emit_includes_no_face_marker(
        self, captured_frame_paths, vision_result_no_face
    ):
        ctx = _make_ctx(
            frame_paths=captured_frame_paths,
            vision_result=vision_result_no_face,
            face_recognition=None,
            person_match=None,
            matched_name=None,
        )
        body = _build_structured_body(ctx)
        assert "Face: not visible" in body

    def test_emit_unknown_person_body(
        self, captured_frame_paths, vision_result_no_face
    ):
        from infra.person_matcher import NoMatch
        ctx = _make_ctx(
            frame_paths=captured_frame_paths,
            vision_result=vision_result_no_face,
            face_recognition=None,
            person_match=NoMatch(reason="no_known_persons"),
            matched_name=None,
        )
        body = _build_structured_body(ctx)
        assert "Unknown person" in body
        assert "no_known_persons" in body

    def test_emit_falls_back_to_text_only_when_no_frames(self):
        from infra.person_matcher import NoMatch

        ctx = _make_ctx(
            frame_paths=[],  # no frames captured
            vision_result={"persons": []},
            face_recognition=None,
            person_match=NoMatch(reason="no_person_in_frame"),
        )
        with patch(
            "infra.send_telegram.send_message"
        ) as mock_text, patch(
            "infra.send_telegram.send_photo_group"
        ) as mock_album, patch(
            "infra.alert_history.append_alert"
        ):
            # Force frame_paths to be empty so we hit the text-only path
            ctx.frame_paths = []
            person_emit_stage(ctx)
        # Phase 6B.141 (2026-08-27): body always goes via send_message.
        # Album is conditional on gate frames existing on disk under
        # ctx.output_dir — empty here, so album skipped.
        assert mock_text.called
        assert not mock_album.called

    def test_emit_appends_audit_log(self, captured_frame_paths, vision_result_no_face):
        from infra.person_matcher import MatchVerdict

        ctx = _make_ctx(
            frame_paths=captured_frame_paths,
            vision_result=vision_result_no_face,
            face_recognition=None,
            person_match=MatchVerdict(
                matched_name="maintainer",
                matched_via="face_recognition",
                confidence=0.85,
            ),
            matched_name="maintainer",
            matched_via="face_recognition",
        )
        with patch(
            "infra.send_telegram.send_message"
        ), patch(
            "infra.send_telegram.send_photo_group"
        ), patch(
            "infra.alert_history.append_alert"
        ) as mock_audit:
            person_emit_stage(ctx)
        assert mock_audit.called
        payload = mock_audit.call_args.args[0]
        assert payload["matched_name"] == "maintainer"
        assert payload["alert_id"] == ctx.alert_id

    def test_emit_sends_2_image_album_crops_only(
        self, tmp_path, vision_result_no_face
    ):
        """Phase 6B.153 (2026-08-28): when the gate wrote 4 wide frames
        + 2 crops to ctx.output_dir, person_emit sends ONLY the 2 crops
        as a 2-image Telegram media group. Wide frames are excluded.

        Per maintainer OOB: "I think we are going to get a lot of
        notifications today. First, for the person pipeline, I want
        to send few images. Just the two cropped images would be
        fine."

        Mirrors the production path that runs for CAM3 person events
        (gate-keeper-tier, GATE_KEEP_DISK_ARTIFACTS=true).
        """
        from PIL import Image

        from infra.person_matcher import NoMatch

        # Simulate the motion gate's disk artifacts
        out_dir = tmp_path / "alert"
        out_dir.mkdir()
        for i in range(1, 5):
            Image.new("RGB", (640, 480), color=(i * 60, 100, 200)).save(
                str(out_dir / f"frame_{i:03d}.jpg"), "JPEG"
            )
        # Two crops: one from frame 3, one from frame 4
        # §11.88 (2026-09-01): crop extension is .png, NOT .jpg.
        Image.new("RGB", (200, 300), color=(255, 0, 0)).save(
            str(out_dir / "frame_003_crop180_200_320x480.png"), "PNG"
        )
        Image.new("RGB", (200, 300), color=(0, 255, 0)).save(
            str(out_dir / "frame_004_crop200_220_300x460.png"), "PNG"
        )

        ctx = _make_ctx(
            output_dir=str(out_dir),
            frame_paths=[],
            vision_result=vision_result_no_face,
            face_recognition=None,
            person_match=NoMatch(reason="no_known_persons"),
        )

        with patch(
            "infra.send_telegram.send_message"
        ) as mock_text, patch(
            "infra.send_telegram.send_photo_group"
        ) as mock_album, patch(
            "infra.alert_history.append_alert"
        ):
            person_emit_stage(ctx)

        # Body still goes via send_message
        assert mock_text.called
        # Album: 2 images (just the crops, wide frames excluded)
        assert mock_album.called
        album_paths = mock_album.call_args.kwargs["frame_paths"]
        assert len(album_paths) == 2
        # Both are crops (sorted by frame name → frame_003_crop before frame_004_crop)
        assert "frame_003_crop" in album_paths[0]
        assert "frame_004_crop" in album_paths[1]
        # Caption is empty (body went via send_message separately)
        assert mock_album.call_args.kwargs["caption"] == ""
        # Both sends succeeded → telegram_sent is True
        assert ctx.telegram_sent is True

    def test_emit_album_sends_text_only_when_no_crops(
        self, tmp_path, vision_result_no_face
    ):
        """Phase 6B.153 (2026-08-28): if the gate found no bbox
        (no_person_in_frame), there are no crops — album is NOT sent.
        Only the text body goes. The text-only fallback was already
        implemented in Phase 6B.141; this test pins down the new
        behavior where the wide-frames fallback is also gone.
        """
        from PIL import Image

        from infra.person_matcher import NoMatch

        out_dir = tmp_path / "alert"
        out_dir.mkdir()
        # 4 wide frames only — no crops
        for i in range(1, 5):
            Image.new("RGB", (640, 480), color=(i * 60, 100, 200)).save(
                str(out_dir / f"frame_{i:03d}.jpg"), "JPEG"
            )

        ctx = _make_ctx(
            output_dir=str(out_dir),
            frame_paths=[],
            vision_result=vision_result_no_face,
            face_recognition=None,
            person_match=NoMatch(reason="no_person_in_frame"),
        )
        with patch(
            "infra.send_telegram.send_message"
        ) as mock_text, patch(
            "infra.send_telegram.send_photo_group"
        ) as mock_album, patch(
            "infra.alert_history.append_alert"
        ):
            person_emit_stage(ctx)

        # Body still sent (text message)
        assert mock_text.called
        # Album NOT sent (no crops → no media group)
        assert not mock_album.called
        # Telegram still counted as sent
        assert ctx.telegram_sent is True

    def test_emit_album_sends_partial_when_one_crop_missing(
        self, tmp_path, vision_result_no_face
    ):
        """Phase 6B.153 (2026-08-28): if only one crop exists (gate
        produced only frame_003 crop, frame_004 crop was None), album
        sends the single crop."""
        from PIL import Image

        from infra.person_matcher import NoMatch

        out_dir = tmp_path / "alert"
        out_dir.mkdir()
        Image.new("RGB", (640, 480), color=(50, 50, 50)).save(
            str(out_dir / "frame_002.jpg"), "JPEG"
        )
        Image.new("RGB", (640, 480), color=(100, 100, 100)).save(
            str(out_dir / "frame_003.jpg"), "JPEG"
        )
        # Only one crop (frame_004_crop absent)
        # §11.88 (2026-09-01): crop extension is .png, NOT .jpg.
        Image.new("RGB", (200, 300), color=(255, 0, 0)).save(
            str(out_dir / "frame_004_crop200_220_300x460.png"), "PNG"
        )

        ctx = _make_ctx(
            output_dir=str(out_dir),
            frame_paths=[],
            vision_result=vision_result_no_face,
            face_recognition=None,
            person_match=NoMatch(reason="no_known_persons"),
        )
        with patch("infra.send_telegram.send_message"), patch(
            "infra.send_telegram.send_photo_group"
        ) as mock_album, patch("infra.alert_history.append_alert"):
            person_emit_stage(ctx)

        assert mock_album.called
        album_paths = mock_album.call_args.kwargs["frame_paths"]
        # Just the one crop — wide frames are excluded entirely
        assert len(album_paths) == 1
        assert "frame_004_crop" in album_paths[0]


# ---------------------------------------------------------------------------
# Audio dispatch
# ---------------------------------------------------------------------------


class TestAudioDispatch:
    def test_audio_skipped_when_env_unset(
        self, captured_frame_paths, vision_result_no_face
    ):
        from infra.person_matcher import NoMatch

        ctx = _make_ctx(
            frame_paths=captured_frame_paths,
            vision_result=vision_result_no_face,
            face_recognition=None,
            person_match=NoMatch(reason="clothing_no_match"),
        )
        with patch(
            "infra.send_telegram.send_message"
        ), patch(
            "infra.send_telegram.send_photo_group"
        ), patch(
            "infra.alert_history.append_alert"
        ), patch.dict(
            "os.environ", {}, clear=True
        ):
            # No PERSON_AUDIO_ENABLED env var — should be no-op
            person_emit_stage(ctx)
        # If we got here without exception, audio was correctly skipped
        assert ctx.telegram_sent is True
    def test_audio_graceful_when_module_missing(
        self, captured_frame_paths, vision_result_no_face
    ):
        from infra.person_matcher import NoMatch

        ctx = _make_ctx(
            frame_paths=captured_frame_paths,
            vision_result=vision_result_no_face,
            face_recognition=None,
            person_match=NoMatch(reason="clothing_no_match"),
        )

        with patch(
            "infra.send_telegram.send_message"
        ), patch(
            "infra.send_telegram.send_photo_group"
        ), patch(
            "infra.alert_history.append_alert"
        ), patch.dict(
            "os.environ", {"PERSON_AUDIO_ENABLED": "1"}
        ), patch(
            "infra.camera_audio", None, create=True
        ):
            person_emit_stage(ctx)
        # Should complete without raising
        assert ctx.telegram_sent is True


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


class TestProcessPersonEvent:
    """Phase 6B.139: orchestrator tests use a GateVerdict with PIL frames
    instead of mocking `capture_frames`. The gate's selected frames are
    the new contract for `ctx.frame_paths`."""

    def test_full_pipeline_match(
        self,
        tmp_path,
        gate_verdict_with_person_frames,
        vision_result_person_visible,
    ):
        ctx = _make_ctx(output_dir=str(tmp_path / "output"))
        ctx.gate_verdict = gate_verdict_with_person_frames

        with patch(
            "infra.vision_analyzer.analyze_frames_queued",
            return_value=vision_result_person_visible,
        ), patch(
            "infra.face_recognition.recognize_faces",
            return_value={
                "faces": [{
                    "bbox": [100, 100, 200, 200],
                    "embedding": [0.1] * 512,
                    "identified_name": "maintainer",
                    "confidence": 0.85,
                    "is_known": True,
                }],
                "identified_person": "maintainer",
                "best_confidence": 0.85,
            },
        ), patch(
            "listener.person_event_pipeline._load_known_persons_for_matching",
            return_value=[],
        ), patch(
            "infra.send_telegram.send_message"
        ), patch(
            "infra.send_telegram.send_photo_group"
        ), patch(
            "infra.alert_history.append_alert"
        ):
            result = process_person_event(ctx)

        assert result["matched_name"] == "maintainer"
        assert result["matched_via"] == "face_recognition"
        assert result["telegram_sent"] is True
        assert result["alert_id"] == ctx.alert_id
        # 6B.139: capture_source is "gate", not "rtsp"
        assert ctx.capture_source == "gate"
        # Gate's middle 2 frames are the inputs to Qwen
        assert len(ctx.frame_paths) == 2

    def test_pipeline_short_circuits_on_capture_failure(self, tmp_path):
        """No gate_verdict → gate_aware_person_capture raises SkipEvent → short-circuit."""
        ctx = _make_ctx(output_dir=str(tmp_path / "output"))
        # gate_verdict stays None — process_person_event will SkipEvent
        result = process_person_event(ctx)

        assert result["telegram_sent"] is False
        assert result["matched_name"] is None
        assert result["structured_body"] == ""
        assert ctx.capture_source == "missing"

    def test_pipeline_no_person_in_vision_returns_unknown(
        self,
        tmp_path,
        gate_verdict_with_person_frames,
    ):
        ctx = _make_ctx(output_dir=str(tmp_path / "output"))
        ctx.gate_verdict = gate_verdict_with_person_frames

        with patch(
            "infra.vision_analyzer.analyze_frames_queued",
            return_value={"persons": [], "scene_description": "empty scene"},
        ), patch(
            "listener.person_event_pipeline._load_known_persons_for_matching",
            return_value=[{"name": "maintainer", "clothing_upper_color": "red"}],
        ), patch(
            "infra.send_telegram.send_message"
        ), patch(
            "infra.send_telegram.send_photo_group"
        ), patch(
            "infra.alert_history.append_alert"
        ):
            result = process_person_event(ctx)
        assert result["matched_name"] is None
        # Phase 6B.162: no_person_in_frame is suppressed — no Telegram, no body
        assert result["suppressed"] is True
        assert result["suppressed_reason"] == "no_person_in_frame"
        assert result["telegram_sent"] is False
        assert result["structured_body"] == ""
        # 6B.139: capture_source is "gate"
        assert ctx.capture_source == "gate"

    def test_pipeline_capture_source_is_gate_after_run(
        self,
        tmp_path,
        gate_verdict_with_person_frames,
        vision_result_person_visible,
    ):
        """Regression: process_person_event sets capture_source='gate' on success.

        Mirrors the vehicle pipeline's contract. This is the field that
        production telemetry logs (see PLAN §11.60 acceptance).
        """
        ctx = _make_ctx(output_dir=str(tmp_path / "output"))
        ctx.gate_verdict = gate_verdict_with_person_frames

        with patch(
            "infra.vision_analyzer.analyze_frames_queued",
            return_value={"persons": []},
        ), patch(
            "infra.send_telegram.send_message"
        ), patch(
            "infra.send_telegram.send_photo_group"
        ), patch(
            "infra.alert_history.append_alert"
        ):
            process_person_event(ctx)

        assert ctx.capture_source == "gate"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


class TestExtractPrimaryPerson:
    def test_extracts_index_zero(self, vision_result_person_visible):
        person = _extract_primary_person(vision_result_person_visible)
        assert person is not None
        assert person["clothing_upper"]["color"] == "blue"

    def test_extracts_primary_person_index(self):
        vr = {
            "persons": [
                {"person_id": "p1", "clothing_upper": {"color": "blue"}},
                {"person_id": "p2", "clothing_upper": {"color": "red"}},
            ],
            "primary_person_index": 1,
        }
        person = _extract_primary_person(vr)
        assert person is not None
        assert person["clothing_upper"]["color"] == "red"

    def test_empty_persons_returns_none(self):
        assert _extract_primary_person({"persons": []}) is None

    def test_out_of_bounds_index_falls_back_to_zero(self):
        vr = {
            "persons": [{"person_id": "p1", "clothing_upper": {"color": "blue"}}],
            "primary_person_index": 99,
        }
        person = _extract_primary_person(vr)
        assert person is not None
        assert person["clothing_upper"]["color"] == "blue"
