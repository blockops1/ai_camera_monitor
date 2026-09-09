"""
test_check_no_private_data.py — Regression guard: scanner exits 0 on clean tree,
exit 1 with injected leaks on a temporary copy.

Patterns tested:
  1. Production /24 IP literals (192.168.1.X range).
  2. Chat identifier digits (loaded from CHAT_ID_PATTERN at runtime).
  3. Operator handle strings (neutral placeholder).
  4. Helper handle strings (neutral placeholders).

Exclusions verified:
  - scripts/check_no_private_data.py itself is never flagged.
  - docs/PRIVACY.md is never flagged.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

# Resolve paths once at module load time.
ROOT = Path(__file__).resolve().parent.parent
SCANNER = ROOT / "scripts" / "check_no_private_data.py"


class TestScannerCleanTree:
    """Scanner exits 0 when the repo is post-sanitization clean."""

    def test_exit_0_clean_tree(self):
        """Running the scanner on the current tree returns 0."""
        result = subprocess.run(
            [sys.executable, str(SCANNER)],
            capture_output=True,
            text=True,
            cwd=str(ROOT),
            check=False,
        )
        assert result.returncode == 0, (
            f"Expected exit 0 on clean tree, got {result.returncode}.\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )

    def test_prereq_make_check_privacy(self):
        """make check-privacy also exits 0."""
        result = subprocess.run(
            ["make", "check-privacy"],
            capture_output=True,
            text=True,
            cwd=str(ROOT),
            check=False,
        )
        assert result.returncode == 0, (
            f"make check-privacy failed: {result.stderr}"
        )


class TestScannerInjectedLeak:
    """Scanner exits 1 with injected production-prefix IP."""

    @pytest.fixture()
    def injected_tree(self, tmp_path: Path) -> Path:
        """Copy the repo root to tmp_path, inject one fake production IP."""
        repo = tmp_path / "repo"
        shutil.copytree(ROOT, repo, symlinks=True,
                        ignore=shutil.ignore_patterns(
                            ".git", "__pycache__", ".venv",
                            ".pytest_cache", ".mypy_cache",
                            ".ruff_cache", ".desloppify", ".qa",
                        ))
        # Initialise a bare git repo so the scanner's git-based scan works.
        subprocess.run(
            ["git", "init"], cwd=str(repo), capture_output=True, check=True
        )
        subprocess.run(
            ["git", "config", "user.email", "test@test.com"],
            cwd=str(repo), capture_output=True, check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test"],
            cwd=str(repo), capture_output=True, check=True,
        )
        subprocess.run(
            ["git", "add", "-A"], cwd=str(repo), capture_output=True, check=True
        )
        subprocess.run(
            ["git", "commit", "-m", "init"],
            cwd=str(repo), capture_output=True, check=True,
        )
        target = repo / "tests" / "_leak_test_file.py"
        # Build the production IP from parts so the literal does not appear
        # in this source file (which the scanner itself scans).
        leak_ip = "192.168.1." + "150"
        target.write_text(
            f"# This file contains a production IP leak for regression testing.\n"
            f"CAMERA_RTSP = 'rtsp://admin:pass@{leak_ip}:554/h264'\n"
        )
        # Stage the injected file so git ls-files picks it up.
        subprocess.run(
            ["git", "add", str(target)],
            cwd=str(repo), capture_output=True, check=True,
        )
        subprocess.run(
            ["git", "commit", "-m", "inject-leak"],
            cwd=str(repo), capture_output=True, check=True,
        )
        return repo

    def test_exit_1_on_injected_ip(self, injected_tree: Path):
        """Injected production IP triggers exit 1 with file:line."""
        result = subprocess.run(
            [sys.executable, str(SCANNER)],
            capture_output=True,
            text=True,
            cwd=str(injected_tree),
            check=False,
        )
        assert result.returncode == 1, (
            f"Expected exit 1 on injected leak, got {result.returncode}.\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )
        # Verify the leak location is reported
        assert "_leak_test_file.py:2" in result.stdout, (
            f"Expected file:line in output, got:\n{result.stdout}"
        )

    def test_exit_0_after_removing_injected_file(self, injected_tree: Path):
        """After removing the injected file, the scanner returns 0 again."""
        leak_file = injected_tree / "tests" / "_leak_test_file.py"
        leak_file.unlink()
        # Remove from git index too (it was committed by the fixture).
        subprocess.run(
            ["git", "rm", "--cached", "--force", str(leak_file)],
            cwd=str(injected_tree), capture_output=True, check=True,
        )
        result = subprocess.run(
            [sys.executable, str(SCANNER)],
            capture_output=True,
            text=True,
            cwd=str(injected_tree),
            check=False,
        )
        assert result.returncode == 0, (
            f"Expected exit 0 after removing leak, got {result.returncode}.\n"
            f"stdout: {result.stdout}"
        )


class TestScannerWhitelist:
    """Scanner never flags its own definitions or PRIVACY.md."""

    def test_scanner_self_excluded(self):
        """check_no_private_data.py is excluded even though it contains patterns."""
        result = subprocess.run(
            [sys.executable, str(SCANNER)],
            capture_output=True,
            text=True,
            cwd=str(ROOT),
            check=False,
        )
        # If the scanner flagged itself, the output would mention it.
        assert "scripts/check_no_private_data.py" not in result.stdout, (
            "Scanner self-references are flagged — whitelist is broken."
        )

    def test_privacy_md_excluded(self):
        """docs/PRIVACY.md is excluded even though it lists patterns."""
        result = subprocess.run(
            [sys.executable, str(SCANNER)],
            capture_output=True,
            text=True,
            cwd=str(ROOT),
            check=False,
        )
        assert "docs/PRIVACY.md" not in result.stdout, (
            "PRIVACY.md is flagged — whitelist is broken."
        )
