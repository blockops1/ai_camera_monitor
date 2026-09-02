"""Integration tests for §11.115.13 cooldown + vehicle-scope in single_pipeline.run.

These tests verify the FULL pre-cascade filter behavior:
- Cooldown suppresses person + animal events
- Cooldown records hit on person (face match) and animal (any)
- Vehicle-class events drop on non-allowlist cameras
- Vehicle-class events proceed on allowlist cameras
- Other class always log-only, no Telegram
- Log entries record every suppression reason

Tests inject a real PipelineCooldown instance + a real camera_code
that goes through is_vehicle_allowed().
"""
from __future__ import annotations

from dataclasses import dataclass, field

from infra.classify_schema import ClassLabel
from listener import single_pipeline
from listener.pipeline_filters import PipelineCooldown

# -----------------------------------------------------------------------------
# Reuse fakes from test_single_pipeline.py for integration testing.
# Defined locally to keep this test independent of test_single_pipeline's
# class internals.
# -----------------------------------------------------------------------------


@dataclass
class FakeQwen:
    """Records calls, returns scripted responses in order."""

    responses: list
    calls: list = field(default_factory=list)

    def __call__(self, frame_paths, camera_name, **kwargs):
        prompt = kwargs.get("event_hint", "")
        if not self.responses:
            raise AssertionError("FakeQwen ran out of responses")
        response = self.responses.pop(0)
        self.calls.append(
            {"frame_paths": list(frame_paths), "prompt": prompt, "response": response}
        )
        return {"raw": response}


class FakeMatcherRegistry:
    def __init__(self):
        self.calls = []
        self.match_results: dict[ClassLabel, dict] = {}

    def person(self, classify, call2_response, crop_a, crop_b, alert_id=None):
        self.calls.append(("person", classify.label, call2_response))
        return self.match_results.get(ClassLabel.PERSON, {"matched": True})

    def animal(self, classify, call2_response, crop_a, crop_b, alert_id=None):
        self.calls.append(("animal", classify.label, call2_response))
        return self.match_results.get(ClassLabel.ANIMAL, {"matched": True})

    def vehicle(self, classify, call2_response, crop_a, crop_b, alert_id=None):
        self.calls.append(("vehicle", classify.label, call2_response))
        return self.match_results.get(ClassLabel.VEHICLE, {"matched": True})


class FakeTelegram:
    def __init__(self):
        self.calls = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)


@dataclass
class FakeLogCall:
    message: str
    context: dict


class FakeLog:
    def __init__(self):
        self.calls = []

    def __call__(self, message: str, **context):
        self.calls.append(FakeLogCall(message=message, context=context))


def _frame_diff(frame_paths):
    return ("/tmp/fake/crop_a.jpg", "/tmp/fake/crop_b.jpg")


def _make_deps(
    qwen_responses: list,
    *,
    cooldown: PipelineCooldown | None = None,
):
    return {
        "has_rtsp_fn": lambda camera_name: True,
        "frame_diff_fn": _frame_diff,
        "qwen_fn": FakeQwen(responses=list(qwen_responses)),
        "matchers": FakeMatcherRegistry(),
        "telegram_fn": FakeTelegram(),
        "log_fn": FakeLog(),
        "cooldown": cooldown or PipelineCooldown(),
    }


# -----------------------------------------------------------------------------
# Tests — Cooldown suppression (§11.115.13)
# -----------------------------------------------------------------------------


class TestPersonCooldownSuppression:
    """Person event gets dropped after a prior face-match hit."""

    def test_first_person_event_proceeds(self) -> None:
        deps = _make_deps(
            qwen_responses=[
                '{"class": "person", "confidence": 0.9, "reasoning": "x"}',
                '{"better_crop": "crop_a"}',
            ],
        )
        result = single_pipeline.run(
            alert_id="a1",
            camera_name="Front Porch",
            camera_code="CAM1",
            captured_at="t",
            frame_paths=["/tmp/f0", "/tmp/f1"],
            event_type="motion",
            **deps,
        )
        assert result.sent_telegram is True
        assert result.skipped_reason is None
        assert len(deps["matchers"].calls) == 1

    def test_second_person_event_suppressed_after_face_match(self) -> None:
        """Cooldown arms after first person matcher hit.

        Subsequent person events (no face match) flow through — that's
        the design. The second event here has NO matcher call because
        we use a single PipelineCooldown instance that has been
        pre-armed via record_hit(matcher_hit=True).
        """
        cooldown = PipelineCooldown()
        # Pre-arm via direct record_hit (simulates first event's match)
        cooldown.record_hit("person", matcher_hit=True)

        deps = _make_deps(
            qwen_responses=[
                '{"class": "person", "confidence": 0.9, "reasoning": "x"}',
            ],
            cooldown=cooldown,
        )

        result = single_pipeline.run(
            alert_id="a2",
            camera_name="Front Porch",
            camera_code="CAM1",
            captured_at="t",
            frame_paths=["/tmp/f0", "/tmp/f1"],
            event_type="motion",
            **deps,
        )
        # No Qwen call 2, no matcher, no Telegram
        assert len(deps["qwen_fn"].calls) == 1
        assert len(deps["matchers"].calls) == 0
        assert len(deps["telegram_fn"].calls) == 0
        assert result.sent_telegram is False
        assert result.log_only is True
        assert result.skipped_reason and result.skipped_reason.startswith(
            "cooldown:"
        )
        # Log entry present
        log_messages = [c.message for c in deps["log_fn"].calls]
        assert any("cooldown suppressed" in m for m in log_messages)

    def test_log_includes_alert_id_and_class(self) -> None:
        """The cooldown suppression log line is queryable."""
        cooldown = PipelineCooldown()
        cooldown.record_hit("person", matcher_hit=True)

        deps = _make_deps(
            qwen_responses=['{"class": "person"}'],
            cooldown=cooldown,
        )

        single_pipeline.run(
            alert_id="alert-xyz",
            camera_name="Front Porch",
            camera_code="CAM1",
            captured_at="t",
            frame_paths=["/tmp/f0", "/tmp/f1"],
            event_type="motion",
            **deps,
        )

        cooldown_logs = [
            c for c in deps["log_fn"].calls if "cooldown suppressed" in c.message
        ]
        assert len(cooldown_logs) == 1
        assert cooldown_logs[0].context["alert_id"] == "alert-xyz"
        assert cooldown_logs[0].context["class_label"] == "person"


class TestAnimalCooldownSuppression:
    """Animal event gets dropped after a prior animal detection."""

    def test_first_animal_event_proceeds(self) -> None:
        deps = _make_deps(
            qwen_responses=[
                '{"class": "animal", "confidence": 0.9, "reasoning": "dog"}',
                '{"species": "dog"}',
            ],
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
        assert result.sent_telegram is True
        assert len(deps["matchers"].calls) == 1

    def test_second_animal_event_suppressed(self) -> None:
        """Animal cooldown arms on ANY detection (matched or not)."""
        cooldown = PipelineCooldown()
        cooldown.record_hit("animal", matcher_hit=False)  # AnimalNoMatch path

        deps = _make_deps(
            qwen_responses=['{"class": "animal"}'],
            cooldown=cooldown,
        )

        result = single_pipeline.run(
            alert_id="a2",
            camera_name="Back Yard",
            camera_code="CAM4",
            captured_at="t",
            frame_paths=["/tmp/f0", "/tmp/f1"],
            event_type="motion",
            **deps,
        )
        assert len(deps["qwen_fn"].calls) == 1
        assert len(deps["matchers"].calls) == 0
        assert result.sent_telegram is False
        assert result.skipped_reason and result.skipped_reason.startswith(
            "cooldown:"
        )


class TestRecordHitIntegration:
    """record_hit() is called post-matcher with the right signal."""

    def test_person_face_match_arms_cooldown(self) -> None:
        """A successful person matcher call records hit → next event suppressed."""
        cooldown = PipelineCooldown()
        deps = _make_deps(
            qwen_responses=[
                '{"class": "person", "confidence": 0.9}',
                '{"better_crop": "crop_a"}',
            ],
            cooldown=cooldown,
        )
        deps["matchers"].match_results[ClassLabel.PERSON] = {"matched": True}

        single_pipeline.run(
            alert_id="a1",
            camera_name="Front Porch",
            camera_code="CAM1",
            captured_at="t",
            frame_paths=["/tmp/f0", "/tmp/f1"],
            event_type="motion",
            **deps,
        )
        # Cooldown is now armed for person
        suppress, _ = cooldown.should_suppress("person")
        assert suppress is True

    def test_person_no_face_match_does_not_arm(self) -> None:
        """A failed person matcher call does NOT arm cooldown."""
        cooldown = PipelineCooldown()
        deps = _make_deps(
            qwen_responses=[
                '{"class": "person", "confidence": 0.9}',
                '{"better_crop": "crop_a"}',
            ],
            cooldown=cooldown,
        )
        deps["matchers"].match_results[ClassLabel.PERSON] = {"matched": False}

        single_pipeline.run(
            alert_id="a1",
            camera_name="Front Porch",
            camera_code="CAM1",
            captured_at="t",
            frame_paths=["/tmp/f0", "/tmp/f1"],
            event_type="motion",
            **deps,
        )
        # Cooldown is NOT armed (no_prior_hit)
        suppress, reason = cooldown.should_suppress("person")
        assert suppress is False
        assert reason == "no_prior_hit"

    def test_animal_no_match_arms_cooldown(self) -> None:
        """AnimalNoMatch (matched=False) still records the hit."""
        cooldown = PipelineCooldown()
        deps = _make_deps(
            qwen_responses=[
                '{"class": "animal", "confidence": 0.9}',
                '{"species": "dog"}',
            ],
            cooldown=cooldown,
        )
        deps["matchers"].match_results[ClassLabel.ANIMAL] = {"matched": False}

        single_pipeline.run(
            alert_id="a1",
            camera_name="Back Yard",
            camera_code="CAM4",
            captured_at="t",
            frame_paths=["/tmp/f0", "/tmp/f1"],
            event_type="motion",
            **deps,
        )
        suppress, _ = cooldown.should_suppress("animal")
        assert suppress is True


# -----------------------------------------------------------------------------
# Tests — Vehicle camera allowlist (§11.115.13)
# -----------------------------------------------------------------------------


class TestVehicleCameraAllowlist:
    """Vehicle matching only on Outside Front Solar + Outside Back Solar."""

    def test_vehicle_on_allowlist_camera_proceeds(self) -> None:
        deps = _make_deps(
            qwen_responses=[
                '{"class": "vehicle", "confidence": 0.9}',
                '{"make": "Toyota"}',
            ],
        )
        result = single_pipeline.run(
            alert_id="a1",
            camera_name="Outside Front Solar",
            camera_code="CAM5",
            captured_at="t",
            frame_paths=["/tmp/f0", "/tmp/f1"],
            event_type="motion",
            **deps,
        )
        assert result.sent_telegram is True
        assert len(deps["matchers"].calls) == 1
        # No vehicle_camera_drop log
        log_messages = [c.message for c in deps["log_fn"].calls]
        assert not any("vehicle_camera_drop" in m for m in log_messages)

    def test_vehicle_on_non_allowlist_camera_dropped(self) -> None:
        deps = _make_deps(
            qwen_responses=[
                '{"class": "vehicle", "confidence": 0.9}',
            ],
        )
        result = single_pipeline.run(
            alert_id="a2",
            camera_name="Front Porch",
            camera_code="CAM1",
            captured_at="t",
            frame_paths=["/tmp/f0", "/tmp/f1"],
            event_type="motion",
            **deps,
        )
        # Qwen call 1 only — no cascade, no matcher, no Telegram
        assert len(deps["qwen_fn"].calls) == 1
        assert len(deps["matchers"].calls) == 0
        assert len(deps["telegram_fn"].calls) == 0
        assert result.sent_telegram is False
        assert result.log_only is True
        assert result.skipped_reason == "vehicle_camera_not_allowed"

        # Log entry present with reason + camera
        drop_logs = [
            c for c in deps["log_fn"].calls if "vehicle_camera_drop" in c.message
        ]
        assert len(drop_logs) == 1
        assert drop_logs[0].context["camera"] == "Front Porch"
        assert drop_logs[0].context["camera_code"] == "CAM1"

    def test_non_vehicle_on_non_allowlist_camera_unaffected(self) -> None:
        """Person + animal on non-allowlist cameras work normally."""
        deps = _make_deps(
            qwen_responses=[
                '{"class": "person", "confidence": 0.9}',
                '{"better_crop": "crop_a"}',
            ],
        )
        result = single_pipeline.run(
            alert_id="a3",
            camera_name="Front Porch",
            camera_code="CAM1",
            captured_at="t",
            frame_paths=["/tmp/f0", "/tmp/f1"],
            event_type="motion",
            **deps,
        )
        assert result.sent_telegram is True
        assert len(deps["matchers"].calls) == 1


# -----------------------------------------------------------------------------
# Tests — Other class behavior (unchanged from prior §11.115 phases)
# -----------------------------------------------------------------------------


class TestOtherClassLogOnly:
    """Other class events still log-only, no Telegram."""

    def test_other_class_skipped(self) -> None:
        deps = _make_deps(
            qwen_responses=['{"class": "other"}'],
        )
        result = single_pipeline.run(
            alert_id="a1",
            camera_name="Front Porch",
            camera_code="CAM1",
            captured_at="t",
            frame_paths=["/tmp/f0", "/tmp/f1"],
            event_type="motion",
            **deps,
        )
        assert result.sent_telegram is False
        assert result.log_only is True
        assert len(deps["matchers"].calls) == 0
        assert len(deps["telegram_fn"].calls) == 0