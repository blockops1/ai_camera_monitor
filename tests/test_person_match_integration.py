"""test_person_match_integration.py — Full person-alert path through pipeline.

Exercises: gate (mocked) -> verify_class (mocked, person) -> detail_class
(mocked, identity_markers) -> match_person -> TG#3 build.

Positive path: vm2 identity_markers match a candidate tag -> matched=True + TG#3.
Negative path: vm2 identity_markers don't match any candidate -> matched=False + TG#3.

No live Telegram calls. No live Qwen calls. All deps mocked via monkeypatch.
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock

import pytest

from infra.gate import GateVerdict
from listener.pipeline import run as pipeline_run

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_ENROLLED_PERSON = {
    "name": "Red Jacket Person",
    "tag": "red-jacket",
    "identity_markers": ["red-jacket", "beard", "tall"],
}

_CANDIDATE_POSITIVE = [_ENROLLED_PERSON]

_CANDIDATE_NEGATIVE = [
    {
        "name": "Blue Jacket Person",
        "tag": "blue-jacket",
        "identity_markers": ["blue-jacket", "short", "glasses"],
    }
]

_SAMPLE_FRAMES = [
    os.path.join(os.path.dirname(__file__), "data", "frame1.jpg"),
    os.path.join(os.path.dirname(__file__), "data", "frame2.jpg"),
]


def _make_gate_verdict():
    """Build a mock GateVerdict with classification='person'."""
    v = MagicMock(spec=GateVerdict)
    v.classification = "person"
    v.class_label = "person"
    v.confidence = 0.88
    v.reason = "high_conf_person"
    v.top_class = "person"
    v.top_confidence = 0.88
    v.crop_a = _SAMPLE_FRAMES[0]
    v.crop_b = _SAMPLE_FRAMES[1]
    v.pairwise_diff_path = _SAMPLE_FRAMES[0]
    # is_none must return False (we are person, not none)
    v.is_none.return_value = False
    return v


@pytest.fixture()
def person_alert():
    """Return a person-mode alert dict."""
    return {
        "id": "evt-fake-001",
        "camera_id": "FRONT",
        "camera_label": "Front Gate",
        "timestamp": "2026-09-12T19:02:50.000+0000",
        "classification": "motion",
        "frames": _SAMPLE_FRAMES,
    }


# ---------------------------------------------------------------------------
# Helpers — build a full mock env for pipeline.run()
# ---------------------------------------------------------------------------


def _patch_pipeline_for_person(monkeypatch, candidates, vm2_result):
    """Patch all external dependencies for a person-mode pipeline run.

    Returns patched (gate_verdict_mock, verify_mock, detail_mock) for
    optional inspection by the caller.
    """
    gate_verdict = _make_gate_verdict()

    # Patch _load_person_candidates to return our fixture candidates.
    monkeypatch.setattr(
        "listener.pipeline._load_person_candidates",
        lambda: candidates,
    )

    # Patch _load_candidates to return [] (no vehicle candidates needed).
    monkeypatch.setattr(
        "listener.pipeline._load_candidates",
        list,
    )

    # Patch run_gate -> return our gate verdict (person passes).
    monkeypatch.setattr(
        "listener.pipeline.run_gate",
        MagicMock(return_value=gate_verdict),
    )

    # Patch verify_class -> return class='person'.
    verify_mock = MagicMock(return_value={"class": "person", "confidence": 0.88})
    monkeypatch.setattr("listener.pipeline.verify_class", verify_mock)

    # Patch detail_class -> return our vm2_result.
    detail_mock = MagicMock(return_value=vm2_result)
    monkeypatch.setattr("listener.pipeline.detail_class", detail_mock)

    # Patch should_suppress -> False (don't suppress).
    monkeypatch.setattr(
        "listener.pipeline.should_suppress",
        MagicMock(return_value=False),
    )

    # Patch record_hit -> no-op.
    monkeypatch.setattr(
        "listener.pipeline.record_hit",
        MagicMock(),
    )

    # Patch prepare_alert_artifacts -> returns a fake artifacts object.
    fake_artifacts = MagicMock()
    fake_artifacts.crop_a_path = _SAMPLE_FRAMES[0]
    fake_artifacts.crop_b_path = _SAMPLE_FRAMES[1]
    fake_artifacts.full_image_path = _SAMPLE_FRAMES[0]
    monkeypatch.setattr(
        "listener.pipeline.prepare_alert_artifacts",
        MagicMock(return_value=fake_artifacts),
    )

    return gate_verdict, verify_mock, detail_mock


# ---------------------------------------------------------------------------
# Positive path — tag matches
# ---------------------------------------------------------------------------


def test_positive_path_match(person_alert, monkeypatch):
    """AC: vm2 identity_markers match candidate tag -> matched=True + TG#3."""
    vm2_result = {
        "identity_markers": ["red-jacket", "beard", "tall"],
        "confidence": 0.92,
        "attributes": {
            "clothing_upper": "red jacket",
            "clothing_lower": "blue jeans",
        },
        "signature": {
            "stable": ["tall"],
            "transient": ["carrying a bag"],
        },
        "notable_details": [],
    }

    _patch_pipeline_for_person(monkeypatch, _CANDIDATE_POSITIVE, vm2_result)

    result = pipeline_run(person_alert)

    assert result["status"] == "ok"
    assert result["classification"] == "motion"

    match_result = result["match_result"]
    assert match_result["matched"] is True
    assert match_result["name"] == "Red Jacket Person"
    assert match_result["tag"] == "red-jacket"

    # TG#3 should be non-empty and contain a match message.
    tg3 = result["tg3"]
    assert tg3
    assert "caption" in tg3
    assert "recognized" in tg3["caption"].lower() or "match" in tg3["caption"].lower()


# ---------------------------------------------------------------------------
# Negative path — no tag match
# ---------------------------------------------------------------------------


def test_negative_path_no_match(person_alert, monkeypatch):
    """AC: vm2 identity_markers don't match any candidate -> matched=False + TG#3."""
    vm2_result = {
        "identity_markers": ["green-hat", "tall"],
        "confidence": 0.85,
        "attributes": {
            "clothing_upper": "green hat",
            "clothing_lower": "black pants",
        },
        "signature": {
            "stable": ["tall"],
            "transient": [],
        },
        "notable_details": [],
    }

    _patch_pipeline_for_person(monkeypatch, _CANDIDATE_NEGATIVE, vm2_result)

    result = pipeline_run(person_alert)

    assert result["status"] == "ok"

    match_result = result["match_result"]
    assert match_result["matched"] is False

    # TG#3 should still be produced (stage 13 fires for person too)
    # but indicate no-match.
    tg3 = result["tg3"]
    assert tg3
    assert "caption" in tg3
    assert "unrecognized" in tg3["caption"].lower() or "no_match" in tg3["caption"].lower() or not tg3["caption"].strip()


# ---------------------------------------------------------------------------
# Edge: empty identity_markers -> no match immediately
# ---------------------------------------------------------------------------


def test_no_identity_markers(person_alert, monkeypatch):
    """AC: person alert with no identity_markers -> matched=False, no crash."""
    vm2_result = {
        "identity_markers": [],
        "confidence": 0.5,
        "attributes": {},
        "signature": {"stable": [], "transient": []},
        "notable_details": [],
    }

    _patch_pipeline_for_person(monkeypatch, _CANDIDATE_POSITIVE, vm2_result)

    result = pipeline_run(person_alert)

    assert result["status"] == "ok"
    assert result["match_result"]["matched"] is False


# ---------------------------------------------------------------------------
# Edge: missing identity_markers key -> no match immediately
# ---------------------------------------------------------------------------


def test_missing_identity_markers_key(person_alert, monkeypatch):
    """AC: person alert without identity_markers key -> matched=False, no crash."""
    vm2_result = {
        "confidence": 0.5,
        "attributes": {},
        "signature": {"stable": [], "transient": []},
        "notable_details": [],
    }

    _patch_pipeline_for_person(monkeypatch, _CANDIDATE_POSITIVE, vm2_result)

    result = pipeline_run(person_alert)

    assert result["status"] == "ok"
    assert result["match_result"]["matched"] is False
