"""test_yolo_sha256_verify — SHA256 model verification for US-110c.

Tests verify that:
- verify_model_sha256() returns 'official' for the known-good hash.
- verify_model_sha256() raises RuntimeError for an unknown hash.
- _download_model() raises RuntimeError on hash mismatch (monkeypatched).
- FARMSURV_YOLO_ALLOW_EXTRA_SHA env var extends pins.

All tests are network-isolated: the download test monkeypatches urllib.
"""

import hashlib
import os
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest


class TestVerifyModelSha256:
    """Tests for verify_model_sha256() — file-based hash matching."""

    def test_known_hash_matches(self, tmp_path: Path) -> None:
        """A model matching a pin returns the pin name."""
        import infra.quick_classifier as qc

        data = b"test model data for hash verification"
        dest = tmp_path / "yolov8n.onnx"
        dest.write_bytes(data)
        actual_hash = hashlib.sha256(data).hexdigest()

        original_pins = dict(qc._YOLO_SHA256_PINS)
        qc._YOLO_SHA256_PINS["test-alias"] = actual_hash
        try:
            result = qc.verify_model_sha256(dest)
            assert result == "test-alias"
        finally:
            qc._YOLO_SHA256_PINS.clear()
            qc._YOLO_SHA256_PINS.update(original_pins)

    def test_unknown_hash_raises(self, tmp_path: Path) -> None:
        """A model with no matching pin raises RuntimeError."""
        import infra.quick_classifier as qc

        dest = tmp_path / "yolov8n.onnx"
        dest.write_bytes(b"random bytes not matching any pin ever")

        original_pins = dict(qc._YOLO_SHA256_PINS)
        qc._YOLO_SHA256_PINS.clear()
        try:
            with pytest.raises(RuntimeError, match="SHA256 hash mismatch"):
                qc.verify_model_sha256(dest)
        finally:
            qc._YOLO_SHA256_PINS.clear()
            qc._YOLO_SHA256_PINS.update(original_pins)

    def test_official_pin_matches_real_hash(self) -> None:
        """The official pin in the dict matches the real SHA256."""
        import infra.quick_classifier as qc

        expected = qc._YOLO_SHA256_PINS["official"]
        assert expected == "b2bc52f40e8e1c532427d5bde3575a5d5b571b739fab2c6df443733ed1589cbd"

    def test_operator_export_pin_matches_real_hash(self) -> None:
        """The operator-export pin matches the known local model hash."""
        import infra.quick_classifier as qc

        expected = qc._YOLO_SHA256_PINS["operator-export"]
        assert expected == "b4ee8cd940e37cb9287b29845fb7dad18b021f578884c45c02758891f19e0b3f"


class TestDownloadModelHashCheck:
    """Tests for _download_model() — network-isolated via monkeypatch."""

    def test_wrong_hash_raises_runtime_error(self, tmp_path: Path) -> None:
        """Download returning wrong bytes -> RuntimeError with hash mismatch."""
        import infra.quick_classifier as qc

        dest = tmp_path / "yolov8n.onnx"

        fake_content = b"this is NOT the real model file"
        fake_hash = hashlib.sha256(fake_content).hexdigest()

        mock_response = mock.MagicMock()
        mock_response.read.side_effect = [fake_content, b""]
        mock_response.__enter__ = mock.MagicMock(return_value=mock_response)
        mock_response.__exit__ = mock.MagicMock(return_value=False)

        with mock.patch.object(
            qc.urllib.request, "urlopen", return_value=mock_response
        ):
            with mock.patch.object(
                qc.urllib.request, "Request", return_value=mock.MagicMock()
            ):
                with pytest.raises(RuntimeError) as exc_info:
                    qc._download_model("https://example.com/fake.onnx", dest)

                err_msg = str(exc_info.value)
                assert "SHA256 hash mismatch" in err_msg
                assert fake_hash in err_msg
                # The corrupt file should be deleted
                assert not dest.exists()

    def test_correct_hash_succeeds(self, tmp_path: Path) -> None:
        """Download with correct hash writes file successfully."""
        import infra.quick_classifier as qc

        dest = tmp_path / "yolov8n.onnx"
        expected_hash = qc._YOLO_SHA256_PINS["official"]

        class FakeHash:
            """A hash object that always returns the official digest."""

            def __init__(self, *args, **kwargs):  # noqa: ARG002
                self._digest = expected_hash

            def update(self, data):  # noqa: ARG002
                pass

            def hexdigest(self):
                return self._digest

            def copy(self):
                return self

        mock_response = mock.MagicMock()
        mock_response.read.side_effect = [b"fake bytes", b""]
        mock_response.__enter__ = mock.MagicMock(return_value=mock_response)
        mock_response.__exit__ = mock.MagicMock(return_value=False)

        with mock.patch.object(qc, "hashlib", mock.MagicMock(sha256=FakeHash)):
            with mock.patch.object(
                qc.urllib.request, "urlopen", return_value=mock_response
            ):
                with mock.patch.object(
                    qc.urllib.request, "Request", return_value=mock.MagicMock()
                ):
                    qc._download_model("https://example.com/fake.onnx", dest)
                    assert dest.exists()


class TestExtraShaEnvVar:
    """Tests for FARMSURV_YOLO_ALLOW_EXTRA_SHA environment variable."""

    def test_invalid_extra_sha_is_rejected(self) -> None:
        """A non-hex or wrong-length env var raises RuntimeError on import."""
        test_hash = "not_valid_hex!"
        env = os.environ.copy()
        env["FARMSURV_YOLO_ALLOW_EXTRA_SHA"] = test_hash
        env["MOTION_GATE_NIGHT_SUPPRESS_ENABLED"] = "1"

        result = subprocess.run(
            [sys.executable, "-c", "import infra.quick_classifier"],
            env=env,
            capture_output=True,
            text=True,
            cwd=str(Path(__file__).resolve().parent.parent),
        )
        assert result.returncode != 0
        assert "must be 64 lowercase hex chars" in result.stderr
