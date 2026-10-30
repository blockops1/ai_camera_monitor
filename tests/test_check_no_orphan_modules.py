"""test_check_no_orphan_modules.py -- Regression scanner for orphan modules.

Verifies that scripts/check_no_orphan_modules.py produces zero matches on
the current v2 production tree (after US-020a/US-020b cleanup) and can
detect synthetic orphan modules.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

# Import the scanner module so we can call its functions directly.
# The scanner lives one directory up from tests/.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from check_no_orphan_modules import collect_public_symbols, discover_modules, scan


@pytest.fixture(scope="module")
def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def modules_on_clean_tree(repo_root: Path) -> list:
    """Pre-collect modules on the clean tree for repeated tests."""
    return discover_modules(repo_root)


class TestScannerPatterns:
    """Verify the scanner correctly collects public symbols."""

    def test_collects_top_level_functions(self, repo_root: Path):
        """Public top-level functions should be collected."""
        mod_path = repo_root / "infra" / "animal_prompt.py"
        syms = collect_public_symbols(mod_path)
        assert "build_animal_prompt" in syms

    def test_collects_top_level_classes(self, repo_root: Path):
        """Public top-level classes should be collected."""
        # infra/gate.py defines the gate runner
        mod_path = repo_root / "infra" / "gate.py"
        syms = collect_public_symbols(mod_path)
        # gate.py has several public symbols
        assert len(syms) > 0

    def test_excludes_private_symbols(self, repo_root: Path):
        """Private symbols (prefixed with _) should not be collected."""
        # Create a temp file with mixed symbols
        tmp = repo_root / "_tmp_orphan_test_symbols.py"
        tmp.write_text("""
def public_fn():
    pass

def _private_fn():
    pass

class PublicClass:
    pass

class _PrivateClass:
    pass

class __DunderClass:
    pass
""")
        try:
            syms = collect_public_symbols(tmp)
            assert "public_fn" in syms
            assert "PublicClass" in syms
            assert "_private_fn" not in syms
            assert "_PrivateClass" not in syms
        finally:
            tmp.unlink()

    def test_returns_empty_on_syntax_error(self):
        """Malformed .py files should return empty, not crash."""
        tmp = Path("/tmp/_bad_module.py")
        tmp.write_text("def broken(\n")
        try:
            syms = collect_public_symbols(tmp)
            assert syms == set()
        finally:
            tmp.unlink()


class TestScannerOnProductionTree:
    """Verify zero matches on the v2 production tree after cleanup."""

    def test_zero_matches(self, modules_on_clean_tree: list):
        """Scanner must produce zero findings on the clean production tree."""
        assert len(modules_on_clean_tree) > 0, (
            "Expected at least one module with public symbols in the production tree"
        )
        repo_root = Path(__file__).resolve().parent.parent
        findings = scan(repo_root)
        assert findings == [], f"Unexpected findings: {findings}"

    def test_module_discovery(self, modules_on_clean_tree: list):
        """discover_modules should return a non-empty list."""
        assert len(modules_on_clean_tree) > 0
        # Verify each module has at least one symbol
        for mod in modules_on_clean_tree:
            assert len(mod.symbols) > 0, f"Module {mod.path} has no public symbols"

    def test_main_exits_zero(self, repo_root: Path):
        """CLI invocation must exit 0 on the clean tree."""
        result = subprocess.run(
            [sys.executable, "scripts/check_no_orphan_modules.py"],
            cwd=str(repo_root),
            check=False,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, (
            f"Scanner exited non-zero: {result.stdout}\n{result.stderr}"
        )


class TestScannerDetectsOrphans:
    """Verify the scanner flags synthetic orphan modules."""

    def test_catches_orphan_function(self, repo_root: Path):
        """A module with only unused functions should be flagged."""
        tmpdir = repo_root / "_tmp_orphan_test"
        tmpdir.mkdir(exist_ok=True)
        try:
            orphan = tmpdir / "orphan_fn.py"
            orphan.write_text("def never_called():\n    pass\n")
            # Temporarily add tmpdir to PROD_DIRS
            import check_no_orphan_modules as scanner_mod

            orig = scanner_mod._PROD_DIRS
            scanner_mod._PROD_DIRS = (*orig, "_tmp_orphan_test")
            try:
                findings = scan(repo_root)
                orphan_findings = [f for f in findings if "orphan_fn" in f]
                assert len(orphan_findings) == 1
                assert "orphan module:" in orphan_findings[0]
                assert "1 symbols" in orphan_findings[0]
            finally:
                scanner_mod._PROD_DIRS = orig
        finally:
            shutil.rmtree(tmpdir)

    def test_catches_orphan_class(self, repo_root: Path):
        """A module with only unused classes should be flagged."""
        tmpdir = repo_root / "_tmp_orphan_test"
        tmpdir.mkdir(exist_ok=True)
        try:
            orphan = tmpdir / "orphan_class.py"
            orphan.write_text("class NeverUsed:\n    pass\n")
            import check_no_orphan_modules as scanner_mod

            orig = scanner_mod._PROD_DIRS
            scanner_mod._PROD_DIRS = (*orig, "_tmp_orphan_test")
            try:
                findings = scan(repo_root)
                orphan_findings = [f for f in findings if "orphan_class" in f]
                assert len(orphan_findings) == 1
            finally:
                scanner_mod._PROD_DIRS = orig
        finally:
            shutil.rmtree(tmpdir)

    def test_does_not_flag_used_module(self, repo_root: Path):
        """A module that's imported by another should NOT be flagged."""
        # animal_prompt.py is imported by vision_analyzer.py
        tmpdir = repo_root / "_tmp_orphan_test"
        tmpdir.mkdir(exist_ok=True)
        try:
            used = tmpdir / "used_module.py"
            used.write_text("def used_fn():\n    pass\n")
            # Create a consumer
            consumer = tmpdir / "consumer.py"
            consumer.write_text("from used_module import used_fn\nresult = used_fn()\n")
            import check_no_orphan_modules as scanner_mod

            orig = scanner_mod._PROD_DIRS
            scanner_mod._PROD_DIRS = (*orig, "_tmp_orphan_test")
            try:
                findings = scan(repo_root)
                orphan_findings = [f for f in findings if "used_module" in f]
                assert len(orphan_findings) == 0, (
                    f"used_module should not be flagged as orphan: {findings}"
                )
            finally:
                scanner_mod._PROD_DIRS = orig
        finally:
            shutil.rmtree(tmpdir)
