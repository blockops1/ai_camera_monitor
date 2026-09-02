"""Unit tests for infra.face_recognition (Phase 6B.106 lift).

The lifted module mirrors ~/ai_camera_monitor/src/face_recognition.py
(archived 2026-08-22) but uses absolute imports for the refactor layout.

Tests mock the insightface package so the 300MB buffalo_l model doesn't
need to be loaded — the production `recognize_faces` function is exercised
end-to-end in the live probe, not in unit tests.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

_root = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_root))


# -----------------------------------------------------------------------------
# Test fixtures: mock insightface so we don't need the 300MB model loaded
# -----------------------------------------------------------------------------


class _FakeFace:
    """Mimics an insightface.Face object: bbox (x1,y1,x2,y2) + 512-d embedding."""

    def __init__(self, bbox: list[float], embedding: list[float]):
        self.bbox = np.array(bbox, dtype=np.float32)
        self.embedding = np.array(embedding, dtype=np.float32)
        self.normed_embedding = self.embedding / np.linalg.norm(self.embedding)


@pytest.fixture
def fake_faces():
    """Three faces: small bbox (rejected), medium bbox (accepted), perfect ID match.

    All embeddings have full unit norm so the lazy normalization in the
    fixture doesn't hit divide-by-zero (the cosine_similarity() helper
    in production handles that case, but the fixture doesn't need to
    test it — that's test_cosine_similarity__zero_vector_returns_zero_not_nan).
    """
    # 30x30 — below MIN_BBOX_SIZE; should be rejected
    small_emb = np.ones(512, dtype=np.float32) * 0.05  # norm ~1.13
    small = _FakeFace(bbox=[10, 10, 40, 40], embedding=small_emb.tolist())

    # 80x80 — above MIN_BBOX_SIZE; should be accepted
    medium_emb = np.ones(512, dtype=np.float32) / np.sqrt(512)
    medium = _FakeFace(bbox=[100, 100, 180, 180], embedding=medium_emb.tolist())

    # 200x200 — above MIN_BBOX_SIZE; should be accepted and match "enrolled"
    # Same direction as medium → cosine sim = 1.0
    large = _FakeFace(bbox=[300, 300, 500, 500], embedding=medium_emb.tolist())

    return [small, medium, large]


@pytest.fixture
def mock_face_app(fake_faces):
    """Patch _get_app() to return a mock FaceAnalysis that yields `fake_faces`."""
    mock_app = MagicMock()
    mock_app.get.return_value = fake_faces
    with patch("infra.face_recognition._get_app", return_value=mock_app) as mock_get_app:
        yield mock_get_app, mock_app


@pytest.fixture(autouse=True)
def enrolled_identity():
    """One enrolled identity: face_embedding aligned with the 'large' fake face.

    Same direction as the 'large' fake face → cosine sim = 1.0.
    Applied automatically to every test in this module so they don't
    need to take it as a parameter.
    """
    import infra.faces as faces_mod

    aligned_emb = np.ones(512, dtype=np.float32) / np.sqrt(512)
    fake_id = {
        "name": "maintainer",
        "role": "owner",
        "face_embedding": aligned_emb.tolist(),
        "sample_count": 1,
    }

    fake_path = "/fake/identities/mr_v.json"
    with patch.object(faces_mod, "_iter_identity_paths", return_value=[fake_path]), \
         patch.object(faces_mod, "load_identity_by_path", return_value=fake_id):
        yield fake_id


# -----------------------------------------------------------------------------
# Module constants
# -----------------------------------------------------------------------------


def test_match_threshold_is_in_arcface_default_range():
    """ArcFace thresholds typically 0.3-0.5; 0.4 is the documented default."""
    from infra.face_recognition import MATCH_THRESHOLD
    assert 0.3 <= MATCH_THRESHOLD <= 0.5, (
        f"MATCH_THRESHOLD={MATCH_THRESHOLD} outside ArcFace default range; "
        f"tune with a real benchmark before changing"
    )


def test_min_bbox_size_matches_documented_value():
    """MIN_BBOX_SIZE=35 was calibrated empirically for our camera distances."""
    from infra.face_recognition import MIN_BBOX_SIZE
    assert MIN_BBOX_SIZE == 35, (
        f"MIN_BBOX_SIZE={MIN_BBOX_SIZE} — only change after a live calibration sweep"
    )


# -----------------------------------------------------------------------------
# Cosine similarity
# -----------------------------------------------------------------------------


def test_cosine_similarity__identical_vectors_returns_one():
    from infra.face_recognition import cosine_similarity
    v = [1.0, 0.0, 0.0]
    assert abs(cosine_similarity(v, v) - 1.0) < 1e-6


def test_cosine_similarity__orthogonal_vectors_returns_zero():
    from infra.face_recognition import cosine_similarity
    a, b = [1.0, 0.0], [0.0, 1.0]
    assert abs(cosine_similarity(a, b)) < 1e-6


def test_cosine_similarity__zero_vector_returns_zero_not_nan():
    """Zero vector must return 0.0, never NaN — matches old module behavior."""
    from infra.face_recognition import cosine_similarity
    assert cosine_similarity([0.0, 0.0], [1.0, 0.0]) == 0.0
    assert cosine_similarity([1.0, 0.0], [0.0, 0.0]) == 0.0
    assert cosine_similarity([0.0, 0.0], [0.0, 0.0]) == 0.0


# -----------------------------------------------------------------------------
# Frame normalization
# -----------------------------------------------------------------------------


def test_load_frame__ndarray_passthrough():
    """If input is already uint8 (H,W,3) ndarray, return unchanged."""
    from infra.face_recognition import _load_frame
    arr = np.zeros((100, 100, 3), dtype=np.uint8)
    result = _load_frame(arr)
    assert result is arr


def test_load_frame__unknown_type_raises_typeerror():
    """Passing a non-frame type raises TypeError with a clear message."""
    from infra.face_recognition import _load_frame
    with pytest.raises(TypeError, match="frame must be ndarray"):
        _load_frame(42)


# -----------------------------------------------------------------------------
# recognize_faces — the main entry point
# -----------------------------------------------------------------------------


def test_recognize_faces__no_faces_returns_empty_result(mock_face_app):
    """If detector finds nothing, return {"faces": [], "identified_person": None}."""
    from infra.face_recognition import recognize_faces

    _mock_get_app, mock_app = mock_face_app
    mock_app.get.return_value = []  # override: no faces this time

    img = np.zeros((480, 640, 3), dtype=np.uint8)
    result = recognize_faces(img)

    assert result == {
        "faces": [],
        "identified_person": None,
        "best_confidence": None,
    }


def test_recognize_faces__below_min_bbox_filtered_out(mock_face_app):
    """Tiny detections (below MIN_BBOX_SIZE) are filtered before embedding."""
    from infra.face_recognition import recognize_faces
    _, _mock_app = mock_face_app

    img = np.zeros((480, 640, 3), dtype=np.uint8)
    result = recognize_faces(img)

    # fake_faces has 3 entries: small (30x30, REJECTED), medium (80x80, KEPT),
    # large (200x200, KEPT). 2 expected to survive.
    assert len(result["faces"]) == 2
    bboxes = [f["bbox"] for f in result["faces"]]
    # small face was rejected
    assert not any(int(b[2] - b[0]) < 50 for b in bboxes), (
        "faces below MIN_BBOX_SIZE should be filtered out"
    )


def test_recognize_faces__exact_match_identifies_with_perfect_confidence(
    mock_face_app,
):
    """A face whose embedding is identical to an enrolled identity's gets identified."""
    from infra.face_recognition import recognize_faces

    img = np.zeros((480, 640, 3), dtype=np.uint8)
    result = recognize_faces(img)

    assert result["identified_person"] == "maintainer"
    assert result["best_confidence"] is not None
    assert result["best_confidence"] > 0.95  # essentially 1.0 cosine sim


def test_recognize_faces__no_match_above_threshold(mock_face_app):
    """If no enrolled identity has cosine_sim >= MATCH_THRESHOLD, return None."""
    import infra.faces as faces_mod
    from infra.face_recognition import recognize_faces

    # Override: no enrolled identities at all
    with patch.object(faces_mod, "_iter_identity_paths", return_value=[]), \
         patch.object(faces_mod, "load_identity_by_path", return_value=None):
        img = np.zeros((480, 640, 3), dtype=np.uint8)
        result = recognize_faces(img)

    assert result["identified_person"] is None
    assert result["best_confidence"] is None
    # Faces still detected though — they're just not identified
    assert len(result["faces"]) >= 1


# -----------------------------------------------------------------------------
# Lazy loading guard
# -----------------------------------------------------------------------------


def test_get_app__lazy_loads_then_caches():
    """_get_app() returns the same cached instance on subsequent calls."""
    from infra import face_recognition as fr_mod

    # Reset cache for this test
    fr_mod._APP_CACHE = None

    mock_app_instance = MagicMock()
    # FaceAnalysis is imported lazily inside _get_app() from insightface.app
    # — patch the SOURCE module, not the consumer (per software-development-
    # practices §4 "patch caller.target when caller re-exports target").
    with patch("insightface.app.FaceAnalysis", return_value=mock_app_instance) as mock_fa:
        app1 = fr_mod._get_app()
        app2 = fr_mod._get_app()

    assert app1 is app2  # cached
    assert mock_fa.call_count == 1  # loaded only once
    mock_app_instance.prepare.assert_called_once()


def test_get_app__import_failure_returns_none(monkeypatch):
    """If insightface is missing, _get_app() returns None — caller handles gracefully."""
    from infra import face_recognition as fr_mod

    fr_mod._APP_CACHE = None

    # Simulate missing insightface package
    with patch.dict(sys.modules, {"insightface": None, "insightface.app": None}):
        # Should not raise — return None
        result = fr_mod._get_app_safe()

    assert result is None, "_get_app_safe() must return None when insightface missing"
