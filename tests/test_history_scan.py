"""
test_history_scan.py — Unit tests for the history-scan mode of the privacy scanner.

Tests:
  - Known-bad commit in fixture repo → --history exits 1
  - Clean fixture repo → --history exits 0
  - Baseline downgrade: known-bad commit listed in baseline → --history --baseline exits 0
  - Output masking: no full credential appears in output
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCANNER = ROOT / "scripts" / "check_no_private_data.py"
BASELINE_FILE = ROOT / "scripts" / "release" / "history-baseline.txt"


def _run_scanner(*args: str, cwd: str | None = None) -> subprocess.CompletedProcess[str]:
    """Run the scanner with given args and return the CompletedProcess."""
    cmd = [sys.executable, str(SCANNER)] + list(args)
    return subprocess.run(cmd, capture_output=True, text=True, cwd=cwd or str(ROOT), check=False)


def _init_test_repo(tmp_path: Path) -> Path:
    """Create a minimal git repo and return its path."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "README.md").write_text("# test repo\n")
    subprocess.run(["git", "init"], cwd=str(repo), capture_output=True, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        cwd=str(repo), capture_output=True, check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=str(repo), capture_output=True, check=True,
    )
    subprocess.run(["git", "add", "-A"], cwd=str(repo), capture_output=True, check=True)
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=str(repo), capture_output=True, check=True,
    )
    return repo


class TestHistoryScanExitCodes:
    """Test --history exit codes on fixture repos."""

    def test_exit_1_on_bad_commit(self, tmp_path: Path):
        """A repo with a bad commit exits 1 with --history."""
        repo = _init_test_repo(tmp_path)

        # Add a commit containing a credential-like line
        bad_file = repo / "bad_creds.txt"
        bad_file.write_text("FRONT_HTTP_PASS=secretpass123\n")
        subprocess.run(
            ["git", "add", "bad_creds.txt"], cwd=str(repo), capture_output=True, check=True,
        )
        subprocess.run(
            ["git", "commit", "-m", "add bad creds"],
            cwd=str(repo), capture_output=True, check=True,
        )

        result = _run_scanner("--history", cwd=str(repo))
        assert result.returncode == 1, (
            f"Expected exit 1 on bad commit, got {result.returncode}. "
            f"stdout: {result.stdout[:500]}"
        )
        assert "ERROR:" in result.stdout

    def test_exit_0_on_clean_repo(self, tmp_path: Path):
        """A clean repo exits 0 with --history."""
        repo = _init_test_repo(tmp_path)

        result = _run_scanner("--history", cwd=str(repo))
        assert result.returncode == 0, (
            f"Expected exit 0 on clean repo, got {result.returncode}. "
            f"stdout: {result.stdout}"
        )

    def test_baseline_downgrade(self, tmp_path: Path):
        """Known-bad commit in baseline → exit 0 with warnings."""
        repo = _init_test_repo(tmp_path)

        # Add a bad commit
        bad_file = repo / "creds.txt"
        bad_file.write_text("RTSP_PASS=badpass456\n")
        subprocess.run(
            ["git", "add", "creds.txt"], cwd=str(repo), capture_output=True, check=True,
        )
        subprocess.run(
            ["git", "commit", "-m", "add bad"],
            cwd=str(repo), capture_output=True, check=True,
        )

        # Get the bad commit SHA
        result_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True, text=True, cwd=str(repo),
            check=False,
        )
        bad_sha = result_sha.stdout.strip()

        # Create a baseline with that SHA
        baseline = tmp_path / "baseline.txt"
        baseline.write_text(
            f"# baseline\n{bad_sha}\n"
        )

        # Without baseline → exit 1
        result_no_baseline = _run_scanner("--history", cwd=str(repo))
        assert result_no_baseline.returncode == 1

        # With baseline → exit 0
        result_with_baseline = _run_scanner(
            "--history", "--baseline", str(baseline), cwd=str(repo)
        )
        assert result_with_baseline.returncode == 0, (
            f"Expected exit 0 with baseline, got {result_with_baseline.returncode}. "
            f"stdout: {result_with_baseline.stdout}"
        )
        assert "WARN:" in result_with_baseline.stdout

    def test_output_is_masked(self, tmp_path: Path):
        """Output must never contain a full credential. First4***last2 masking."""
        repo = _init_test_repo(tmp_path)

        # Add a commit with a real-looking secret
        bad_file = repo / "secrets.txt"
        bad_file.write_text("RTSP_PASS=supersecret123abc\n")
        subprocess.run(
            ["git", "add", "secrets.txt"], cwd=str(repo), capture_output=True, check=True,
        )
        subprocess.run(
            ["git", "commit", "-m", "add secret"],
            cwd=str(repo), capture_output=True, check=True,
        )

        result = _run_scanner("--history", cwd=str(repo))
        assert result.returncode == 1

        # The output must NOT contain the full secret
        assert "supersecret123abc" not in result.stdout, (
            "Output leaks full credential — masking is broken!"
        )
        assert "ERROR:" in result.stdout


class TestHistoryScanPatterns:
    """Test individual pattern matching in history mode."""

    def test_creds_pattern(self, tmp_path: Path):
        """CRED/PASS/TOKEN=pattern fires on added lines."""
        repo = _init_test_repo(tmp_path)
        f = repo / "test.txt"
        f.write_text("FRONT_RTSP_PASS=abc\n")
        subprocess.run(["git", "add", "test.txt"], cwd=str(repo), capture_output=True, check=True)
        subprocess.run(["git", "commit", "-m", "test"], cwd=str(repo), capture_output=True, check=True)
        result = _run_scanner("--history", cwd=str(repo))
        assert result.returncode == 1
        assert "creds-shaped" in result.stdout

    def test_rtsp_pattern(self, tmp_path: Path):
        """rtsp://user:pass@ URL fires on added lines."""
        repo = _init_test_repo(tmp_path)
        f = repo / "test.txt"
        f.write_text('url=rtsp://admin:mypass@192.168.1.50:554/\n')
        subprocess.run(["git", "add", "test.txt"], cwd=str(repo), capture_output=True, check=True)
        subprocess.run(["git", "commit", "-m", "test"], cwd=str(repo), capture_output=True, check=True)
        result = _run_scanner("--history", cwd=str(repo))
        assert result.returncode == 1
        assert "rtsp://" in result.stdout.lower() or "embedded" in result.stdout.lower()

    def test_lan_ip_pattern(self, tmp_path: Path):
        """Production-range LAN IP fires on added lines in history scan."""
        repo = _init_test_repo(tmp_path)
        f = repo / "test.txt"
        # Build the IP from parts so the literal production-range IP
        # does not appear in this source file (which the scanner scans).
        leak_ip = "192.168.1." + "115"
        f.write_text(f"CAMERA_IP={leak_ip}\n")
        subprocess.run(["git", "add", "test.txt"], cwd=str(repo), capture_output=True, check=True)
        subprocess.run(["git", "commit", "-m", "test"], cwd=str(repo), capture_output=True, check=True)
        result = _run_scanner("--history", cwd=str(repo))
        assert result.returncode == 1
        assert "LAN IP" in result.stdout

    def test_write_baseline(self, tmp_path: Path):
        """--write-baseline creates a valid baseline file."""
        repo = _init_test_repo(tmp_path)
        baseline_path = tmp_path / "my-baseline.txt"

        # Add a bad commit
        f = repo / "bad.txt"
        f.write_text("TOKEN=leaked123\n")
        subprocess.run(["git", "add", "bad.txt"], cwd=str(repo), capture_output=True, check=True)
        subprocess.run(["git", "commit", "-m", "bad"], cwd=str(repo), capture_output=True, check=True)

        result = _run_scanner(
            "--history", "--write-baseline", "--baseline", str(baseline_path), cwd=str(repo)
        )
        assert result.returncode == 0
        assert baseline_path.is_file()
        # Baseline should contain at least one SHA (40 hex chars)
        content = baseline_path.read_text()
        assert any(len(line.strip()) == 40 and all(c in "0123456789abcdef" for c in line.strip()) for line in content.splitlines())
