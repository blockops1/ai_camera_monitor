#!/usr/bin/env python3
"""check_no_orphan_modules.py -- Static scanner for orphan production modules.

Walks the production source tree and flags any .py file that defines public
symbols (top-level functions and classes) but is not imported or referenced
by any file outside its own subtree.

A module is an *orphan* when every file that references its symbols lives
inside the same package -- no external caller exists.

Scan targets (production source tree only):
  infra/  listener/  vehicle_matcher/  telegram_formatter/

Exclusions:
  - tests/
  - models/
  - scripts/
  - docs/
  - archive/
  - data/
  - .venv/
  - .git/
  - __pycache__/

Usage:
    python scripts/check_no_orphan_modules.py
    python scripts/check_no_orphan_modules.py infra/
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
from pathlib import Path
from typing import NamedTuple

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


class ModuleInfo(NamedTuple):
    """Metadata about a single .py module."""

    path: Path  # relative to repo root
    symbols: set[str]  # public top-level function/class names


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Production source directories (relative to repo root).
# vehicle_position/ was removed in US-020b; it no longer exists.
_PROD_DIRS: tuple[str, ...] = (
    "infra",
    "listener",
    "vehicle_matcher",
    "telegram_formatter",
)

# Directories to skip entirely when walking.
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
    "_tmp_",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _should_skip_dir(rel_path: str) -> bool:
    """Return True if any component of the relative path is in the skip set."""
    return any(part in _SKIP_PARTS for part in rel_path.split("/"))


def collect_public_symbols(filepath: Path) -> set[str]:
    """Parse a .py file and collect its public top-level symbols.

    Returns a set of names defined at the top level that do NOT start with
    a single underscore (i.e. public functions and classes).
    """
    symbols: set[str] = set()
    try:
        source = filepath.read_text(errors="replace")
        tree = ast.parse(source, filename=str(filepath))
    except (SyntaxError, OSError):
        return symbols

    for node in ast.iter_child_nodes(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            name = node.name
            if not name.startswith("_"):
                symbols.add(name)

    return symbols


def find_cross_references(
    symbol: str, repo_root: Path, module_file: Path
) -> list[Path]:
    """Find files outside *module_file* that reference *symbol*.

    A module's "own subtree" is the file itself. Any reference from a
    different file in the production tree is a cross-reference.
    """
    hits: list[Path] = []
    pattern = re.compile(r"\b" + re.escape(symbol) + r"\b")
    for dname in _PROD_DIRS:
        dpath = repo_root / dname
        if not dpath.is_dir():
            continue
        for pyfile in dpath.rglob("*.py"):
            rel = str(pyfile.relative_to(repo_root))
            if _should_skip_dir(rel):
                continue
            # Exclude the module's own file.
            if pyfile == module_file:
                continue
            try:
                text = pyfile.read_text(errors="replace")
            except OSError:
                continue
            if pattern.search(text):
                hits.append(pyfile)
    return hits


def discover_modules(repo_root: Path) -> list[ModuleInfo]:
    """Walk production dirs and return ModuleInfo for every .py file."""
    modules: list[ModuleInfo] = []
    for dname in _PROD_DIRS:
        dpath = repo_root / dname
        if not dpath.is_dir():
            continue
        for pyfile in dpath.rglob("*.py"):
            rel = str(pyfile.relative_to(repo_root))
            if _should_skip_dir(rel):
                continue
            symbols = collect_public_symbols(pyfile)
            if symbols:
                modules.append(ModuleInfo(path=pyfile, symbols=symbols))
    return modules


def scan(repo_root: Path, target: Path | None = None) -> list[str]:
    """Run the orphan scanner. Return list of finding strings."""
    modules = discover_modules(repo_root)
    if not modules:
        return []

    findings: list[str] = []

    for mod in modules:
        # Check each symbol for cross-references outside the module's own file.
        external_refs: set[str] = set()
        for symbol in mod.symbols:
            refs = find_cross_references(symbol, repo_root, mod.path)
            if refs:
                external_refs.add(symbol)

        # If NO symbol has external references, flag the module.
        if not external_refs:
            rel_path = mod.path.relative_to(repo_root)
            findings.append(
                f"orphan module: {rel_path} "
                f"({len(mod.symbols)} symbols, 0 cross-references)"
            )

    return findings


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Scan production source tree for orphan modules."
    )
    parser.add_argument(
        "target",
        nargs="?",
        default=None,
        help="Directory to scan (defaults to production source tree).",
    )
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parent.parent

    if args.target:
        target = Path(args.target)
        if target.is_dir():
            # Override production dirs for targeted scans.
            global _PROD_DIRS
            _PROD_DIRS = (target.name,)
            repo_root = target.parent

    findings = scan(repo_root)

    if findings:
        print(f"Orphan modules found ({len(findings)} hit(s)):\n")
        for f in findings:
            print(f)
        return 1
    else:
        print("OK: no orphan production modules found.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
