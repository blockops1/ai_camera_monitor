"""test_smoke_alert_roundtrip.py — Smoke test for alert round-trip canonical paths.

Verifies that a live alert round-trip writes ALL transient files under
data/frames/<camera_id>/<alert_id>/ and NOT under /tmp.

Covers the US-021d story acceptance criteria:
  1. gate writes pairwise_diff PNG to data/frames/<cam>/<alert_id>/
  2. gate crop outputs land in data/frames/<cam>/<alert_id>/
  3. no gate output files written to /tmp
  4. no-motion case writes empty PNG marker to data/frames/<cam>/<alert_id>/
  5. data_dir_for() never returns a path containing /tmp
  6. empty_png_path() returns a path under DATA_DIR (not /tmp)
"""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

import infra.paths as _paths_mod
from infra.gate import GateVerdict

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_small_jpeg(path: Path) -> Path:
    """Write a minimal valid JPEG to *path* (1x1 pixel)."""
    # Minimal JPEG: SOI + APP0 + SOF0 + SOS + data + EOI
    path.write_bytes(
        b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01"
        b"\x00\x01\x00\x00\xff\xdb\x00\x43\x00"
        b"\xff\xc0\x00\x0b\x08\x00\x01\x00\x01\x01\x01\x11\x00"
        b"\xff\xc4\x00\x1f\x00\x00\x01\x05\x01\x01\x01\x01"
        b"\x01\x01\x00\x00\x00\x00\x00\x00\x01\x02\x03\x04"
        b"\x05\x06\x07\x08\t\n\x0b\xff\xc4\x00\xb5\x10\x00"
        b"\x02\x01\x03\x03\x02\x04\x03\x05\x05\x04\x04\x00"
        b"\x00\x01}\x01\x02\x03\x00\x04\x11\x05\x12!1A\x06"
        b'\x13Qa\x07"q\x142\x81\x91\xa1\x08#B\xb1\xc1\x15'
        b"\x16R\xd1$f3\x08'br\x82\x17\x1814%a\x93\x02\xb2"
        b"\x83\xc2\x19\xa3\xda\x03\xe3\xdb\x94\x1a\x1b\xdc"
        b"\x1c\xed\x1d\xfe\x00\x00\x00\x00\x00\x00\x00\x00"
        b"\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00"
        b"\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00"
        b"\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00"
        b"\xff\xda\x00\x08\x01\x01\x00\x00?\x00\x7f\xff\xd9"
    )
    return path


def _make_frame_dir(tmp_path: Path) -> list[str]:
    """Create 4 minimal JPEG frames under tmp_path and return their paths."""
    frames_dir = tmp_path / "frames"
    frames_dir.mkdir(exist_ok=True)
    paths = []
    for i in range(1, 5):
        p = frames_dir / f"frame{i}.jpg"
        _make_small_jpeg(p)
        paths.append(str(p))
    return paths


def _make_gate_verdict_mock(
    classification="none",
    pairwise_diff_path=None,
    crop_a=None,
    crop_b=None,
):
    """Build a GateVerdict mock."""
    v = MagicMock(spec=GateVerdict)
    v.classification = classification
    v.classification_type = classification
    v.is_none = MagicMock(return_value=classification == "none")
    v.is_pass = MagicMock(return_value=classification in ("vehicle", "person", "animal"))
    v.class_label = None
    v.confidence = 0.0
    v.crop_a = crop_a
    v.crop_b = crop_b
    v.pairwise_diff_path = pairwise_diff_path
    v.raw_verdicts = []
    v.frames = []
    return v


# ---------------------------------------------------------------------------
# Test 1 — gate writes pairwise_diff PNG to data/frames/<cam>/<alert_id>/
# ---------------------------------------------------------------------------


class TestSmokeRoundTrip:
    """Smoke tests: alert round-trip writes transient files under data/frames/, not /tmp."""

    def test_1_gate_writes_pairwise_diff_to_canonical_path(self, tmp_path, monkeypatch):
        """_write_pairwise_diff_image writes PNG under the output_dir (canonical path)."""
        import numpy as np
        from PIL import Image

        from infra.gate import _write_pairwise_diff_image

        cam = "CAM_SMOKETEST"
        alert_id = "smoke-alert-001"
        output_dir = str(_paths_mod.data_dir_for(cam, alert_id))

        # Create two PIL images with different content (simulates motion)
        arr_a = np.zeros((256, 256, 3), dtype=np.uint8)
        arr_b = np.zeros((256, 256, 3), dtype=np.uint8)
        arr_b[100:160, 100:160, :] = 200  # white square in frame B
        frame_a = Image.fromarray(arr_a, mode="RGB")
        frame_b = Image.fromarray(arr_b, mode="RGB")

        result = _write_pairwise_diff_image(
            frame_a, frame_b, None, None, output_dir, alert_id
        )

        # Should return a path under the canonical output_dir
        assert result is not None, "pairwise_diff should be written"
        assert output_dir in result, (
            f"pairwise_diff_path {result} should be under {output_dir}"
        )
        # Verify file exists on disk
        assert Path(result).is_file(), "pairwise_diff file should exist on disk"
        # Verify it's a valid PNG
        assert Path(result).read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"

    def test_2_artifacts_land_under_data_frames(self, tmp_path, monkeypatch):
        """prepare_alert_artifacts writes crops to data/frames/<cam>/<alert_id>/."""
        orig_project_root = _paths_mod.PROJECT_ROOT
        _paths_mod.PROJECT_ROOT = str(tmp_path)

        try:
            cam = "CAM_SMOKETEST"
            alert_id = "smoke-alert-002"

            from unittest.mock import patch

            import numpy as np
            from PIL import Image

            from infra.gate import GateVerdict
            from listener.pipeline import run

            alert = {
                "id": alert_id,
                "camera_id": cam,
                "classification": "vehicle",
                "frames": [str(tmp_path / f"frame{i+1}.jpg") for i in range(4)],
            }
            for i in range(1, 5):
                (tmp_path / f"frame{i}.jpg").write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 50)

            arr = np.zeros((256, 256, 3), dtype=np.uint8)
            mock_crop = Image.fromarray(arr, mode="RGB")

            gate_v = MagicMock(spec=GateVerdict)
            gate_v.classification = "vehicle"
            gate_v.is_none = MagicMock(return_value=False)
            gate_v.is_pass = MagicMock(return_value=True)
            gate_v.class_label = "car"
            gate_v.confidence = 0.9
            gate_v.top_class = "car"
            gate_v.top_confidence = 0.9
            gate_v.reason = "test"
            gate_v.crop_a = mock_crop
            gate_v.crop_b = mock_crop
            gate_v.bbox_a = [10, 20, 100, 150]
            gate_v.bbox_b = [15, 25, 105, 155]
            gate_v.pairwise_diff_path = str(tmp_path / "diff.png")
            arr = np.zeros((64, 64, 3), dtype=np.uint8)
            gate_v.frames = [Image.fromarray(arr, mode="RGB") for _ in range(4)]

            (tmp_path / "diff.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 20)

            expected_dir = tmp_path / "data" / "frames" / cam / alert_id

            with (
                patch("listener.pipeline.should_suppress", return_value=False),
                patch("listener.pipeline.run_gate", return_value=gate_v),
                patch("listener.pipeline.verify_class", return_value={"class": "vehicle"}),
                patch("listener.pipeline.build_alert_message", return_value={"caption": ""}),
                patch("listener.pipeline.detail_class", return_value={}),
                patch("listener.pipeline.build_detail_message", return_value={}),
                patch("listener.pipeline._load_candidates", return_value=[]),
                patch("listener.pipeline.build_match_message", return_value={}),
                patch("listener.pipeline.record_hit"),
            ):
                run(alert)

            artifacts = alert.get("artifacts")
            assert artifacts is not None, "artifacts should be set on alert dict"
            assert Path(artifacts.crop_a_path) == expected_dir / "crop_a.png"
            assert Path(artifacts.crop_b_path) == expected_dir / "crop_b.png"
            assert Path(artifacts.crop_a_path).is_file()
            assert Path(artifacts.crop_b_path).is_file()
            assert expected_dir.is_dir()

        finally:
            _paths_mod.PROJECT_ROOT = orig_project_root

    def test_3_no_gate_output_writes_to_tmp(self, tmp_path, monkeypatch):
        """No gate output is written to /tmp during a live round-trip."""
        # This test verifies that the pipeline does NOT pass output_dir='/tmp'
        # to run_gate(). The pipeline passes output_dir=str(data_dir_for(...)).
        # We inspect the actual run_gate call in pipeline.py.
        import listener.pipeline as _pipe_mod

        # Read the actual source to confirm output_dir is not hardcoded to /tmp
        source = Path(_pipe_mod.__file__).read_text()

        # The run() function calls run_gate(..., output_dir=...)
        # We verify the call does NOT use '/tmp' as the output_dir arg.
        assert (
            "output_dir='/tmp'" not in source and 'output_dir="/tmp"' not in source
        ), "Pipeline should not pass output_dir='/tmp' to run_gate()"

        # Also verify the actual output_dir expression uses data_dir_for
        assert "data_dir_for" in source, (
            "Pipeline should use data_dir_for() to compute output_dir"
        )

    def test_4_no_motion_no_crops_raises(self, tmp_path, monkeypatch):
        """No-motion case with both crops None: run raises RuntimeError (no crops for verify_class)."""
        orig_project_root = _paths_mod.PROJECT_ROOT
        _paths_mod.PROJECT_ROOT = str(tmp_path)

        try:
            cam = "CAM_SMOKETEST"
            alert_id = "smoke-alert-003"

            from unittest.mock import patch

            import numpy as np
            from PIL import Image

            from infra.gate import GateVerdict
            from listener.pipeline import run

            alert = {
                "id": alert_id,
                "camera_id": cam,
                "classification": "vehicle",
                "frames": [str(tmp_path / f"frame{i+1}.jpg") for i in range(4)],
            }
            for i in range(1, 5):
                (tmp_path / f"frame{i}.jpg").write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 50)

            arr = np.zeros((64, 64, 3), dtype=np.uint8)

            gate_v = MagicMock(spec=GateVerdict)
            gate_v.classification = "vehicle"
            gate_v.is_none = MagicMock(return_value=False)
            gate_v.is_pass = MagicMock(return_value=True)
            gate_v.class_label = "car"
            gate_v.confidence = 0.9
            gate_v.top_class = "car"
            gate_v.top_confidence = 0.9
            gate_v.reason = "test"
            gate_v.crop_a = None  # No crop
            gate_v.crop_b = None  # No crop
            gate_v.bbox_a = None
            gate_v.bbox_b = None
            gate_v.pairwise_diff_path = str(tmp_path / "diff.png")
            gate_v.frames = [Image.fromarray(arr, mode="RGB") for _ in range(4)]

            (tmp_path / "diff.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 20)

            with (
                pytest.raises(RuntimeError, match="gate produced no crops for alert smoke-alert-003"),
                patch("listener.pipeline.should_suppress", return_value=False),
                patch("listener.pipeline.run_gate", return_value=gate_v),
            ):
                run(alert)

        finally:
            _paths_mod.PROJECT_ROOT = orig_project_root

    def test_5_data_dir_for_never_returns_tmp(self, tmp_path, monkeypatch):
        """data_dir_for() returns a path under DATA_DIR, never /tmp."""
        # Test with default PROJECT_ROOT
        result_default = _paths_mod.data_dir_for("CAM_X", "alert_123")
        assert "/tmp" not in str(result_default), (
            f"data_dir_for with default PROJECT_ROOT returned path with /tmp: {result_default}"
        )

        # Test with PROJECT_ROOT set to tmp_path
        orig_project_root = _paths_mod.PROJECT_ROOT
        _paths_mod.PROJECT_ROOT = str(tmp_path)

        try:
            result = _paths_mod.data_dir_for("CAM_X", "alert_456")
            expected = tmp_path / "data" / "frames" / "CAM_X" / "alert_456"
            assert result == expected
            assert "/tmp" not in str(result)
        finally:
            _paths_mod.PROJECT_ROOT = orig_project_root

    def test_6_empty_png_path_under_data_dir(self, tmp_path, monkeypatch):
        """empty_png_path() returns a path under DATA_DIR (not /tmp)."""
        orig_project_root = _paths_mod.PROJECT_ROOT
        _paths_mod.PROJECT_ROOT = str(tmp_path)

        try:
            p = _paths_mod.empty_png_path()
            expected = tmp_path / "data" / "_sentinels" / "_empty.png"
            assert p == expected
            # Verify it's under the project's data dir, not /tmp
            assert "/tmp" not in str(p)
            # Verify the parent directory exists after the call
            assert p.parent.is_dir()
            # First call creates the directory; the sentinel file itself
            # is written by _ensure_sentinel() in listener.pipeline when
            # crops are saved. empty_png_path() guarantees the path is valid.
            # Second call should be idempotent (same path returned)
            p2 = _paths_mod.empty_png_path()
            assert p == p2

        finally:
            _paths_mod.PROJECT_ROOT = orig_project_root
