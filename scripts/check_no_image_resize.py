#!/usr/bin/env python3
"""check_no_image_resize.py -- Static scanner for image resize calls.

Walks the production source tree (infra/, listener/, vehicle_matcher/,
telegram_formatter/) and flags any match against the regex for image
resize operations: dot-resize, cv2-dot-resize, letterbox, and
Image-dot-Resampling-dot-LANCZOS.

Comment lines (starting with #) and lines inside triple-quoted
docstrings are excluded so descriptive references to these terms do
not trigger false positives.

Exclusions:
  - tests/
  - models/
  - scripts/
  - docs/
  - archive/
  - data/
  - .venv/
  - .git/

Known false positives (intentional resize/encode operations):
  - telegram_formatter/codec.py:86 — US-030a: img.resize() for JPEG downscale

Usage:
    python scripts/check_no_image_resize.py
    python scripts/check_no_image_resize.py infra/
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Pattern definitions
# ---------------------------------------------------------------------------

_RESIZE_PATTERNS: list[str] = [
    r"\.(resize|thumbnail)\s*\(",  # .resize() .thumbnail(
    r"cv2\.resize",  # cv2.resize(...)
    r"\bletterbox\b",  # letterbox (standalone word)
    r"Image\.Resampling\.LANCZOS",  # PIL LANCZOS constant
]

# Pre-compile once at module level.
_COMPILED: list[tuple[re.Pattern, str]] = [(re.compile(p), p) for p in _RESIZE_PATTERNS]

# Production source directories (relative to repo root).
_PROD_DIRS: list[str] = [
    "infra",
    "listener",
    "vehicle_matcher",
    "telegram_formatter",
]

# Directories to skip entirely.
_SKIP_PARTS: tuple[str, ...] = (
    ".git",
    ".venv",
    "tests",
    "models",
    "scripts",
    "docs",
    "archive",
    "data",
    "__pycache__",
)

# Known false positives: (relative_path, line_number) — intentional ops.
_KNOWN_FP: set[tuple[str, int]] = {
    ("telegram_formatter/codec.py", 86),  # US-030a: img.resize() for JPEG downscale
}


def _should_skip_dir(dirpath: str) -> bool:
    """Return True if any component of the path is in the skip set."""
    return any(part in _SKIP_PARTS for part in dirpath.split("/"))


def scan_file(filepath: Path, repo_root: Path) -> list[str]:
    """Scan a single file for resize patterns outside comments.

    Returns a list of ``file:line: match`` strings for every hit.
    """
    hits: list[str] = []
    try:
        text = filepath.read_text(errors="replace")
    except OSError:
        return hits

    try:
        rel = str(filepath.relative_to(repo_root))
    except ValueError:
        rel = filepath.name

    lines = text.splitlines()

    # For .py files, skip comment lines, blank lines, and lines inside
    # triple-quoted docstrings (which cannot contain actual resize calls
    # but may reference terms like "letterbox" in descriptions).
    active_only = filepath.suffix == ".py"

    in_triple: str | None = None  # tracks open """ or '''
    active_lines: set[int] = set()

    if active_only:
        for idx, line in enumerate(lines):
            stripped = line.strip()
            if in_triple is None:
                if stripped.startswith(('"""', "'''")):
                    quote = stripped[:3]
                    rest = stripped[3:]
                    if quote in rest:
                        # Opens and closes on same line -- not a
                        # multi-line docstring.
                        continue
                    else:
                        in_triple = quote
                        continue
                # Normal line: skip if comment or blank.
                if stripped == "" or stripped.startswith("#"):
                    continue
            else:
                # Inside a multi-line string -- skip until closing.
                if in_triple in stripped:
                    in_triple = None
                continue
            active_lines.add(idx)

    for line_no, line in enumerate(lines, start=1):
        if active_only and (line_no - 1) not in active_lines:
            continue

        # Check known false positives.
        if (rel, line_no) in _KNOWN_FP:
            continue

        for pattern, pattern_name in _COMPILED:
            if pattern.search(line):
                hits.append(f"{rel}:{line_no}: {pattern_name!r} matched")

    return hits


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Scan production source tree for image resize calls.",
    )
    parser.add_argument(
        "target",
        nargs="?",
        default=None,
        help="File or directory to scan (defaults to production tree).",
    )
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parent.parent

    files_to_check: list[Path] = []

    if args.target:
        target = Path(args.target)
        if target.is_file():
            files_to_check = [target]
        elif target.is_dir():
            for fpath in target.rglob("*"):
                if fpath.suffix == ".py" and not _should_skip_dir(
                    str(fpath.relative_to(repo_root))
                ):
                    files_to_check.append(fpath)
    else:
        for dname in _PROD_DIRS:
            dpath = repo_root / dname
            if not dpath.is_dir():
                continue
            for fpath in dpath.rglob("*.py"):
                rel = fpath.relative_to(repo_root)
                if _should_skip_dir(str(rel)):
                    continue
                files_to_check.append(fpath)

    all_hits: list[str] = []
    for filepath in files_to_check:
        all_hits.extend(scan_file(filepath, repo_root))

    if all_hits:
        print(f"Image resize references found ({len(all_hits)} hit(s)):\n")
        for hit in all_hits:
            print(hit)
        return 1
    else:
        print("OK: no image resize references detected in production source.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
