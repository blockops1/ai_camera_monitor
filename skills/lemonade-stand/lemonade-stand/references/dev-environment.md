# Lemonade Stand — Dev Environment Reference

## Directory Structure
```
~/ralph/projects/lemonade-stand/     # Dev workspace (circuits, foundry contracts, Ralph PRD, git)
~/staging_apps/lemonade-stand/      # Staging (Next.js app, test server port 3000)
~/production_apps/lemonade-stand/    # Production deployment — NEVER EDIT
```

## Workflow Rule
**NEVER edit files in `~/production_apps/`.** All development and testing happens in dev and staging. Production is a deployment target populated by sync, never authored to directly.

Rationale: editing production directly creates a "stranded fixes" problem — you make fixes in production but the canonical dev/staging copies are stale. This happened 2026-05-01.

## Sync Commands
```bash
# Step 1: Verify working tree is clean (changes must be committed FIRST)
cd ~/ralph/projects/lemonade-stand && git status
git log --oneline -3   # your fix must appear here

# Step 2: Sync dev → staging
~/ralph/sync-to-staging.sh --apply

# Step 3: Kill stale dev server (hot-reload can cache stale output)
kill $(lsof -ti:3000)

# Step 4: Restart from staging (NOT from production_apps)
cd ~/staging_apps/lemonade-stand
NEXT_PUBLIC_CHAIN_ID=2651420 \
NEXT_PUBLIC_RPC_URL=https://horizen-testnet.rpc.caldera.xyz/http \
NEXT_PUBLIC_CONTRACT_ADDRESS=0x0000000000000000000000000000000000000000 \
npm run dev -- --hostname 0.0.0.0 &

# Step 5: Verify the running code has your changes
grep AD_COSTS ~/staging_apps/lemonade-stand/src/game/LemonadeStand.ts
```

**The most common mistake (2026-05-01):** Edit files, skip `git add && git commit`, run sync. Rsync sends the old committed version. Now the staging server runs code that doesn't have your fix, and git doesn't have your fix either — it's in a no-man's land.

**Recovery when sync went wrong:**
```bash
# Staging has correct values, dev doesn't → copy staging back to dev, commit
cp ~/staging_apps/lemonade-stand/src/game/LemonadeStand.ts \
   ~/ralph/projects/lemonade-stand/src/game/
cd ~/ralph/projects/lemonade-stand
git add src/game/LemonadeStand.ts && git commit -m "fix: AD_COSTS (recovered from staging)"

# Dev has correct values, staging doesn't → commit dev, then sync, then restart
cd ~/ralph/projects/lemonade-stand && git add src/game/LemonadeStand.ts && git commit -m "fix: AD_COSTS"
~/ralph/sync-to-staging.sh --apply
kill $(lsof -ti:3000) && cd ~/staging_apps/lemonade-stand && npm run dev -H 0.0.0.0 &
```

## Dev Server (staging)
```bash
cd ~/staging_apps/lemonade-stand
NEXT_PUBLIC_CHAIN_ID=2651420 \
NEXT_PUBLIC_RPC_URL=https://horizen-testnet.rpc.caldera.xyz/http \
NEXT_PUBLIC_CONTRACT_ADDRESS=0x0000000000000000000000000000000000000000 \
npm run dev -- --hostname 0.0.0.0
```

## Circuit Compilation
```bash
cd ~/ralph/projects/lemonade-stand/circuits
docker run --rm \
  -v ~/ralph/projects/lemonade-stand:/app/projects/lemonade-stand \
  -w /app/projects/lemonade-stand/circuits \
  --entrypoint nargo ralph-local:latest build
```
Exit 0 = success. Warnings about private constants being invisible from main.nr are expected — does not affect proving.

## ZK Circuit Key Constants (cent units — updated 2026-05-09)
All monetary values in **cents**. `12000 = $120.00`. Switched from 10-cent units.

| Constant | Value | Note |
|---|---|---|
| `STARTING_MONEY` | 12000 | $120.00 in cents |
| `ZK_MAX_CUSTOMERS` | 6000 | Day 7 hot+tier0+radio = 3,000 (well under cap) |
| `ZK_MAX_TOTAL_PROFIT` | 1,976,000 | ~$19,760 (hot+radio+$3.50 all week) |
| `ZK_MAX_TOTAL_REVENUE` | 6,200,000 | Conservative upper bound |
| `BASE_CUSTOMERS_PER_DAY` | [3,5,7,9,11,13,16] | Day 1→7 — REBALANCED 2026-05-09 |
| Cost per cup | 40 cents | 1×20 + 2×4 + 12×1 (lemon $0.20, sugar $0.04, ice $0.01) |
| Recipe | 1 lemon + 2 sugar + 12 ice | per cup |

**⚠️ IMPORTANT (2026-05-09):** The actual `.env.local` values are `LEMON_COST=20` (cents, i.e. $0.20/lemon), `SUGAR_COST=4`, `ICE_COST=1`. This gives a true per-cup cost of $0.40 (NOT $0.70 as previously documented). The game economy was rebalanced around these values. The circuit constants in `circuits/src/lib.nr` must match.

## Weather Derivation (TypeScript ↔ Circuit parity)
TypeScript: `H(H(sessionId) + turn) % 4` → `deriveWeatherLocal()`
Circuit: `keccak256(keccak256(session_id)[0:4] || turn) % 4` → `derive_weather_free()`
Both must use the same nested hash pattern. Previous circuit used flat `H(session_id || turn)` — WRONG.

## Customer Formula (CORRECT — updated 2026-05-09)
```
floor(BASE[day] × weatherMult × priceMult × adMult / 100)
```
- `BASE` = `[3, 5, 7, 9, 11, 13, 16]` (day 1→7) — REBALANCED from [2,5,7,10,13,17,20]
- weatherMult: `[15, 12, 10, 7]` ÷ 10 = `[1.5, 1.2, 1.0, 0.7]` (hot/sunny/cloudy/rainy)
- priceMult: 8 tiers — `[50, 38, 32, 27, 23, 17, 9, 6]` (tier = min(floor(price/100), 7))
- adMult: `[25, 18, 12, 10]` ÷ 10 = `[2.5, 1.8, 1.2, 1.0]` (radio/social/flyers/none)
- `/100` converts three scaled-integer multipliers back to real values

Previous circuit: `(10 * wm * pm * am) / 1000` — WRONG. Was dividing by 1000 instead of 100, and had no BASE array.

## Non-ASCII Characters in Noir Comments
Noir's ACIR compiler does NOT support non-ASCII in comments. Use only ASCII characters:
- × → `x`
- − → `-`
- — → `--`
- ÷ → `/`
