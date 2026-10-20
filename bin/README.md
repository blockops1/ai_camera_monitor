# bin/ — v2 project utilities

## preflight_v2_only.sh

Preflight directory guard. Verifies that the current working directory is
the v2 project root (`<INSTALL_DIR>`).

**Why:** prevents future US-062x stories and other worker scripts from
accidentally applying v1/refactor code to v2 (or vice versa).

**Usage:**
```bash
cd <INSTALL_DIR>
bash bin/preflight_v2_only.sh   # exits 0
```

**Override:** set `V2_ROOT` env var to a custom path.

**Exit codes:** 0 = OK, 1 = cwd mismatch (prints ERROR to stderr).
