#!/usr/bin/env python3
"""Driver for PRD-V2-021: canonicalize transient image paths.

Migrates /tmp writes in production code to canonical
<PROJECT_ROOT>/data/frames/<camera_id>/<alert_id>/ paths.

Four stories, ~110 LOC, no daemon restarts required (operators can
restart manually after smoke test US-021d).

Stories:
  US-021a — pipeline.py _crop_paths + sentinel -> data/frames paths (30 LOC)
  US-021b — gate pairwise_diff -> canonical data path (25 LOC, depends on a)
  US-021c — no-tmp-writes scanner + Makefile + pre-push (25 LOC, depends on a,b)
  US-021d — operator-driven smoke test (30 LOC, depends on a,b,c)

Required env vars (no defaults):
  DRIVER_REPO_PATH         absolute path to farm-surveillance-v2/ repo
  OPERATOR_TELEGRAM_CHAT_ID operator chat id for /alert smoke posts

Usage:
  OPERATOR_TELEGRAM_CHAT_ID=12345 DRIVER_REPO_PATH=/Users/jill/farm-surveillance-v2 \\
      python3 scripts/run_phase_v2_021.py
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path


# --- env ----------------------------------------------------------------

REQUIRED_ENV_VARS = ("DRIVER_REPO_PATH", "OPERATOR_TELEGRAM_CHAT_ID")


def require_env(name: str) -> str:
    """Return env var or abort with explicit missing-var error."""
    val = os.environ.get(name)
    if not val:
        sys.stderr.write(f"ERROR: required env var {name} not set\n")
        sys.exit(2)
    return val


# --- paths --------------------------------------------------------------

def repo_root() -> Path:
    return Path(require_env("DRIVER_REPO_PATH"))


def prd_path() -> Path:
    return repo_root() / "docs" / "PHASE-V2-021-PRD-canonicalize-tmp-paths.json"


# --- story status -------------------------------------------------------

def read_story_status(story_id: str) -> dict:
    """Read a story's status from the PRD JSON."""
    import json
    with open(prd_path()) as f:
        d = json.load(f)
    for s in d["stories"]:
        if s["id"] == story_id:
            return s
    raise KeyError(f"story {story_id} not found in PRD")


def mark_story(story_id: str, passes: bool, commit_sha: str | None) -> None:
    """Update a story's passes + commit_sha fields."""
    import json
    with open(prd_path()) as f:
        d = json.load(f)
    for s in d["stories"]:
        if s["id"] == story_id:
            s["passes"] = passes
            s["commit_sha"] = commit_sha
            break
    with open(prd_path(), "w") as f:
        json.dump(d, f, indent=2)


# --- git helpers --------------------------------------------------------

def git(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
    """Run git in the repo root."""
    cwd = cwd or repo_root()
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )


def current_sha() -> str:
    r = git("rev-parse", "HEAD")
    return r.stdout.strip()


def working_tree_clean() -> bool:
    r = git("status", "--porcelain")
    return r.stdout.strip() == ""


# --- per-story runners --------------------------------------------------

def run_story_a() -> bool:
    """US-021a: pipeline.py _crop_paths + sentinel -> data/frames paths.

    Walks operator through:
      1. Write infra/paths.py with PROJECT_ROOT + data_dir_for() + empty_png_path()
      2. Edit listener/pipeline.py: _crop_paths + _tiny_png to use canonical paths
      3. Update tests/test_pipeline.py mocks
      4. Run pytest, confirm 132/132
      5. Commit
    """
    print("\n=== US-021a: pipeline.py _crop_paths + sentinel -> canonical paths ===")
    print("Steps:")
    print("  1. Create infra/paths.py with PROJECT_ROOT, data_dir_for(), empty_png_path()")
    print("  2. Edit listener/pipeline.py: _crop_paths() and _tiny_png() to use new paths")
    print("  3. Update tests/test_pipeline.py mocks")
    print("  4. pytest tests/ -x --tb=short  (must hit 132/132 baseline)")
    print("  5. git add infra/paths.py listener/pipeline.py tests/test_pipeline.py")
    print("  6. git commit -m 'refactor(v2): US-021a - migrate pipeline.py crop paths to canonical data dir'")
    print()
    print("AC checklist:")
    print("  AC1: grep -nE '/tmp/_ga|/tmp/_gb|/tmp/_empty' listener/pipeline.py = 0 hits")
    print("  AC2: infra/paths.py exists with the three helpers + mkdir parents")
    print("  AC3: pytest 132/132 (or higher)")
    print("  AC4: live alert -> data/frames/<cam>/<alert_id>/crop_{a,b}.png visible")
    print("  AC5: one commit, message above")
    print()
    if not confirm("Did the operator complete US-021a? [y/N] "):
        return False
    sha = current_sha()
    mark_story("US-021a", passes=True, commit_sha=sha)
    return True


def run_story_b() -> bool:
    """US-021b: gate pairwise_diff -> canonical data path.

    Walks operator through:
      1. Edit listener/pipeline.py:121 - replace output_dir='/tmp' with data_dir_for()
      2. Add assert in infra/gate.py:_write_pairwise_diff_image
      3. Update tests/test_gate.py + tests/conftest.py
      4. Run pytest
      5. Commit
    """
    print("\n=== US-021b: gate pairwise_diff -> canonical data path ===")
    print("Steps:")
    print("  1. Edit listener/pipeline.py:121 - output_dir=str(data_dir_for(camera_id, alert_id))")
    print("  2. Add assert in infra/gate.py:_write_pairwise_diff_image() that '/tmp' not in output_dir")
    print("  3. Update tests/test_gate.py mocks to use tmp_path-based canonical paths")
    print("  4. Add tests/test_gate.py::test_gate_pairwise_diff_does_not_write_to_tmp")
    print("  5. pytest tests/ -x --tb=short")
    print("  6. git add listener/pipeline.py infra/gate.py tests/test_gate.py tests/conftest.py")
    print("  7. git commit -m 'refactor(v2): US-021b - gate pairwise_diff writes to canonical data path'")
    print()
    if not confirm("Did the operator complete US-021b? [y/N] "):
        return False
    sha = current_sha()
    mark_story("US-021b", passes=True, commit_sha=sha)
    return True


def run_story_c() -> bool:
    """US-021c: no-tmp-writes regression scanner.

    Mirrors the existing check_no_private_data.py pattern.
    """
    print("\n=== US-021c: no-tmp-writes scanner + Makefile + pre-push ===")
    print("Steps:")
    print("  1. Write scripts/check_no_tmp_production_writes.py")
    print("  2. Write tests/test_check_no_tmp_production_writes.py")
    print("  3. Add 'check-no-tmp' target to Makefile")
    print("  4. Update .git/hooks/pre-push to run all 4 scanners (privacy, resize, jpeg, tmp)")
    print("  5. pytest tests/ -x --tb=short")
    print("  6. git add scripts/check_no_tmp_production_writes.py tests/test_check_no_tmp_production_writes.py Makefile .git/hooks/pre-push")
    print("  7. git commit -m 'feat(v2): US-021c - no-tmp-writes scanner + Makefile + pre-push wiring'")
    print()
    if not confirm("Did the operator complete US-021c? [y/N] "):
        return False
    sha = current_sha()
    mark_story("US-021c", passes=True, commit_sha=sha)
    return True


def run_story_d() -> bool:
    """US-021d: operator-driven smoke test on live daemon."""
    print("\n=== US-021d: live alert smoke test on canonical paths ===")
    print("Steps:")
    print("  1. Write scripts/smoke_alert_canonical_paths.py")
    print("  2. Run it against the live daemon (port 8090)")
    print("  3. Verify data/frames/<cam>/<alert_id>/ has crop_a.png, crop_b.png, pairwise_diff.png")
    print("  4. Verify /tmp/_ga.png, /tmp/_gb.png, /tmp/_empty.png, /tmp/pairwise_diff_*.png do NOT exist")
    print("  5. Verify operator received Telegram alert with PNG crops (lossless)")
    print("  6. git add scripts/smoke_alert_canonical_paths.py")
    print("  7. git commit -m 'feat(v2): US-021d - smoke test for canonical-path alert pipeline'")
    print()
    if not confirm("Did the operator complete US-021d? [y/N] "):
        return False
    sha = current_sha()
    mark_story("US-021d", passes=True, commit_sha=sha)
    return True


# --- confirmation prompt ------------------------------------------------

def confirm(prompt: str) -> bool:
    """Confirm yes/no from operator. Defaults to No (must explicitly accept)."""
    resp = input(prompt).strip().lower()
    return resp in ("y", "yes")


# --- main ---------------------------------------------------------------

STORY_RUNNERS = {
    "US-021a": run_story_a,
    "US-021b": run_story_b,
    "US-021c": run_story_c,
    "US-021d": run_story_d,
}


def main() -> int:
    print("PRD-V2-021 driver: canonicalize /tmp writes to data/frames/<cam>/<alert_id>/")
    print()

    # env gate
    for var in REQUIRED_ENV_VARS:
        require_env(var)

    print(f"DRIVER_REPO_PATH = {require_env('DRIVER_REPO_PATH')}")
    print(f"OPERATOR_TELEGRAM_CHAT_ID = {require_env('OPERATOR_TELEGRAM_CHAT_ID')}")
    print()

    # Pre-flight
    if not working_tree_clean():
        print("WARN: working tree is dirty. Commit or stash before running.")
        if not confirm("Continue anyway? [y/N] "):
            return 1

    # Walk stories in topo order
    failed = []
    for story_id, runner in STORY_RUNNERS.items():
        status = read_story_status(story_id)
        if status["passes"]:
            print(f"[skip] {story_id} already marked passes=True ({status.get('commit_sha')})")
            continue
        if story_id in (s for s in (runner.__name__,)):
            pass
        if runner():
            print(f"[done] {story_id}")
        else:
            print(f"[stop] {story_id} failed or aborted")
            failed.append(story_id)
            break

    print()
    if failed:
        print(f"ABORTED at {failed[0]}; fix and re-run.")
        return 1
    print("PRD-V2-021 complete: 4/4 stories passes=True")
    return 0


if __name__ == "__main__":
    sys.exit(main())
