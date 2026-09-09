#!/usr/bin/env python3
"""check_no_orphan_modules.py — Scan for orphan production modules.

A production module is "orphaned" when none of its public symbols are
referenced by any other production file. This scanner walks the production
source tree, collects public symbols (top-level functions and classes) from
each .py file, then checks whether at least one symbol is imported or used
by a file outside its own package subtree.

Exits 0 (clean) if no orphans are found; exits 1 and prints one line per
orphan:

    orphan module: <path> (<symbol_count> symbols, 0 cross-references)

Exclusions (directories never scanned):
    tests/, docs/, scripts/, models/, archive/, data/, .venv/, .git/
"""

from __future__ import annotations

import argparse
import ast
import os
import re
import subprocess
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

PRODUCTION_DIRS: list[str] = [
    "infra",
    "listener",
    "telegram_formatter",
    "vehicle_matcher",
]

EXCLUDE_DIRS: set[str] = {
    "tests",
    "docs",
    "scripts",
    "models",
    "archive",
    "data",
    ".venv",
    ".git",
    "logs",
    "config",
    "venv",
}


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
        return Path.cwd()
    return Path(result.stdout.strip())


def _get_production_py_files(repo_root: Path) -> list[Path]:
    """Walk production dirs and return .py files, skipping __init__.py."""
    files: list[Path] = []
    for d in PRODUCTION_DIRS:
        dir_path = repo_root / d
        if not dir_path.is_dir():
            continue
        for fname in sorted(dir_path.iterdir()):
            if fname.is_file() and fname.suffix == ".py" and fname.name != "__init__.py":
                files.append(fname)
    return sorted(files)


def _get_public_symbols(path: Path) -> list[str]:
    """Parse a .py file and return top-level function and class names."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            tree = ast.parse(f.read(), filename=str(path))
    except (SyntaxError, UnicodeDecodeError):
        return []

    return [
        node.name
        for node in ast.iter_child_nodes(tree)
        if isinstance(node, (ast.FunctionDef, ast.ClassDef))
    ]


def _get_all_production_files(repo_root: Path) -> list[Path]:
    """Return ALL .py files in production dirs (including __init__.py)."""
    files: list[Path] = []
    for d in PRODUCTION_DIRS:
        dir_path = repo_root / d
        if not dir_path.is_dir():
            continue
        for fname in sorted(dir_path.rglob("*.py")):
            files.append(fname)
    return sorted(files)


def _get_all_other_production_files(repo_root: Path, module_path: Path) -> list[Path]:
    """Return production .py files that are NOT in the same top-level dir as module_path."""
    module_dir = str(module_path.parent.name)  # e.g. "infra"
    all_files = _get_all_production_files(repo_root)
    return [
        f for f in all_files
        if str(f.parent.name) != module_dir
    ]


def _count_cross_references(
    module_path: Path,
    symbols: list[str],
    other_files: list[Path],
) -> int:
    """Count how many of the module's public symbols appear in other files."""
    if not symbols:
        return 0

    # Build a regex that matches any of the public symbols as a whole-word
    # occurrence (import, usage, etc.), but NOT as a substring of another name.
    escaped = [re.escape(s) for s in symbols]
    pattern = re.compile(r"\b(?:" + "|".join(escaped) + r")\b")

    total_refs = 0
    for fpath in other_files:
        try:
            with open(fpath, "r", encoding="utf-8") as f:
                content = f.read()
        except (UnicodeDecodeError, OSError):
            continue
        if pattern.search(content):
            total_refs += 1

    return total_refs


# ---------------------------------------------------------------------------
# Core scan
# ---------------------------------------------------------------------------


def check_no_orphans(repo_root: Path | None = None) -> list[tuple[str, int, int]]:
    """Run the orphan scan. Returns list of (path, sym_count, xref_count) orphans."""
    if repo_root is None:
        repo_root = _get_repo_root()

    py_files = _get_production_py_files(repo_root)
    all_files = _get_all_production_files(repo_root)

    orphans: list[tuple[str, int, int]] = []

    for module_path in py_files:
        symbols = _get_public_symbols(module_path)
        if not symbols:
            # Module has no public symbols — it's effectively empty,
            # which is a form of orphan (nothing to import).
            orphans.append((str(module_path), 0, 0))
            continue

        other_files = [f for f in all_files if str(f.parent.name) != module_path.parent.name]

        if not other_files:
            # Only one production dir — every module is potentially cross-referenced.
            # Still do the cross-ref check against all files.
            pass

        xrefs = _count_cross_references(module_path, symbols, other_files)

        if xrefs == 0:
            orphans.append((str(module_path), len(symbols), 0))

    return orphans


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    """Entry point. Parse args, scan, report."""
    parser = argparse.ArgumentParser(
        description="Detect orphan production modules with no external references."
    )
    parser.add_argument(
        "--repo-root",
        type=str,
        help="Override the git repo root (default: auto-detect).",
    )
    args = parser.parse_args(argv)

    repo_root = Path(args.repo_root) if args.repo_root else _get_repo_root()
    orphans = check_no_orphans(repo_root)

    if orphans:
        for path, sym_count, xref_count in orphans:
            print(f"orphan module: {path} ({sym_count} symbols, {xref_count} cross-references)")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
