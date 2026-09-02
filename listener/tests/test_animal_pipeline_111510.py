"""§11.115.10 tests for animal_event_pipeline cascade-aware behavior."""
from __future__ import annotations

import unittest
from dataclasses import fields
from types import SimpleNamespace
from unittest.mock import patch

from listener.animal_event_pipeline import (
    AnimalContext,
    _animal_emit_stage,
    _animal_identify_cascade_path,
    _animal_identify_legacy_path,
    _animal_match_stage,
    process_animal_event,
)

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _make_context(*, call2_response=None, known_animals=None):
    return AnimalContext(
        alert_id="test-alert",
        camera_name="Front Porch",
        timestamp="2026-09-02T12:00:00-04:00",
        event_type="animal",
        rtsp_url="rtsp://example/stream",
        output_dir="/tmp/test-frames/",
        bot_token="dummy-token",
        chat_id="dummy-chat",
        api_url="http://127.0.0.1:8093/v1/chat/completions",
        call2_response=call2_response,
        known_animals=known_animals,
    )


def _sample_call2_animal(species="cat", confidence=0.85, **extra):
    return {
        "species": species,
        "breed": "tabby",
        "size": "medium",
        "color_pattern": "brown",
        "distinctive_features": ["blue collar"],
        "action": "walking",
        "confidence": confidence,
        "notable_details": ["walking along fence line"],
        **extra,
    }


def _verdict(name=None, reason=None, score=None, species=None):
    """Build a SimpleNamespace that mimics AnimalMatchVerdict/NoMatch."""
    return SimpleNamespace(
        name=name, reason=reason, score=score, species=species
    )


# ---------------------------------------------------------------------------
# AnimalContext fields
# ---------------------------------------------------------------------------


class TestAnimalContextNewFields(unittest.TestCase):
    def test_cascade_fields_exist(self):
        names = {f.name for f in fields(AnimalContext)}
        for f in (
            "call2_response",
            "classify",
            "crop_a_path",
            "crop_b_path",
            "known_animals",
        ):
            self.assertIn(f, names, f"missing field {f}")

    def test_cascade_field_defaults(self):
        ctx = AnimalContext(
            alert_id="x",
            camera_name="y",
            timestamp="z",
            event_type="animal",
            rtsp_url="r",
            output_dir="/tmp",
            bot_token="b",
            chat_id="c",
            api_url="a",
        )
        self.assertIsNone(ctx.call2_response)
        self.assertIsNone(ctx.classify)
        self.assertEqual(ctx.crop_a_path, "")
        self.assertEqual(ctx.crop_b_path, "")
        self.assertIsNone(ctx.known_animals)


# ---------------------------------------------------------------------------
# animal_identify
# ---------------------------------------------------------------------------


class TestAnimalIdentifyCascadePath(unittest.TestCase):
    def test_cascade_passes_call2_through(self):
        call2 = _sample_call2_animal(species="dog", confidence=0.92)
        ctx = _make_context(call2_response=call2)
        _animal_identify_cascade_path(ctx)
        self.assertEqual(ctx.vision_result["species"], "dog")
        self.assertEqual(ctx.vision_result["confidence"], 0.92)
        self.assertEqual(ctx.vision_result["distinctive_features"], ["blue collar"])
        self.assertEqual(ctx.vision_result["_source"], "cascade")

    def test_cascade_empty_call2(self):
        """call2_response = {} still sets vision_result (source=cascade)."""
        ctx = _make_context(call2_response={})
        _animal_identify_cascade_path(ctx)
        self.assertEqual(ctx.vision_result["_source"], "cascade")
        self.assertIsNone(ctx.vision_result.get("species"))


class TestAnimalIdentifyLegacyPath(unittest.TestCase):
    def test_legacy_stub(self):
        ctx = _make_context(call2_response=None)
        _animal_identify_legacy_path(ctx)
        self.assertEqual(ctx.vision_result["_source"], "legacy_scaffold")


# ---------------------------------------------------------------------------
# animal_match
# ---------------------------------------------------------------------------


class TestAnimalMatchStage(unittest.TestCase):
    def test_match_with_no_vision_result_skips(self):
        ctx = _make_context()
        ctx.vision_result = {}
        _animal_match_stage(ctx)
        self.assertIsNone(ctx.animal_match)

    def test_match_calls_match_animal(self):
        """match_animal called with vision_result + known_animals."""
        call2 = _sample_call2_animal(species="cat")
        known = [{"id": "a1", "name": "Whiskers", "species": "cat"}]
        ctx = _make_context(call2_response=call2, known_animals=known)
        ctx.vision_result = dict(call2)
        ctx.vision_result["_source"] = "cascade"

        with patch("infra.animal_matcher.match_animal") as mock_match:
            mock_match.return_value = _verdict(name="Whiskers", score=0.95, species="cat")
            _animal_match_stage(ctx)

        mock_match.assert_called_once()
        self.assertEqual(ctx.animal_match.name, "Whiskers")

    def test_match_no_known_animals(self):
        """match_animal still called with empty list; returns NoMatch."""
        call2 = _sample_call2_animal(species="raccoon")
        ctx = _make_context(call2_response=call2, known_animals=[])
        ctx.vision_result = dict(call2)
        ctx.vision_result["_source"] = "cascade"

        with patch("infra.animal_matcher.match_animal") as mock_match:
            mock_match.return_value = _verdict(reason="no_known_animals")
            _animal_match_stage(ctx)

        mock_match.assert_called_once_with(ctx.vision_result, [])

    def test_match_animal_import_failure(self):
        """If infra.animal_matcher can't import, animal_match=None."""
        call2 = _sample_call2_animal(species="cat")
        ctx = _make_context(call2_response=call2, known_animals=[])
        ctx.vision_result = dict(call2)
        ctx.vision_result["_source"] = "cascade"

        with patch.dict("sys.modules", {"infra.animal_matcher": None}):
            _animal_match_stage(ctx)
        self.assertIsNone(ctx.animal_match)


# ---------------------------------------------------------------------------
# animal_emit
# ---------------------------------------------------------------------------


class TestAnimalEmitStage(unittest.TestCase):
    def test_emit_builds_body_for_match(self):
        ctx = _make_context()
        ctx.vision_result = _sample_call2_animal(species="cat")
        ctx.animal_match = _verdict(name="Whiskers", score=0.9)
        _animal_emit_stage(ctx)
        self.assertIn("Animal", ctx.structured_body)
        self.assertIn("Whiskers", ctx.structured_body)
        self.assertIn("walking along fence line", ctx.structured_body)
        self.assertFalse(ctx.telegram_sent)

    def test_emit_builds_body_for_no_match(self):
        ctx = _make_context()
        ctx.vision_result = _sample_call2_animal(species="raccoon")
        ctx.animal_match = _verdict(reason="species_filter_no_candidates")
        _animal_emit_stage(ctx)
        self.assertIn("species_filter_no_candidates", ctx.structured_body)
        self.assertIn("raccoon", ctx.structured_body)

    def test_emit_handles_empty_notable(self):
        ctx = _make_context()
        ctx.vision_result = {"species": "cat", "notable_details": []}
        ctx.animal_match = _verdict(reason="no_known_animals")
        _animal_emit_stage(ctx)
        self.assertNotIn("notes:", ctx.structured_body)


# ---------------------------------------------------------------------------
# process_animal_event orchestrator
# ---------------------------------------------------------------------------


class TestProcessAnimalEvent(unittest.TestCase):
    def test_full_pipeline_cascade_path(self):
        """call2 + known_animals → cascade identify → match → emit."""
        call2 = _sample_call2_animal(species="cat")
        known = [{"id": "a1", "name": "Whiskers", "species": "cat"}]
        ctx = _make_context(call2_response=call2, known_animals=known)

        with patch("infra.animal_matcher.match_animal") as mock_match:
            mock_match.return_value = _verdict(
                name="Whiskers", score=0.9, species="cat"
            )
            result = process_animal_event(ctx)

        self.assertEqual(result["species"], "cat")
        self.assertEqual(result["matched"], "Whiskers")
        self.assertIsNone(result["no_match_reason"])
        self.assertFalse(result["telegram_sent"])
        self.assertEqual(ctx.vision_result["_source"], "cascade")

    def test_full_pipeline_legacy_path(self):
        """No call2 → legacy scaffold → match still runs (sees empty)."""
        ctx = _make_context(call2_response=None, known_animals=[])
        with patch("infra.animal_matcher.match_animal") as mock_match:
            mock_match.return_value = _verdict(reason="no_known_animals")
            result = process_animal_event(ctx)
        self.assertEqual(ctx.vision_result["_source"], "legacy_scaffold")
        self.assertIsNone(result["matched"])
        self.assertEqual(result["no_match_reason"], "no_known_animals")

    def test_result_dict_keys(self):
        """Returned dict has expected schema."""
        ctx = _make_context(call2_response=_sample_call2_animal())
        with patch("infra.animal_matcher.match_animal") as mock_match:
            mock_match.return_value = _verdict(reason="no_known_animals")
            result = process_animal_event(ctx)
        for k in (
            "alert_id", "camera_name", "species", "matched",
            "no_match_reason", "telegram_sent",
        ):
            self.assertIn(k, result, f"missing key: {k}")


if __name__ == "__main__":
    unittest.main()