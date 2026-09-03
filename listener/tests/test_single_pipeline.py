"""Tests for listener.single_pipeline — §11.115.5 single-pipeline orchestrator.

The single pipeline runs the full §11.115 flow:
  1. RTSP-presence filter (cameras.has_rtsp)
  2. pairwise diff → crop_a + crop_b
  3. (YOLO pre-filter — out of scope here; tests use fake)
  4. Qwen call 1 (shared classify) via two_call_cascade
  5. Qwen call 2 (class-specific) via two_call_cascade
  6. class-specific post-processing (face match / vehicle match / animal match)
  7. Telegram send with BOTH crops OR log-only (other class)

Tests inject fake implementations of every dependency:
  - frame_diff_fn(frame_paths) → (crop_a_path, crop_b_path)
  - qwen_fn (used by two_call_cascade)
  - matchers: dict[ClassLabel, Callable] OR a single matcher_fn
  - telegram_fn(crop_a, crop_b, classify, call2_response) → None
  - log_fn(message) → None

Tests assert on observable outputs only (Telegram sent? what crops?
what matcher was called?). The orchestrator is the glue.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from infra.classify_schema import ClassLabel
from listener import single_pipeline
from listener.pipeline_filters import PipelineCooldown

# -----------------------------------------------------------------------------
# Test fixtures: fake dependency implementations
# -----------------------------------------------------------------------------


@dataclass
class FakeTelegramCall:
    crop_a: str
    crop_b: str
    classify_label: ClassLabel
    call2_response: dict | None


class FakeTelegram:
    def __init__(self) -> None:
        self.calls: list[FakeTelegramCall] = []

    def __call__(self, crop_a, crop_b, classify_label, call2_response, **kwargs):
        self.calls.append(
            FakeTelegramCall(
                crop_a=crop_a,
                crop_b=crop_b,
                classify_label=classify_label,
                call2_response=call2_response,
            )
        )


@dataclass
class FakeLogCall:
    message: str
    context: dict


class FakeLog:
    def __init__(self) -> None:
        self.calls: list[FakeLogCall] = []

    def __call__(self, message: str, **context):
        self.calls.append(FakeLogCall(message=message, context=context))


@dataclass
class FakeFrameDiff:
    """Returns fixed crop paths. Records calls."""

    crop_a_path: str = "/tmp/fake/crop_a.jpg"
    crop_b_path: str = "/tmp/fake/crop_b.jpg"
    call_count: int = 0

    def __call__(self, frame_paths, **kwargs):
        self.call_count += 1
        return self.crop_a_path, self.crop_b_path


@dataclass
class FakeQwen:
    """Records calls, returns scripted responses in order."""

    responses: list[str]
    calls: list = field(default_factory=list)

    def __call__(self, frame_paths, camera_name, **kwargs):
        prompt = kwargs.get("event_hint", "")
        if not self.responses:
            raise AssertionError("FakeQwen ran out of responses")
        response = self.responses.pop(0)
        self.calls.append(
            {
                "frame_paths": list(frame_paths),
                "prompt": prompt,
                "response": response,
            }
        )
        return {"raw": response}


@dataclass
class FakeMatcherCall:
    classify_label: ClassLabel
    call2_response: dict | None
    crop_a: str
    crop_b: str


class FakeMatcherRegistry:
    """Records which matcher was called."""

    def __init__(self) -> None:
        self.calls: list[FakeMatcherCall] = []

    def vehicle(self, classify, call2_response, crop_a, crop_b, **kwargs):
        self.calls.append(
            FakeMatcherCall(ClassLabel.VEHICLE, call2_response, crop_a, crop_b)
        )
        return {"match": "found", "vehicle_id": "v1"}

    def person(self, classify, call2_response, crop_a, crop_b, **kwargs):
        self.calls.append(
            FakeMatcherCall(ClassLabel.PERSON, call2_response, crop_a, crop_b)
        )
        return {"match": "found", "identity_id": "i1"}

    def animal(self, classify, call2_response, crop_a, crop_b, **kwargs):
        self.calls.append(
            FakeMatcherCall(ClassLabel.ANIMAL, call2_response, crop_a, crop_b)
        )
        return {"match": "found", "animal_id": "a1"}


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------


def _make_deps(
    qwen_responses: list[str],
    *,
    has_rtsp: bool = True,
    crop_a: str = "/tmp/fake/crop_a.jpg",
    crop_b: str = "/tmp/fake/crop_b.jpg",
):
    """Build a fresh set of fakes for a single test run."""
    return {
        "has_rtsp_fn": lambda camera_name: has_rtsp,
        "frame_diff_fn": FakeFrameDiff(crop_a_path=crop_a, crop_b_path=crop_b),
        "qwen_fn": FakeQwen(responses=list(qwen_responses)),
        "matchers": FakeMatcherRegistry(),
        "telegram_fn": FakeTelegram(),
        "log_fn": FakeLog(),
        "cooldown": PipelineCooldown(),
    }


# -----------------------------------------------------------------------------
# Tests
# -----------------------------------------------------------------------------


class TestRtspGate:
    """single_pipeline drops events from cameras without RTSP."""

    def test_drops_alert_when_no_rtsp(self) -> None:
        deps = _make_deps(
            qwen_responses=[],
            has_rtsp=False,
        )
        result = single_pipeline.run(
            alert_id="a1",
            camera_name="Front Porch",
            camera_code="CAM1",
            captured_at="2026-09-02 12:34:56",
            frame_paths=["/tmp/f0.jpg", "/tmp/f1.jpg", "/tmp/f2.jpg", "/tmp/f3.jpg"],
            event_type="motion",
            **deps,
        )
        # No Qwen, no Telegram, no log
        assert result.skipped_reason == "no_rtsp"
        assert len(deps["qwen_fn"].calls) == 0
        assert len(deps["telegram_fn"].calls) == 0
        assert len(deps["matchers"].calls) == 0

    def test_proceeds_when_rtsp_present(self) -> None:
        deps = _make_deps(
            qwen_responses=[
                '{"class": "vehicle", "confidence": 0.9, "reasoning": "x"}',
                "{}",
            ],
            has_rtsp=True,
        )
        _ = single_pipeline.run(
            alert_id="a1",
            camera_name="Outside Front Solar",
            camera_code="CAM5",
            captured_at="t",
            frame_paths=["/tmp/f0.jpg", "/tmp/f1.jpg"],
            event_type="motion",
            **deps,
        )
        # Qwen call 1 happens; call 2 may or may not depending on
        # downstream filters (vehicle camera allowlist, cooldown).
        # What this test asserts is that the RTSP gate does NOT block —
        # so the pipeline progresses past `has_rtsp_fn`.
        assert len(deps["qwen_fn"].calls) >= 1


class TestEmptyFramesGate:
    """§11.115.16 — defensive guard for gate-suppressed (empty frame_paths) alerts."""

    def test_drops_alert_when_no_frames(self) -> None:
        deps = _make_deps(
            qwen_responses=[],
            has_rtsp=True,
        )
        result = single_pipeline.run(
            alert_id="alert-nof",
            camera_name="Outside Front Solar",
            camera_code="CAM6",
            captured_at="2026-09-02T18:00:00.000Z",
            frame_paths=[],
            event_type="vehicle",
            **deps,
        )
        assert result.skipped_reason == "no_frames"
        assert result.sent_telegram is False
        assert result.log_only is True
        assert deps["frame_diff_fn"].call_count == 0
        assert any("no frames from motion gate" in c.message for c in deps["log_fn"].calls)

    def test_no_frames_does_not_call_frame_diff(self) -> None:
        deps = _make_deps(qwen_responses=[], has_rtsp=True)
        single_pipeline.run(
            alert_id="alert-nof-2",
            camera_name="Outside Front Solar",
            camera_code="CAM6",
            captured_at="2026-09-02T18:00:00.000Z",
            frame_paths=[],
            event_type="vehicle",
            **deps,
        )
        assert deps["frame_diff_fn"].call_count == 0


class TestSinglePipelineVehicle:
    """vehicle class → vehicle matcher → Telegram."""

    def test_vehicle_class_calls_vehicle_matcher(self) -> None:
        deps = _make_deps(
            qwen_responses=[
                '{"class": "vehicle", "confidence": 0.9, "reasoning": "sedan"}',
                '{"make": "Toyota", "model": "Camry"}',
            ]
        )
        single_pipeline.run(
            alert_id="a1",
            camera_name="Outside Front Solar",
            camera_code="CAM5",
            captured_at="t",
            frame_paths=["/tmp/f0", "/tmp/f1"],
            event_type="motion",
            **deps,
        )
        # Vehicle matcher called once
        assert len(deps["matchers"].calls) == 1
        assert deps["matchers"].calls[0].classify_label is ClassLabel.VEHICLE

    def test_vehicle_class_sends_both_crops_to_telegram(self) -> None:
        deps = _make_deps(
            qwen_responses=[
                '{"class": "vehicle", "confidence": 0.9, "reasoning": "x"}',
                "{}",
            ],
            crop_a="/tmp/CROP_A.jpg",
            crop_b="/tmp/CROP_B.jpg",
        )
        single_pipeline.run(
            alert_id="a1",
            camera_name="Outside Front Solar",
            camera_code="CAM5",
            captured_at="t",
            frame_paths=["/tmp/f0", "/tmp/f1"],
            event_type="motion",
            **deps,
        )
        assert len(deps["telegram_fn"].calls) == 1
        call = deps["telegram_fn"].calls[0]
        assert call.crop_a == "/tmp/CROP_A.jpg"
        assert call.crop_b == "/tmp/CROP_B.jpg"


class TestSinglePipelinePerson:
    """person class → person matcher (face recognition happens inside) → Telegram."""

    def test_person_class_calls_person_matcher(self) -> None:
        deps = _make_deps(
            qwen_responses=[
                '{"class": "person", "confidence": 0.85, "reasoning": "walking"}',
                '{"better_crop": "crop_a", "attributes": {}}',
            ]
        )
        single_pipeline.run(
            alert_id="a1",
            camera_name="Front Porch",
            camera_code="CAM1",
            captured_at="t",
            frame_paths=["/tmp/f0", "/tmp/f1"],
            event_type="motion",
            **deps,
        )
        assert len(deps["matchers"].calls) == 1
        assert deps["matchers"].calls[0].classify_label is ClassLabel.PERSON


class TestSinglePipelineAnimal:
    """animal class → animal matcher → Telegram."""

    def test_animal_class_calls_animal_matcher(self) -> None:
        deps = _make_deps(
            qwen_responses=[
                '{"class": "animal", "confidence": 0.7, "reasoning": "dog"}',
                '{"species": "dog"}',
            ]
        )
        single_pipeline.run(
            alert_id="a1",
            camera_name="Back Yard",
            camera_code="CAM4",
            captured_at="t",
            frame_paths=["/tmp/f0", "/tmp/f1"],
            event_type="motion",
            **deps,
        )
        assert len(deps["matchers"].calls) == 1
        assert deps["matchers"].calls[0].classify_label is ClassLabel.ANIMAL


class TestSinglePipelineOther:
    """other class → log only, NO Telegram, NO matcher."""

    def test_other_class_logs_only(self) -> None:
        deps = _make_deps(
            qwen_responses=[
                '{"class": "other", "confidence": 0.6, "reasoning": "tree"}',
            ]
        )
        result = single_pipeline.run(
            alert_id="a1",
            camera_name="Back Yard",
            camera_code="CAM4",
            captured_at="t",
            frame_paths=["/tmp/f0", "/tmp/f1"],
            event_type="motion",
            **deps,
        )
        # No matchers, no Telegram
        assert len(deps["matchers"].calls) == 0
        assert len(deps["telegram_fn"].calls) == 0
        # Log called with "other" message
        assert any("other" in c.message.lower() for c in deps["log_fn"].calls)
        assert result.classify_label is ClassLabel.OTHER
        assert result.sent_telegram is False

    def test_other_class_qwen_called_once(self) -> None:
        """`other` is decided after call 1, so call 2 is skipped."""
        deps = _make_deps(
            qwen_responses=[
                '{"class": "other", "confidence": 0.6, "reasoning": "x"}',
            ]
        )
        single_pipeline.run(
            alert_id="a1",
            camera_name="x",
            camera_code="CAM1",
            captured_at="t",
            frame_paths=["/tmp/f0", "/tmp/f1"],
            event_type="motion",
            **deps,
        )
        assert len(deps["qwen_fn"].calls) == 1


class TestSinglePipelineFallback:
    """Qwen returns garbage → falls back to `other` → log only."""

    def test_malformed_response_falls_back(self) -> None:
        deps = _make_deps(
            qwen_responses=["I don't know what I'm looking at"]
        )
        result = single_pipeline.run(
            alert_id="a1",
            camera_name="x",
            camera_code="CAM1",
            captured_at="t",
            frame_paths=["/tmp/f0", "/tmp/f1"],
            event_type="motion",
            **deps,
        )
        assert result.classify_label is ClassLabel.OTHER
        assert result.classify_fallback_used is True
        assert result.sent_telegram is False
        assert len(deps["telegram_fn"].calls) == 0


class TestPipelineResultDataclass:
    """run() returns a PipelineResult with observable fields."""

    def test_result_carries_skipped_reason(self) -> None:
        deps = _make_deps(qwen_responses=[], has_rtsp=False)
        result = single_pipeline.run(
            alert_id="a1",
            camera_name="x",
            camera_code="CAM1",
            captured_at="t",
            frame_paths=["/tmp/f0", "/tmp/f1"],
            event_type="motion",
            **deps,
        )
        assert result.skipped_reason == "no_rtsp"

    def test_result_carries_classify_label(self) -> None:
        deps = _make_deps(
            qwen_responses=[
                '{"class": "vehicle", "confidence": 0.9, "reasoning": "x"}',
                "{}",
            ]
        )
        result = single_pipeline.run(
            alert_id="a1",
            camera_name="x",
            camera_code="CAM1",
            captured_at="t",
            frame_paths=["/tmp/f0", "/tmp/f1"],
            event_type="motion",
            **deps,
        )
        assert result.classify_label is ClassLabel.VEHICLE

    def test_result_carries_alert_id(self) -> None:
        deps = _make_deps(qwen_responses=[], has_rtsp=False)
        result = single_pipeline.run(
            alert_id="alert-xyz-789",
            camera_name="x",
            camera_code="CAM1",
            captured_at="t",
            frame_paths=["/tmp/f0", "/tmp/f1"],
            event_type="motion",
            **deps,
        )
        assert result.alert_id == "alert-xyz-789"


class TestFrameDiffIntegration:
    """single_pipeline calls the frame_diff dependency."""

    def test_frame_diff_called_once(self) -> None:
        deps = _make_deps(
            qwen_responses=[
                '{"class": "vehicle", "confidence": 0.9, "reasoning": "x"}',
                "{}",
            ]
        )
        single_pipeline.run(
            alert_id="a1",
            camera_name="x",
            camera_code="CAM1",
            captured_at="t",
            frame_paths=["/tmp/f0", "/tmp/f1", "/tmp/f2", "/tmp/f3"],
            event_type="motion",
            **deps,
        )
        assert deps["frame_diff_fn"].call_count == 1

    def test_qwen_uses_diff_crops_not_original_frames(self) -> None:
        """Qwen receives crop_a/crop_b from frame_diff, not original frame_paths."""
        deps = _make_deps(
            qwen_responses=[
                '{"class": "vehicle", "confidence": 0.9, "reasoning": "x"}',
                "{}",
            ],
            crop_a="/tmp/diff/A.jpg",
            crop_b="/tmp/diff/B.jpg",
        )
        single_pipeline.run(
            alert_id="a1",
            camera_name="x",
            camera_code="CAM1",
            captured_at="t",
            frame_paths=["/tmp/orig/0.jpg", "/tmp/orig/1.jpg"],
            event_type="motion",
            **deps,
        )
        # Both Qwen calls received the diff crops, not originals
        for call in deps["qwen_fn"].calls:
            assert call["frame_paths"] == ["/tmp/diff/A.jpg", "/tmp/diff/B.jpg"]
