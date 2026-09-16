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

## How to Spot Leaks

Run the scanner before pushing:

```bash
python scripts/check_no_private_data.py
```

Or use the Makefile target (see `Makefile`):

```bash
make check-privacy
```

The scanner checks tracked files (via `git ls-files`) against three pattern
categories: production /24 IP literals, chat identifier digits, and operator
+ helper handle strings. Operator and helper handles are loaded at runtime
from `data/private_identifiers.json` (gitignored); when that file is missing,
the scanner's handle pattern set is empty — that is the intended behavior for
public CI runs where the operator's personal identifiers are not available.
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
scanner and blocks the push on any leak. Install the hook:

```bash
cp .git/hooks/pre-push .git/hooks/pre-push  # if you created it
chmod +x .git/hooks/pre-push
```

The hook runs `scripts/check_no_private_data.py` and exits 1 if any private
data is detected, preventing the push.
