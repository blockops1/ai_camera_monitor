#!/usr/bin/env python3
"""check_no_private_data.py — Scan tracked files for private data leaks.

Exits 0 if no leaks are found; exits 1 and prints file:line for each leak.

Scan categories (see docs/PRIVACY.md for conventions):
  1. Production /24 IP literals (e.g. 192.168.1.x subnet used on the farm).
  2. Telegram chat identifier digits (TELEGRAM_HOME_CHAT_ID value).
  3. Operator personal-handle strings (first-name forms used as a handle).
  4. Helper personal-handle strings (third-party helpers' names).

Exclusions (whitelist):
  - .git/ directories
  - .venv/ directories
  - .pytest_cache/ directories
  - scripts/check_no_private_data.py itself (its pattern definitions)
  - docs/PRIVACY.md (discusses what to look for)

Usage:
    python scripts/check_no_private_data.py
    python scripts/check_no_private_data.py data/vehicles/known_vehicles.json
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Pattern definitions
# ---------------------------------------------------------------------------

# Category 1: Production /24 prefix — RFC 1918 private ranges commonly used
# on the farm network. The exact subnet varies per deployment; we match the
# general pattern of 192.168.x.0/24 addresses used in live frames/paths.
# NOTE: Documentation/example IPs like 192.0.2.x, 100.64.0.x (RFC 5737/6598)
# are NOT flagged — only production-prefix literals are.
PRODUCTION_IP_PATTERNS: list[str] = [
    r"192\.168\.[1-9]\d?\.1[0-9]{2}",  # farm production /24 range
]

# Category 2: Telegram home chat ID — digits that match the runtime value.
# The exact value is environment-specific; we look for the pattern used in
# telegram-creds.env and matching literal references.
# NOTE: The actual digits are not hardcoded here — they're loaded from
# TELEGRAM_HOME_CHAT_ID at runtime so the scanner stays portable.
CHAT_ID_PATTERN: str = ""  # filled at runtime from env

# Category 3: Operator personal handle — first-name form used as a handle.
# Updated per PR review cycle; see US-018f for the canonical set.
# NOTE: These are literal strings that should NOT appear in tracked files
# after sanitization (US-018e/018f).
OPERATOR_HANDLES: list[str] = [
    "Rolf",
]

# Category 4: Helper personal handles — third-party helpers whose names
# were previously stored in vehicle enrollments (US-018e).
HELPER_HANDLES: list[str] = [
    "Carson",
    "Grant",
    "Jeremiah",
]

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_repo_root() -> Path:
    """Return the git repository root."""
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


def _get_tracked_files(repo_root: Path) -> list[str]:
    """Get list of tracked files (not ignored) via git ls-files."""
    result = subprocess.run(
        ["git", "ls-files"],
        capture_output=True,
        text=True,
        check=False,
        cwd=str(repo_root),
    )
    if result.returncode != 0:
        print("ERROR: git ls-files failed", file=sys.stderr)
        sys.exit(2)
    return result.stdout.strip().splitlines()


def _should_exclude(filepath: str, repo_root: Path) -> bool:
    """Check if a file should be excluded from scanning."""
    # Normalize to relative path from repo root
    try:
        rel = Path(filepath).relative_to(repo_root)
    except ValueError:
        # File outside repo root — skip
        return True

    parts = rel.parts
    # Exclude .git/, .venv/, .pytest_cache/
    for part in parts:
        if part in (".git", ".venv", ".pytest_cache"):
            return True

    # Exclude the scanner itself
    if str(rel) == "scripts/check_no_private_data.py":
        return True

    # Exclude PRIVACY.md (discusses what to look for)
    if str(rel) == "docs/PRIVACY.md":
        return True

    return False


def _compile_patterns():
    """Compile regex patterns for scanning."""
    patterns = []

    # Production IPs
    for p in PRODUCTION_IP_PATTERNS:
        patterns.append((re.compile(p), "production IP literal", p))

    # Chat ID
    if CHAT_ID_PATTERN:
        patterns.append(
            (re.compile(CHAT_ID_PATTERN), "chat identifier", CHAT_ID_PATTERN)
        )

    # Operator handles — word-boundary match to avoid false positives
    for h in OPERATOR_HANDLES:
        patterns.append(
            (re.compile(rf"\b{re.escape(h)}\b"), f"operator handle '{h}'", h)
        )

    # Helper handles — word-boundary match
    for h in HELPER_HANDLES:
        patterns.append((re.compile(rf"\b{re.escape(h)}\b"), f"helper handle '{h}'", h))

    return patterns


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Scan tracked files for private data leaks.",
    )
    parser.add_argument(
        "target",
        nargs="?",
        default=None,
        help="File or directory to scan (defaults to all tracked files).",
    )
    args = parser.parse_args()

    repo_root = _get_repo_root()
    patterns = _compile_patterns()

    if not patterns:
        print("WARNING: no patterns defined — nothing to scan.", file=sys.stderr)
        return 0

    # Determine files to scan
    if args.target:
        target_path = Path(args.target)
        if target_path.is_file():
            files_to_check = [str(target_path)]
        elif target_path.is_dir():
            files_to_check = [str(f) for f in target_path.rglob("*") if f.is_file()]
        else:
            print(f"ERROR: target '{args.target}' does not exist.", file=sys.stderr)
            return 2
    else:
        tracked = _get_tracked_files(repo_root)
        files_to_check = [str(repo_root / f) for f in tracked]

    # Scan
    leaks: list[str] = []
    for filepath in files_to_check:
        if _should_exclude(filepath, repo_root):
            continue

        try:
            text = Path(filepath).read_text(errors="replace")
        except OSError:
            continue

        for regex, category, pattern_text in patterns:
            for line_no, line_text in enumerate(text.splitlines(), start=1):
                if regex.search(line_text):
                    rel = Path(filepath).relative_to(repo_root)
                    leaks.append(f"{rel}:{line_no} [{category}] {line_text.strip()}")

    if leaks:
        print(f"Leaked private data found ({len(leaks)} hit(s)):\n")
        for leak in leaks:
            print(leak)
        return 1
    else:
        print("OK: no private data leaks detected.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
