"""test_cross_diff_bbox_e2e — e2e regression tests on ground-truth FRONT alerts.

Validates that compute_cross_diff_bboxes produces correct bboxes for real
frame sequences from the FRONT camera. Covers three behavioural categories:

- Entering (N alerts):  subject enters frame; bbox_a and bbox_b both active.
  IoU(bbox_a, expected_bbox_a) > 0.50 and IoU(bbox_b, expected_bbox_b) > 0.50.
- Exit (N alerts):       subject exiting; only bbox_a active.
  IoU(bbox_a, expected_bbox_a) > 0.50; bbox_b is None.
- Stationary (N alerts): no motion; both bboxes are None.
  bbox_a is None AND bbox_b is None. No fallback warning in logs.

Ground truth comes from data/frames/FRONT/ground_truth.json which is
regenerated periodically by the US-109d pipeline.

Each alert parameter is tested individually; if that alert's frame directory
is missing from disk the test is skipped (not the whole suite).

Acceptance criteria:
  - AC1: pytest tests/test_cross_diff_bbox_e2e.py -v passes
  - AC2: ruff check tests/test_cross_diff_bbox_e2e.py = 0 errors
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from infra.frame_diff import _compute_iou, compute_cross_diff_bboxes

# ============================================================================
# Fixtures
# ============================================================================

# Directory containing FRONT alert frame data.
FRONT_FRAMES_DIR = Path(__file__).resolve().parents[1] / "data" / "frames" / "FRONT"

# Ground-truth JSON with hand-labeled expected bboxes per alert.
GROUND_TRUTH_PATH = FRONT_FRAMES_DIR / "ground_truth.json"


def _load_ground_truth() -> dict:
    """Load ground_truth.json, returning only alert entries (no _generated keys)."""
    with open(GROUND_TRUTH_PATH) as f:
        raw = json.load(f)
    return {k: v for k, v in raw.items() if not k.startswith("_")}


@pytest.fixture(scope="session")
def ground_truth() -> dict:
    """Load ground_truth.json once for the entire test session."""
    return _load_ground_truth()


# ============================================================================
# Helpers
# ============================================================================


def _load_frames(alert_id: str) -> tuple[str, str, str, str] | None:
    """Return a 4-tuple of sorted frame file paths for an alert, or None."""
    alert_dir = FRONT_FRAMES_DIR / alert_id
    if not alert_dir.is_dir():
        return None
    files = sorted(f for f in alert_dir.iterdir() if f.name.startswith("frame_"))
    if len(files) < 4:
        return None
    f1, f2, f3, f4 = [str(f) for f in files[:4]]
    return (f1, f2, f3, f4)


def _skip_if_frames_absent(alert_id: str) -> None:
    """Skip the current test if this alert's frames are missing."""
    frames = _load_frames(alert_id)
    if frames is None:
        pytest.skip(f"alert {alert_id}: frame directory or insufficient frames")


# ============================================================================
# Per-alert parameterized tests
# ============================================================================

# Dynamically parameterize over every alert in ground_truth.json.
# If an alert's frame directory is missing from disk, the test is
# skipped at the parametrization/setup stage, NOT the whole suite.
_ALERT_IDS = list(_load_ground_truth().keys())


@pytest.mark.parametrize("alert_id", _ALERT_IDS)
class TestCrossDiffBboxes:
    """Test every alert in ground_truth.json.

    Each alert's frames are loaded and compute_cross_diff_bboxes returns
    (bbox_a, bbox_b). The result is compared against ground truth.
    If an alert's frames are absent from disk, the alert is skipped.
    """

    def test_bbox_type(self, ground_truth: dict, alert_id: str, caplog):
        """Verify bbox types match ground-truth expectations."""
        _skip_if_frames_absent(alert_id)
        gt = ground_truth[alert_id]
        expected_a = gt["bbox_a"]["expected_bbox"]
        expected_b = gt["bbox_b"]["expected_bbox"]

        frames = _load_frames(alert_id)
        assert frames is not None
        f1, f2, f3, f4 = frames
        with caplog.at_level(logging.DEBUG):
            bbox_a, bbox_b = compute_cross_diff_bboxes(f1, f2, f3, f4, cam_id="FRONT")

        # bbox_a type must match
        assert (bbox_a is None) == (expected_a is None), (
            f"{alert_id}: bbox_a type mismatch — "
            f"computed={bbox_a}, expected={expected_a}"
        )
        # bbox_b type must match
        assert (bbox_b is None) == (expected_b is None), (
            f"{alert_id}: bbox_b type mismatch — "
            f"computed={bbox_b}, expected={expected_b}"
        )

    def test_bbox_iou(self, ground_truth: dict, alert_id: str):
        """For non-None bboxes, IoU vs expected must exceed 0.50."""
        _skip_if_frames_absent(alert_id)
        gt = ground_truth[alert_id]
        expected_a = gt["bbox_a"]["expected_bbox"]
        expected_b = gt["bbox_b"]["expected_bbox"]

        frames = _load_frames(alert_id)
        assert frames is not None
        f1, f2, f3, f4 = frames
        bbox_a, bbox_b = compute_cross_diff_bboxes(f1, f2, f3, f4, cam_id="FRONT")

        if expected_a is not None:
            iou = _compute_iou(bbox_a, tuple(expected_a))
            assert iou > 0.50, (
                f"{alert_id} bbox_a IoU={iou:.3f} <= 0.50"
            )
        if expected_b is not None:
            iou = _compute_iou(bbox_b, tuple(expected_b))
            assert iou > 0.50, (
                f"{alert_id} bbox_b IoU={iou:.3f} <= 0.50"
            )

    def test_center_x_within_10pct(self, ground_truth: dict, alert_id: str):
        """bbox center x within 10% of expected bbox center x."""
        _skip_if_frames_absent(alert_id)
        gt = ground_truth[alert_id]
        expected_a = gt["bbox_a"]["expected_bbox"]
        expected_b = gt["bbox_b"]["expected_bbox"]

        frames = _load_frames(alert_id)
        assert frames is not None
        f1, f2, f3, f4 = frames
        bbox_a, bbox_b = compute_cross_diff_bboxes(f1, f2, f3, f4, cam_id="FRONT")

        if expected_a is not None:
            bbox_cx = bbox_a[0] + bbox_a[2] / 2.0
            expected_cx = expected_a[0] + expected_a[2] / 2.0
            tolerance = abs(expected_cx) * 0.10
            assert abs(bbox_cx - expected_cx) <= tolerance, (
                f"{alert_id} bbox_a center_x mismatch"
            )
        if expected_b is not None:
            bbox_cx = bbox_b[0] + bbox_b[2] / 2.0
            expected_cx = expected_b[0] + expected_b[2] / 2.0
            tolerance = abs(expected_cx) * 0.10
            assert abs(bbox_cx - expected_cx) <= tolerance, (
                f"{alert_id} bbox_b center_x mismatch"
            )

    def test_no_fallback_warning(self, ground_truth: dict, alert_id: str, caplog):
        """Alerts must NOT emit any fallback warning."""
        _skip_if_frames_absent(alert_id)

        frames = _load_frames(alert_id)
        assert frames is not None
        f1, f2, f3, f4 = frames
        with caplog.at_level(logging.WARNING):
            compute_cross_diff_bboxes(f1, f2, f3, f4, cam_id="FRONT")

        fallback_messages = [
            rec.message for rec in caplog.records if "fallback" in rec.message.lower()
        ]
        assert fallback_messages == [], (
            f"{alert_id}: unexpected fallback warnings: {fallback_messages}"
        )
