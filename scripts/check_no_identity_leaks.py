#!/usr/bin/env python3
"""check_no_identity_leaks — scan the public tree for personal identity leaks.

Complements check_no_private_data.py (secrets/IPs/UUIDs) with the category
that scanner was blind to: PEOPLE and PLACES. Added 2026-10-30 after the
v0.7.3 privacy review found names, site coordinates, and scanner-bypass
disclosures riding in the public tree.

Two needle sources:
  1. Generic detectors below (always active): high-precision lat/lon pairs,
     tailscale host IPs, os-user path shapes.
  2. Operator needles: scripts/release/identity-needles.txt, one
     "label|regex" per line ('#'-comments allowed). That file lives under
     scripts/release/ which the public cut strips, so operator-specific
     values never ship in the scanner itself. Missing file = generic-only.

NOT wired into .pre-push-configured: the internal repo legitimately holds
operator identity in AGENTS.md/.mailmap/PRDs (stripped from public cuts);
those paths are excluded below so this can also run locally on main.
Run from repo root. Exit 0 = clean, exit 1 = leaks found.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Internal-only paths (stripped by scripts/release/config.yaml strip_paths).
EXCLUDED_PATHS = {"AGENTS.md", ".mailmap"}
EXCLUDED_PREFIXES = ("docs/PHASE-V2-", "scripts/release/")

# Generic detectors — no operator-specific values here.
GENERIC_NEEDLES: list[tuple[str, re.Pattern]] = [
    # 4+ decimal precision lat/lon pair — almost always a real location
    ("geo-pair", re.compile(r"(?<![\d.])\d{1,2}\.\d{3,}[^\d\n]{1,15}-\d{1,3}\.\d{3,}(?!\d)")),
    # CGNAT/Tailscale host address (not the /10 range notation)
    ("tailscale-host-ip", re.compile(r"100\.(6[4-9]|7\d)\.\d{1,3}\.\d{1,3}")),
    # OS-specific home paths with a username
    ("os-user-path", re.compile(r"/(Users|home)/[a-z0-9_-]{2,}/")),
    # scanner-bypass how-to phrasing
    ("scanner-bypass-howto", re.compile(r"octet\s*(<|is less than)\s*100", re.I)),
]

# Documented sample addresses allowed despite matching tailscale-host-ip.
IP_ALLOWLIST = {"100.64.0.2", "100.64.0.5"}
# Generic/placeholder usernames allowed for os-user-path.
PLACEHOLDER_USERS = {"alice", "bob", "carol", "runner", "test", "user", "travis",
                    "circleci", "admin", "username", "name", "you", "john", "jane",
                    "john_doe", "jdoe", "developer"}


def load_operator_needles() -> list[tuple[str, re.Pattern]]:
    f = ROOT / "scripts/release/identity-needles.txt"
    needles = []
    if f.is_file():
        for line in f.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            label, _, pat = line.partition("|")
            if pat:
                needles.append((label.strip(), re.compile(pat)))
    return needles


def tracked_text_files() -> list[Path]:
    out = subprocess.run(
        ["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout.splitlines()
    files: list[Path] = []
    for rel in out:
        if rel in EXCLUDED_PATHS or rel.startswith(EXCLUDED_PREFIXES):
            continue
        p = ROOT / rel
        if not p.is_file():
            continue
        try:
            p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        files.append(p)
    return files


def main() -> int:
    needles = GENERIC_NEEDLES + load_operator_needles()
    findings = 0
    for f in tracked_text_files():
        rel = str(f.relative_to(ROOT))
        for lineno, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            for label, pat in needles:
                m = pat.search(line)
                if not m:
                    continue
                if label == "tailscale-host-ip":
                    # Range notation and documented samples are not host leaks
                    scrub = line.replace("100.64.0.0/10", "").replace("100.64.0.0", "")
                    rest = pat.sub(lambda mm: "" if mm.group(0) in IP_ALLOWLIST else mm.group(0), scrub)
                    if not pat.search(rest):
                        continue
                if label == "os-user-path":
                    user = m.group(0).split("/")[2]
                    if user in PLACEHOLDER_USERS:
                        continue
                findings += 1
                print(f"IDENTITY LEAK [{label}] {rel}:{lineno}: {line.strip()[:120]}")
    if findings:
        print(f"\n{findings} identity leak(s) found — scrub before publishing.")
        return 1
    print("check_no_identity_leaks: PASS (no identity leaks in tree)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
