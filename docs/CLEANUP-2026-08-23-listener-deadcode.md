# CLEANUP-2026-08-23 — Listener dead-code pass (Phase.106 Commit 4)

**Scope:** Remove `_send_arriving_message` and 3 dead imports from `listener/listener.py`.
**Why:** The 6B.105c extraction lifted `_send_arriving_message` into `listener/vehicle_event_pipeline.py:277` but never deleted the listener.py copy. The listener.py version has been dead since 2026-08-21. PLAN Part 9 (`PLAN.md` lines 776-789) is already DONE — this is post-Part-9 listener cleanup, not new module-purity work.
**Trigger:** active-tasks.md "Phase.106 Commit 4 — Module-purity dead-code pass (PLAN Part 9)" (deferred to today, 2026-08-23).

## Inventory

| File | Lines | Action | Risk |
|---|---|---|---|
| `listener/listener.py` | 1514–1597 (84L function) | DELETE — copy to archive first | LOW (zero callers, see verification) |
| `listener/listener.py` | 65 (`from infra.audit_telegram import log_outbound_telegram`) | DELETE import | LOW (used only by dead function) |
| `listener/listener.py` | 74 (`from infra.notifier import notify`) | DELETE import | LOW (used only by dead function) |
| `listener/listener.py` | 80 (`VEHICLE_ARRIVING_ENABLED` in `from infra.paths import (...)`) | REMOVE from multi-import | LOW (used only by dead function) |
| `listener/_send_arriving_message_archive_6B106c.py` | NEW | CREATE — verbatim copy of lines 1495–1623 (the 6B.110 banner block + `_send_arriving_message`) | n/a |
| `~/archive/<legacy-repo>-listener-deadcode-2026-08-23/MANIFEST.md` | NEW | CREATE | n/a |

## Pre-flight verification (DONE 2026-08-23)

- `grep -rn "_send_arriving_message\b" --include="*.py" .` → 10 hits, all in:
  - `listener/listener.py` (definition only — no callers)
  - `listener/vehicle_event_pipeline.py` (the LIVE definition + 1 internal call)
  - tests + scripts (all reference `vehicle_event_pipeline._send_arriving_message`)
  - comments + PLAN.md
  - `infra/audit_telegram.py`, `telegram_formatter/vehicle_alert.py` (comments only)
- `grep -rn "from listener.listener import" --include="*.py" .` → zero hits
- `grep -rn "_send_arriving_message\b" listener/listener.py` → 1 hit (the def itself at L1514), zero call sites
- The three imports (`log_outbound_telegram`, `notify`, `VEHICLE_ARRIVING_ENABLED`) each have exactly ONE usage in listener.py — inside `_send_arriving_message`. None are used elsewhere in the file.

## Execution order

1. Write MANIFEST.md → `~/archive/<legacy-repo>-listener-deadcode-2026-08-23/MANIFEST.md`
2. Create `listener/_send_arriving_message_archive_6B106c.py` (verbatim copy of the 6B.110 banner block + `_send_arriving_message` from listener.py L1495–L1623). Per archive-first-workflow skill: this is the rollback path.
3. Delete the 6B.110 banner block (L1495–L1511) + `_send_arriving_message` (L1514–L1597) + the format-helpers banner block (L1601–L1621) from listener.py. The format-helpers banner is already-removed code (verified L1604-1611 are pure comments referring to symbols no longer defined here); the block itself is documentation-only but misleading because it implies the helpers are still here. **Actually — keep that comment block, it's accurate documentation that 6B.106 extracted them.** Just delete lines 1495–1597 (banner + `_send_arriving_message`).
4. Remove the three unused imports (L65, L74, L80).
5. Verify: `pytest`, `ruff check`, grep checks, live probe.
6. Listener restart via `launchctl unload/load` (per pitfall #54 / 6B.102 memory-verified pattern).
7. Commit.

## Verification commands

```bash
# 1. tests still green
./.venv/bin/python -m pytest

# 2. ruff clean
./.venv/bin/python -m ruff check listener/

# 3. dead code is gone
grep -n "_send_arriving_message" listener/listener.py          # → no hits
grep -n "log_outbound_telegram\|from infra.notifier import notify" listener/listener.py  # → no hits
grep -n "VEHICLE_ARRIVING_ENABLED" listener/listener.py         # → no hits

# 4. line count down
wc -l listener/listener.py                                       # was 1896, expect ~1809

# 5. live probe
source .venv/bin/activate && python3 scripts/probe_vehicle_event_pipeline.py  # end-to-end
```

## Rollback path

```bash
# Restore the dead function from the sibling archive
cp listener/_send_arriving_message_archive_6B106c.py /tmp/_send_arriving_message.py
# (then patch listener.py to add it back — or simpler:)
git checkout HEAD~1 -- listener/listener.py
```

The sibling `_send_arriving_message_archive_6B106c.py` makes rollback a `cp` away; full rollback is `git revert` on the commit.

## Notes

- Listener PID before: 94695 (port 8090, healthy)
- Net delta: ~84 lines deleted, 3 imports removed, ~1896 → ~1809 LOC
- This commit has zero behavior change. The dead function was never called; the dead imports never executed. The live `vehicle_event_pipeline._send_arriving_message` is untouched.
