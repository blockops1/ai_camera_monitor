#!/usr/bin/env python3
"""check_no_jpeg.py -- Scan production source for JPEG references.

Exits 0 if no JPEG references are found; exits 1 and prints file:line
for each reference.

Scan targets (production source tree only):
  infra/  listener/  vehicle_position/  telegram_formatter/

Patterns (hardcoded, no env dependency):
  format='JPEG'  format="JPEG"      -- explicit JPEG format argument
  '.jpg'  ".jpg" '.jpeg' ".jpeg"     -- JPEG file extensions
  _tiny_jpeg(                          -- hardcoded JPEG blob helper

Exclusions:
  - tests/, models/, scripts/, docs/, archive/, data/
  - .venv/, .git/
  - _b64() mime-fallback at infra/vision_analyzer.py:111
    (defensive: image/jpeg is a non-PNG fallback, not a JPEG producer)

Usage:
    python scripts/check_no_jpeg.py
    python scripts/check_no_jpeg.py infra/
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Patterns to flag
# ---------------------------------------------------------------------------

_JPEG_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"format\s*=\s*['\"]JPEG['\"]"), "JPEG format argument"),
    (re.compile(r"[\"'][^\"]*\.jpe?g[^\"]*[\"']"), "JPEG file extension"),
    (re.compile(r"\b_tiny_jpeg\b"), "JPEG blob helper"),
]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_SCAN_DIRS: tuple[str, ...] = (
    "infra",
    "listener",
    "vehicle_position",
    "telegram_formatter",
)

_EXCLUDE_DIRS: set[str] = {
    "tests",
    "models",
    "scripts",
    "docs",
    "archive",
    "data",
    ".venv",
    ".git",
    "__pycache__",
}

# Known false positives: (relative_path, line_number)
_KNOWN_FP: set[tuple[str, int]] = {
    ("infra/vision_analyzer.py", 111),   # mime fallback, not JPEG producer
    ("telegram_formatter/dispatcher.py", 38),  # _sniff_mime docstring mentions .jpg
    ("telegram_formatter/dispatcher.py", 49),  # _sniff_mime returns image/jpeg, .jpg for JPEG files
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_repo_root() -> Path:
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        print("ERROR: not a git repository (or git not available)", file=sys.stderr)
        sys.exit(2)
    return Path(result.stdout.strip())


def _scan_file(filepath: Path, repo_root: Path) -> list[str]:
    """Return list of 'file:line [category] line' matches for filepath."""
    try:
        rel = str(filepath.relative_to(repo_root))
    except ValueError:
        # File outside repo (e.g. /tmp/ for manual testing) — use basename.
        rel = filepath.name
    findings: list[str] = []
    try:
        text = filepath.read_text(errors="replace")
    except OSError:
        return findings

    for line_no, line in enumerate(text.splitlines(), start=1):
        if (rel, line_no) in _KNOWN_FP:
            continue
        for pattern, category in _JPEG_PATTERNS:
            if pattern.search(line):
                findings.append(f"{rel}:{line_no} [{category}] {line.strip()}")
                break  # one finding per line
    return findings


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Scan production source for JPEG references."
    )
    parser.add_argument(
        "target",
        nargs="?",
        default=None,
        help="Directory to scan (defaults to production source tree).",
    )
    args = parser.parse_args()

    repo_root = _get_repo_root()

    if args.target:
        target = Path(args.target)
        if target.is_dir():
            scan_dirs = [target]
        elif target.is_file():
            scan_dirs = []
            # Single file: just check it (works even if outside repo)
            findings = _scan_file(target, repo_root)
            if findings:
                print(f"JPEG reference found ({len(findings)} hit(s)):\n")
                for f in findings:
                    print(f)
                return 1
            print("OK: no JPEG references found.")
            return 0
        else:
            print(f"ERROR: '{args.target}' does not exist.", file=sys.stderr)
            return 2
    else:
        scan_dirs = [repo_root / d for d in _SCAN_DIRS]

    # Walk each target directory
    all_findings: list[str] = []
    for scan_dir in scan_dirs:
        if not scan_dir.is_dir():
            continue
        for filepath in sorted(scan_dir.rglob("*.py")):
            # Check exclusions via path parts
            rel = str(filepath.relative_to(repo_root))
            parts = Path(rel).parts
            if any(p in _EXCLUDE_DIRS for p in parts):
                continue
            all_findings.extend(_scan_file(filepath, repo_root))

    if all_findings:
        print(f"JPEG reference found ({len(all_findings)} hit(s)):\n")
        for f in all_findings:
            print(f)
        return 1
    else:
        print("OK: no JPEG references found.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
