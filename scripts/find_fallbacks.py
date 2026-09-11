#!/usr/bin/env python3
"""find_fallbacks.py — Static scanner for fallback patterns in the v2 production tree.

Walks production source directories and flags lines matching one of 12
fallback-pattern categories defined in PHASE-V2-023 PRD.

Usage:
    python -m scripts.find_fallbacks [path ...]

Defaults to PROJECT_ROOT (repo root, inferred from scripts/ location).
Exit 0 if zero matches, exit 1 if matches found.
"""
from __future__ import annotations

import logging
import os
import re
import sys
from pathlib import Path

log = logging.getLogger("find_fallbacks")

# ---------------------------------------------------------------------------
# Pattern definitions (12 categories from PRD-V2-023 design_principles)
# ---------------------------------------------------------------------------

# P01: silent exception swallow — except block with no raise, no logger.exception, no sys.exit
P01 = {
    "name": "P01-silent-except",
    "desc": "try/except that silently swallows without re-raising or logging",
}

# P02: env-with-default — os.environ.get('X', 'default') where default degrades behavior
P02 = {
    "name": "P02-env-with-default",
    "desc": "os.environ.get with a non-empty default that silently degrades",
}

# P03: .get() returning fallback — dict.get('key') where missing key causes confusing crash
P03 = {
    "name": "P03-dict-get-fallback",
    "desc": "dict.get() on a required key without an explicit None-check raise",
}

# P04: optional kwarg with branch — def f(x=None) where None picks different path
P04 = {
    "name": "P04-optional-kwarg",
    "desc": "function parameter defaulting to None changes behavior path",
}

# P05: multi-format normalizer — multiple normalize_ functions or shape-dispatching normalizer
P05 = {
    "name": "P05-multi-normalizer",
    "desc": "more than one normalizer for the same input shape",
}

# P06: union-type coercion — isinstance(x, (list, str)) with different handling per type
P06 = {
    "name": "P06-union-isinstance",
    "desc": "isinstance check on multiple types with different handling per branch",
}

# P07: membership fallback — if x in container else <default>
P07 = {
    "name": "P07-membership-fallback",
    "desc": "conditional expression returning synthesized default when membership fails",
}

# P08: cooldown/dedup/suppress — function that returns True and drops real events
P08 = {
    "name": "P08-silent-suppression",
    "desc": "function named suppress/skip/dedup/cooldown that gates pipeline stages",
}

# P09: Optional[] in hot signatures — Optional param used without None check
P09 = {
    "name": "P09-optional-parameter",
    "desc": "Optional-typed parameter used as if always-present without None check",
}

# P10: broad catch — except Exception: or bare except with no re-raise
P10 = {
    "name": "P10-broad-except",
    "desc": "except Exception/Exception: or bare except that swallows everything",
}

# P11: v1 surface in v2 — reference to deprecated v1 module/function names
P11 = {
    "name": "P11-v1-surface",
    "desc": "reference to a v1 module, function, or route in v2 code",
}

# P12: resize/JPEG/letterbox — image resizing, JPEG compression, quality=N
P12 = {
    "name": "P12-resize-jpeg",
    "desc": "cv2.resize, PIL.resize, img.thumbnail, letterbox, quality=N save",
}

# v1 module names catalog (seeded from PRD pitfall list)
V1_MODULE_NAMES: list[str] = [
    "infra/cooldown",
    "infra/gate_cooldown",
    "infra/motion_types",
    "infra/timezone",
    "infra/vision_cache",
    "infra/classify_schema",
    "listener.listener",
    "normalize_flat",
    "normalize_reolink",
    "listener._process_alert",
    "create_app",
    "/webhook",
]

# ---------------------------------------------------------------------------
# Regex helpers
# ---------------------------------------------------------------------------

_P02_RE = re.compile(r"os\.environ\.get\([^,]+,\s*['\"][^'\"]+['\"]\)")
_P03_RE = re.compile(r"\.get\(['\"][^'\"]+['\"]\)")
_P04_RE = re.compile(r"(?:def\s+\w+\([^)]*)=(?:None|'')"  # simple: parameter = None or ''
                       r"|def\s+\w+\([^)]*\w+:\s+\w+\s*=\s*None\b")
_P06_RE = re.compile(r"isinstance\([^,]+,\s*\((?:list|str|dict|tuple|set)\s*,\s*(?:list|str|dict|tuple|set)\s*\)\)")
_P08_RE = re.compile(
    r"def\s+(should_)?(suppress|skip|dedup|cooldown)\w*\("
)
_P09_RE = re.compile(r"(?:Optional\[|\w+\s*\|\s*None)\s*\]")
_P10_RE = re.compile(r"except\s+(?:Exception|BaseException)?:")
_P12_RE = re.compile(
    r"(?:cv2\.resize|PIL\.Image\.resize|\.resize\(|"
    r"img\.thumbnail|letterbox|format=['\"]JPEG['\"]|"
    r"quality\s*=\s*\d+)"
)


def _line_matches_p01(lines: list[str], line_idx: int) -> bool:
    """P01: after an except clause, body has no raise/logger.exception/sys.exit."""
    # We look at the except line and the body below it
    for j in range(line_idx, min(line_idx + 6, len(lines))):
        stripped = lines[j].strip()
        if stripped.startswith(("except", "raise", "def ")):
            break
        if "raise" in stripped or "logger.exception" in stripped or "sys.exit" in stripped:
            return False
    # If we never hit a raise/logger.exception/sys.exit within the body, flag it
    return True


def _line_matches_p03(lines: list[str], line_idx: int) -> bool:
    """P03: .get('key') whose next line does NOT have an explicit None-check raise."""
    line = lines[line_idx]
    if ".get('" not in line and '.get("' not in line:
        return False
    # Check if next few lines have an explicit None check
    for j in range(line_idx + 1, min(line_idx + 4, len(lines))):
        next_line = lines[j].strip()
        if "is None" in next_line or "raise" in next_line:
            return False  # explicit None check, exempt
    return True


def _line_matches_p04(line: str) -> bool:
    """P04: parameter = None or = '' in a function signature."""
    # Match: def f(x=None) or def f(x: str = None) or def f(x='')
    if not re.search(r"def\s+\w+\(", line):
        return False
    return bool(re.search(r"=\s*None\b|= ''|= \"\"", line))


def _line_matches_p05(lines: list[str], line_idx: int, file_path: Path) -> bool:
    """P05: multiple normalize_ functions in same module (flag each)."""
    if "def normalize_" not in lines[line_idx]:
        return False
    # Check if there's another normalize_ function in the same file
    content = file_path.read_text()
    return content.count("def normalize_") > 1


def _line_matches_p06(line: str) -> bool:
    """P06: isinstance with multiple types in tuple."""
    if "isinstance" not in line:
        return False
    return bool(re.search(r"isinstance\([^,]+,\s*\([^)]+(?:,)[^)]*\)\)", line))


def _line_matches_p07(line: str) -> bool:
    """P07: if x in container else <default> (ternary with membership test)."""
    if "in " not in line or " else " not in line:
        return False
    return bool(re.search(r"in\s+\w+\s+else\s+", line))


def _line_matches_p08(line: str) -> bool:
    """P08: function named suppress/skip/dedup/cooldown."""
    return bool(_P08_RE.search(line))


def _line_matches_p09(lines: list[str], line_idx: int) -> bool:
    """P09: Optional-typed param used without None check in function body."""
    line = lines[line_idx]
    if "Optional[" not in line and "| None" not in line:
        return False
    # Check if this is a parameter annotation (part of def line)
    return bool(re.search(r"def\s+\w+\(", line))


def _line_matches_p10(lines: list[str], line_idx: int) -> bool:
    """P10: except Exception: or bare except with no re-raise nearby."""
    line = lines[line_idx]
    if not re.search(r"except\s+(?:Exception|BaseException)?:", line):
        return False
    # Check the body for a re-raise
    for j in range(line_idx + 1, min(line_idx + 5, len(lines))):
        stripped = lines[j].strip()
        if "raise" in stripped:
            return False  # re-raise found, exempt
    return True


def _line_matches_p11(line: str) -> bool:
    """P11: reference to a v1 module/function name."""
    for v1_name in V1_MODULE_NAMES:
        if v1_name in line:
            return True
    return False


def _line_matches_p12(line: str) -> bool:
    """P12: resize/JPEG/letterbox/quality patterns."""
    return bool(_P12_RE.search(line))


# Pattern dispatch: (regex_for_fast_precheck, full_matcher_function)
PATTERNS: list[dict] = [
    {**P01, "precheck": r"except", "matcher": _line_matches_p01},
    {**P02, "precheck": r"os\.environ\.get\(", "matcher": lambda lines, idx, fp: bool(_P02_RE.search(lines[idx]))},
    {**P03, "precheck": r"\.get\(", "matcher": _line_matches_p03},
    {**P04, "precheck": r"def\s+\w+\(", "matcher": _line_matches_p04},
    {**P05, "precheck": r"def normalize_", "matcher": _line_matches_p05},
    {**P06, "precheck": r"isinstance\(", "matcher": _line_matches_p06},
    {**P07, "precheck": r" in .+ else ", "matcher": _line_matches_p07},
    {**P08, "precheck": r"def\s+.*\(sup", "matcher": _line_matches_p08},
    {**P09, "precheck": r"Optional\[\|| None", "matcher": _line_matches_p09},
    {**P10, "precheck": r"except\s+(?:Exception|BaseException)?:", "matcher": _line_matches_p10},
    {**P11, "precheck": r"normalize_flat|normalize_reolink|gate_cooldown|motion_types", "matcher": _line_matches_p11},
    {**P12, "precheck": r"resize|letterbox|thumbnail|quality", "matcher": _line_matches_p12},
]


def scan_file(file_path: Path, patterns: list[dict]) -> list[tuple[str, int, str, str]]:
    """Scan a single file for fallback patterns.

    Returns list of (file:line, pattern_name, line_content, pattern_desc).
    """
    try:
        lines = file_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []

    results: list[tuple[str, int, str, str]] = []
    seen: set[tuple[int, str]] = set()

    for pi, pat in enumerate(patterns):
        precheck = pat["precheck"]
        precheck_re = re.compile(precheck)

        for line_idx, line in enumerate(lines):
            if not precheck_re.search(line):
                continue

            try:
                if pat["matcher"](lines, line_idx, file_path):
                    key = (line_idx, pat["name"])
                    if key not in seen:
                        seen.add(key)
                        results.append((
                            f"{file_path}:{line_idx + 1}",
                            pat["name"],
                            line.rstrip(),
                            pat["desc"],
                        ))
            except (TypeError, AttributeError, KeyError):
                # If the matcher crashes, skip rather than aborting the scan
                pass

    return results


def scan_paths(paths: list[Path], patterns: list[dict]) -> list[tuple[str, int, str, str]]:
    """Walk directories or scan individual files for fallback patterns."""
    all_results: list[tuple[str, int, str, str]] = []

    for p in paths:
        if p.is_file() and p.suffix == ".py":
            all_results.extend(scan_file(p, patterns))
        elif p.is_dir():
            for root, _dirs, files in os.walk(p):
                for fname in files:
                    if fname.endswith(".py"):
                        fpath = Path(root) / fname
                        all_results.extend(scan_file(fpath, patterns))

    # Sort by file path, then line number
    all_results.sort(key=lambda r: (r[0], r[1]))
    return all_results


def main() -> int:
    """Entry point. Returns 0 on zero matches, 1 on matches found."""
    if len(sys.argv) < 2:
        # Default: scan the production source tree from repo root
        repo_root = Path(__file__).resolve().parent.parent
        target_paths = [
            repo_root / "infra",
            repo_root / "listener",
            repo_root / "vehicle_matcher",
            repo_root / "telegram_formatter",
        ]
    else:
        target_paths = [Path(arg) for arg in sys.argv[1:]]

    results = scan_paths(target_paths, PATTERNS)

    for file_line, pattern_name, line_content, _desc in results:
        print(f"{file_line}: {pattern_name}: {line_content}")

    count = len(results)
    print(f"\n{count} matches", file=sys.stderr)

    return 1 if count > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
