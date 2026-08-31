---
name: lemonade-stand
description: ZK circuit constants for lemonade stand — recipe, costs, demand, ZK_MAX values in cent units
category: gaming
---

# ZK Circuit Constants — Lemonade Stand

> **Status: CURRENT as of 2026-05-09**
> Recipe: 1 lemon + 2 sugar + 12 ice. Cost/cup = 40 cents.
> AD_COSTS MISMATCH: **RESOLVED** — both TypeScript and circuit now use cents.

## Unit System
All monetary values in **cents** (integer). `12000 = $120.00`.

## Recipe & Costs
- 1 lemon @ **20 cents** ($0.20) ← lowered 2026-05-09 (was $0.50)
- 2 sugar @ **4 cents each** ($0.04) → 8 cents total
- 12 ice @ **1 cent each** ($0.01) → 12 cents total
- **Cost per cup: 40 cents** (1×20 + 2×4 + 12×1)

## Customer Count Formula
```
totalCustomers = floor(BASE[day] * wm * pm * am / 100)
```
- `BASE` = `[3, 5, 7, 9, 11, 13, 16]` (days 1–7)
- `wm` = `[15, 12, 10, 7]` (hot/sunny/cloud/rainy) ÷ 10 → [1.5, 1.2, 1.0, 0.7]
- `pm` = `[50, 38, 32, 27, 23, 17, 9, 6]` (8 tiers, tier = min(floor(price/100), 7))
- `am` = `[25, 18, 12, 10]` (radio/social/flyers/none) ÷ 10 → [2.5, 1.8, 1.2, 1.0]
- `ZK_MAX_CUSTOMERS = 6000` (hard cap)

## Base Demand Curve
`BASE_CUSTOMERS_PER_DAY = [3, 5, 7, 9, 11, 13, 16]`

## Per-Day Best-Case (Hot × tier3 $3.50 × radio)
Formula: `floor(BASE × 15 × 27 × 25 / 100)` — hot(wm=15), $3.50(tier3,pm=27), radio(am=25)
Profit = `customers × 350 - customers × 40 - 4500` = `customers × 310 - 4500`

| Day | BASE | Raw Calc | Customers | Revenue | COGS | AdCost | Profit |
|-----|------|----------|-----------|---------|------|--------|--------|
| 1 | 3 | 3×15×27×25=30375/100=303 | 303 | $1,060 | $121 | $45 | $894 |
| 2 | 5 | 506 | 506 | $1,771 | $202 | $45 | $1,524 |
| 3 | 7 | 708 | 708 | $2,478 | $283 | $45 | $2,150 |
| 4 | 9 | 911 | 911 | $3,188 | $364 | $45 | $2,779 |
| 5 | 11 | 1,113 | 1,113 | $3,896 | $445 | $45 | $3,406 |
| 6 | 13 | 1,316 | 1,316 | $4,606 | $526 | $45 | $4,035 |
| 7 | 16 | 1,620 | 1,620 | $5,670 | $648 | $45 | $4,977 |
| **Total** | | | | **$22,669** | | | **$19,765** |

**ZK_MAX_TOTAL_PROFIT = 1,976,000** (cents = ~$19,765)

## Price Multiplier Tiers
`tier = min(floor(price / 100), 7)` — 8 tiers:
- $0.50–$0.99 → mult **50** (demand × 5.0)
- $1.00–$1.49 → mult **38** (demand × 3.8)
- $1.50–$1.99 → mult **32** (demand × 3.2)
- $2.00–$2.99 → mult **27** (demand × 2.7)
- $3.00–$3.99 → mult **23** (demand × 2.3)
- $4.00–$4.99 → mult **17** (demand × 1.7)
- $5.00–$5.99 → mult **9** (demand × 0.9)
- $6.00+ → mult **6** (demand × 0.6)

## Hard Cap Constants (Noir lib.nr) — CURRENT

```noir
global STARTING_MONEY: u64 = 12000;  // $120.00 in cents

global BASE_CUSTOMERS_PER_DAY: [u32; 7] = [3, 5, 7, 9, 11, 13, 16];
global ZK_MAX_CUSTOMERS: u32 = 6000;

global LEMON_COST: u64 = 20;   // cents ← lowered 2026-05-09 (was 50)
global SUGAR_COST: u64 = 4;    // cents
global ICE_COST: u64 = 1;       // cents

// Price multiplier tiers (scaled ×10)
global PRICE_MULTS: [u32; 8] = [50, 38, 32, 27, 23, 17, 9, 6];

// AD costs (cents) — matches TypeScript config.ts
global AD_COSTS: [u64; 4] = [0, 900, 2400, 4500];
global AD_MULTS: [u32; 4] = [10, 12, 18, 25];  // ÷10 → [1.0, 1.2, 1.8, 2.5]

global ZK_MAX_DAILY_REVENUE: [u64; 7] = [
    300000, 500000, 700000, 900000, 1100000, 1400000, 1800000
];
global ZK_MAX_TOTAL_REVENUE: u64 = 6200000;
global ZK_MAX_DAILY_PROFIT: [u64; 7] = [
    270000, 460000, 650000, 840000, 1030000, 1310000, 1695000
];
global ZK_MAX_TOTAL_PROFIT: u64 = 1976000;
global ZK_MAX_MONEY: u64 = 5000000;
```

## TypeScript Constants (from config.ts — single source of truth)

```typescript
RECIPE = { lemonsPerCup: 1, sugarPerCup: 2, icePerCup: 12 }
INGREDIENT_COSTS = { lemons: 20, sugar: 4, ice: 1 }  // cents ← 2026-05-09 (was 50)
STARTING_MONEY = 12000  // cents
COST_PER_CUP = 40       // 1×50 + 2×4 + 12×1 = 40
AD_COSTS = [0, 900, 2400, 4500]  // cents — matches circuit
BASE_CUSTOMERS_PER_DAY = [3, 5, 7, 9, 11, 13, 16]
PRICE_MULTS = [50, 38, 32, 27, 23, 17, 9, 6]
```

## Game Balance Summary (2026-05-09)
| Scenario | Final Money |
|----------|------------|
| Worst: rainy + no ads + $6.00 | ~$1,610 |
| Rainy + social + $3.50 | ~$5,684 |
| Hot + radio + $3.50 (optimal) | ~$16,908 |

$5,000 target is achievable with decent weather reading + social/radio ads.

## Three-System Alignment Checklist

| File | Update when changing |
|------|-----------------------|
| `src/game/config.ts` | Recipe, costs, BASE, AD_COSTS, PRICE_MULTS |
| `circuits/src/lib.nr` | Same literal values + ZK_MAX_* bounds |
| `.env.local` | NEXT_PUBLIC_* overrides (optional) |

**Always:** 1) Update config.ts, 2) Update lib.nr, 3) `nargo build`, 4) Commit circuit artifact, 5) Push.

## Compile Command
```bash
cd ~/ralph/projects/lemonade-stand/circuits
docker run --rm --entrypoint bash -v ~/ralph/projects:/app/projects ralph-local:latest \
  -c "cd /app/projects/lemonade-stand/circuits && nargo build"
```
Exit 0 = success. Commit `circuits/target/lemonade_stand.json`.
