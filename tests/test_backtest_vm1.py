"""E2E smoke tests for scripts.backtests.backtest_vm1_motion.run_backtest.

Runs each ground-truth alert fixture through the backtest harness, verifies
the return dict shape, and asserts the output PNG exists and is non-trivial.

Does NOT require a live vision model -- the harness falls back to
{"class": "unsure", "bbox_a": null, "bbox_b": null} on HTTP error.

Acceptance criteria:
  - AC1: pytest tests/test_backtest_vm1.py -v passes
  - AC2: ruff check tests/test_backtest_vm1.py = 0 errors
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import dotenv
import pytest

# Load env so infra modules pick up VISION_LLM_URL from .env.
dotenv.load_dotenv("infra/.env")
dotenv.load_dotenv(os.path.expanduser("~/.env"))

# Ensure the v2 repo root is on sys.path so infra imports resolve.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.backtests.backtest_vm1_motion import run_backtest

# Directory containing FRONT alert frame data.
FRONT_FRAMES_DIR = Path(__file__).resolve().parents[1] / "data" / "frames" / "FRONT"


def _load_ground_truth() -> dict:
    """Load ground_truth.json, returning only alert entries."""
    gt_path = FRONT_FRAMES_DIR / "ground_truth.json"
    with open(gt_path) as f:
        raw = json.load(f)
    return {k: v for k, v in raw.items() if not k.startswith("_")}


@pytest.fixture(scope="session")
def ground_truth() -> dict:
    """Load ground_truth.json once for the entire test session."""
    return _load_ground_truth()


# ============================================================================
# Helpers
# ============================================================================


def _skip_if_frames_absent(alert_id: str) -> None:
    """Skip the current test if this alert's frames are missing."""
    alert_dir = FRONT_FRAMES_DIR / alert_id
    if not alert_dir.is_dir():
        pytest.skip(f"alert {alert_id}: frame directory absent")
    files = sorted(f for f in alert_dir.iterdir() if f.name.startswith("frame_"))
    if len(files) < 4:
        pytest.skip(f"alert {alert_id}: fewer than 4 frames ({len(files)})")


# ============================================================================
# Tests
# ============================================================================

# Dynamically parameterize over every alert in ground_truth.json.
# If an alert's frame directory is missing from disk, the test is
# skipped at the parametrization/setup stage, NOT the whole suite.
_ALERT_IDS = list(_load_ground_truth().keys())


@pytest.mark.parametrize("alert_id", _ALERT_IDS)
class TestBacktestVM1Motion:
    """Run each ground-truth alert through the backtest harness."""

    def test_run_backtest_smoke(self, ground_truth: dict, alert_id: str):
        """Run one alert through the harness and assert output shape + PNG."""
        _skip_if_frames_absent(alert_id)
        frame_dir = str(FRONT_FRAMES_DIR / alert_id)

        result = run_backtest(
            alert_id,
            frame_dir=frame_dir,
            gate_bbox_a=None,
            gate_bbox_b=None,
            sleep_seconds=0.1,
        )

        # Assert dict keys.
        required_keys = {
            "alert_id", "vm1_class", "vm1_bbox_a", "vm1_bbox_b", "image_path"
        }
        assert required_keys.issubset(result.keys()), (
            f"Missing keys: {required_keys - result.keys()}"
        )

        # Assert PNG exists and is >1KB.
        img_path = result["image_path"]
        assert os.path.isfile(img_path), f"PNG not found at {img_path}"
        sz = os.path.getsize(img_path)
        assert sz > 1024, f"PNG too small ({sz} bytes) at {img_path}"
