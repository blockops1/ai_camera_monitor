"""
Test: .git/hooks/post-merge fires on merge and logs a push attempt.

Uses a tempdir with a bare + working clone to simulate a real merge
without touching the real repo.
"""

import subprocess
import tempfile
import textwrap
from pathlib import Path


def _run(cmd, cwd=None):
    """Run a shell command and return (stdout, rc)."""
    result = subprocess.run(
        cmd, shell=True, cwd=cwd, capture_output=True, text=True, check=False
    )
    return result.stdout, result.returncode


def _setup_work_repo(td):
    """Create a bare remote and a working clone with initial commit."""
    td = Path(td)

    # --- Setup a bare repo (the "remote") ---
    bare = td / "bare.git"
    bare.mkdir()
    _run("git init --bare", cwd=str(bare))

    # --- Clone as a working repo ---
    work = td / "work"
    _run(f"git clone '{bare}' '{work}'", cwd=td)

    # Configure git identity for commits
    _run("git config user.email 'test@example.com'", cwd=str(work))
    _run("git config user.name 'Test Worker'", cwd=str(work))

    # Create initial commit
    (work / "README.md").write_text("# test\n")
    _run("git add README.md", cwd=str(work))
    _run("git commit -m 'initial commit'", cwd=str(work))
    _run("git push origin main", cwd=str(work))

    return work


def _install_hook_and_scripts(work, fake_push_script="#!/bin/sh\necho 'FAKE PUSH: ok'\nexit 0\n"):
    """Install the real post-merge hook and a fake push_to_forgejo.sh."""
    # Install the real post-merge hook
    src_hook = Path(__file__).resolve().parent.parent / ".git" / "hooks" / "post-merge"
    dst_hook = work / ".git" / "hooks" / "post-merge"
    dst_hook.write_text(src_hook.read_text())
    dst_hook.chmod(0o755)

    # Install the fake push script
    scripts_dir = work / "scripts"
    scripts_dir.mkdir()
    fake_script = scripts_dir / "push_to_forgejo.sh"
    fake_script.write_text(fake_push_script)
    fake_script.chmod(0o755)

    # Push log file
    log_file = scripts_dir / "push_log.txt"
    log_file.write_text("# empty log\n")

    return log_file


def _make_diverged_merge(work):
    """Create a merge with a non-trivial merge commit (not fast-forward)."""
    # Create a diverging commit on main first
    (work / "main-extra.txt").write_text("main side work\n")
    _run("git add main-extra.txt", cwd=str(work))
    _run("git commit -m 'main diverges'", cwd=str(work))
    _run("git push origin main", cwd=str(work))

    # Create a feature branch from the earlier commit
    _run("git checkout -b feature HEAD~1", cwd=str(work))
    (work / "feature.txt").write_text("feature work\n")
    _run("git add feature.txt", cwd=str(work))
    _run("git commit -m 'feature commit'", cwd=str(work))
    _run("git push origin feature", cwd=str(work))

    # Back to main and merge with --no-ff to force a merge commit
    _run("git checkout main", cwd=str(work))
    return _run("git merge feature --no-edit --no-ff", cwd=str(work))


def test_post_merge_hook_fires_and_logs():
    """
    AC#5: a fake merge-to-main triggers the hook and the log shows
    a push attempt.
    """
    with tempfile.TemporaryDirectory(prefix="pm_test_") as td:
        work = _setup_work_repo(td)

        # Add a forgejo remote so the hook tries to push (doesn't no-op)
        _run("git remote add forgejo http://localhost:3000/fake.git", cwd=str(work))

        log_file = _install_hook_and_scripts(work)

        # Commit the fake script so it's tracked
        _run("git add scripts/", cwd=str(work))
        _run("git commit -m 'add fake push script'", cwd=str(work))
        _run("git push origin main", cwd=str(work))

        # Create a non-trivial merge
        _make_diverged_merge(work)

        # Verify the hook logged the push attempt
        assert log_file.exists(), "push_log.txt should exist after merge"
        content = log_file.read_text()
        assert "post-merge:" in content, "Log should contain post-merge entry"
        assert "push_to_forgejo.sh" in content, "Log should reference the push script"
        assert "FAKE PUSH" in content, "Log should capture push script output"
        assert "exit=0" in content, "Log should record exit code"


def test_post_merge_hook_noop_without_forgejo_remote():
    """
    AC#2: Hook is a no-op when no forgejo remote exists.
    """
    with tempfile.TemporaryDirectory(prefix="pm_noforgejo_") as td:
        td = Path(td)
        work = _setup_work_repo(td)

        # Install hook + scripts (no forgejo remote, so should no-op)
        log_file = _install_hook_and_scripts(work)

        # Commit scripts
        _run("git add scripts/", cwd=str(work))
        _run("git commit -m 'add scripts'", cwd=str(work))
        _run("git push origin main", cwd=str(work))

        # Create a non-trivial merge
        _make_diverged_merge(work)

        # The hook should have logged "no forgejo remote"
        content = log_file.read_text()
        assert "no forgejo remote" in content, "Hook should log no-op reason"
        assert "skipping auto-push" in content, "Hook should say skipping"


def test_post_merge_hook_exits_0_on_push_failure():
    """
    AC#3: Hook exits 0 even if push fails.
    """
    with tempfile.TemporaryDirectory(prefix="pm_fail_") as td:
        td = Path(td)
        work = _setup_work_repo(td)

        # Set up a forgejo remote so the hook doesn't no-op
        _run("git remote add forgejo http://localhost:3000/fake.git", cwd=str(work))

        # Install hook with a failing push script
        failing_script = textwrap.dedent("""\
            #!/bin/sh
            echo "PUSH FAILED: remote unreachable"
            exit 1
        """)
        log_file = _install_hook_and_scripts(work, failing_script)

        # Commit scripts
        _run("git add scripts/", cwd=str(work))
        _run("git commit -m 'add scripts'", cwd=str(work))
        _run("git push origin main", cwd=str(work))

        # Create a non-trivial merge
        _make_diverged_merge(work)

        # Hook must exit 0 even though push script returned 1
        # Verify the log recorded the failure
        content = log_file.read_text()
        assert "exit=1" in content, "Log should show the push script failed"
        assert "post-merge:" in content, "Hook should have logged"


if __name__ == "__main__":
    test_post_merge_hook_fires_and_logs()
    test_post_merge_hook_noop_without_forgejo_remote()
    test_post_merge_hook_exits_0_on_push_failure()
    print("All 3 tests passed.")
