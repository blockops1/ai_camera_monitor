"""E2E tests for the person match branch in pipeline Stage 12 + Stage 13.

Verifies the person mode path through stage_perform_match and stage_build_tg3.

Test strategy:
    - Replace ``listener.pipeline._load_person_candidates`` with a
      synthetic candidate list so ``stage_perform_match(mode="person", vm2)``
      runs end-to-end with deterministic data (no file I/O).
    - Call ``stage_build_tg3`` to verify TG#3 output shape.
    - Real functions only -- no substitute objects on the formatter.

Acceptance criteria:
    AC1: pytest tests/e2e/test_pipeline_person_stub.py -v passes (>= 2 tests)
    AC2: ruff check tests/e2e/test_pipeline_person_stub.py = 0 errors
    AC3: zero substitute objects used anywhere in the test
"""

from __future__ import annotations

import os

# ---------------------------------------------------------------------------
# MUST come before ALL other imports: infra._require_env checks happen at
# module import time for quick_classifier, quiet_hours, alert_overrides, etc.
# ---------------------------------------------------------------------------
os.environ.setdefault("MOTION_GATE_NIGHT_SUPPRESS_ENABLED", "0")
os.environ.setdefault("FARMSURV_QUIET_HOURS_ENABLED", "0")
os.environ.setdefault("FARMSURV_NIGHT_TWILIGHT_BUFFER_MIN", "0")
os.environ.setdefault("FARMSURV_QUIET_HOURS_CAMERAS", "")
os.environ.setdefault("FARMSURV_NIGHT_SUPPRESS_CAMERAS", "")
os.environ.setdefault("FARMSURV_NIGHT_SUPPRESS_CLASSES", "")
os.environ.setdefault("ANIMAL_EMBEDDER_LIVE", "0")
# ---------------------------------------------------------------------------

import pytest

from listener.pipeline import stage_build_tg3, stage_perform_match

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_vm2(mode: str = "person", identity_markers: list[str] | None = None, **kw) -> dict:
    """Build a minimal VM2 result dict for the person branch."""
    vm2 = {
        "class": mode,
        "identity_markers": identity_markers or [],
        **kw,
    }
    return vm2


def _make_candidates(tags: list[str], markers: list[list[str]] | None = None) -> list[dict]:
    """Build a list of known-person candidate dicts."""
    markers = markers or []
    candidates = []
    for i, tag in enumerate(tags):
        cand_markers = markers[i] if markers and i < len(markers) else [tag]
        candidates.append({
            "tag": tag,
            "identity_markers": cand_markers,
        })
    return candidates


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _synthetic_candidates(monkeypatch):
    """Replace ``_load_person_candidates`` with synthetic data.

    The real ``_load_person_candidates`` reads from disk (PERSON_KNOWN_FILE).
    We replace it so tests run deterministically with no file I/O.
    """
    synthetic = _make_candidates(["john_doe", "jane_doe"], [
        ["john_doe"], ["jane_doe"],
    ])
    monkeypatch.setattr("listener.pipeline._load_person_candidates", lambda: synthetic)
    yield


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------


@pytest.mark.e2e
class TestPersonNoMatch:
    """AC1: No-match -- no identity_markers -> matched=False."""

    def test_no_identity_markers_returns_false(self):
        """No identity_markers in vm2 -> immediate no match."""
        vm2 = _make_vm2("person", identity_markers=[])
        result = stage_perform_match("person", vm2, camera_label="FRONT")

        assert result["matched"] is False


@pytest.mark.e2e
class TestPersonTagMatch:
    """AC2: Tag match -- first identity_marker exactly matches a candidate tag."""

    def test_tag_match_succeeds(self):
        """identity_markers[0] = 'john_doe' matches candidate tag 'john_doe'."""
        vm2 = _make_vm2("person", identity_markers=["john_doe"])
        result = stage_perform_match("person", vm2, camera_label="FRONT")

        assert result["matched"] is True
        assert result["tag"] == "john_doe"

    def test_tag_match_case_insensitive(self):
        """Tag match is case-insensitive, whitespace-stripped."""
        vm2 = _make_vm2("person", identity_markers=["  John_Doe  "])
        result = stage_perform_match("person", vm2, camera_label="FRONT")

        assert result["matched"] is True
        assert result["tag"] == "john_doe"

    def test_tag_match_no_match(self):
        """No candidate tag matches the first identity_marker."""
        vm2 = _make_vm2("person", identity_markers=["alice_smith"])
        result = stage_perform_match("person", vm2, camera_label="FRONT")

        assert result["matched"] is False


@pytest.mark.e2e
class TestPersonTg3Path:
    """AC3: TG#3 path -- stage_build_tg3(mode="person") produces valid output."""

    def test_tg3_no_match_person(self):
        """No-match person -> stage_build_tg3 returns dict with unrecognized indicator."""
        vm2 = _make_vm2("person", identity_markers=["unknown"])
        match_result = stage_perform_match("person", vm2, camera_label="FRONT")

        tg3 = stage_build_tg3(
            mode="person",
            match_result=match_result,
            vm2_result=vm2,
            crop_a_path="/tmp/fake_crop.jpg",
            crop_b_path="/tmp/fake_crop2.jpg",
            camera_label="FRONT Gate",
            alert={"id": "evt-001", "timestamp": "2026-09-26T10:00:00Z"},
        )

        # No-match person -> builds a caption with unrecognized indicator.
        assert "caption" in tg3
        assert tg3["photos"] == []
        assert "Unrecognized person" in tg3["caption"]

    def test_tg3_match_person(self):
        """Matched person -> stage_build_tg3 returns dict with caption."""
        vm2 = _make_vm2("person", identity_markers=["john_doe"])
        match_result = stage_perform_match("person", vm2, camera_label="FRONT")

        tg3 = stage_build_tg3(
            mode="person",
            match_result=match_result,
            vm2_result=vm2,
            crop_a_path="/tmp/fake_crop.jpg",
            crop_b_path="/tmp/fake_crop2.jpg",
            camera_label="FRONT Gate",
            alert={"id": "evt-001", "timestamp": "2026-09-26T10:00:00Z"},
        )

        # Matched person -> should have a caption (person-aware caption
        # formatting is wired in US-059e, not this card).
        assert "caption" in tg3
        assert tg3["photos"] == []
        assert "Camera:" in tg3["caption"]


@pytest.mark.e2e
class TestPersonNoGate:
    """AC4: Person matcher runs on ALL cameras (no FRONT/BACK gate).

    OUTSIDE_* cameras now proceed through the person branch and produce
    TG#3 (gate removed in US-106b).
    """

    def test_outside_camera_perform_match_proceeds(self):
        """stage_perform_match(mode='person', camera_label='OUTSIDE_BACK_SOLAR') -> runs matcher."""
        vm2 = _make_vm2("person", identity_markers=["john_doe"])
        result = stage_perform_match("person", vm2, camera_label="OUTSIDE_BACK_SOLAR")

        # Tag 'john_doe' is in synthetic candidates -> matched=True
        assert result["matched"] is True
        assert result["tag"] == "john_doe"

    def test_outside_front_camera_perform_match_proceeds(self):
        """stage_perform_match(mode='person', camera_label='OUTSIDE_FRONT_SOLAR') -> runs matcher."""
        vm2 = _make_vm2("person", identity_markers=["john_doe"])
        result = stage_perform_match("person", vm2, camera_label="OUTSIDE_FRONT_SOLAR")

        # Tag 'john_doe' is in synthetic candidates -> matched=True
        assert result["matched"] is True
        assert result["tag"] == "john_doe"

    def test_outside_camera_build_tg3_proceeds(self):
        """stage_build_tg3(mode='person', camera_label='OUTSIDE_BACK_SOLAR') -> valid caption."""
        match_result = {"matched": True, "tag": "john_doe"}
        vm2 = {"class": "person", "identity_markers": ["john_doe"]}
        tg3 = stage_build_tg3(
            mode="person",
            match_result=match_result,
            vm2_result=vm2,
            crop_a_path="/tmp/fake_crop.jpg",
            crop_b_path="/tmp/fake_crop2.jpg",
            camera_label="OUTSIDE_BACK_SOLAR",
            alert={"id": "evt-001", "timestamp": "2026-09-26T10:00:00Z"},
        )

        assert "caption" in tg3
        assert "OUTSIDE_BACK_SOLAR" in tg3["caption"]

    def test_back_camera_perform_match_allowed(self):
        """stage_perform_match(mode='person', camera_label='BACK') -> match proceeds."""
        vm2 = _make_vm2("person", identity_markers=["john_doe"])
        result = stage_perform_match("person", vm2, camera_label="BACK")

        assert result["matched"] is True

    def test_back_camera_build_tg3_allowed(self):
        """stage_build_tg3(mode='person', camera_label='BACK') -> valid caption."""
        # Match shape with label for formatter (mimics cosine-match shape).
        match_result = {
            "matched": True,
            "candidate": {"id": "john_doe", "label": "john_doe"},
            "cosine_score": 0.0,
            "tier1_score": 0.0,
        }
        vm2 = {"class": "person", "identity_markers": ["john_doe"]}
        tg3 = stage_build_tg3(
            mode="person",
            match_result=match_result,
            vm2_result=vm2,
            crop_a_path="/tmp/fake_crop.jpg",
            crop_b_path="/tmp/fake_crop2.jpg",
            camera_label="BACK",
            alert={"id": "evt-001", "timestamp": "2026-09-26T10:00:00Z"},
        )

        assert "caption" in tg3
        assert "john_doe" in tg3["caption"]

    def test_vehicle_branch_unaffected_by_gate(self):
        """Animal mode (any camera) proceeds -- person gate removal doesn't affect animal."""
        vm2_animal = {"class": "animal", "crop_a_path": "/tmp/fake.jpg"}
        result = stage_perform_match("animal", vm2_animal, camera_label="OUTSIDE_FRONT_SOLAR")

        # Animal should return no-match (no real crops) but not skip
        assert "matched" in result
