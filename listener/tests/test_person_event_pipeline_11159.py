"""§11.115.9 tests for person_event_pipeline's cascade-consumption rewrite.

person_identify_stage now has two paths:
  A. CASCADE: ctx.call2_response is set — consume it, no Qwen call,
              ArcFace on the chosen crop_a/crop_b.
  B. LEGACY: ctx.call2_response is None — run the original Qwen person
             call + use face_visible + frame_paths[0].

These tests pin the cascade path's behavior so future regressions in
the rewrite are caught early.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from dataclasses import fields
from unittest.mock import patch

from listener.person_event_pipeline import (
    PersonContext,
    _log_call2_attrs,
    _person_identify_cascade_path,
    _run_face_recognition_on_path,
    person_identify_stage,
)

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _make_context(
    *,
    call2_response=None,
    crop_a_path="",
    crop_b_path="",
    frame_paths=None,
    face_recognition=None,
):
    """Build a PersonContext with only the fields we care about populated."""

    # The real PersonContext has fields including a number of defaults.
    # Build it positionally where possible by skipping required ones and
    # using kwargs for the rest.
    return PersonContext(
        alert_id="test-alert",
        camera_name="Front Porch",
        timestamp="2026-09-02T12:00:00-04:00",
        event_type="person",
        rtsp_url="rtsp://example/stream",
        output_dir="/tmp/test-frames/",
        bot_token="dummy-token",
        chat_id="dummy-chat",
        api_url="http://127.0.0.1:8093/v1/chat/completions",
        call2_response=call2_response,
        crop_a_path=crop_a_path,
        crop_b_path=crop_b_path,
        frame_paths=frame_paths or [],
        face_recognition=face_recognition,
    )


def _sample_call2(better_crop="crop_a", *, attributes=None, confidence=0.85):
    return {
        "better_crop": better_crop,
        "attributes": attributes or {
            "clothing_upper": {"color": "blue", "type": "shirt"},
            "clothing_lower": {"color": "black", "type": "pants"},
            "carrying": [],
            "action": "walking",
            "silhouette": {"build": "average", "height": "medium"},
            "skin_tone": "light",
            "age_range": "young_adult",
            "hair": {"color": "brown", "length": "short", "style": "straight"},
            "facial_hair": "clean_shaven",
            "glasses": "none",
        },
        "signature": {"stable": [], "transient": ["carrying grocery bags"]},
        "confidence": confidence,
        "notable_details": ["wearing reflective vest"],
    }


# ---------------------------------------------------------------------------
# cascade path — A
# ---------------------------------------------------------------------------


class TestCascadePathConsumesCall2(unittest.TestCase):
    def test_cascade_path_no_internal_qwen_call(self):
        """When call2_response is set, analyze_frames_queued must NOT be called."""
        ctx = _make_context(call2_response=_sample_call2(better_crop="crop_a"))
        with patch(
            "infra.vision_analyzer.analyze_frames_queued"
        ) as mock_qwen:
            _person_identify_cascade_path(ctx)
        mock_qwen.assert_not_called()

    def test_cascade_path_populates_vision_result_from_call2(self):
        """vision_result must contain the call2 fields, not old persons[]."""
        call2 = _sample_call2(better_crop="crop_a")
        ctx = _make_context(call2_response=call2)
        with patch("infra.face_recognition.recognize_faces") as mock_arc:
            mock_arc.return_value = {"faces": [], "identified_person": None, "best_confidence": None}
            _person_identify_cascade_path(ctx)

        self.assertEqual(ctx.vision_result["better_crop"], "crop_a")
        self.assertEqual(ctx.vision_result["attributes"], call2["attributes"])
        self.assertEqual(ctx.vision_result["signature"], call2["signature"])
        self.assertEqual(ctx.vision_result["confidence"], call2["confidence"])
        self.assertEqual(ctx.vision_result["notable_details"], call2["notable_details"])
        self.assertEqual(ctx.vision_result["_source"], "cascade")
        self.assertNotIn(
            "persons",
            ctx.vision_result,
            "§11.115 schema has no persons[] — that was the old shape",
        )

    def test_cascade_path_calls_arcface_on_crop_a(self):
        """better_crop=crop_a → recognize_faces called with crop_a_path."""
        with tempfile.TemporaryDirectory() as tmp:
            crop_a = os.path.join(tmp, "crop_a.jpg")
            crop_b = os.path.join(tmp, "crop_b.jpg")
            # write dummy bytes so PIL.Image.open is happy
            with open(crop_a, "wb") as f:
                f.write(b"\xff\xd8\xff\xe0\x00\x10JFIFdummy")
            with open(crop_b, "wb") as f:
                f.write(b"\xff\xd8\xff\xe0\x00\x10JFIFdummy")

            ctx = _make_context(
                call2_response=_sample_call2(better_crop="crop_a"),
                crop_a_path=crop_a,
                crop_b_path=crop_b,
            )
            with patch(
                "infra.face_recognition.recognize_faces"
            ) as mock_arc:
                mock_arc.return_value = {
                    "faces": [{"bbox": [0, 0, 10, 10]}],
                    "identified_person": "Alice",
                    "best_confidence": 0.92,
                }
                _person_identify_cascade_path(ctx)

        mock_arc.assert_called_once_with(crop_a)
        self.assertEqual(ctx.face_recognition["identified_person"], "Alice")
        self.assertEqual(ctx.face_recognition["best_confidence"], 0.92)

    def test_cascade_path_calls_arcface_on_crop_b(self):
        """better_crop=crop_b → recognize_faces called with crop_b_path."""
        with tempfile.TemporaryDirectory() as tmp:
            crop_a = os.path.join(tmp, "crop_a.jpg")
            crop_b = os.path.join(tmp, "crop_b.jpg")
            for p in (crop_a, crop_b):
                with open(p, "wb") as f:
                    f.write(b"\xff\xd8\xff\xe0\x00\x10JFIFdummy")

            ctx = _make_context(
                call2_response=_sample_call2(better_crop="crop_b"),
                crop_a_path=crop_a,
                crop_b_path=crop_b,
            )
            with patch(
                "infra.face_recognition.recognize_faces"
            ) as mock_arc:
                mock_arc.return_value = {"faces": [], "identified_person": None, "best_confidence": None}
                _person_identify_cascade_path(ctx)

        mock_arc.assert_called_once_with(crop_b)

    def test_cascade_path_neither_skips_arcface(self):
        """better_crop=neither → no ArcFace call, face_recognition=None."""
        ctx = _make_context(
            call2_response=_sample_call2(better_crop="neither"),
            crop_a_path="/tmp/crop_a.jpg",
            crop_b_path="/tmp/crop_b.jpg",
        )
        with patch("infra.face_recognition.recognize_faces") as mock_arc:
            _person_identify_cascade_path(ctx)
        mock_arc.assert_not_called()
        self.assertIsNone(ctx.face_recognition)

    def test_cascade_path_missing_crop_path_skips_arcface(self):
        """If crop_a is empty (failed crop), fall back to skipping ArcFace."""
        ctx = _make_context(
            call2_response=_sample_call2(better_crop="crop_a"),
            crop_a_path="",  # failed crop
            crop_b_path="/tmp/crop_b.jpg",
        )
        with patch("infra.face_recognition.recognize_faces") as mock_arc:
            _person_identify_cascade_path(ctx)
        mock_arc.assert_not_called()
        self.assertIsNone(ctx.face_recognition)

    def test_cascade_path_arcface_returns_empty_faces_dict_not_none(self):
        """When ArcFace runs but finds no faces, return empty-faces dict, not None."""
        with tempfile.TemporaryDirectory() as tmp:
            crop_a = os.path.join(tmp, "crop_a.jpg")
            with open(crop_a, "wb") as f:
                f.write(b"\xff\xd8\xff\xe0\x00\x10JFIFdummy")

            ctx = _make_context(
                call2_response=_sample_call2(better_crop="crop_a"),
                crop_a_path=crop_a,
                crop_b_path="",
            )
            with patch(
                "infra.face_recognition.recognize_faces"
            ) as mock_arc:
                mock_arc.return_value = {"faces": [], "identified_person": None, "best_confidence": None}
                _person_identify_cascade_path(ctx)

        # Must be a dict (so caller can distinguish "ran but empty" from
        # "didn't run"), not None.
        self.assertIsInstance(ctx.face_recognition, dict)
        self.assertEqual(ctx.face_recognition["faces"], [])

    def test_cascade_path_recognize_faces_exception_returns_none(self):
        """If recognize_faces raises, face_recognition=None (defensive)."""
        with tempfile.TemporaryDirectory() as tmp:
            crop_a = os.path.join(tmp, "crop_a.jpg")
            with open(crop_a, "wb") as f:
                f.write(b"\xff\xd8\xff\xe0\x00\x10JFIFdummy")

            ctx = _make_context(
                call2_response=_sample_call2(better_crop="crop_a"),
                crop_a_path=crop_a,
                crop_b_path="",
            )
            with patch(
                "infra.face_recognition.recognize_faces"
            ) as mock_arc:
                mock_arc.side_effect = RuntimeError("InsightFace model not loaded")
                _person_identify_cascade_path(ctx)
        self.assertIsNone(ctx.face_recognition)


# ---------------------------------------------------------------------------
# dispatcher — both paths
# ---------------------------------------------------------------------------


class TestPersonIdentifyStageDispatcher(unittest.TestCase):
    def test_dispatcher_routes_cascade(self):
        ctx = _make_context(call2_response=_sample_call2(better_crop="crop_a"))
        with patch(
            "listener.person_event_pipeline._person_identify_cascade_path"
        ) as mock_cascade, patch(
            "listener.person_event_pipeline._person_identify_legacy_path"
        ) as mock_legacy:
            person_identify_stage(ctx)
        mock_cascade.assert_called_once_with(ctx)
        mock_legacy.assert_not_called()

    def test_dispatcher_routes_legacy_when_no_call2(self):
        ctx = _make_context(call2_response=None, frame_paths=["/tmp/fake.jpg"])
        with patch(
            "listener.person_event_pipeline._person_identify_cascade_path"
        ) as mock_cascade, patch(
            "listener.person_event_pipeline._person_identify_legacy_path"
        ) as mock_legacy:
            person_identify_stage(ctx)
        mock_legacy.assert_called_once_with(ctx)
        mock_cascade.assert_not_called()


# ---------------------------------------------------------------------------
# PersonContext dataclass fields
# ---------------------------------------------------------------------------


class TestPersonContextNewFields(unittest.TestCase):
    def test_call2_response_field_exists(self):
        names = {f.name for f in fields(PersonContext)}
        self.assertIn("call2_response", names)
        self.assertIn("crop_a_path", names)
        self.assertIn("crop_b_path", names)
        self.assertIn("classify", names)

    def test_call2_response_default_none(self):
        ctx = PersonContext(
            alert_id="x",
            camera_name="y",
            timestamp="z",
            event_type="person",
            rtsp_url="r",
            output_dir="/tmp",
            bot_token="b",
            chat_id="c",
            api_url="a",
        )
        self.assertIsNone(ctx.call2_response)
        self.assertEqual(ctx.crop_a_path, "")
        self.assertEqual(ctx.crop_b_path, "")
        self.assertIsNone(ctx.classify)


# ---------------------------------------------------------------------------
# _log_call2_attrs — sanity
# ---------------------------------------------------------------------------


class TestLogCall2Attrs(unittest.TestCase):
    def test_logs_all_attribute_fields(self):
        ctx = _make_context()
        call2 = _sample_call2()
        # _log_call2_attrs emits a single log.info call. Patch the
        # module-level logger and assert it was called with all
        # required keys.
        with patch("listener.person_event_pipeline.log") as mock_log:
            _log_call2_attrs(ctx, call2)
        self.assertEqual(mock_log.info.call_count, 1)
        msg = mock_log.info.call_args.args[0]
        for key in (
            "better_crop", "conf",
            "upper.color", "upper.type",
            "lower.color", "lower.type",
            "action",
            "silhouette.build", "silhouette.height",
            "skin_tone", "age_range",
            "hair.color", "hair.length",
            "facial_hair", "glasses",
            "stable",
        ):
            self.assertIn(key, msg, f"missing key in log: {key}")

    def test_logs_handles_missing_attributes(self):
        """Empty call2 must not raise."""
        ctx = _make_context()
        with patch("listener.person_event_pipeline.log"):
            _log_call2_attrs(ctx, {})  # no attributes key


# ---------------------------------------------------------------------------
# _run_face_recognition_on_path — direct tests
# ---------------------------------------------------------------------------


class TestRunFaceRecognitionOnPath(unittest.TestCase):
    def test_calls_recognize_faces_with_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            crop = os.path.join(tmp, "crop.jpg")
            with open(crop, "wb") as f:
                f.write(b"\xff\xd8\xff\xe0\x00\x10JFIFdummy")
            ctx = _make_context()
            with patch("infra.face_recognition.recognize_faces") as mock_arc:
                mock_arc.return_value = {
                    "faces": [{"bbox": [0, 0, 100, 100]}],
                    "identified_person": "Bob",
                    "best_confidence": 0.88,
                }
                result = _run_face_recognition_on_path(ctx, crop, reason="better_crop=crop_a")
        mock_arc.assert_called_once_with(crop)
        self.assertEqual(result["identified_person"], "Bob")

    def test_empty_path_returns_none(self):
        ctx = _make_context()
        result = _run_face_recognition_on_path(ctx, "", reason="better_crop=crop_a")
        self.assertIsNone(result)

    def test_recognize_faces_missing_returns_none(self):
        """If infra.face_recognition can't be imported, return None."""
        ctx = _make_context()
        with patch.dict(
            "sys.modules", {"infra.face_recognition": None}
        ):
            result = _run_face_recognition_on_path(ctx, "/tmp/fake.jpg")
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
