"""
test_vision_attrs_logging_6B164.py — Tests for Phase 6B.164 logging additions.

Phase 6B.164 (2026-08-29): added vision_attrs structured log line so the
matcher scoring is debuggable. Tests pin:
  - _log_vision_attrs emits one log line per call
  - log line contains every stable-attribute field
  - log line includes face_visible and scene_description (truncated)
  - log line handles missing fields (None / absent) gracefully
  - log line handles malformed nested blocks (silhouette=null, hair=null)
  - log line handles empty persons list
  - person_match_stage NoMatch path logs best_confidence too
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path
from unittest.mock import patch

# Make repo root importable
sys_root = Path(__file__).resolve().parents[2]
if str(sys_root) not in sys.path:
    sys.path.insert(0, str(sys_root))

from listener.person_event_pipeline import (
    PersonContext,
    _log_vision_attrs,
)


def _make_ctx(alert_id: str = "alert-1") -> PersonContext:
    return PersonContext(
        alert_id=alert_id,
        camera_name="CAM3",
        timestamp="2026-08-29 14:32:10",
        event_type="person",
        rtsp_url="rtsp://example",
        output_dir="/tmp/test",
        bot_token="test-token",
        chat_id="test-chat",
        api_url="http://example/api",
    )


def _capture_log(caplog, level=logging.INFO):
    caplog.set_level(level, logger="listener.person_event_pipeline")
    return caplog


# ---------- _log_vision_attrs tests ----------

def test_logs_all_stable_attribute_fields(caplog):
    """Every stable-attribute field appears in the log line."""
    ctx = _make_ctx("alert-1")
    _capture_log(caplog)
    vision_result = {
        "persons": [{
            "person_id": "p1",
            "face_visible": True,
            "silhouette": {"build": "average", "height": "tall"},
            "skin_tone": "light",
            "age_range": "middle_aged",
            "hair": {"color": "gray", "length": "bald", "style": "shaved"},
            "facial_hair": "none",
            "glasses": "prescription",
        }],
        "primary_person_index": 0,
        "scene_description": "Person walking on gravel path",
        "confidence": 0.85,
    }
    _log_vision_attrs(ctx, vision_result)
    text = caplog.text
    for field in ("face_visible=True", "persons=1", "conf=0.85",
                  "silhouette.build=average", "silhouette.height=tall",
                  "skin_tone=light", "age_range=middle_aged",
                  "hair.color=gray", "hair.length=bald", "hair.style=shaved",
                  "facial_hair=none", "glasses=prescription",
                  "scene='Person walking on gravel path'"):
        assert field in text, f"missing '{field}' in log:\n{text}"


def test_handles_null_stable_attributes(caplog):
    """Back-to-camera shot: all stable attrs are null. Log still emits."""
    ctx = _make_ctx("alert-2")
    _capture_log(caplog)
    vision_result = {
        "persons": [{
            "person_id": "p1",
            "face_visible": False,
            "silhouette": {"build": None, "height": None},
            "skin_tone": None,
            "age_range": None,
            "hair": {"color": None, "length": None, "style": None},
            "facial_hair": None,
            "glasses": None,
        }],
        "primary_person_index": 0,
        "scene_description": "Person walking away from camera",
        "confidence": 0.6,
    }
    _log_vision_attrs(ctx, vision_result)
    text = caplog.text
    for field in ("face_visible=False", "silhouette.build=None",
                  "silhouette.height=None", "skin_tone=None", "age_range=None",
                  "hair.color=None", "hair.length=None", "hair.style=None",
                  "facial_hair=None", "glasses=None"):
        assert field in text, f"missing '{field}' in log:\n{text}"


def test_handles_malformed_nested_blocks(caplog):
    """silhouette=null, hair=null (not even dicts). Should log None, not crash."""
    ctx = _make_ctx("alert-3")
    _capture_log(caplog)
    vision_result = {
        "persons": [{
            "person_id": "p1",
            "face_visible": False,
            "silhouette": None,
            "skin_tone": None,
            "age_range": None,
            "hair": None,
            "facial_hair": None,
            "glasses": None,
        }],
        "primary_person_index": 0,
        "scene_description": "",
        "confidence": 0.5,
    }
    _log_vision_attrs(ctx, vision_result)
    text = caplog.text
    for field in ("silhouette.build=None", "silhouette.height=None",
                  "hair.color=None", "hair.length=None", "hair.style=None"):
        assert field in text, f"missing '{field}' in log:\n{text}"


def test_handles_empty_persons_list(caplog):
    """No persons — log still emits one line with persons=0."""
    ctx = _make_ctx("alert-4")
    _capture_log(caplog)
    vision_result = {"persons": [], "primary_person_index": 0,
                     "scene_description": "empty", "confidence": 0.0}
    _log_vision_attrs(ctx, vision_result)
    text = caplog.text
    assert "persons=0" in text
    # Primary is None — all attrs default to None
    assert "silhouette.build=None" in text


def test_truncates_long_scene_description(caplog):
    """Scene description over 120 chars should be truncated."""
    ctx = _make_ctx("alert-5")
    _capture_log(caplog)
    long_scene = "x" * 200
    vision_result = {
        "persons": [], "primary_person_index": 0,
        "scene_description": long_scene, "confidence": 0.5,
    }
    _log_vision_attrs(ctx, vision_result)
    text = caplog.text
    # Should not contain full 200-char string (truncated to 120)
    assert "x" * 121 not in text
    # But should contain 120 of them
    assert "x" * 120 in text


def test_log_line_includes_alert_id(caplog):
    """Log line prefix uses ctx.alert_id so we can grep for it."""
    ctx = _make_ctx("MY-SPECIAL-ID")
    _capture_log(caplog)
    vision_result = {"persons": [], "primary_person_index": 0,
                     "scene_description": "", "confidence": 0.0}
    _log_vision_attrs(ctx, vision_result)
    assert "[MY-SPECIAL-ID]" in caplog.text
    assert "vision_attrs:" in caplog.text


# ---------- person_match_stage NoMatch best_confidence log test ----------

def test_no_match_log_includes_best_confidence(caplog):
    """NoMatch path now logs best_confidence (Phase 6B.164)."""
    from infra.person_matcher import NoMatch

    ctx = _make_ctx("alert-6")
    ctx.vision_result = {"persons": [], "primary_person_index": 0,
                         "scene_description": "", "confidence": 0.5}
    ctx.face_recognition = None

    nm = NoMatch(reason="stable_attributes_no_match",
                 best_candidate_name="maintainer",
                 best_candidate_confidence=0.420)

    _capture_log(caplog)

    # Patch out the inner match_person call and the known_persons loader
    # so we just exercise the post-match logging branch.
    with patch("infra.person_matcher.match_person", return_value=nm), \
         patch("listener.person_event_pipeline._load_known_persons_for_matching",
               return_value=[{"name": "maintainer"}]):
        from listener.person_event_pipeline import person_match_stage
        person_match_stage(ctx)

    text = caplog.text
    assert "best_confidence=0.420" in text, f"missing best_confidence:\n{text}"
    assert "best_candidate='maintainer'" in text
    assert "reason=stable_attributes_no_match" in text


def test_no_match_log_handles_none_best_confidence(caplog):
    """NoMatch with best_confidence=None logs 'None' (no candidates)."""
    from infra.person_matcher import NoMatch

    ctx = _make_ctx("alert-7")
    ctx.vision_result = {"persons": [], "primary_person_index": 0,
                         "scene_description": "", "confidence": 0.5}
    ctx.face_recognition = None

    nm = NoMatch(reason="stable_attributes_no_match",
                 best_candidate_name=None,
                 best_candidate_confidence=None)

    _capture_log(caplog)

    with patch("infra.person_matcher.match_person", return_value=nm), \
         patch("listener.person_event_pipeline._load_known_persons_for_matching",
               return_value=[]):
        from listener.person_event_pipeline import person_match_stage
        person_match_stage(ctx)

    text = caplog.text
    assert "best_confidence=None" in text, f"missing 'best_confidence=None':\n{text}"
