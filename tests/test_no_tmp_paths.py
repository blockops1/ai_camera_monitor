"""Regression test: assert no /tmp/ references in production code.

This test scans every Python file in the production directories
(infra/, listener/, telegram_formatter/, vehicle_matcher/) and
fails if any file contains the literal string '/tmp/'.

Exception: this test file itself and other test files/fixtures are
allowed to contain '/tmp/' (they are the regression guard, not
the bug).
"""

import pathlib
import re

# Directories under the project root that are considered production code.
_PRODUCTION_DIRS = ["infra", "listener", "telegram_formatter", "vehicle_matcher"]

# The forbidden substring.
_FORBIDDEN = "/tmp/"

# Regex to find '/tmp/' occurrences in a line.
_RE = re.compile(re.escape(_FORBIDDEN))


def _production_root() -> pathlib.Path:
    """Return the project root (parent of the production directories)."""
    return pathlib.Path(__file__).resolve().parent.parent


def _iter_py_files(roots: list[pathlib.Path]) -> list[pathlib.Path]:
    """Yield every .py file under the given root directories."""
    files: list[pathlib.Path] = []
    for root in roots:
        if root.is_dir():
            files.extend(p for p in root.rglob("*.py"))
    return sorted(files)


def _scan_file(path: pathlib.Path) -> list[tuple[int, str]]:
    """Return list of (line_number, line_text) where '/tmp/' is found."""
    matches: list[tuple[int, str]] = []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return matches

    for lineno, line in enumerate(text.splitlines(), start=1):
        if _RE.search(line):
            matches.append((lineno, line.rstrip()))
    return matches


def test_no_tmp_paths_in_production() -> None:
    """Assert no production file contains a '/tmp/' path reference.

    Fails with a message listing every offending file:line.
    """
    root = _production_root()
    prod_roots = [root / d for d in _PRODUCTION_DIRS]
    all_files = _iter_py_files(prod_roots)

    violations: list[str] = []
    for fpath in all_files:
        matches = _scan_file(fpath)
        for lineno, line in matches:
            rel = fpath.relative_to(root)
            violations.append(f"  {rel}:{lineno}: {line}")

    if violations:
        total = len(violations)
        summary = "\n".join(violations)
        msg = (
            f"{total} production file(s) contain(s) the forbidden string "
            f"'/tmp/':\n{summary}"
        )
        assert False, msg
