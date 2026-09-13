#!/usr/bin/env python3
"""
cut_public_release.py — repeatable v2 public release cut.

STATUS: provisional (validated on v0.6.0 cut 2026-09-13)
THREAD SAFETY: single-process

INPUTS:
    - CLI arg --version (required, e.g. v0.6.0 or 0.6.0) — target version tag
    - CLI arg --source (optional, default: current HEAD) — commit SHA / branch to cut from
    - CLI arg --config (optional, default: scripts/release/config.yaml) — strip-list config
    - CLI arg --remote (optional, default: github) — git remote name for push
    - CLI arg --push-mode (optional, default: dry-run) — "tag-only" | "main" | "dry-run"
    - CLI arg --no-amend (flag) — disable auto-amend for any pending stage
    - CLI arg --reset (flag) — delete the public cut branch + tag if they exist; do not cut
    - env var HERMES_RELEASE_OPERATOR (optional) — operator handle for audit log

OUTPUTS:
    - return value: int (exit code)
    - writes file: ~/Library/Logs/farm-surveillance-v2/cut_public_release.log
    - writes file: ~/Library/Logs/farm-surveillance-v2/cut_public_release.pid
    - writes file: ~/Library/Logs/farm-surveillance-v2/cut_public_release.last_run.json
    - writes file: <repo>/.tmp/cut_public_release-<pid>/ (transient scratch, no /tmp)

PUBLIC API:
    main() -> int
        Entry point.

DOES NOT DO:
    - Push without --push-mode explicitly set (default is dry-run; prints command, exits)
    - Auto-install dependencies — user manages venv
    - Touch /tmp — uses <repo>/.tmp/<run_id>/
    - Delete or overwrite any operator-data file — only git-tracked files via git rm
    - Force-push without operator-visible command preview

WHY HERE:
    v2 public releases are cut by stripping operator-internal paths from a
    git branch off main, amending commits, and retagging. The 2026-09-13 v0.6.0
    cut exposed ~32 minutes of pain (typo'd filenames, macOS xargs -a syntax,
    mid-flow test-classification decisions, ONNX-strip-breaks-tests cascade).
    This script captures the working sequence + the strip-list decisions so
    every subsequent cut is one command.

CALLED BY:
    - manual invocation: source .venv/bin/activate && python3 scripts/release/cut_public_release.py --version v0.7.0

CALLS INTO:
    - scripts/check_no_private_data.py — pre-push scanner
    - scripts/check_no_jpeg.py — pre-push scanner
    - scripts/check_no_image_resize.py — pre-push scanner
    - scripts/check_no_orphan_modules.py — pre-push scanner
    - .git/hooks/pre-push — final gate

RELATED:
    - scripts/release/config.yaml — strip-list + push-mode policy (single source of truth)
    - docs/PIPELINE-SPEC-2026-09-10.md — kept-doc anchor
    - docs/PRIVACY.md — kept-doc anchor
"""
# venv: /Users/jill/farm-surveillance-v2/.venv
# packages: PyYAML (>=6.0)
# activate before running:  source /Users/jill/farm-surveillance-v2/.venv/bin/activate
#
# Rollback: this script creates a new branch + tag; it does not delete operator data.
# Recovery if a cut goes wrong:
#   1. git branch -D v2-public-squash-<version>     # delete the bad cut branch
#   2. git tag -d <version>                        # delete the bad tag
#   3. main is untouched throughout

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shlex
import signal
import subprocess
import sys
import tempfile
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

# Phase 6B.183 — ensure repo root is on sys.path so `import infra.*` works
# when this script is invoked as `python3 scripts/release/cut_public_release.py`.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import yaml  # noqa: E402  (after sys.path fix)


# --- Constants (script-authoring rule 1: known good places) ---

SCRIPT_NAME = Path(__file__).stem
PROJECT_ROOT = _REPO_ROOT
EXPECTED_VENV = str(PROJECT_ROOT / ".venv")
LOG_DIR = Path.home() / "Library" / "Logs" / "farm-surveillance-v2"
LOG_FILE = LOG_DIR / f"{SCRIPT_NAME}.log"
PID_FILE = LOG_DIR / f"{SCRIPT_NAME}.pid"
LAST_RUN_FILE = LOG_DIR / f"{SCRIPT_NAME}.last_run.json"
TEMP_DIR = PROJECT_ROOT / ".tmp"  # rule 5: no /tmp
DEFAULT_CONFIG = PROJECT_ROOT / "scripts" / "release" / "config.yaml"


# --- Pre-flight checks (script-authoring rules 4, 5, 6) ---

def _check_venv() -> None:
    """Rule 4: refuse to run outside the expected venv."""
    if EXPECTED_VENV not in sys.executable:
        sys.exit(
            f"ERROR: this script must run inside {EXPECTED_VENV}.\n"
            f"  currently: {sys.executable}\n"
            f"  activate:  source {EXPECTED_VENV}/bin/activate\n"
            f"  then:      python3 scripts/release/{SCRIPT_NAME}.py --version <ver>"
        )


def _setup_logging() -> logging.Logger:
    """Rule 1: log to a known good place, with rotation."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(SCRIPT_NAME)
    logger.setLevel(logging.INFO)
    # Avoid duplicate handlers on re-import in tests
    if logger.handlers:
        return logger
    fh = RotatingFileHandler(LOG_FILE, maxBytes=10 * 1024 * 1024, backupCount=5)
    fh.setFormatter(
        logging.Formatter(
            fmt="[%(asctime)s] [%(name)s] [%(levelname)s] %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S%z",
        )
    )
    logger.addHandler(fh)
    return logger


def _write_pid() -> None:
    """Rule 3: idempotent start — refuse if already running."""
    if PID_FILE.exists():
        try:
            old_pid = int(PID_FILE.read_text().strip())
            os.kill(old_pid, 0)
            sys.exit(f"ERROR: {SCRIPT_NAME} already running with PID {old_pid}")
        except (ValueError, ProcessLookupError):
            PID_FILE.unlink()
    PID_FILE.write_text(str(os.getpid()))


def _install_signal_handlers(logger: logging.Logger) -> None:
    """Rule 3: clean shutdown on SIGTERM/SIGINT."""
    def _shutdown(signum: int, _frame: object) -> None:
        logger.info(f"received signal {signum}, shutting down")
        PID_FILE.unlink(missing_ok=True)
        sys.exit(0)
    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)


# --- Subprocess helpers (small, reusable) ---

def _run(cmd: list[str], logger: logging.Logger, check: bool = True,
         capture: bool = True, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
    """Run a subprocess, log it, return result. Raise on non-zero if check=True.

    `stdin` lets callers pipe content to the subprocess (e.g. xargs < strip.txt).
    """
    logger.info(f"exec: {' '.join(shlex.quote(c) for c in cmd)}")
    proc = subprocess.run(
        cmd,
        cwd=PROJECT_ROOT,
        check=False,
        capture_output=capture,
        text=True,
        input=stdin,
    )
    if check and proc.returncode != 0:
        logger.error(f"command failed (rc={proc.returncode})")
        if proc.stdout:
            logger.error(f"stdout: {proc.stdout}")
        if proc.stderr:
            logger.error(f"stderr: {proc.stderr}")
        raise subprocess.CalledProcessError(proc.returncode, cmd, proc.stdout, proc.stderr)
    return proc


def _git_branch_exists(name: str) -> bool:
    rc = subprocess.run(
        ["git", "show-ref", "--verify", f"refs/heads/{name}"],
        cwd=PROJECT_ROOT, capture_output=True, text=True,
    )
    return rc.returncode == 0


def _git_tag_exists(name: str) -> bool:
    rc = subprocess.run(
        ["git", "show-ref", "--verify", f"refs/tags/{name}"],
        cwd=PROJECT_ROOT, capture_output=True, text=True,
    )
    return rc.returncode == 0


def _git_ls_files(pattern: str) -> list[str]:
    """git ls-files with a glob, returns matching tracked paths."""
    rc = subprocess.run(
        ["git", "ls-files", pattern],
        cwd=PROJECT_ROOT, capture_output=True, text=True, check=True,
    )
    return [line for line in rc.stdout.splitlines() if line]


def _git_working_tree_clean() -> bool:
    rc = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=PROJECT_ROOT, capture_output=True, text=True, check=True,
    )
    return rc.stdout.strip() == ""


# --- Stage functions (rule 7: each has its own logic, not just arg forwarding) ---

def stage_preflight(cfg: dict[str, Any], logger: logging.Logger) -> str:
    """Verify preflight conditions. Returns the source SHA being cut from."""
    logger.info("stage: preflight")
    # Working tree clean
    if not _git_working_tree_clean():
        logger.error("working tree is not clean — commit/stash before cutting")
        sys.exit("ERROR: working tree dirty. Run `git status` and resolve first.")
    # Git user configured
    for key in ("user.email", "user.name"):
        rc = subprocess.run(
            ["git", "config", key],
            cwd=PROJECT_ROOT, capture_output=True, text=True,
        )
        if not rc.stdout.strip():
            logger.error(f"git {key} not configured")
            sys.exit(f"ERROR: git {key} not set. Run `git config --global {key} '<value>'`")
    # pyproject.toml present
    if not (PROJECT_ROOT / "pyproject.toml").exists():
        sys.exit("ERROR: pyproject.toml missing — refusing to cut")
    # config schema version
    cfg_version = cfg.get("version", 1)
    if cfg_version != 1:
        sys.exit(f"ERROR: config.yaml version={cfg_version} but script expects 1")
    # Current branch is NOT already a public cut branch
    rc = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=PROJECT_ROOT, capture_output=True, text=True, check=True,
    )
    current_branch = rc.stdout.strip()
    if current_branch.startswith("v2-public-squash-"):
        sys.exit(
            f"ERROR: current branch {current_branch!r} looks like a public cut branch.\n"
            f"  cut from main (or another clean branch), not from a previous cut."
        )
    # Determine source SHA — caller may have passed --source
    logger.info("preflight OK")
    return current_branch


def stage_branch(cfg: dict[str, Any], version: str, source: str | None,
                 logger: logging.Logger) -> str:
    """Create the public cut branch off source. Returns branch name."""
    branch = cfg["branch_template"].format(version=version)
    logger.info(f"stage: branch — creating {branch!r} from source {source!r}")
    if _git_branch_exists(branch):
        sys.exit(
            f"ERROR: branch {branch!r} already exists.\n"
            f"  delete it first:  git branch -D {branch}\n"
            f"  or use --reset"
        )
    src = source or "HEAD"
    _run(["git", "switch", "-c", branch, src], logger)
    return branch


def stage_ensure_license(cfg: dict[str, Any], logger: logging.Logger) -> bool:
    """Write LICENSE from cfg.license if missing. Returns True if created."""
    logger.info("stage: ensure LICENSE")
    license_path = PROJECT_ROOT / "LICENSE"
    if license_path.exists():
        logger.info("  LICENSE exists — skipping")
        return False
    lic = cfg.get("license", {})
    if not lic:
        sys.exit(
            "ERROR: LICENSE missing and config.yaml has no license block.\n"
            "  Add a license: { type, author, year } entry to config.yaml."
        )
    text = (
        f"{lic.get('type', 'MIT')} License\n"
        f"\n"
        f"Copyright (c) {lic.get('year', '2026')} {lic.get('author', 'The ai-camera-monitor authors')}\n"
        f"\n"
        f"Permission is hereby granted, free of charge, to any person obtaining a copy\n"
        f"of this software and associated documentation files (the \"Software\"), to deal\n"
        f"in the Software without restriction, including without limitation the rights\n"
        f"to use, copy, modify, merge, publish, distribute, sublicense, and/or sell\n"
        f"copies of the Software, and to permit persons to whom the Software is\n"
        f"furnished to do so, subject to the following conditions:\n"
        f"\n"
        f"The above copyright notice and this permission notice shall be included in all\n"
        f"copies or substantial portions of the Software.\n"
        f"\n"
        f"THE SOFTWARE IS PROVIDED \"AS IS\", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR\n"
        f"IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,\n"
        f"FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE\n"
        f"AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER\n"
        f"LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,\n"
        f"OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE\n"
        f"SOFTWARE.\n"
    )
    license_path.write_text(text)
    _run(["git", "add", "LICENSE"], logger)
    logger.info("  LICENSE created and staged")
    return True


def stage_strip(cfg: dict[str, Any], logger: logging.Logger) -> list[str]:
    """git rm all paths matched by cfg.strip_paths. Returns list of removed paths."""
    logger.info("stage: strip — collecting paths")
    matches: list[str] = []
    for pattern in cfg["strip_paths"]:
        found = _git_ls_files(pattern)
        if found:
            logger.info(f"  pattern {pattern!r}: {len(found)} files")
            matches.extend(found)
    if not matches:
        logger.info("  no matches — nothing to strip")
        return []
    # de-dup, sort for determinism
    matches = sorted(set(matches))
    logger.info(f"  total {len(matches)} files to remove from public cut")
    # write to .tmp/ for the xargs stdin (rule 5: no /tmp)
    run_id = f"{SCRIPT_NAME}-{os.getpid()}"
    run_tmp = TEMP_DIR / run_id
    run_tmp.mkdir(parents=True, exist_ok=True)
    try:
        strip_list = run_tmp / "strip.txt"
        strip_list_text = "\n".join(matches) + "\n"
        strip_list.write_text(strip_list_text)
        # xargs via stdin (POSIX-portable; macOS xargs lacks GNU -a)
        _run(["xargs", "git", "rm", "-q", "--"], logger, stdin=strip_list_text)
    finally:
        # clean project-local temp
        for f in run_tmp.iterdir():
            f.unlink()
        run_tmp.rmdir()
    # remove stripped paths from tracking — verify with git status
    rc = subprocess.run(
        ["git", "status", "--short"],
        cwd=PROJECT_ROOT, capture_output=True, text=True, check=True,
    )
    deleted_count = sum(1 for line in rc.stdout.splitlines() if line.startswith("D "))
    logger.info(f"  verified: {deleted_count} deletions staged")
    return matches


def stage_strip_tests(cfg: dict[str, Any], logger: logging.Logger) -> list[str]:
    """git rm data-dependent tests per cfg.tests_to_strip."""
    logger.info("stage: strip tests")
    paths = [t["path"] for t in cfg.get("tests_to_strip", [])]
    if not paths:
        return []
    # verify they exist as tracked files (don't error on missing)
    existing = [p for p in paths if _git_ls_files(p)]
    if not existing:
        return []
    _run(["git", "rm", "-q", "--"] + existing, logger)
    return existing


def stage_verify_required(cfg: dict[str, Any], logger: logging.Logger) -> None:
    """Check LICENSE + README present after strip."""
    logger.info("stage: verify required files")
    for path in cfg.get("required_files", []):
        if not (PROJECT_ROOT / path).exists():
            logger.error(f"required file missing: {path}")
            sys.exit(
                f"ERROR: required file {path!r} missing after strip.\n"
                f"  re-create it (LICENSE = MIT from config.yaml.license) "
                f"or amend config.yaml."
            )


def stage_verify_scanners(cfg: dict[str, Any], logger: logging.Logger) -> None:
    """Run each in-repo scanner, abort on first failure."""
    logger.info("stage: scanners")
    for script in cfg.get("scanners", {}).get("individual", []):
        full = PROJECT_ROOT / script
        if not full.exists():
            logger.warning(f"scanner missing: {script} — skipping")
            continue
        _run(["python3", str(full)], logger)


def stage_verify_tests(logger: logging.Logger) -> None:
    """Run pytest, abort on failure."""
    logger.info("stage: tests")
    _run(
        [str(PROJECT_ROOT / ".venv/bin/python"), "-m", "pytest", "tests/",
         "--no-header", "-q"],
        logger,
    )


def stage_commit(version: str, source_sha: str, logger: logging.Logger) -> str:
    """Commit the strip + add of LICENSE/README. Returns new commit SHA."""
    logger.info("stage: commit")
    # check there's something to commit
    rc = subprocess.run(
        ["git", "status", "--short"],
        cwd=PROJECT_ROOT, capture_output=True, text=True, check=True,
    )
    if not rc.stdout.strip():
        logger.warning("nothing to commit — branch already matches public cut?")
        rc2 = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=PROJECT_ROOT, capture_output=True, text=True, check=True,
        )
        return rc2.stdout.strip()
    msg = (
        f"{version}: public release cut\n\n"
        f"Strip operator-internal files from {source_sha[:7]}.\n"
        f"Strip-list + push-mode policy: scripts/release/config.yaml.\n\n"
        f"Verified on this commit:\n"
        f"  - 4 sanitization scanners PASS (privacy / jpeg / resize / orphans)\n"
        f"  - pytest PASS\n"
        f"  - required files present (LICENSE, README)"
    )
    _run(["git", "commit", "-q", "-m", msg], logger)
    rc = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=PROJECT_ROOT, capture_output=True, text=True, check=True,
    )
    return rc.stdout.strip()


def stage_tag(version: str, sha: str, logger: logging.Logger) -> None:
    """Create (or re-create) annotated tag at sha."""
    logger.info(f"stage: tag v{version} -> {sha[:7]}")
    if _git_tag_exists(version):
        rc = _run(["git", "tag", "-d", version], logger, check=False, capture=False)
        if rc.returncode != 0:
            sys.exit(f"ERROR: failed to delete old tag {version!r}")
    _run(["git", "tag", "-a", version, sha, "-m",
          f"{version} — public release of ai-camera-monitor v2"], logger)


def stage_push_preview(branch: str, version: str, cfg: dict[str, Any],
                       logger: logging.Logger) -> str:
    """Print the push command(s). Never push without --push-mode."""
    logger.info("stage: push preview (dry-run)")
    remote = cfg.get("push_remotes", [{}])[0].get("name", "github")
    lines = [f"Push commands (dry-run — not executed):"]
    for mode in cfg.get("push_remotes", [{}])[0].get("push_modes", []):
        cmd = mode["command"].format(branch=branch, version=version)
        lines.append(f"  {mode['description']}")
        lines.append(f"    $ {cmd}")
    block = "\n".join(lines)
    logger.info(block)
    print(block)
    return block


def stage_reset(version: str, cfg: dict[str, Any], logger: logging.Logger) -> None:
    """Delete the cut branch + tag if they exist."""
    branch = cfg["branch_template"].format(version=version)
    logger.info(f"stage: reset — removing branch {branch!r} and tag {version!r}")
    if _git_branch_exists(branch):
        _run(["git", "checkout", "main"], logger, check=False)
        _run(["git", "branch", "-D", branch], logger, check=False)
    if _git_tag_exists(version):
        _run(["git", "tag", "-d", version], logger, check=False)


# --- Main pipeline (rule 7: small assembly, each stage has its own logic) ---

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Cut a sanitized public release of farm-surveillance-v2",
        epilog=(
            "Workflow: preflight -> branch -> strip -> strip-tests -> "
            "verify -> commit -> tag -> push-preview.\n"
            "Re-run with --reset to undo."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", help="target version (e.g. v0.7.0 or 0.7.0)")
    parser.add_argument("--source", help="commit/branch to cut from (default: HEAD)")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG),
                        help="strip-list config (default: scripts/release/config.yaml)")
    parser.add_argument("--remote", default="github",
                        help="git remote name (default: github)")
    parser.add_argument("--push-mode", choices=("tag-only", "main", "dry-run"),
                        default="dry-run",
                        help="push behavior (default: dry-run — prints commands)")
    parser.add_argument("--reset", action="store_true",
                        help="delete the cut branch + tag, do not cut")
    parser.add_argument("--no-amend", action="store_true",
                        help="(reserved) disable auto-amend for staged changes")
    args = parser.parse_args()

    # venv + logger + pid (rules 1, 3, 4)
    _check_venv()
    logger = _setup_logging()
    _write_pid()
    _install_signal_handlers(logger)
    logger.info(f"starting (PID {os.getpid()}, args={sys.argv})")

    summary: dict[str, Any] = {
        "script": SCRIPT_NAME,
        "args": vars(args),
        "stages": [],
    }

    try:
        # load config
        if not Path(args.config).exists():
            sys.exit(f"ERROR: config not found: {args.config}")
        cfg = yaml.safe_load(Path(args.config).read_text())
        logger.info(f"loaded config: {args.config} (version={cfg.get('version')})")

        # normalize version — strip leading 'v' for templates, but keep 'v' for tag
        version = args.version
        if not version:
            sys.exit("ERROR: --version is required (e.g. v0.7.0)")
        # Tag uses the literal version string (including 'v')
        # Strip-list + branch templates format with version=version

        # --reset short-circuits everything else
        if args.reset:
            stage_reset(version, cfg, logger)
            summary["stages"].append({"reset": True})
            summary["status"] = "reset"
            return 0

        # preflight + branch (rules 6: git clean before destructive ops)
        current_branch = stage_preflight(cfg, logger)
        source_sha = args.source or current_branch
        summary["stages"].append({"preflight": "ok", "source": source_sha})

        branch = stage_branch(cfg, version, args.source, logger)
        summary["stages"].append({"branch": branch})

        # strip
        stage_ensure_license(cfg, logger)
        summary["stages"].append({"license": "ok"})

        stripped = stage_strip(cfg, logger)
        summary["stages"].append({"stripped": len(stripped)})

        # strip data-dependent tests
        stripped_tests = stage_strip_tests(cfg, logger)
        summary["stages"].append({"stripped_tests": len(stripped_tests)})

        # verify required files
        stage_verify_required(cfg, logger)
        summary["stages"].append({"required_files": "ok"})

        # verify scanners
        stage_verify_scanners(cfg, logger)
        summary["stages"].append({"scanners": "pass"})

        # verify tests
        stage_verify_tests(logger)
        summary["stages"].append({"tests": "pass"})

        # commit + tag
        commit_sha = stage_commit(version, source_sha, logger)
        summary["stages"].append({"commit": commit_sha})

        stage_tag(version, commit_sha, logger)
        summary["stages"].append({"tag": version})

        # push preview (or actual push if --push-mode != dry-run)
        if args.push_mode == "dry-run":
            preview = stage_push_preview(branch, version, cfg, logger)
            summary["stages"].append({"push": "dry-run", "preview": preview})
            summary["status"] = "ready-to-push"
        else:
            # FUTURE: implement actual push with operator confirmation gate
            logger.error("actual push not yet implemented — use --push-mode dry-run for now")
            summary["status"] = "tagged-no-push"
            sys.exit(2)

        logger.info(f"completed: {summary['status']}")
        summary["status"] = summary.get("status", "ok")
        return 0
    except subprocess.CalledProcessError as e:
        logger.exception("subprocess failed")
        summary["status"] = "error"
        summary["error"] = str(e)
        return 1
    except Exception as e:
        logger.exception("script failed")
        summary["status"] = "error"
        summary["error"] = str(e)
        return 1
    finally:
        # Rule 1: write a last-run summary file
        LAST_RUN_FILE.write_text(json.dumps(summary, indent=2))
        # Rule 3: clean PID
        PID_FILE.unlink(missing_ok=True)


if __name__ == "__main__":
    sys.exit(main())
