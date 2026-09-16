# Decision: GATE_KEEP_DISK_ARTIFACTS placement (US-047d)

**Date:** 2026-09-15
**Decision:** Move `GATE_KEEP_DISK_ARTIFACTS` from the launchd plist to `.env` (canonical config).

## Background

Phase V2-047 ("plist-envvar-cleanup") moved runtime config out of the launchd
plist and into `.env` for VISION_LLM_URL, LISTEN_HOST, and LISTEN_PORT. The
operator (2026-09-15) explicitly extended this principle to
`GATE_KEEP_DISK_ARTIFACTS`. US-047d records the decision and the migration.

## Decision

**Place `GATE_KEEP_DISK_ARTIFACTS` in `.env`, not the plist.**

Rationale:
1. **Consistency.** The other behavior/endpoint vars (VISION_LLM_URL,
   LISTEN_HOST, LISTEN_PORT, MOTION_GATE_NIGHT_*, DISPLAY_TZ) all live in
   `.env`. Splitting `GATE_KEEP_DISK_ARTIFACTS` into the plist creates
   a two-place config that's harder to audit and grep.
2. **Canonical config principle.** Per US-047 / PHASE-V2-047, `.env` is the
   single source of truth for non-secrets and non-launchd-required vars.
   `GATE_KEEP_DISK_ARTIFACTS` is a behavior flag — not a launchd
   requirement, not a secret — so it belongs with its peers.
3. **No rotation requirement.** Operator (2026-09-15) confirmed the flag is
   not rotated at runtime. A static `.env` value is sufficient.
4. **Trivial migration.** Plist edit is a 2-line change (remove the key, no
   replacement), plus `launchctl unload && launchctl load` to pick up the
   modified plist (which now just removes a key; behavior unchanged because
   `.env` already had the value).

## Migration steps (executed 2026-09-15)

1. ✅ Added `GATE_KEEP_DISK_ARTIFACTS=true` to `.env.example` (tracked)
2. ✅ Added `GATE_KEEP_DISK_ARTIFACTS=true` to `.env` (local, gitignored)
3. ✅ Removed `GATE_KEEP_DISK_ARTIFACTS` from
   `~/Library/LaunchAgents/com.farm.surveillance.v2.plist`
   (`plutil -remove EnvironmentVariables.GATE_KEEP_DISK_ARTIFACTS`)
4. ✅ Removed the matching block from `generate_plist()` template in
   `listener/daemon.py` so future plist regenerations match reality
5. ✅ Restarted listener via `launchctl kickstart -k` to pick up new plist
6. ✅ Verified daemon imports `.env` (python-dotenv) so the var still
   resolves to `true`

## Verification

```bash
# Plist no longer contains the key
plutil -extract EnvironmentVariables.GATE_KEEP_DISK_ARTIFACTS raw \
  ~/Library/LaunchAgents/com.farm.surveillance.v2.plist
# → No value at that key path

# .env.example documents the key
grep GATE_KEEP_DISK_ARTIFACTS .env.example
# → GATE_KEEP_DISK_ARTIFACTS=true

# .env has the local value
grep GATE_KEEP_DISK_ARTIFACTS .env
# → GATE_KEEP_DISK_ARTIFACTS=true

# Daemon code reads via os.environ — works with either source
grep -n GATE_KEEP_DISK_ARTIFACTS infra/gate.py
# → os.environ.get("GATE_KEEP_DISK_ARTIFACTS", "") (line 166)
```

## Remaining plist EnvironmentVariables

After this migration the plist retains only launchd-required or
operator-set-at-load vars:

- `PATH` — launchd requires this for `ProgramArguments` resolution
- `LISTEN_HOST` — operator-set at install; safe but cosmetically belongs in `.env`
- `LISTEN_PORT` — same as LISTEN_HOST
- `VISION_LLM_URL` — moved to `.env` via US-047a; this is a leftover
  (operator-direct cleanup pending)

US-047d does not address the LISTEN_HOST/LISTEN_PORT/VISION_LLM_URL
leftovers; that's a separate cleanup story if operator wants full plist
minimalization.

## Cross-references

- `docs/PHASE-V2-047-PRD-plist-envvar-cleanup.json` — parent PRD
- `infra/gate.py:161-167` — `_is_keep_disk_artifacts_enabled()` consumer
- `listener/daemon.py:generate_plist()` — plist template (now cleaned)
- Operator directive 2026-09-15: "Move it to the environmental file"
