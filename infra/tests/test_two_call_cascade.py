"""Tests for infra.two_call_cascade.

§11.115.3: the shared-classify → class-specific cascade.

The cascade is a thin orchestrator that runs Qwen twice:
  Call 1: shared classify (vehicle / person / animal / other)
  Call 2: class-specific (prompt chosen by call 1's result)

Tests inject a fake Qwen callable to assert the cascade:
  - passes the right images to call 1
  - passes the right images to call 2
  - routes to the right call-2 prompt factory based on call 1's class
  - returns the parsed ClassifyResult + the raw call-2 response

`other` class MUST NOT trigger a call 2 (no second Qwen call).
"""
from __future__ import annotations

from dataclasses import dataclass

from infra import two_call_cascade
from infra.classify_schema import ClassLabel

# -----------------------------------------------------------------------------
# Test fixtures
# -----------------------------------------------------------------------------


@dataclass
class FakeQwenCall:
    """Records one Qwen call. FakeQwen uses these to track call order."""

    frame_paths: list[str]
    prompt: str
    response: str


class FakeQwen:
    """Drop-in replacement for analyze_frames_queued.

    Each call appends a FakeQwenCall to `.calls` and returns the matching
    pre-loaded response. The test sets up `.responses` in the order
    calls are expected.
    """

    def __init__(self, responses: list[str]) -> None:
        self.responses = responses
        self.calls: list[FakeQwenCall] = []

    def __call__(self, frame_paths, camera_name, **kwargs):
        # The cascade may pass extra kwargs (api_url, alert_id, etc).
        # We don't care about them in tests; just record and return next response.
        prompt = kwargs.get("event_hint", "")
        if not self.responses:
            raise AssertionError(
                "FakeQwen called more times than responses provided"
            )
        response = self.responses.pop(0)
        self.calls.append(
            FakeQwenCall(
                frame_paths=list(frame_paths),
                prompt=prompt or "",
                response=response,
            )
        )
        return {"raw": response}


def _fake_person_factory(camera_name: str, captured_at: str, **kwargs) -> str:
    return f"[PERSON PROMPT for {camera_name} @ {captured_at}]"


def _fake_vehicle_factory(camera_name: str, captured_at: str, **kwargs) -> str:
    return f"[VEHICLE PROMPT for {camera_name} @ {captured_at}]"


def _fake_animal_factory(camera_name: str, captured_at: str, **kwargs) -> str:
    return f"[ANIMAL PROMPT for {camera_name} @ {captured_at}]"


def _always_raise_factory(*args, **kwargs):
    raise AssertionError("call 2 factory should NOT be called for `other`")


# -----------------------------------------------------------------------------
# Tests
# -----------------------------------------------------------------------------


class TestTwoCallCascadeVehicle:
    """cascade → vehicle → 2 calls (classify, then vehicle-specific)."""

    def test_vehicle_class_triggers_two_qwen_calls(self) -> None:
        crop_a = "/tmp/fake/crop_a.jpg"
        crop_b = "/tmp/fake/crop_b.jpg"
        qwen = FakeQwen(
            responses=[
                '{"class": "vehicle", "confidence": 0.9, "reasoning": "sedan"}',
                '{"make": "Toyota", "model": "Camry", "color": "silver"}',
            ]
        )
        result = two_call_cascade.run(
            frame_paths=[crop_a, crop_b],
            camera_name="Front Porch",
            captured_at="2026-09-02 12:34:56",
            qwen_fn=qwen,
            call2_prompts={
                ClassLabel.VEHICLE: _fake_vehicle_factory,
                ClassLabel.PERSON: _fake_person_factory,
                ClassLabel.ANIMAL: _fake_animal_factory,
            },
        )
        assert len(qwen.calls) == 2
        assert result.classify.label is ClassLabel.VEHICLE
        assert result.classify.fallback_used is False
        assert result.call2_response == {
            "raw": '{"make": "Toyota", "model": "Camry", "color": "silver"}'
        }
        assert result.call2_was_skipped is False

    def test_call1_uses_both_crops(self) -> None:
        crop_a = "/tmp/fake/crop_a.jpg"
        crop_b = "/tmp/fake/crop_b.jpg"
        qwen = FakeQwen(
            responses=[
                '{"class": "vehicle", "confidence": 0.9, "reasoning": "x"}',
                "{}",
            ]
        )
        two_call_cascade.run(
            frame_paths=[crop_a, crop_b],
            camera_name="x",
            captured_at="t",
            qwen_fn=qwen,
            call2_prompts={
                ClassLabel.VEHICLE: _fake_vehicle_factory,
            },
        )
        # Call 1 must have seen BOTH crops
        assert qwen.calls[0].frame_paths == [crop_a, crop_b]
        # Call 2 must also have seen BOTH crops (no new crops ever)
        assert qwen.calls[1].frame_paths == [crop_a, crop_b]

    def test_call2_uses_vehicle_factory(self) -> None:
        crop_a = "/tmp/fake/crop_a.jpg"
        crop_b = "/tmp/fake/crop_b.jpg"
        qwen = FakeQwen(
            responses=[
                '{"class": "vehicle", "confidence": 0.9, "reasoning": "x"}',
                "{}",
            ]
        )
        _result = two_call_cascade.run(
            frame_paths=[crop_a, crop_b],
            camera_name="Front Porch",
            captured_at="2026-09-02 12:34:56",
            qwen_fn=qwen,
            call2_prompts={
                ClassLabel.VEHICLE: _fake_vehicle_factory,
            },
        )
        # Call 2 prompt came from the vehicle factory
        assert "[VEHICLE PROMPT for Front Porch @ 2026-09-02 12:34:56]" in (
            qwen.calls[1].prompt
        )


class TestTwoCallCascadePerson:
    """cascade → person → 2 calls."""

    def test_person_class_triggers_person_factory(self) -> None:
        qwen = FakeQwen(
            responses=[
                '{"class": "person", "confidence": 0.85, "reasoning": "walking"}',
                '{"better_crop": "crop_a", "attributes": {"x": 1}}',
            ]
        )
        result = two_call_cascade.run(
            frame_paths=["/tmp/a", "/tmp/b"],
            camera_name="Front Porch",
            captured_at="t",
            qwen_fn=qwen,
            call2_prompts={
                ClassLabel.PERSON: _fake_person_factory,
                ClassLabel.VEHICLE: _fake_vehicle_factory,
            },
        )
        assert result.classify.label is ClassLabel.PERSON
        assert result.call2_was_skipped is False
        assert "[PERSON PROMPT" in qwen.calls[1].prompt


class TestTwoCallCascadeAnimal:
    """cascade → animal → 2 calls."""

    def test_animal_class_triggers_animal_factory(self) -> None:
        qwen = FakeQwen(
            responses=[
                '{"class": "animal", "confidence": 0.7, "reasoning": "dog"}',
                '{"species": "dog", "breed": "lab"}',
            ]
        )
        result = two_call_cascade.run(
            frame_paths=["/tmp/a", "/tmp/b"],
            camera_name="Front Porch",
            captured_at="t",
            qwen_fn=qwen,
            call2_prompts={
                ClassLabel.ANIMAL: _fake_animal_factory,
            },
        )
        assert result.classify.label is ClassLabel.ANIMAL
        assert "[ANIMAL PROMPT" in qwen.calls[1].prompt


class TestTwoCallCascadeOther:
    """cascade → other → 1 call only (no call 2)."""

    def test_other_class_skips_call2(self) -> None:
        qwen = FakeQwen(
            responses=[
                '{"class": "other", "confidence": 0.6, "reasoning": "tree branch"}',
            ]
        )
        result = two_call_cascade.run(
            frame_paths=["/tmp/a", "/tmp/b"],
            camera_name="Front Porch",
            captured_at="t",
            qwen_fn=qwen,
            call2_prompts={
                ClassLabel.VEHICLE: _always_raise_factory,
                ClassLabel.PERSON: _always_raise_factory,
                ClassLabel.ANIMAL: _always_raise_factory,
            },
        )
        assert result.classify.label is ClassLabel.OTHER
        assert result.call2_was_skipped is True
        assert result.call2_response is None
        assert len(qwen.calls) == 1

    def test_other_class_log_only_no_telegram(self) -> None:
        """The cascade result must signal that `other` should be logged, not sent."""
        qwen = FakeQwen(
            responses=[
                '{"class": "other", "confidence": 0.6, "reasoning": "tree branch"}',
            ]
        )
        result = two_call_cascade.run(
            frame_paths=["/tmp/a", "/tmp/b"],
            camera_name="Front Porch",
            captured_at="t",
            qwen_fn=qwen,
            call2_prompts={},
        )
        # call2_was_skipped=True + label=OTHER is the signal
        assert result.should_send_telegram is False
        assert result.should_log is True


class TestTwoCallCascadeFallback:
    """Bad Qwen response → fallback to OTHER (no call 2)."""

    def test_malformed_response_falls_back_to_other(self) -> None:
        qwen = FakeQwen(
            responses=[
                "I don't know what I'm looking at",
            ]
        )
        result = two_call_cascade.run(
            frame_paths=["/tmp/a", "/tmp/b"],
            camera_name="Front Porch",
            captured_at="t",
            qwen_fn=qwen,
            call2_prompts={},
        )
        assert result.classify.label is ClassLabel.OTHER
        assert result.classify.fallback_used is True
        assert result.call2_was_skipped is True
        assert len(qwen.calls) == 1

    def test_unknown_class_falls_back_to_other(self) -> None:
        qwen = FakeQwen(
            responses=[
                '{"class": "spaceship", "confidence": 0.99, "reasoning": "flying"}',
            ]
        )
        result = two_call_cascade.run(
            frame_paths=["/tmp/a", "/tmp/b"],
            camera_name="Front Porch",
            captured_at="t",
            qwen_fn=qwen,
            call2_prompts={},
        )
        assert result.classify.label is ClassLabel.OTHER
        assert result.classify.fallback_used is True
        assert result.call2_was_skipped is True


class TestTwoCallCascadeMissingFactory:
    """If call2_prompts lacks an entry for the classified class, treat as `other`."""

    def test_missing_factory_skips_call2(self) -> None:
        qwen = FakeQwen(
            responses=[
                '{"class": "person", "confidence": 0.9, "reasoning": "x"}',
            ]
        )
        result = two_call_cascade.run(
            frame_paths=["/tmp/a", "/tmp/b"],
            camera_name="Front Porch",
            captured_at="t",
            qwen_fn=qwen,
            call2_prompts={
                # no PERSON factory
                ClassLabel.VEHICLE: _fake_vehicle_factory,
            },
        )
        # Person class but no person factory → treated like other (log only)
        assert result.classify.label is ClassLabel.PERSON
        assert result.call2_was_skipped is True
        assert result.should_send_telegram is False
        assert result.should_log is True
        assert len(qwen.calls) == 1


class TestTwoCallCascadeResultFlags:
    """Result dataclass exposes should_send_telegram and should_log flags."""

    def test_vehicle_result_should_send(self) -> None:
        qwen = FakeQwen(
            responses=[
                '{"class": "vehicle", "confidence": 0.9, "reasoning": "x"}',
                "{}",
            ]
        )
        result = two_call_cascade.run(
            frame_paths=["/tmp/a", "/tmp/b"],
            camera_name="Front Porch",
            captured_at="t",
            qwen_fn=qwen,
            call2_prompts={ClassLabel.VEHICLE: _fake_vehicle_factory},
        )
        assert result.should_send_telegram is True
        assert result.should_log is False

    def test_person_result_should_send(self) -> None:
        qwen = FakeQwen(
            responses=[
                '{"class": "person", "confidence": 0.9, "reasoning": "x"}',
                "{}",
            ]
        )
        result = two_call_cascade.run(
            frame_paths=["/tmp/a", "/tmp/b"],
            camera_name="Front Porch",
            captured_at="t",
            qwen_fn=qwen,
            call2_prompts={ClassLabel.PERSON: _fake_person_factory},
        )
        assert result.should_send_telegram is True
