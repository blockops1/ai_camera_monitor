#!/usr/bin/env python3
"""audit_orphans.py — One-shot orphan audit with verbose module-by-module report.

Runs symbol+cross-reference logic on every production .py file, prints a
full module-by-module report, and also detects module-level imports
(e.g. ``from infra.paths import FRAMES_DIR``) so modules that export
constants rather than functions don't get falsely flagged.

Exits 0 if zero orphans are found; exits 1 and prints an orphans summary.
"""
from __future__ import annotations

import ast
import re
import subprocess
import sys
from pathlib import Path

# Production dirs (same as check_no_orphan_modules.py)
PRODUCTION_DIRS: list[str] = [
    "infra",
    "listener",
    "telegram_formatter",
    "vehicle_matcher",
]


def _get_repo_root() -> Path:
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        return Path.cwd()
    return Path(result.stdout.strip())


def _collect_py_files(repo_root: Path) -> list[Path]:
    """Walk production dirs, return non-init .py files."""
    files: list[Path] = []
    for d in PRODUCTION_DIRS:
        dir_path = repo_root / d
        if not dir_path.is_dir():
            continue
        for fname in sorted(dir_path.rglob("*.py")):
            if fname.name != "__init__.py":
                files.append(fname)
    return sorted(files)


def _get_public_symbols(path: Path) -> list[str]:
    """Return top-level function and class names."""
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


def _count_xrefs(symbol: str, repo_root: Path, module_path: Path) -> int:
    """Count cross-references for one symbol across all other production files."""
    escaped = re.escape(symbol)
    pattern = re.compile(r"\b" + escaped + r"\b")
    module_abspath = str(module_path.resolve())
    total = 0
    for d in PRODUCTION_DIRS:
        dir_path = repo_root / d
        if not dir_path.is_dir():
            continue
        for fname in sorted(dir_path.rglob("*.py")):
            if str(fname.resolve()) == module_abspath:
                continue
            try:
                with open(fname, "r", encoding="utf-8") as f:
                    if pattern.search(f.read()):
                        total += 1
            except (UnicodeDecodeError, OSError):
                continue
    return total


def _check_module_imported(module_path: Path, repo_root: Path) -> int:
    """Check if this module (by import path) is imported anywhere else.

    Returns count of other files that import this module (``from X import ...``
    or ``import X``) via any of its dotted import paths.
    """
    # Derive the import path from the file's position in production dirs
    rel = module_path.relative_to(repo_root)  # e.g. infra/paths.py
    parts = list(rel.parts)
    parts[-1] = parts[-1].rsplit(".", 1)[0]  # strip .py suffix
    import_paths = []
    # All prefix combos: infra.paths, infra (when from infra import paths)
    for i in range(1, len(parts) + 1):
        import_paths.append(".".join(parts[:i]))
    # Also the last part alone: "paths" (for ``from infra import paths``)
    import_paths.append(parts[-1])

    module_abspath = str(module_path.resolve())
    total = 0
    for d in PRODUCTION_DIRS:
        dir_path = repo_root / d
        if not dir_path.is_dir():
            continue
        for fname in sorted(dir_path.rglob("*.py")):
            if str(fname.resolve()) == module_abspath:
                continue
            try:
                with open(fname, "r", encoding="utf-8") as f:
                    content = f.read()
                for imp in import_paths:
                    # Match: from X import ...  or  import X  or  import X.Y
                    for pat in [
                        re.compile(r"\bfrom\s+" + re.escape(imp) + r"\b"),
                        re.compile(r"\bimport\s+" + re.escape(imp) + r"\b"),
                    ]:
                        if pat.search(content):
                            total += 1
                            break
            except (UnicodeDecodeError, OSError):
                continue
    return total


def _run_coverage(repo_root: Path) -> None:
    """Print a coverage report sorted by coverage%, or skip if coverage is missing."""
    try:
        import coverage as cov_mod
        cov = cov_mod.Coverage(source=["infra", "listener", "telegram_formatter", "vehicle_matcher"])
        cov.erase()
        cov.run(["-m", "pytest", "tests/", "--deselect=tests/test_home_env.py::TestLoadHomeEnv::test_telegram_token_length_revealed_not_value"])
        print()
        cov.report(sort="cover")
    except ModuleNotFoundError:
        print("coverage not installed — skip coverage report.")


def main(argv: list[str] | None = None) -> int:
    repo_root = _get_repo_root()
    py_files = _collect_py_files(repo_root)

    print("=" * 72)
    print("ORPHAN AUDIT — verbose module report")
    print(f"Repo root: {repo_root}")
    print(f"Production dirs: {PRODUCTION_DIRS}")
    print(f"Modules scanned: {len(py_files)}")
    print("=" * 72)

    orphans: list[dict] = []

    for fpath in py_files:
        symbols = _get_public_symbols(fpath)
        sym_count = len(symbols)

        xref_counts: list[tuple[str, int]] = []
        for sym in symbols:
            refs = _count_xrefs(sym, repo_root, fpath)
            xref_counts.append((sym, refs))

        total_sym_xrefs = sum(r for _, r in xref_counts)
        module_imports = _check_module_imported(fpath, repo_root)
        total_xrefs = total_sym_xrefs + module_imports
        status = "ORPHAN" if total_xrefs == 0 else "linked"

        if total_xrefs == 0:
            orphans.append({
                "path": str(fpath),
                "symbols": symbols,
                "sym_xrefs": total_sym_xrefs,
                "module_imports": 0,
            })

        print(f"\n  {fpath.name:<30} [{status:>6}]  symbols={sym_count:>2}  sym_xrefs={total_sym_xrefs:>3}  mod_imports={module_imports:>3}")
        for sym, refs in xref_counts:
            marker = " <-- no cross-refs" if refs == 0 else ""
            print(f"    - {sym:<35} xrefs={refs:>3}{marker}")
        if module_imports > 0:
            print(f"    [module-level imports detected: {module_imports}]")

    print("\n" + "=" * 72)
    print(f"TOTAL ORPHANS: {len(orphans)}")

    if orphans:
        print("\nOrphan modules:")
        for o in orphans:
            print(f"  - {o['path']} ({len(o['symbols'])} symbols, 0 cross-references)")
        return 1

    # Coverage summary
    print("\n" + "=" * 72)
    print("COVERAGE SUMMARY")
    _run_coverage(repo_root)
    print("=" * 72)
    return 0


if __name__ == "__main__":
    sys.exit(main())
