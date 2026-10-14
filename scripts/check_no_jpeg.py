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
  - _sniff_mime() byte-sniffer in telegram_formatter/dispatcher.py
    (defensive: detects JPG/JPEG magic bytes for send_media_group fallback,
     does not produce JPEGs. Excluded by function name to survive line drift.)

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

# Known false positives.
#
# Line-number-based exclusions (fragile across edits — keep small):
_KNOWN_FP: set[tuple[str, int]] = {
    # V2-030 EXCEPTION: Telegram photo delivery uses JPEG q88 1920px.
    # Authorized by operator (Rolfe) on 2026-09-13: this is the one permitted
    # JPEG producer path. Compression + cache + retention are wired per the
    # V2-030 PRD. Must not be re-flagged by this scanner.
    ("telegram_formatter/codec.py", 93),  # JPEG dst filename
    ("telegram_formatter/codec.py", 94),  # JPEG save call
}

# Function-name-based exclusions (durable across line drift):
# Excludes every line INSIDE the named function (detected by a `def name(` line
# preceding the match and the next top-level `def ` or end-of-file after it).
_KNOWN_FP_BY_FUNCTION: set[tuple[str, str]] = {
    # _b64() in vision_analyzer.py: image/jpeg is a non-PNG fallback for
    # vision responses, not a JPEG producer. Defensive only.
    ("infra/vision_analyzer.py", "_b64"),
    # _sniff_mime() in dispatcher.py: detects JPG/JPEG magic bytes for the
    # send_media_group fallback path. Does not produce JPEGs.
    ("telegram_formatter/dispatcher.py", "_sniff_mime"),
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


def _function_line_ranges(text: str) -> dict[str, list[tuple[int, int]]]:
    """Return {function_name: [(start_line, end_line), ...]} for top-level defs.

    Top-level = column 0 (no indentation). Inner defs (inside classes or
    functions) are NOT tracked — the `_KNOWN_FP_BY_FUNCTION` set should
    only reference top-level functions whose names are stable.
    """
    lines = text.splitlines()
    starts: list[tuple[int, str]] = []
    for i, line in enumerate(lines, start=1):
        if line.startswith("def ") and "(" in line:
            # extract name
            name = line[4:line.index("(")].strip()
            starts.append((i, name))
    # find end of each function: next top-level def or EOF
    ranges: dict[str, list[tuple[int, int]]] = {}
    for idx, (start_line, name) in enumerate(starts):
        if idx + 1 < len(starts):
            end_line = starts[idx + 1][0] - 1
        else:
            end_line = len(lines)
        ranges.setdefault(name, []).append((start_line, end_line))
    return ranges


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

    # Build function-line ranges once for this file
    func_ranges = _function_line_ranges(text)
    # Map (rel, func_name) -> list of (start, end) ranges to skip
    skip_ranges: list[tuple[int, int]] = []
    for path, func_name in _KNOWN_FP_BY_FUNCTION:
        if path == rel and func_name in func_ranges:
            for start, end in func_ranges[func_name]:
                skip_ranges.append((start, end))

    def _in_skip_range(line_no: int) -> bool:
        return any(start <= line_no <= end for start, end in skip_ranges)

    for line_no, line in enumerate(text.splitlines(), start=1):
        if (rel, line_no) in _KNOWN_FP:
            continue
        if _in_skip_range(line_no):
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
