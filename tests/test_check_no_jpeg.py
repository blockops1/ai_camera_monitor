"""test_check_no_jpeg.py -- Regression scanner for JPEG references.

Verifies that scripts/check_no_jpeg.py produces zero matches on the
current v2 production tree (after US-019f/g/h).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

# Import the scanner module so we can call its functions directly.
# The scanner lives one directory up from tests/.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from check_no_jpeg import _JPEG_PATTERNS, _KNOWN_FP, _scan_file


def _make_tmp_py(content: str, repo_root: Path) -> Path:
    """Write content to a temp .py file under repo_root and return the path."""
    tmp = repo_root / "_tmp_jpeg_test.py"
    tmp.write_text(content)
    return tmp


@pytest.fixture(scope="module")
def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


class TestScannerPatterns:
    """Verify the scanner patterns catch actual JPEG references."""

    def test_format_jpeg_pattern(self):
        """format='JPEG' should be flagged."""
        for pat, _ in _JPEG_PATTERNS:
            if "JPEG format" in _:
                assert pat.search("format='JPEG'")
                assert pat.search('format="JPEG"')
                assert not pat.search("format='PNG'")

    def test_file_extension_pattern(self):
        """.jpg / .jpeg string should be flagged."""
        for pat, _ in _JPEG_PATTERNS:
            if "JPEG file" in _:
                assert pat.search("'file.jpg'")
                assert pat.search(' "image.jpeg" ')
                assert not pat.search("'file.png'")

    def test_tiny_jpeg_pattern(self):
        """_tiny_jpeg() call should be flagged."""
        for pat, _ in _JPEG_PATTERNS:
            if "JPEG blob" in _:
                assert pat.search("_tiny_jpeg(data)")
                assert not pat.search("tiny_jpg(data)")


class TestScannerOnProductionTree:
    """Verify zero matches on the v2 production tree."""

    def test_zero_matches(self, repo_root: Path):
        """Scanner must produce zero findings on the clean production tree."""
        scan_dirs = ["infra", "listener", "vehicle_position", "telegram_formatter"]
        total = 0
        for d in scan_dirs:
            dir_path = repo_root / d
            if not dir_path.is_dir():
                continue
            for fp in sorted(dir_path.rglob("*.py")):
                total += 1
                findings = _scan_file(fp, repo_root)
                assert findings == [], f"Unexpected finding in {fp.relative_to(repo_root)}: {findings}"
        assert total > 0, "Expected at least one .py file in production dirs"

    def test_known_fp_excluded(self, repo_root: Path):
        """Known false-positive lines must be excluded from the JPEG scanner.

        Updated for v0.6.3 (2026-09-18): _sniff_mime moved to detect_mime_type(),
        line numbers shifted to (40, 51).
        """
        assert _KNOWN_FP == {
            ("infra/vision_analyzer.py", 111),  # mime fallback, not JPEG producer
            ("telegram_formatter/codec.py", 93),  # V2-030 JPEG exception (codec encodes bytes)
            ("telegram_formatter/codec.py", 94),  # V2-030 JPEG exception (codec docstring)
            ("telegram_formatter/dispatcher.py", 40),  # detect_mime_type docstring mentions .jpg
            ("telegram_formatter/dispatcher.py", 51),  # detect_mime_type returns image/jpeg, .jpg
        }

    def test_main_exits_zero(self, repo_root: Path):
        """CLI invocation must exit 0 on the clean tree."""
        result = subprocess.run(
            [sys.executable, "scripts/check_no_jpeg.py"],
            cwd=str(repo_root),
            check=False,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"Scanner exited non-zero: {result.stdout}\n{result.stderr}"


class TestScannerDetectsLeak:
    """Verify the scanner flags synthetic JPEG leaks."""

    def test_catches_format_jpeg(self, repo_root: Path):
        tmp = _make_tmp_py("x = format='JPEG'", repo_root)
        try:
            findings = _scan_file(tmp, repo_root)
            assert len(findings) == 1
            assert "JPEG format argument" in findings[0]
        finally:
            tmp.unlink()

    def test_catches_jpg_extension(self, repo_root: Path):
        tmp = _make_tmp_py('path = "crop.jpg"', repo_root)
        try:
            findings = _scan_file(tmp, repo_root)
            assert len(findings) == 1
            assert "JPEG file extension" in findings[0]
        finally:
            tmp.unlink()

    def test_catches_tiny_jpeg(self, repo_root: Path):
        tmp = _make_tmp_py("_tiny_jpeg(buf)", repo_root)
        try:
            findings = _scan_file(tmp, repo_root)
            assert len(findings) == 1
            assert "JPEG blob helper" in findings[0]
        finally:
            tmp.unlink()
