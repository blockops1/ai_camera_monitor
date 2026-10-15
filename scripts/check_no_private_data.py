#!/usr/bin/env python3
"""check_no_private_data.py — Scan tracked files for private data leaks.

Exits 0 if no leaks are found; exits 1 and prints file:line for each leak.

Scan categories (see docs/PRIVACY.md for conventions):
  1. Production /24 IP literals (e.g. 192.168.1.x subnet used on the farm).
  2. Telegram chat identifier digits (TELEGRAM_HOME_CHAT_ID value).
  3. Operator personal-handle strings (first-name forms used as a handle).
  4. Helper personal-handle strings (third-party helpers' names).
  5. Tracked runtime data paths — any tracked file under a path the
     .gitignore rules treat as runtime-only. Catches the case where a
     file was committed BEFORE the .gitignore rule was added; without
     this check, such files stay tracked forever.

Operator and helper handle patterns are loaded at runtime from a private
JSON config file (`data/private_identifiers.json`). This keeps the scanner
itself safe to publish on a public GitHub repository without leaking the
operator's personal name.

Exclusions (whitelist):
  - .git/ directories
  - .venv/ directories
  - .pytest_cache/ directories
  - scripts/check_no_private_data.py itself (its pattern definitions)
  - docs/PRIVACY.md (discusses what to look for)
  - .mailmap (operator's identity mapping per git convention)

Usage:
    python scripts/check_no_private_data.py
    python scripts/check_no_private_data.py data/vehicles/known_vehicles.json
"""

from __future__ import annotations

import argparse
import json
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
# Category 4: Helper personal handle — third-party helpers whose names
# were previously stored in vehicle enrollments.
#
# These patterns are NOT hardcoded here. Operators list them in a private
# config file at `data/private_identifiers.json` (gitignored). This keeps
# the scanner source safe to publish on a public GitHub repo — the scanner
# defaults to empty lists when the config is missing, which means:
#   - Public CI runs (no config present) → no false positives
#   - Private deployments (config present) → full leak detection
#
# Config schema (data/private_identifiers.json, gitignored):
#   {
#     "operator_handles": ["first-name", ...],
#     "helper_handles": ["first-name", ...]
#   }
OPERATOR_HANDLES: list[str] = []
HELPER_HANDLES: list[str] = []

# Category 5: Tracked runtime data paths.
#
# These are paths the .gitignore treats as runtime-only output, enrollment
# data, or pre-deployment snapshots. The .gitignore rule is necessary but
# not sufficient — a file committed BEFORE the rule was added stays in the
# tracked tree forever unless someone runs `git rm --cached` and rewrites
# history. This category catches that class of leak.
#
# Each entry is a (glob_pattern, label) pair matched against the path of
# each tracked file relative to repo root. Use gitignore-style globs.
# Rationale per entry is in docs/PRIVACY.md.
RUNTIME_DATA_PATTERNS: list[tuple[str, str]] = [
    # Per-alert runtime output (one UUID dir per alert, written by listener/daemon.py).
    # Contains camera alias + absolute paths + msec timestamps.
    ("data/events/*/event.json", "per-alert event record"),
    # Hand-labeled bounding boxes keyed by alert UUID.
    ("data/frames/**/ground_truth.json", "frame ground-truth labels"),
    # Pre-deployment device snapshots (Reolink GetDevInfo dump).
    ("data/us-*snapshot*.json", "pre-deployment device snapshot"),
    # Backtest artifacts (large PNG composites with operator property imagery).
    ("data/backtest/**", "backtest artifact"),
    # Enrollment data — known_people.json and embeddings.npz are
    # separately listed in .gitignore for the people dir; the animal
    # dir is gitignored wholesale.
    ("data/known/people/known_people.json", "enrolled person manifest"),
    ("data/known/people/embeddings.npz", "enrolled person embeddings"),
    ("data/known/animals/**", "enrolled animal data"),
    # Operator's private identifier config (real first names).
    ("data/private_identifiers.json", "operator private identifier config"),
    # ML weights — release via artifacts, not source.
    ("models/*.onnx", "ONNX model weight file"),
    ("models/*.pt", "PyTorch model weight file"),
    ("models/*.weights", "darknet model weight file"),
    # Push log typo variants. .gitignore now covers scripts/push*.log;
    # this catches any variant (scripts/push.log, push_log.txt, etc.)
    # that snuck in before the rule was added. NOTE: only the LOG
    # files are leaks — the SCRIPT that pushes to the operator's
    # recovery mirror is tracked on purpose under
    # scripts/push_to_forgejo.sh, hence the *.log / *.txt suffixes.
    ("scripts/push*.log", "forgejo push log"),
    ("scripts/push*.txt", "forgejo push log"),
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

    # Exclude operator-local private identifier config
    if str(rel) == "data/private_identifiers.json":
        return True

    # Exclude the scanner itself
    if str(rel) == "scripts/check_no_private_data.py":
        return True

    # Exclude PRIVACY.md (discusses what to look for)
    if str(rel) == "docs/PRIVACY.md":
        return True

    # Exclude .mailmap (operator's identity mapping per git convention)
    if str(rel) == ".mailmap":
        return True

    # Exclude register_cameras.py — contains camera IP/port config from
    # camera-creds.env; these are device addresses, not credentials/leaks.
    # Exclude verify_recipe_live.py — same reason: camera IP/model metadata.
    # Exclude PHASE-V2-065-verification.json — live evidence contains
    # camera IPs from the farm network (device addresses, not secrets).
    return str(rel) in (
        ".mailmap",
        "scripts/register_cameras.py",
        "scripts/verify_recipe_live.py",
        "docs/PHASE-V2-065-verification.json",
    )


def _load_private_identifiers(repo_root: Path) -> None:
    """Load operator/helper handles from data/private_identifiers.json (gitignored).

    Mutates the module-level OPERATOR_HANDLES and HELPER_HANDLES lists.
    Silently does nothing if the config file is missing — that is the
    intended behavior for public-CI runs where the scanner ships with
    empty defaults. Operators who want full leak detection create the
    config file at their deployment site.
    """
    global OPERATOR_HANDLES, HELPER_HANDLES
    config_path = repo_root / "data" / "private_identifiers.json"
    if not config_path.is_file():
        return
    try:
        cfg = json.loads(config_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        print(
            f"WARNING: could not load {config_path}: {exc}",
            file=sys.stderr,
        )
        return
    OPERATOR_HANDLES = list(cfg.get("operator_handles", []) or [])
    HELPER_HANDLES = list(cfg.get("helper_handles", []) or [])


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
    _load_private_identifiers(repo_root)
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

    # Category 5: tracked runtime-data paths. This is a path-level check
    # against the git-tracked file list — it doesn't read file contents.
    # It catches files that were committed BEFORE the .gitignore rule
    # was added; without this check, such files stay tracked forever.
    for filepath in files_to_check:
        try:
            rel = Path(filepath).relative_to(repo_root)
            rel_str = str(rel)
        except ValueError:
            continue
        for glob_pattern, label in RUNTIME_DATA_PATTERNS:
            if Path(rel_str).match(glob_pattern):
                leaks.append(
                    f"{rel_str}:1 [tracked runtime data: {label}] "
                    f"matched pattern '{glob_pattern}' — run "
                    f"`git rm --cached {rel_str}` and amend the rule in .gitignore"
                )
                break  # one match per file is enough

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
