"""test_cv2_strip — venv-isolation verifier (post-strip sanity).

Verifies that opencv-python-headless has been fully stripped from the
runtime environment and that no cv2 fallback paths or residue remain.

Acceptance criteria:
  AC1: pytest tests/test_cv2_strip.py -q --no-header → 'passed' with N ≥ 4
  AC2: cv2 importlib.util.find_spec returns None (absent)
  AC3: av present, cv2 absent, PIL/numpy/onnxruntime/flask all present
  AC4: PersistentRTSPReader importable without cv2
  RESIDUE: no cv2 imports anywhere in the runtime tree

Git: commit on main with US-038f in message.
"""

from __future__ import annotations

import importlib
import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# AC2: cv2 must be absent from the venv
# ---------------------------------------------------------------------------


class TestCv2Absent:
    """Verify opencv-python-headless is completely gone."""

    def test_cv2_import_fails(self):
        """import cv2 must raise ModuleNotFoundError."""
        # Ensure cv2 is not already imported in this process.
        sys.modules.pop("cv2", None)
        for key in list(sys.modules.keys()):
            if key.startswith("cv2"):
                sys.modules.pop(key, None)
        with pytest.raises(ModuleNotFoundError):
            import cv2  # noqa: F401

    def test_cv2_find_spec_none(self):
        """importlib.util.find_spec('cv2') must return None."""
        # Clear any prior import.
        for key in list(sys.modules.keys()):
            if key.startswith("cv2"):
                sys.modules.pop(key, None)
        assert importlib.util.find_spec("cv2") is None, (
            "cv2 should be absent from this venv"
        )


# ---------------------------------------------------------------------------
# AC3: dependency presence check
# ---------------------------------------------------------------------------


class TestDependencyPresence:
    """Verify that required deps are present and cv2 is absent."""

    def test_av_present(self):
        """av (PyAV) must be importable."""
        # av.__spec__ may not be set on some builds; use __import__ as fallback.
        try:
            assert importlib.util.find_spec("av") is not None
        except ValueError:
            _av = importlib.import_module("av")
            assert _av is not None

    def test_cv2_absent(self):
        """cv2 must be absent."""
        for key in list(sys.modules.keys()):
            if key.startswith("cv2"):
                sys.modules.pop(key, None)
        assert importlib.util.find_spec("cv2") is None

    def test_pil_present(self):
        """PIL (Pillow) must be present."""
        assert importlib.util.find_spec("PIL") is not None

    def test_numpy_present(self):
        """numpy must be present."""
        assert importlib.util.find_spec("numpy") is not None

    def test_onnxruntime_present(self):
        """onnxruntime must be present."""
        assert importlib.util.find_spec("onnxruntime") is not None

    def test_flask_present(self):
        """flask must be present."""
        assert importlib.util.find_spec("flask") is not None


# ---------------------------------------------------------------------------
# AC4: PersistentRTSPReader importable (PyAV still resolves)
# ---------------------------------------------------------------------------


class TestPyAvBinding:
    """Verify PyAV-based imports still work after cv2 strip."""

    def test_persistent_rtsp_reader_import(self):
        """from infra.frame_capture import PersistentRTSPReader must succeed."""
        from infra.frame_capture import PersistentRTSPReader  # noqa: F401

        # Basic sanity: it's a class, not a module shadow.
        assert isinstance(
            PersistentRTSPReader, type
        ), "PersistentRTSPReader should be a class"


# ---------------------------------------------------------------------------
# RESIDUE: no cv2 references in the runtime tree
# ---------------------------------------------------------------------------


class TestNoCv2Residue:
    """Verify zero cv2 residue in the runtime production tree."""

    @pytest.fixture(scope="module")
    def repo_root(self) -> Path:
        return Path(__file__).resolve().parent.parent

    def test_no_cv2_in_runtime_dirs(self, repo_root: Path):
        """grep for cv2 patterns across runtime dirs must return zero matches.

        git grep exit codes:
          0 = matches found (FAIL), 1 = no matches (PASS), >1 = error.
        """
        result = subprocess.run(
            [
                "git",
                "-C",
                str(repo_root),
                "grep",
                "-rnE",
                r"import cv2|from cv2|cv2\.|try:.*import cv2|except ImportError.*cv2",
                "--",
                "infra/",
                "listener/",
                "telegram_formatter/",
                "vehicle_matcher/",
            ],
            capture_output=True,
            text=True,
        )
        # exit 0 = matches (bad), exit 1 = no matches (good), >1 = git error
        assert result.returncode == 1, (
            f"Found cv2 residue in runtime tree:\n{result.stdout}"
        )


# NOTE: GIT-1 (working tree clean) is checked via shell before commit,
# not inside pytest. See AC spec:
#   git status --porcelain | wc -l → 0
# It is a pre-commit gate, not a runtime assertion.
