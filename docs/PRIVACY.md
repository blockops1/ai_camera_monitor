# Privacy Convention — Private Data in farm-surveillance-v2

## Purpose

This directory contains no production-identity data. All secrets, identifiers,
and personally-identifying strings are stored outside version control or use
neutral placeholders. This doc describes the convention so contributors and
CI hooks can spot leaks before they are committed or pushed.

## Three Categories of Private Data

### 1. Camera / Farm Network IPs

**What is private:** Real IP addresses of the farm's Reolink cameras and the
production /24 subnet they live on (e.g. `192.168.1.x`).

**Where they belong:** `camera-creds.env` (gitignored via `*.env`).

**Safe to track:** RFC 5737 TEST-NET-1 addresses (`192.0.2.x`) used in tests
and documentation. These are globally reserved for examples and never collide
with production hardware.

### 2. Telegram Chat Identifier

**What is private:** The numeric value of `TELEGRAM_HOME_CHAT_ID` — the
Telegram group or DM used to receive alerts. This ties the pipeline to a
specific person or location.

**Where it belongs:** `~/.env` (gitignored by nature of the dot-prefix). The
launchd daemon loads it via `listener.daemon._load_home_env()`.

**Safe to track:** The literal string `<HOME_CHAT_ID>` as a placeholder in
comments or documentation.

### 3. Operator / Helper Handle Strings

**What is private:** First-name forms used as personal handles — for example
the operator's handle and helper handles. These identify specific individuals.

**Where they belong:** Outside the repo (operator's hermes profile, personal
records, or `data/private_identifiers.json` at the operator's deployment site).
In code they should appear only as neutral role-words like "the operator" or
"[helper-A]".

**Safe to track:** The neutral role-words themselves.

### 4. Tracked Runtime Data Paths

**What is private:** Any tracked file under a path the `.gitignore`
treats as runtime-only output, enrollment data, or pre-deployment
snapshots. The `.gitignore` rule is necessary but not sufficient — a
file committed BEFORE the rule was added stays in the tracked tree
forever unless someone runs `git rm --cached` and rewrites history.

**Where it belongs:** Off the tracked tree. Either gitignored
(prevent future commits) or untracked (`git rm --cached` for the
existing file).

**Paths guarded by this check (see `RUNTIME_DATA_PATTERNS` in
`scripts/check_no_private_data.py`):**

| Path glob | Why it's private |
|---|---|
| `data/events/*/event.json` | Per-alert runtime record: camera alias, absolute paths, msec timestamps. |
| `data/frames/**/ground_truth.json` | Hand-labeled bounding boxes keyed by alert UUID. |
| `data/us-*snapshot*.json` | Pre-deployment Reolink GetDevInfo dump (model + firmware + hardware IDs). |
| `data/backtest/**` | Backtest PNG composites (~80MB, operator property imagery). |
| `data/known/people/known_people.json` | Enrolled-person manifest (real first names + face embeddings). |
| `data/known/people/embeddings.npz` | Enrolled-person face embeddings. |
| `data/known/animals/**` | Enrolled-animal manifest + biometrics. |
| `data/private_identifiers.json` | Operator's private handle list (already gitignored). |
| `models/*.onnx` / `models/*.pt` / `models/*.weights` | ML model weights; release via artifacts, not source. |
| `scripts/push*.log` / `scripts/push*.txt` | Forgejo push logs; can reveal internal mirror hostname. |

**How to recover when the scanner flags a tracked path:**

```bash
# 1. Untrack the file (keeps it on disk for the operator to use).
git rm --cached path/to/leaked-file

# 2. Confirm the .gitignore rule exists. If not, add one.
#    For a brand-new leak class, update RUNTIME_DATA_PATTERNS in
#    scripts/check_no_private_data.py + add a regression test in
#    tests/test_check_no_private_data.py.

# 3. Commit + push the .gitignore rule and the `git rm --cached` change.
#    Old commits will still contain the leaked file in history; to
#    scrub history, use `git filter-repo` (see docs/PRIVACY.md § History
#    Scrub if/when it lands).
```

## How to Spot Leaks

Run the scanner before pushing:

```bash
python scripts/check_no_private_data.py
```

Or use the Makefile target (see `Makefile`):

```bash
make check-privacy
```

The scanner checks tracked files (via `git ls-files`) against four
pattern categories: production /24 IP literals, chat identifier digits,
operator + helper handle strings, and tracked runtime data paths.
Operator and helper handles are loaded at runtime from
`data/private_identifiers.json` (gitignored); when that file is missing,
the scanner's handle pattern set is empty — that is the intended
behavior for public CI runs where the operator's personal identifiers
are not available. The runtime-data-paths check is a path-level glob
match against `git ls-files`, not a content scan.
It excludes:

- `.git/`, `.venv/`, `.pytest_cache/` directories
- `.env` files (gitignored)
- `data/private_identifiers.json` (gitignored)
- `scripts/check_no_private_data.py` itself
- `docs/PRIVACY.md` (this file, which discusses what to look for)
- `.mailmap` (operator's identity mapping per git convention, not a leak)

## VISION_LLM_URL Note

The default `VISION_LLM_URL` of `http://localhost:8080/v1/chat/completions`
is a generic development placeholder. It does not leak an internal IP, a
personal identifier, or a secret. It is safe to track as-is.

## Adding New Private Data

If you add a new category (new IP range, new handle, new identifier), update
three places:

1. `scripts/check_no_private_data.py` — add the pattern
2. `docs/PRIVACY.md` — document the category
3. `tests/test_check_no_private_data.py` — add a regression test

## Pre-Push Protection

A `.git/hooks/pre-push` hook (or manual `make check-privacy`) runs the
scanner and blocks the push on any leak. The hook is shipped in the
repo and is already executable — it activates on first `git push` from
a fresh clone. To verify it's wired up:

```bash
ls -la .git/hooks/pre-push
# -rwxr-xr-x ... .git/hooks/pre-push

# Manual install (only if the hook is missing or unexecutable):
cp scripts/check_no_private_data.py .git/hooks/pre-push-check-privacy.sh
chmod +x .git/hooks/pre-push-check-privacy.sh
```

The hook runs `scripts/check_no_private_data.py` and exits 1 if any
private data is detected, preventing the push.
