---
name: camofox-server-install-and-verify
description: 'Use when installing, upgrading, or troubleshooting the Camofox browser server on this Mac. Triggers: "camofox install", "camofox :9377", "camofox not responding", "browser_* tools broken", "playwright loop again", "ERR_DLOPEN_FAILED better-sqlite3". Verifies server health end-to-end including a real browser tab navigation, not just `/health` returning 200.'
---

# camofox-server-install-and-verify

One-shot recipe for getting Camofox (https://github.com/jo-inc/camofox-browser) running
on the Mac mini with no warmup and `browser_*` tool support.

**Source of truth:** `~/camofox-browser/` (local clone at v1.13.0 + two local commits).
**Service:** `~/Library/LaunchAgents/com.local.camofox.plist` (launchd, KeepAlive on crash).
**Endpoint:** http://127.0.0.1:9377 — **loopback only**, never bind to LAN.

## When to load this skill

- Server returns 5xx, ERR_DLOPEN_FAILED on better-sqlite3, or `/health` says `browserConnected:false` after first request
- `browser_*` tools error with "Cannot connect to Camofox at :9377"
- Building a new Cam mini / clone (fresh install)
- `git pull` inside `~/camofox-browser/` and the local patch (server.js:670) is gone

## Steps (in order, do not skip)

### 1. Use the system Node, not the Hermes-shipped one

```bash
which node           # MUST be /opt/homebrew/bin/node (Node 26+)
which npm            # MUST be /opt/homebrew/bin/npm (npm 11+), NOT /Users/<user>/.local/bin/npm
node --version       # v26.5.0
npm --version        # 11.17.0
```

**Why:** `~/camofox-browser/node_modules/better-sqlite3/` is a native addon. If npm
uses `/Users/<user>/.hermes/node/bin/node` (Hermes-shipped Node 22), the addon gets
compiled for `NODE_MODULE_VERSION 127` but the runtime uses Node 26 → 147, and you
get `ERR_DLOPEN_FAILED`. The Hermes-shipped npm ends up first in PATH.

**Fix if wrong:** `export PATH="/opt/homebrew/bin:$PATH"` then re-run `npm`. Or
call `/opt/homebrew/bin/npm` explicitly.

### 2. Clone (only if `~/camofox-browser/` doesn't already exist)

```bash
git clone https://github.com/jo-inc/camofox-browser ~/camofox-browser
cd ~/camofox-browser
```

Do NOT use `npm install -g @askjo/camofox-browser` — we pin to a specific commit
with local patches, so a source clone is required.

### 3. Approve install scripts BEFORE npm install

npm 10+ blocks native modules' install scripts by default. Without approval, no
error is printed but the addon silently doesn't compile.

```bash
cd ~/camofox-browser
npm install              # installs 450 packages (~120 MB node_modules)
npm approve-scripts better-sqlite3   # THIS LINE is what makes step 4 actually compile
```

`npm approve-scripts better-sqlite3` approves both `better-sqlite3@13.0.1` (top
level) and `better-sqlite3@12.11.1` (nested under `camoufox-js`).

Expected after install:
- `~/camofox-browser/node_modules/better-sqlite3/prebuilds/darwin-arm64.node` (today's date)
- `~/camofox-browser/node_modules/camoufox-js/node_modules/better-sqlite3/build/Release/better_sqlite3.node` (compiled, today)

**Verification trap — DO NOT use `pip show playwright`-style thinking:**
- `npm ls better-sqlite3` succeeds against the right npm even if the addon doesn't work.
- The real test is loading the addon in Node 26:
  ```bash
  /opt/homebrew/bin/node -e "require('/Users/<user>/camofox-browser/node_modules/camoufox-js/node_modules/better-sqlite3'); console.log('OK')"
  # MUST print OK. If it prints "compiled against a different Node.js version", reinstall.
  ```

### 4. Apply the local no-warmup patch (one-shot, idempotent)

The upstream server treats `BROWSER_IDLE_TIMEOUT_MS=0` as "kill on next tick"
because Node's `setTimeout(fn, 0)` semantics. The README documents "0 = never",
so we patch `server.js` to honor it.

```bash
# Backup the file before editing
cp ~/camofox-browser/server.js ~/camofox-browser/server.js.bak

# Insert these 5 lines after the const INTENTIONAL_STOP_REASONS = ... block (line 664):
# Treat BROWSER_IDLE_TIMEOUT_MS=0 as "never shut down" (matches README documented behavior).
# Node's setTimeout(fn, 0) fires on the next tick, so we substitute a far-future value.
const BROWSER_IDLE_TIMEOUT_EFFECTIVE_MS =
  BROWSER_IDLE_TIMEOUT_MS > 0 ? BROWSER_IDLE_TIMEOUT_MS : 0x7fffffff;
```

Then on line 670 (inside `scheduleBrowserIdleShutdown()`), change the trailing
`, BROWSER_IDLE_TIMEOUT_MS);` to `, BROWSER_IDLE_TIMEOUT_EFFECTIVE_MS);`.

Verify the patch is in place:
```bash
grep -n "BROWSER_IDLE_TIMEOUT_EFFECTIVE_MS" ~/camofox-browser/server.js
# Should show lines 667-668 (the const) and 676 (the setTimeout call)
```

Commit it locally:
```bash
cd ~/camofox-browser
git add server.js
git config user.email "jill@local" && git config user.name "Jill Agent"
git commit -m "patch: treat BROWSER_IDLE_TIMEOUT_MS=0 as 'never' (matches README)"
```

**If `git pull` overwrites the patch:** re-apply by running the cp + edit above.
This is the expected workflow until upstream merges.

### 5. Install the launchd plist

`~/camofox-browser/com.local.camofox.plist` is the source — copy it to LaunchAgents:

```bash
cp ~/camofox-browser/com.local.camofox.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.local.camofox.plist
```

Verify it loaded:
```bash
launchctl list | grep camofox
# Should show: PID  STATUS LABEL
#              12345  0    com.local.camofox
```

Why the plist (not `npm start &`):
- Keeps the server alive across SSH/logout
- Restarts on crash (KeepAlive=true on `Crashed`)
- Doesn't restart on `kill -TERM` (KeepAlive=false on `SuccessfulExit`)
- Logs go to `~/.camofox/server.log` and `~/.camofox/server.err.log`

### 6. Verify end-to-end (CRITICAL — `/health` alone is not enough)

A 200 on `/health` is necessary but not sufficient. The next-stage test is
opening an actual tab and confirming the browser loads.

```bash
# Start with the env vars that matter
cd ~/camofox-browser
BROWSER_IDLE_TIMEOUT_MS=0 \
  CAMOFOX_BIND_HOST=127.0.0.1 \
  CAMOFOX_CRASH_REPORT_ENABLED=false \
  CAMOFOX_DISABLE_DEFAULT_ADDONS=1 \
  /opt/homebrew/bin/node ~/camofox-browser/server.js &
SERVER_PID=$!

# Wait for /health
for i in $(seq 1 30); do
  sleep 2
  curl -fs -o /dev/null http://127.0.0.1:9377/health && break
done

# Open a real tab to https (NOT about:blank — URL scheme rejected)
TAB=$(curl -fs -X POST http://127.0.0.1:9377/tabs \
  -H 'Content-Type: application/json' \
  -d '{"userId":"verify","sessionKey":"verify-1","url":"https://example.com"}' \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['tabId'])")

# /health should now show browserConnected:true
curl -s http://127.0.0.1:9377/health | grep browserConnected

# Snapshot the tab to prove the browser is actually rendering
curl -s "http://127.0.0.1:9377/tabs/$TAB/snapshot?userId=verify" | head -c 500

# Close the tab, wait 30s, then re-check — proves the no-warmup patch works
curl -s -X DELETE "http://127.0.0.1:9377/tabs/$TAB?userId=verify"
sleep 30
curl -s http://127.0.0.1:9377/health | grep browserConnected
# MUST still be true (otherwise the patch didn't apply)

# And: open a new tab — must take <1s, not 1.8s
time curl -fs -X POST http://127.0.0.1:9377/tabs \
  -H 'Content-Type: application/json' \
  -d '{"userId":"verify","sessionKey":"verify-2","url":"https://example.com"}'
# Expected: < 1.0s on a second tab
```

If any step fails, see Troubleshooting below.

## Troubleshooting

### Symptom: ERR_DLOPEN_FAILED on better-sqlite3

```
Error: The module '...better_sqlite3.node' was compiled against
a different Node.js version using NODE_MODULE_VERSION 127.
This version of Node.js requires NODE_MODULE_VERSION 147.
```

**Cause:** npm in PATH is the Hermes-shipped one (Node 22). The compile worked
against Node 22 but the server runs on Node 26.

**Fix:**
```bash
export PATH="/opt/homebrew/bin:$PATH"
cd ~/camofox-browser
rm -rf node_modules/better-sqlite3/build
npm rebuild better-sqlite3
```

If `npm rebuild` finishes instantly with no node-gyp output, the install scripts
still aren't approved. Re-run `npm approve-scripts better-sqlite3`.

### Symptom: "browser idle shutdown" appears in server.log after closing tabs

**Cause:** The local no-warmup patch isn't applied, or got overwritten by a
`git pull`.

**Fix:** Re-apply the patch (step 4 above). Or pass
`BROWSER_IDLE_TIMEOUT_MS=2147483647` (the actual `0x7fffffff` value) and the
code will treat it as the same effective timeout.

### Symptom: `/health` returns 200 but browserConnected stays false

**Cause:** Server started but Camoufox binary failed to launch. Check
`~/.camofox/server.err.log` for errors. Common causes:
- `camoufox` binary not cached — fix: `cd ~/camofox-browser && npx camoufox-js fetch`
- Proxy config invalid — unset `PROXY_*` env vars (we don't use a proxy locally)
- HEADLESS test failed on first navigate — fix: `CAMOFOX_DISABLE_DEFAULT_ADDONS=1` (skips uBlock download)

### Symptom: launchd shows the agent but it's "throttled"

**Cause:** launchd ThrottleInterval is 10s; if the agent crashes 3× in 10 minutes,
launchd stops trying. Usually indicates an underlying config bug.

**Fix:** `tail -50 ~/.camofox/server.err.log` for the actual error.

### Symptom: `browser_*` tools in this Hermes session can't reach Camofox

**Cause:** Camofox is loopback-only. Some `browser_*` tool implementations try
LAN IPs first. Confirm with `curl http://127.0.0.1:9377/health` from the shell
that's running the agent. If that works but `browser_*` doesn't, the tool's URL
config is wrong (it's `CAMOFOX_URL` env, default `http://localhost:9377`).

## Pitfalls (must avoid)

1. **Never `npm install -g camofox-browser`** — we need to control the commit
   + apply the local patch. Global install makes patching fragile.

2. **Never trust `npm ls` / `pip show` as proof of working** — verify by
   actually loading the module in Node.

3. **Never edit `server.js` without committing** — an unpinned `git pull` will
   clobber the patch and you'll be debugging "browser died after 30s" again.

4. **Never bind Camofox to 0.0.0.0** — it's loopback only. The plist sets
   `CAMOFOX_BIND_HOST=127.0.0.1`. The agent has no business serving other hosts.

5. **Never change `BROWSER_IDLE_TIMEOUT_MS=0` in the plist to something else
   without re-reading step 4** — it depends on the local patch to be honored.
   If you remove the patch first, then change to a positive value, you're fine.
   If you keep the patch and change to 0 anyway, the patch treats 0 as 0x7fffffff
   and behavior continues to be "never."

6. **The 2026-07-25 run incident** — earlier in the session I said
   "playwright is missing" and proposed installing it. Root cause was the wrong
   python interpreter (session python != farm-surveillance venv python). Always
   verify a tool works by loading it in the interpreter you'll use at runtime —
   `pip show` / `npm ls` / `which` aren't enough.

## Quick verification (under 5 seconds)

```bash
curl -fs http://127.0.0.1:9377/health \
  | python3 -c "import sys,json; h=json.load(sys.stdin); print('OK' if h.get('engine')=='camoufox' else 'FAIL'); print('browserConnected:', h.get('browserConnected'))"
```

Expected output: `OK` and `browserConnected: true`. If `False`, the patch isn't
applied OR no tab has been opened recently to trigger Camoufox warmup.
