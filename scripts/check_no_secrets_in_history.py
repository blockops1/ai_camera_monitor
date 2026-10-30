#!/usr/bin/env python3
"""History-level secret scanner for PUBLIC remotes (pre-push, github only).

Why this exists (2026-10-20 incident): camera-creds.env.bak.1790803938 was
committed on 2026-10-01 by a kanban 'BEFORE snapshot' step and sat in history.
The pre-existing credential guard was TREE-only (tip-of-branch), so every push
afterwards passed while carrying the leaked blob in ~541 commits of history.
Tree checks cannot see below the tip. This scanner closes that gap.

Two checks, run against ALL objects reachable from the refs being pushed:

  1. FORBIDDEN PATHS — any commit in history touching:
       .env, camera-creds.env, telegram-creds.env, llm-creds.env,
       *.bak, *.bak.<digits>
     (*.example templates are allowed.)

  2. SECRET LITERALS — every value loaded from the local credential files
     (.env, camera-creds.env, telegram-creds.env in repo root + ~/.env) is
     searched for, byte-exact, inside every blob reachable from pushed refs.
     Values are never printed; only var names + paths are reported.

Usage:
    check_no_secrets_in_history.py REF [REF...]   # usually from pre-push stdin

Exit 0 = clean. Exit 1 = secrets found (block push).
"""
import os
import re
import subprocess
import sys

REPO = subprocess.run(
    ["git", "rev-parse", "--show-toplevel"],
    capture_output=True, text=True, check=True).stdout.strip()

FORBIDDEN_RE = re.compile(
    r'(^|/)(\.env|camera-creds\.env|telegram-creds\.env|llm-creds\.env)'
    r'(\.bak(\..+)?)?$|\.bak(\..+)?$'
)
EXAMPLE_OK = re.compile(r'\.example$')
SECRET_NAME_RE = re.compile(r'(?i)(PASS|PASSWORD|TOKEN|SECRET|KEY)')
# identifiers that look like secrets but are operator-visible on purpose
OK_PUBLIC = {"TELEGRAM_ALLOWED_USERS", "TELEGRAM_HOME_CHAT_ID"}


def load_secret_values():
    vals = {}
    for f in (".env", "camera-creds.env", "telegram-creds.env",
              os.path.expanduser("~/.env")):
        p = f if os.path.isabs(f) else os.path.join(REPO, f)
        try:
            fh = open(p)
        except FileNotFoundError:
            continue
        with fh:
            for line in fh:
                m = re.match(r'^([A-Z0-9_]+)=(.+)$', line.strip())
                if not m:
                    continue
                k, v = m.group(1), m.group(2).strip().strip('"').strip("'")
                if len(v) >= 6 and SECRET_NAME_RE.search(k) and k not in OK_PUBLIC:
                    vals[k] = v
    return vals


def reachable_objects(refs):
    # Scan exactly the refs being pushed (pre-push passes local SHAs on stdin
    # or commit SHAs as args) — NOT `--all`, because stale local branches that
    # were never public must not block pushes, while anything reachable from a
    # pushed ref is exactly what becomes public.
    objs = {}
    for ref in refs:
        out = subprocess.run(
            ["git", "rev-list", "--objects", ref],
            capture_output=True, text=True, cwd=REPO).stdout
        for line in out.splitlines():
            if ' ' in line:
                sha, path = line.split(' ', 1)
                objs.setdefault(sha, set()).add(path)
    return objs


def main():
    refs = [a for a in sys.argv[1:] if not a.startswith('-')]
    if not refs:
        print("usage: check_no_secrets_in_history.py REF [REF...]")
        return 0  # nothing pushed

    objs = reachable_objects(refs)
    fails = []

    # 1) forbidden paths anywhere in history
    for sha, paths in objs.items():
        for p in paths:
            if FORBIDDEN_RE.search(p) and not EXAMPLE_OK.search(p):
                fails.append(f"path {p!r} reachable in history (blob {sha[:9]})")

    # 2) secret literals in any reachable blob
    secrets = load_secret_values()
    if secrets:
        shas = sorted(objs)
        proc = subprocess.Popen(
            ["git", "cat-file", "--batch"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, cwd=REPO)
        out, _ = proc.communicate(('\n'.join(shas) + '\n').encode(), timeout=600)
        i = 0
        while i < len(out):
            nl = out.find(b'\n', i)
            if nl == -1:
                break
            parts = out[i:nl].decode(errors='replace').split(' ')
            if len(parts) == 3 and parts[1] == 'blob':
                size = int(parts[2])
                content = out[nl + 1:nl + 1 + size]
                for k, v in secrets.items():
                    if v.encode() in content:
                        paths = sorted(objs.get(parts[0], {'<unknown>'}))
                        fails.append(
                            f"secret {k} found in blob {parts[0][:9]} at {paths[:3]}")
                i = nl + 1 + size + 1
            else:
                i = nl + 1

    if fails:
        print("HISTORY SECRET SCAN FAILED — refusing push to public remote:")
        seen = set()
        for f in fails:
            if f not in seen:
                seen.add(f)
                print("  -", f)
        print(f"({len(seen)} distinct problems)")
        return 1
    print(f"history secret scan: clean ({len(objs)} objects)")
    return 0


if __name__ == '__main__':
    sys.exit(main())
