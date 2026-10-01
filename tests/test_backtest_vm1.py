"""E2E smoke test for infra.backtest_vm1_motion.run_backtest.

Runs one alert fixture through the backtest harness, verifies the return
dict shape, and asserts the output PNG exists and is non-trivial.

Does NOT require a live vision model -- the harness falls back to
{"class": "unsure", "bbox_a": null, "bbox_b": null} on HTTP error.
"""
import os
import sys

import dotenv

# Load env so infra modules pick up VISION_LLM_URL from .env.
dotenv.load_dotenv("infra/.env")
dotenv.load_dotenv(os.path.expanduser("~/.env"))

# Ensure the v2 repo root is on sys.path so infra imports resolve.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import infra.backtest_vm1_motion as m


def test_run_backtest_smoke():
    """Run one alert through the harness and assert output shape + PNG."""
    alert_id = "00382a70-7708-48f8-bcc4-0b9095192dc6"
    frame_dir = "data/frames/FRONT/00382a70-7708-48f8-bcc4-0b9095192dc6"

    result = m.run_backtest(
        alert_id,
        frame_dir=frame_dir,
        gate_bbox_a=None,
        gate_bbox_b=None,
        sleep_seconds=0.1,
    )

    # Assert dict keys.
    required_keys = {"alert_id", "vm1_class", "vm1_bbox_a", "vm1_bbox_b", "image_path"}
    assert required_keys.issubset(result.keys()), (
        f"Missing keys: {required_keys - result.keys()}"
    )

    # Assert PNG exists and is >1KB.
    img_path = result["image_path"]
    assert os.path.isfile(img_path), f"PNG not found at {img_path}"
    sz = os.path.getsize(img_path)
    assert sz > 1024, f"PNG too small ({sz} bytes) at {img_path}"
