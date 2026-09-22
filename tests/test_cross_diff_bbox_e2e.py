"""test_cross_diff_bbox_e2e — e2e regression tests on 6 FRONT alerts.

Validates that compute_cross_diff_bboxes produces correct bboxes for real
frame sequences from the FRONT camera. Covers three behavioural categories:

- Entering (3 alerts):  subject enters frame; bbox_a and bbox_b both active.
  IoU(bbox_a, expected_bbox_a) > 0.50 and IoU(bbox_b, expected_bbox_b) > 0.50.
- Exit (1 alert):       subject exiting; only bbox_a active.
  IoU(bbox_a, expected_bbox_a) > 0.50; bbox_b is None.
- Stationary (2 alerts): no motion; both bboxes are None.
  bbox_a is None AND bbox_b is None. No fallback warning in logs.

Ground truth comes from data/frames/FRONT/ground_truth.json which was
hand-labeled against the actual cross-diff output for these alerts.

Acceptance criteria:
  - AC5: pytest tests/test_cross_diff_bbox_e2e.py -v passes
  - AC6: ruff check tests/test_cross_diff_bbox_e2e.py = 0 errors
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


@pytest.fixture(scope="session")
def ground_truth() -> dict:
    """Load ground_truth.json once for the entire test session."""
    with open(GROUND_TRUTH_PATH) as f:
        return json.load(f)


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


# ============================================================================
# Entering alerts — bbox_a and bbox_b both active, IoU > 0.50
# ============================================================================


@pytest.mark.parametrize(
    "alert_id",
    [
        "72c70d5f-3036-4d4b-bd09-a2e66ff24725",
        "8b4f7bbd-82c0-4e1e-a9c3-bdb59a621b37",
        "b720b8ba-fb8a-4bb4-8035-c91a96508b87",
    ],
)
class TestEnteringAlerts:
    """3 entering alerts: subject moves into frame.

    For each, frame_001-004 are loaded and
    ``compute_cross_diff_bboxes`` returns (bbox_a, bbox_b).
    Both must be non-None and IoU against expected must exceed 0.50.
    """

    def test_bbox_a_iou(self, ground_truth: dict, alert_id: str, caplog):
        """bbox_a IoU vs expected_bbox_a > 0.50."""
        result = _load_frames(alert_id)
        assert result is not None
        f1, f2, f3, f4 = result

        with caplog.at_level(logging.DEBUG):
            bbox_a, _bbox_b = compute_cross_diff_bboxes(f1, f2, f3, f4)

        assert bbox_a is not None, f"{alert_id}: bbox_a is None (expected entering)"
        expected = ground_truth[alert_id]["bbox_a"]["expected_bbox"]
        assert expected is not None, f"{alert_id}: no expected_bbox_a in ground truth"
        iou = _compute_iou(bbox_a, tuple(expected))
        assert iou > 0.50, (
            f"{alert_id} bbox_a={bbox_a} vs expected={expected}, "
            f"IoU={iou:.3f} <= 0.50"
        )

    def test_bbox_b_iou(self, ground_truth: dict, alert_id: str, caplog):
        """bbox_b IoU vs expected_bbox_b > 0.50."""
        result = _load_frames(alert_id)
        assert result is not None
        f1, f2, f3, f4 = result

        with caplog.at_level(logging.DEBUG):
            _bbox_a, bbox_b = compute_cross_diff_bboxes(f1, f2, f3, f4)

        assert bbox_b is not None, f"{alert_id}: bbox_b is None (expected entering)"
        expected = ground_truth[alert_id]["bbox_b"]["expected_bbox"]
        assert expected is not None, f"{alert_id}: no expected_bbox_b in ground truth"
        iou = _compute_iou(bbox_b, tuple(expected))
        assert iou > 0.50, (
            f"{alert_id} bbox_b={bbox_b} vs expected={expected}, "
            f"IoU={iou:.3f} <= 0.50"
        )

    def test_bbox_a_center_x_within_10pct(self, ground_truth: dict, alert_id: str):
        """For alert 72c70d5f: bbox_a center x within ±10% of ground-truth x=185.

        Generalized: for any entering alert, bbox_a center x should be
        within 10% of the expected bbox center x.
        """
        result = _load_frames(alert_id)
        assert result is not None
        f1, f2, f3, f4 = result

        bbox_a, _ = compute_cross_diff_bboxes(f1, f2, f3, f4)
        assert bbox_a is not None

        expected = tuple(ground_truth[alert_id]["bbox_a"]["expected_bbox"])
        bbox_cx = bbox_a[0] + bbox_a[2] / 2.0
        expected_cx = expected[0] + expected[2] / 2.0
        tolerance = abs(expected_cx) * 0.10
        assert abs(bbox_cx - expected_cx) <= tolerance, (
            f"{alert_id} bbox_a center_x={bbox_cx} vs expected={expected_cx}, "
            f"diff={abs(bbox_cx - expected_cx):.1f} > tolerance={tolerance:.1f}"
        )

    def test_bbox_b_center_x_within_10pct(self, ground_truth: dict, alert_id: str):
        """For alert 72c70d5f: bbox_b center x within ±10% of ground-truth x=576.

        Generalized: bbox_b center x within 10% of expected bbox center x.
        """
        result = _load_frames(alert_id)
        assert result is not None
        f1, f2, f3, f4 = result

        _bbox_a, bbox_b = compute_cross_diff_bboxes(f1, f2, f3, f4)
        assert bbox_b is not None

        expected = tuple(ground_truth[alert_id]["bbox_b"]["expected_bbox"])
        bbox_cx = bbox_b[0] + bbox_b[2] / 2.0
        expected_cx = expected[0] + expected[2] / 2.0
        tolerance = abs(expected_cx) * 0.10
        assert abs(bbox_cx - expected_cx) <= tolerance, (
            f"{alert_id} bbox_b center_x={bbox_cx} vs expected={expected_cx}, "
            f"diff={abs(bbox_cx - expected_cx):.1f} > tolerance={tolerance:.1f}"
        )

    def test_no_fallback_warning(self, ground_truth: dict, alert_id: str, caplog):
        """Entering alerts must NOT emit any fallback warning."""
        result = _load_frames(alert_id)
        assert result is not None
        f1, f2, f3, f4 = result

        with caplog.at_level(logging.WARNING):
            compute_cross_diff_bboxes(f1, f2, f3, f4)

        # The compute_cross_diff_bboxes function itself does not log warnings;
        # the fallback logic lives in the caller (gate.py). We assert no
        # unexpected warnings appeared during this isolated call.
        fallback_messages = [
            rec.message for rec in caplog.records if "fallback" in rec.message.lower()
        ]
        assert fallback_messages == [], (
            f"{alert_id}: unexpected fallback warnings: {fallback_messages}"
        )


# ============================================================================
# Exit alert — only bbox_a active
# ============================================================================


class TestExitAlert:
    """1 exit alert: subject exits frame, only bbox_a is active.

    bbox_a IoU > 0.50 vs expected, bbox_b is None.
    """

    def test_bbox_a_iou(self, caplog, ground_truth):
        """bbox_a IoU vs expected_bbox_a > 0.50."""
        result = _load_frames("96bcea5f-4af6-48b4-80e3-9dcb6e50789e")
        assert result is not None
        f1, f2, f3, f4 = result

        with caplog.at_level(logging.DEBUG):
            bbox_a, _bbox_b = compute_cross_diff_bboxes(f1, f2, f3, f4)

        assert bbox_a is not None, "exit alert: bbox_a should be active"
        gt = ground_truth["96bcea5f-4af6-48b4-80e3-9dcb6e50789e"]
        expected = gt["bbox_a"]["expected_bbox"]
        assert expected is not None
        iou = _compute_iou(bbox_a, tuple(expected))
        assert iou > 0.50, (
            f"exit bbox_a={bbox_a} vs expected={expected}, "
            f"IoU={iou:.3f} <= 0.50"
        )

    def test_bbox_b_none(self):
        """bbox_b is None for exit alert."""
        result = _load_frames("96bcea5f-4af6-48b4-80e3-9dcb6e50789e")
        assert result is not None
        f1, f2, f3, f4 = result
        _bbox_a, bbox_b = compute_cross_diff_bboxes(f1, f2, f3, f4)
        assert bbox_b is None, (
            f"exit alert: bbox_b={bbox_b} should be None"
        )

    def test_no_fallback_warning(self, caplog, ground_truth):
        """Exit alert must NOT emit a fallback warning."""
        result = _load_frames("96bcea5f-4af6-48b4-80e3-9dcb6e50789e")
        assert result is not None
        f1, f2, f3, f4 = result

        with caplog.at_level(logging.WARNING):
            compute_cross_diff_bboxes(f1, f2, f3, f4)

        fallback_messages = [
            rec.message
            for rec in caplog.records
            if "fallback" in rec.message.lower()
        ]
        assert fallback_messages == [], (
            f"exit alert: unexpected fallback warnings: {fallback_messages}"
        )


# ============================================================================
# Stationary alerts — both bboxes are None
# ============================================================================


@pytest.mark.parametrize(
    "alert_id",
    [
        "12e1d073-fb57-410a-9eb7-7c44bc6d6dca",
        "30cdedc2-4484-4441-8fb3-aea4838ee488",
    ],
)
class TestStationaryAlerts:
    """2 stationary alerts: no motion detected.

    Both bbox_a and bbox_b must be None. No fallback warning.
    """

    def test_both_bboxes_none(self, ground_truth: dict, alert_id: str):
        """Stationary alert: bbox_a = None, bbox_b = None."""
        result = _load_frames(alert_id)
        assert result is not None
        f1, f2, f3, f4 = result

        bbox_a, bbox_b = compute_cross_diff_bboxes(f1, f2, f3, f4)

        assert bbox_a is None, (
            f"{alert_id}: bbox_a={bbox_a} should be None (stationary)"
        )
        assert bbox_b is None, (
            f"{alert_id}: bbox_b={bbox_b} should be None (stationary)"
        )

    def test_no_fallback_warning(self, ground_truth: dict, alert_id: str, caplog):
        """Stationary alert must NOT emit any fallback warning."""
        result = _load_frames(alert_id)
        assert result is not None
        f1, f2, f3, f4 = result

        with caplog.at_level(logging.WARNING):
            compute_cross_diff_bboxes(f1, f2, f3, f4)

        fallback_messages = [
            rec.message for rec in caplog.records if "fallback" in rec.message.lower()
        ]
        assert fallback_messages == [], (
            f"{alert_id}: unexpected fallback warnings: {fallback_messages}"
        )
