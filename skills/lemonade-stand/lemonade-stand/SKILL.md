---
name: lemonade-stand
description: Lemonade stand game development — game logic, demand formula, weather derivation, simulation testing
category: gaming
---

# Lemonade Stand — Game Dev Skill

## Trigger
Working on the lemonade-stand project (production_apps/lemonade-stand or ralph/projects/lemonade-stand).

## Project Paths (CORRECT — update any stale references)
- Dev: `~/ralph/projects/lemonade-stand/` — editable workspace (circuits, contracts, Ralph PRD)
- Staging: `~/staging_apps/lemonade-stand/` — test server runs here (port 3000)
- Production: `~/production_apps/lemonade-stand/` — deployment target, **NEVER edited directly**

**Rule: No code is ever edited in production_apps/. Ever.**

## Key Files
- **Config (single source of truth):** `src/game/config.ts` — RECIPE, INGREDIENT_COSTS, STARTING_MONEY, AD_COSTS, AD_MULTS, WEATHER_MULTS, PRICE_OPTIONS, COST_PER_CUP. All values from `process.env.NEXT_PUBLIC_*` with defaults. All other game files import from here — no duplication.
- Game logic: `src/game/LemonadeStand.ts` — imports from config
- Game simulation: `src/game/GameState.ts` — imports from config; standalone demand/weather engine
- UI: `src/pages/game/[sessionId].tsx` — imports from config for recipe display and ingredient prices
- GameControls: `src/components/GameControls/index.tsx` — imports from config for price labels
- Circuit: `circuits/src/lib.nr` — recipe/cost constants hardcoded (must match config.ts — Noir has no env var mechanism)
- `.env.local` — runtime overrides for all game parameters

## Customer Count Formula (2026-05-09 — CURRENT)
```
totalCustomers = Math.floor(BASE[day] * wm * pm * am / 100)
```
Where all four values are **stored as scaled integers** and the final `/100` converts back to the actual customer count:

- `BASE` = `BASE_CUSTOMERS_PER_DAY[day - 1]` = **`[3, 5, 7, 9, 11, 13, 16]`** ← rebalanced 2026-05-09
- `wm` = `WEATHER_MULTS[weatherIdx]` → **`[15, 12, 10, 7]`** (hot/sunny/cloudy/rainy) → divide by 10 for actual multiplier
  - **⚠️ Index 0 = HOT, not rainy.** `[15, 12, 10, 7]` means hot=1.5×, sunny=1.2×, cloudy=1.0×, rainy=0.7×
- `pm` = `PRICE_MULTS[tier]` → **`[50, 38, 32, 27, 23, 17, 9, 6]`** (8 tiers, tier = `min(floor(price_cents / 100), 7)`) ← rebalanced 2026-05-09
- `am` = `AD_MULTS[adIdx]` → `[25, 18, 12, 10]` (radio/social/flyers/none) → divide by 10 for actual multiplier
  - **⚠️ Index 3 = "none" = ×1.0.** This was a past bug (old code used index 0 for none, causing a demand penalty with no ads).
- Result capped at `ZK_MAX_CUSTOMERS = 6000`

**Why `/100`:** `wm`, `pm`, and `am` are stored as integers ×10 their actual value. `BASE` is a small integer. Multiplying three scaled values together produces a number 100× too large, so we divide by 100 to get the real customer count.

**Example:** Day 1 (BASE=3), hot weather (weatherIdx=0, wm=15), $3.00 price (tier=3, pm=27), radio ads (adIdx=0, am=25):
`floor(3 × 15 × 27 × 25 / 100) = floor(303.75) = 303 customers`

**Example:** Day 7 (BASE=16), hot (wm=15), $0.50 (tier=0, pm=50), radio (am=25):
`floor(16 × 15 × 50 × 25 / 100) = floor(3,000) = 3,000 customers (at cap)`

## Recipe & Costs (CENT units — current as of 2026-05-09)
All monetary values in **cents** (integer). `12000 = $120.00`.

**Recipe (per 1 cup):** 1 lemon + 2 sugar + 12 ice
**Ingredient costs:** lemons **$0.50**, sugar **$0.04**, ice **$0.01**
**Cost per cup:** 40 cents (1×50 + 2×4 + 12×1 = 40)
**Starting money:** 12000 cents ($120.00)

**AD_COSTS** (cents, both config.ts AND circuit must match): `[0, 900, 2400, 4500]` (none/flyers/social/radio) = [$0, $9, $24, $45]

**⚠️ CRITICAL — Cost per cup vs. config display mismatch (2026-05-09):**
`COST_PER_CUP` in config.ts = **40 cents** (1×lemons + 2×sugar + 12×ice = 1×50 + 2×4 + 12×1).
However, `.env.local` sets `NEXT_PUBLIC_LEMON_COST=50` which equals **$0.50/lemon**, giving a true per-cup cost of $0.70.
The game's *ingredient purchase UI* correctly uses `NEXT_PUBLIC_LEMON_COST` (real-world prices).
The game's *demand formula* uses `COST_PER_CUP = 40` for ZK proof calculations.
When balancing game economy: use 40 cents/cup for demand modeling. The actual cost at purchase is $0.70/cup (lemons $0.50 + sugar $0.08 + ice $0.12).

**Configuration:** Three places must stay in sync:
1. `src/game/config.ts` — canonical TypeScript values
2. `src/pages/game/[sessionId].tsx` + `src/components/GameControls/index.tsx` — UI display (import from config)
3. `circuits/src/lib.nr` — ZK circuit hardcodes same values (Noir has no env var mechanism)

**⚠️ CRITICAL: Circuit sync rule.** Noir has no env var mechanism — circuit constants are hardcoded literals. When changing any game parameter (recipe, costs, BASE, price multipliers), always update `circuits/src/lib.nr` alongside `config.ts`. The TypeScript dev server hot-reloads but the circuit only changes when `nargo build` is re-run. After any circuit change: build (`docker run ... nargo build`), commit the `.json` artifact, push.

**⚠️ AD_COSTS sync (RESOLVED 2026-05-08):** Both TypeScript `config.ts` AND circuit `lib.nr` now store `AD_COSTS = [0, 900, 2400, 4500]` (cents). Previously the circuit stored dollars `[0, 90, 240, 450]` causing ZK proof rejection for paid ads. This is now fixed — but remains the pattern to watch for.

## ZK Circuit Constants (cents) — UPDATED 2026-05-09
**Customer demand:**
- `ZK_MAX_CUSTOMERS = 6000` — day 7 best-case: floor(16 × 15 × 50 × 25 / 100) = 3,000 (cap still 6000)

**Per-day best-case (Hot × tier3 $3.50 × radio) — UPDATED 2026-05-09:**
```
Day 1: BASE=3   → floor(3*15*27*25/100) = 303 customers  → rev=$1,060 / profit=$894
Day 2: BASE=5   → 506 customers  → rev=$1,771 / profit=$1,524
Day 3: BASE=7   → 708 customers  → rev=$2,478 / profit=$2,150
Day 4: BASE=9   → 911 customers  → rev=$3,188 / profit=$2,779
Day 5: BASE=11  → 1,113 customers → rev=$3,896 / profit=$3,405
Day 6: BASE=13  → 1,316 customers → rev=$4,606 / profit=$4,035
Day 7: BASE=16  → 1,620 customers → rev=$5,670 / profit=$4,977
```

**ZK_MAX_DAILY_PROFIT** (7 values): `[89400, 152400, 215000, 277900, 340500, 403500, 497700]`
**ZK_MAX_TOTAL_PROFIT** = `1,976,000` (~$19,760) — hot+radio+$3.50 all week
**ZK_MAX_TOTAL_REVENUE** = `6,200,000` (conservative upper bound)

**NOTE:** The ZK circuit constants (BASE_CUSTOMERS_PER_DAY, ZK_MAX_*, AD_COSTS) are hardcoded in `circuits/src/lib.nr` and must match `config.ts`. After changing any game parameter, rebuild circuit with `docker run --rm -v ~/ralph/projects/lemonade-stand:/app/projects/lemonade-stand -w /app/projects/lemonade-stand/circuits --entrypoint nargo ralph-local:latest build`. Commit the `.json` artifact alongside the config change.

**Price multiplier tiers** (tier = `min(floor(price / 100), 7)` — 8 tiers) — UPDATED 2026-05-09:
- $0.50–$0.99 → mult **50**
- $1.00–$1.49 → mult **38**
- $1.50–$1.99 → mult **32**
- $2.00–$2.99 → mult **27**
- $3.00–$3.99 → mult **23**
- $4.00–$4.99 → mult **17**
- $5.00–$5.99 → mult **9**
- $6.00+ → mult **6**

**Current game balance (2026-05-09):**
- Worst case (rainy + no ads + $6.00): **~$1,610**
- Optimal (hot + radio + $3.50): **~$16,900**
- $5,000 target: requires rainy weather + social/radio ads, or good weather with any ad tier

See `references/zk-constants.md` for full constant table and circuit compile command.

## Demand Formula Reference
See `references/demand-formula.md` for full formula, two-constant pattern, and worked examples.

## Environment & Sync Workflow
See `references/dev-environment.md` for directory structure, sync commands, dev server startup, circuit compile, and ZK constant table.

## E2E Testing (Playwright)
`e2e-test.js` in the dev project root (`~/ralph/projects/lemonade-stand/`) runs a full 7-day free-mode game on both desktop (1280×800) and mobile (390×844 iPhone 14 Pro) viewports.

**Test strategy:** $2.00 price + no ads + daily restock of 8 cups worth of ingredients (8 lemons, 16 sugar, 96 ice). Verified profitable on both viewports — win condition triggers Day 4.

**Math verification:** Per-day checks for purchase cost, revenue, cups sold, money flow, and demand-vs-inventory. All calculations match expected values.

**Run test:**
```bash
cd ~/ralph/projects/lemonade-stand
NEXT_PUBLIC_CHAIN_ID=2651420 NEXT_PUBLIC_RPC_URL=https://horizen-testnet.rpc.caldera.xyz/http \
NEXT_PUBLIC_CONTRACT_ADDRESS=0x0000000000000000000000000000000000000000 \
NODE_PATH=/Users/<user>/.hermes/hermes-agent/node_modules node e2e-test.js
```

**Playwright debugging pattern:** When locator selectors fail, write a throwaway script (e.g. `/tmp/inspect-game.js`) using `[class*="className"]` partial class matching and `input[type="number"]` input selectors to inspect the actual DOM. Do NOT use XPath with text content that appears in multiple elements (e.g. "Lemons" appears in both inventory panel AND results panel — strict mode violation). Use sequential `nth()` indexing on form-level locators instead.

**50-game strategy test results:** See `references/playwright-strategy-tests.md` (pre-rebalance 2026-05-07 — game economy has since been rebalanced; current game is profitable with correct strategy).

## Simulation Script
`test-simulation.ts` (root of staging project). Plays 20 full games with a fixed strategy. Useful for verifying demand calculations and game balance. Run from staging:
```
cd ~/staging_apps/lemonade-stand && npx tsx test-simulation.ts
```
Requires dev server stopped first (port 3000 conflict).
1. `deriveWeatherLocal` missing → added static method to WeatherOracle
2. Recipe not shown → added RECIPE constant + UI grid
3. Money display inconsistent → formatMoney() with Intl.NumberFormat
4. `MAX_CUSTOMERS` renamed → `ZK_MAX_CUSTOMERS`
5. Recipe panel not shown → added to game UI above Buy Ingredients
6. globals.css corrupted (self-importing: `import '../styles/globals.css'`) → restore from staging
7. `crypto.randomUUID()` not available in older browsers (e.g. older Safari on older iOS) → use `generateUUID()` polyfill in `index.tsx`
## Testing Workflow

**Rule: Test in dev (`~/ralph/projects/lemonade-stand/`). Sync to staging only when satisfied.**

Staging is a deployment target, not a development environment. Process:
1. Edit + test in dev
2. Commit to dev git
3. Sync dev → staging (`~/ralph/sync-to-staging.sh --apply`) for final verification
4. Only then push to production

**E2E test runs against dev** (port 3000):
```bash
cd ~/ralph/projects/lemonade-stand
NEXT_PUBLIC_CHAIN_ID=2651420 NEXT_PUBLIC_RPC_URL=https://horizen-testnet.rpc.caldera.xyz/http \
NEXT_PUBLIC_CONTRACT_ADDRESS=0x0000000000000000000000000000000000000000 \
npm run dev -- --hostname 0.0.0.0
# in another terminal:
NODE_PATH=/Users/<user>/.hermes/hermes-agent/node_modules node e2e-test.js
```

**Staging server (only after dev testing is done):**
Kill port 3000, restart staging:
```bash
cd ~/staging_apps/lemonade-stand && npm run dev -- -H 0.0.0.0
```

## GitHub Setup
**One-time only — after repo creation on GitHub:**
```bash
cd ~/ralph/projects/lemonade-stand
git remote add origin git@github.com:blockops1/lemonade-stand-zen.git  # SSH (preferred)
git branch -M main
git push -u origin main
```
Repo: https://github.com/blockops1/lemonade-stand-zen

**generateUUID() browser compatibility — CRITICAL:** See `references/browser-compatibility.md` for the full breakdown of three `crypto.randomUUID()` call patterns, which files are affected, and how to test.

**Push timeout:** First push may time out at 60s even on successful push. Check `git remote -v` to confirm the push actually landed. Retry if needed — subsequent pushes usually succeed.

## Git Hygiene Rules
- **COMMIT BEFORE SYNC — every time.** Uncommitted working-tree changes are invisible to rsync.
- **Use `git add -- [paths]` or `git add -p`** — never `git add .` (picks up everything including archive directories).
- **Always `git status` before and after** any file operation in project directories.
- **Divergent histories are fine** — if dev and staging have different commits but identical file contents, leave it. Don't force-sync just to match git history. Files are the source of truth, not `git log`.
- If a commit gets polluted with wrong files: `git revert` or interactive rebase before pushing. Once on GitHub, rewriting history requires force-push.

## Sync Workflow — Bidirectional

The sync script runs **dev → staging only**. But fixes often get stranded in staging (staging-only git commits, or edits made directly in staging). Running sync blindly overwrites staging with stale dev files.

**The iron rule: COMMIT BEFORE SYNC. Every time.**

```
git add [files] && git commit -m "description"   ← do this FIRST
~/ralph/sync-to-staging.sh --apply               ← then this
```

**Why this matters:** The sync script rsyncs from dev to staging. If your fixes are only in the working tree (not committed), rsync sends the old committed version, not your new changes. The running server then hot-reloads the stale committed code and you waste time wondering why "fixes aren't working."

**The failure pattern (2026-05-01):**
1. Edit LemonadeStand.ts → AD_COSTS correct in working tree
2. Don't git add/commit
3. Run sync → rsync sends the old committed version to staging
4. Declare "fixed!" while git still has the old values
5. Later: `git log` shows the commit was never made

**Verification checklist — run before and after sync:**

```bash
# BEFORE: is working tree clean? are your changes committed?
cd ~/ralph/projects/lemonade-stand && git status   # must say "nothing to commit"
git log --oneline -3                                # your fix must appear here

# AFTER: did staging get the right values?
grep AD_COSTS ~/staging_apps/lemonade-stand/src/game/LemonadeStand.ts
```

**If you find uncommitted changes after a sync failure:**
```bash
# Option 1: commit them (correct approach)
cd ~/ralph/projects/lemonade-stand
git add src/game/LemonadeStand.ts && git commit -m "fix: AD_COSTS"
~/ralph/sync-to-staging.sh --apply

# Option 2: copy staging's newer file back to dev (if staging has the fix)
cp ~/staging_apps/lemonade-stand/src/game/LemonadeStand.ts \
   ~/ralph/projects/lemonade-stand/src/game/
# then commit from staging's version
cd ~/ralph/projects/lemonade-stand && git add src/game/LemonadeStand.ts && git commit -m "fix: AD_COSTS (from staging)"
```

**When sync went wrong (do NOT run sync again):**
- Staging has correct values, dev has wrong → copy staging → dev, then commit
- Both have correct values but different from circuit → update circuit, commit, sync

```
Fixes made in staging (UI/logic testing):
  staging → dev: cp ~/staging_apps/lemonade-stand/src/game/LemonadeStand.ts \
                   ~/ralph/projects/lemonade-stand/src/game/

Fixes made in dev (circuit, contracts):
  dev → staging: ~/ralph/sync-to-staging.sh --apply
  Then restart staging server: kill port 3000 + restart
```

**Signs staging has the newer version (do NOT run sync):**
- Game INITIAL STATE log shows wrong STARTING_MONEY (e.g. 120000 = $12,000 instead of 1200 = $120)
- AD_COSTS values differ between dev and staging `grep AD_COSTS`

**Always after a server restart:** verify the running code is actually the new code by checking the INITIAL STATE log or the home page money display. Next.js dev server hot-reloads but can get stuck on stale compiled output — always restart fully (`pkill node` + restart) when in doubt.

When game logic changes (LemonadeStand.ts, WeatherOracle.ts, pages):
1. Edit in `~/ralph/projects/` or `~/staging_apps/` (staging is faster for UI testing)
2. **Commit to dev git FIRST** — `git add [files] && git commit -m "description"`
3. If in staging: `~/ralph/sync-to-staging.sh --apply` pushes to dev (preserves git history)
4. **Restart staging server fully:** `kill $(lsof -ti:3000)` + restart (hot-reload can get stuck on stale output)
5. Test at `localhost:3000`

**Old pattern (WRONG): editing production_apps directly.** Always stop. Fix in staging/dev, sync, redeploy.

## Run Simulation
```
cd ~/staging_apps/lemonade-stand && npx tsx test-simulation.ts
```
Requires dev server stopped first (port 3000 conflict).
