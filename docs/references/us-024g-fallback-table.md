# US-024g — fallback inventory entries to remove

Supporting reference for card US-024g. Card body links here.

## Entries to remove (4 entries, all PRESENT on `main` per `git show main:infra/cleanup.py` 2026-09-13)

| Line | Pattern (current) | Recommended fix (from inventory) | Pattern code |
|---|---|---|---|
| 126 | `except OSError: pass` (rmdir empty camera_dir) | Log the error | P01 |
| 140 | `except OSError: pass` (rmdir empty camera_dir nested) | Log the error | P01 |
| 142 | `except OSError as e: log.error(...); stats["errors"] += 1` (parent block) | This entry's body already logs but L140 inside doesn't — `log.exception(...)` for both | P01 |
| 209 | `except OSError: pass` (alert cleanup rmtree) | Log the error | P01 |

## Context

All 4 entries are inside `infra/cleanup.py`'s best-effort cleanup loops. The recommended fix for all of them is to add `log.exception(...)` so failures are visible in the structured log. Cleanup is genuinely best-effort — `raise` would defeat its purpose — so `log+continue` is correct.

## Verification commands (copy-paste to re-verify)

```bash
git -C /Users/jill/farm-surveillance-v2 grep -nE 'except OSError:\s*$' -- infra/cleanup.py
git -C /Users/jill/farm-surveillance-v2 show main:infra/cleanup.py | awk 'NR==126 || NR==140 || NR==142 || NR==209'
```

After US-024g merges:
- The first grep should return 0 lines (no bare `except OSError:`).
- The 4 cited lines should each contain `log.exception(...)` (or equivalent) instead of `pass`.
