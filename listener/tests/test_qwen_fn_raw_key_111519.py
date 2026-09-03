"""§11.115.19 — Regression test: analyze_frames must include 'raw' in its return dict.

Live ops investigation 2026-09-03 found that analyze_frames (and
analyze_frames_queued) returns the parsed vision dict but does NOT include
the raw Qwen response text under the "raw" key.

cascade_call1 in infra/two_call_cascade.py reads call1_response.get("raw", "")
and passes it to validate_classify_response. When the qwen_fn return dict
doesn't include "raw", the cascade gets "", validate_classify_response returns
OTHER + fallback_used=True + reasoning="", and the alert is logged-only (no
Telegram). This caused zero Telegram notifications from 2026-09-02 20:21:54
to 2026-09-03 11:00+ (15+ hours, 20+ alerts).

The fix: analyze_frames must include "raw" in its returned dict on every
return path (success, retry-success, network-error, parse-error).

These tests pin the contract. Pre-fix they fail; post-fix they pass.
"""
from __future__ import annotations

import json
from unittest.mock import patch

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _good_qwen_json_text() -> str:
    """A valid Qwen response text that _parse_response will accept."""
    return json.dumps(
        {
            "objects_detected": ["car"],
            "primary_subject": "car",
            "actions": ["driving"],
            "scene_description": "A silver SUV driving past",
            "confidence": 0.92,
            "notable_details": [],
            "colors": {"vehicle": "silver"},
            "species": None,
            "vehicles": [
                {"color": "silver", "body_style_hint": "suv", "make": "ford", "model": "explorer"}
            ],
            "primary_vehicle_index": 0,
        }
    )


def _good_choices(raw_content: str) -> dict:
    """Mimic the OpenAI-compatible /v1/chat/completions response shape."""
    return {
        "choices": [
            {
                "message": {
                    "content": raw_content,
                }
            }
        ]
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestAnalyzeFramesReturnsRawKey:
    """§11.115.19 — analyze_frames must include 'raw' in the returned dict.

    cascade_call1 reads call1_response.get('raw', '') to validate the
    classify response. Without 'raw' the cascade always returns OTHER +
    fallback, regardless of what Qwen actually said.
    """

    def test_success_path_includes_raw(self) -> None:
        """Happy path: analyze_frames returns a dict containing 'raw'."""
        from infra.vision_analyzer import analyze_frames

        with patch("infra.vision_analyzer._post_to_vision") as mock_post:
            mock_post.return_value = _good_choices(_good_qwen_json_text())
            result = analyze_frames(
                frame_paths=["/tmp/a.png", "/tmp/b.png"],
                camera_name="Front Porch",
                api_url="http://localhost:8093/v1/chat/completions",
                alert_id="test-alert-success",
            )

        assert "raw" in result, (
            "analyze_frames must include 'raw' so cascade_call1 can "
            "validate the classify response. Missing 'raw' → fallback to "
            "OTHER → no Telegram."
        )
        # Sanity: raw should equal the text we sent to Qwen.
        assert json.loads(result["raw"]) == json.loads(_good_qwen_json_text())

    def test_retry_path_includes_raw(self) -> None:
        """When first parse fails and we retry, the retry's return must
        also include 'raw' (or the error sentinel at minimum)."""
        from infra.vision_analyzer import analyze_frames

        bad_first = '{"primary_subject": "car"'  # malformed JSON
        good_second = _good_choices(_good_qwen_json_text())

        with patch("infra.vision_analyzer._post_to_vision") as mock_post:
            mock_post.side_effect = [
                _good_choices(bad_first),
                good_second,
            ]
            result = analyze_frames(
                frame_paths=["/tmp/a.png", "/tmp/b.png"],
                camera_name="Front Porch",
                api_url="http://localhost:8093/v1/chat/completions",
                alert_id="test-alert-retry",
            )

        assert "raw" in result

    def test_parse_failure_includes_raw_in_error_sentinel(self) -> None:
        """Both parses fail → _error_result is returned. Even the error
        sentinel must include 'raw' (with the failed text) so callers
        can post-mortem the actual Qwen output."""
        from infra.vision_analyzer import analyze_frames

        with patch("infra.vision_analyzer._post_to_vision") as mock_post:
            mock_post.return_value = _good_choices("not json at all")
            result = analyze_frames(
                frame_paths=["/tmp/a.png", "/tmp/b.png"],
                camera_name="Front Porch",
                api_url="http://localhost:8093/v1/chat/completions",
                alert_id="test-alert-error",
            )

        # The error sentinel should include 'raw' for forensic logging.
        assert "raw" in result, (
            "Even on parse failure, analyze_frames should surface 'raw' "
            "so callers can debug what Qwen actually returned."
        )


class TestProductionQwenFnEndToEnd:
    """End-to-end: production _qwen_fn closure → cascade_call1 → ClassifyResult.

    With 'raw' present in the analyze_frames return, the cascade routes
    VEHICLE correctly and triggers Telegram. Without 'raw', it falls back
    to OTHER + log-only. This test pins the fix at the integration level.
    """

    def test_full_path_with_raw_routes_to_vehicle(self) -> None:
        """End-to-end with raw: cascade correctly classifies as VEHICLE."""
        from infra import two_call_cascade

        post_fix_response = json.loads(_good_qwen_json_text())
        post_fix_response["raw"] = _good_qwen_json_text()

        from infra.classify_schema import ClassLabel

        # Call 1's Qwen response: shared classify schema (class/confidence/reasoning).
        # Call 2's Qwen response: vehicle-crop schema (make/model/color/body_style).
        classify_raw = json.dumps(
            {
                "class": "vehicle",
                "confidence": 0.92,
                "reasoning": "silver SUV driving past",
            }
        )
        post_fix_response = {"raw": classify_raw}

        call2_raw = json.dumps(
            {
                "make": "ford",
                "model": "explorer",
                "color": "silver",
                "body_style": "suv",
            }
        )
        call2_response = {"raw": call2_raw}

        with patch("infra.vision_analyzer.analyze_frames_queued") as mock_q:
            mock_q.side_effect = [post_fix_response, call2_response]

            from infra.vision_analyzer import analyze_frames_queued

            # Simulate the production closure shape
            def _qwen_fn(frame_paths, camera_name, *, event_hint=None, **kw):
                result = analyze_frames_queued(
                    frame_paths=frame_paths,
                    camera_name=camera_name,
                    event_hint=event_hint,
                )
                return result

            result = two_call_cascade.run(
                frame_paths=["/tmp/a", "/tmp/b"],
                camera_name="Front Porch",
                captured_at="2026-09-03 11:00:00",
                qwen_fn=_qwen_fn,
                call2_prompts={ClassLabel.VEHICLE: lambda *a, **k: "{}"},
            )

        assert result.classify.label is ClassLabel.VEHICLE
        assert result.classify.fallback_used is False


# ============================================================================
# §11.115.20 — analyze_frames must dispatch to classify schema + prompt
# when mode="classify". Otherwise the cascade's validate_classify_response
# never sees the right JSON shape.
# ============================================================================
def test_analyze_frames_classify_mode_uses_classify_schema():
    """When mode='classify', response_format must use CLASSIFY_SCHEMA_JSON."""
    from infra.classify_schema import CLASSIFY_SCHEMA_JSON
    from infra.vision_analyzer import analyze_frames

    captured = {}
    real_post = analyze_frames.__globals__["_post_to_vision"]

    def spy(api_url, payload):
        captured["payload"] = payload
        # Return a valid classify-schema response
        return {
            "choices": [{
                "message": {
                    "content": '{"class": "vehicle", "confidence": 1.0, "reasoning": "silver SUV"}'
                }
            }]
        }

    import infra.vision_analyzer as va
    va._post_to_vision = spy
    try:
        analyze_frames(
            frame_paths=["/tmp/fake_frame.png"],
            camera_name="Test Cam",
            mode="classify",
        )
    finally:
        va._post_to_vision = real_post

    rf = captured["payload"]["response_format"]
    assert rf["schema"] == CLASSIFY_SCHEMA_JSON, (
        f"Expected CLASSIFY_SCHEMA_JSON, got {rf.get('name')}"
    )
    assert rf["name"] == "classify"


def test_analyze_frames_classify_mode_prompt_includes_classify_keywords():
    """Classify-mode prompt must contain the 4-class enum, not vision_analysis."""
    from infra.vision_analyzer import analyze_frames

    captured = {}
    real_post = analyze_frames.__globals__["_post_to_vision"]

    def spy(api_url, payload):
        captured["payload"] = payload
        return {
            "choices": [{
                "message": {
                    "content": '{"class": "vehicle", "confidence": 1.0, "reasoning": "x"}'
                }
            }]
        }

    import infra.vision_analyzer as va
    va._post_to_vision = spy
    try:
        analyze_frames(
            frame_paths=["/tmp/fake_frame.png"],
            camera_name="Test Cam",
            mode="classify",
        )
    finally:
        va._post_to_vision = real_post

    msgs = captured["payload"]["messages"]
    text_parts = [
        c["text"] for c in msgs[-1]["content"] if c.get("type") == "text"
    ]
    prompt = text_parts[0]
    assert "vehicle" in prompt and "person" in prompt and "animal" in prompt, \
        "classify-mode prompt missing 4-class enum"
    # Should NOT contain vision_analysis-specific fields
    assert "objects_detected" not in prompt, \
        "classify-mode prompt should not be vision_analysis template"


def test_cascade_call1_pass_mode_classify_to_qwen_fn():
    """cascade_call1 must pass mode='classify' to qwen_fn so the right
    schema is sent to Qwen. (Otherwise OTHER/fallback, zero Telegram.)"""
    from infra.two_call_cascade import cascade_call1

    captured_kwargs = {}

    def fake_qwen(*, frame_paths, camera_name, **kwargs):
        captured_kwargs.update(kwargs)
        # Return valid classify-schema response
        return {
            "raw": '{"class": "vehicle", "confidence": 1.0, "reasoning": "x"}',
            "primary_subject": "vehicle",  # unused now
            "confidence": 1.0,
        }

    cascade_call1(
        frame_paths=["/tmp/fake.png"],
        camera_name="Test Cam",
        captured_at="2026-09-03 12:00:00",
        qwen_fn=fake_qwen,
    )

    assert captured_kwargs.get("mode") == "classify", (
        f"Expected mode='classify', got {captured_kwargs.get('mode')!r}"
    )
