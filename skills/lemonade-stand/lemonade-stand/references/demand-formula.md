# Lemonade Stand — Demand Formula & Balance Reference

> **Status: CURRENT as of 2026-05-09** (post rebalance)
> **⚠️ Demand formula references (e.g. this file) become stale when config.ts changes.**
> **Always verify against `src/game/config.ts` as the source of truth.**

## Customer Count Formula
```
totalCustomers = Math.floor(BASE[day] * wm * pm * am / 100)
```
All four values stored as **scaled integers** (×10 actual), final `/100` converts back.

## Current Constants (from config.ts — 2026-05-09)

| Parameter | Array | Meaning | Index → Actual |
|-----------|-------|---------|----------------|
| `BASE` | `[3, 5, 7, 9, 11, 13, 16]` | Base customers by day | day 1→3, day 7→16 |
| `WEATHER_MULTS` | `[15, 12, 10, 7]` | Weather multiplier | 0=hot(1.5×), 1=sunny(1.2×), 2=cloudy(1.0×), 3=rainy(0.7×) |
| `PRICE_MULTS` | `[50, 38, 32, 27, 23, 17, 9, 6]` | Price tier multiplier | tier = min(floor(price/100), 7) |
| `AD_MULTS` | `[25, 18, 12, 10]` | Ad multiplier | 0=radio(2.5×), 1=social(1.8×), 2=flyers(1.2×), 3=none(1.0×) |

**⚠️ Index 0 for both weather and ads is the BEST option** (hot=1.5×, radio=2.5×).
Past bug: "none" ads was at index 0 with value 10, causing ×1.0 demand but stored as a discount tier.

## Price Tier Lookup
`tier = min(floor(price_cents / 100), 7)`

| Price | Cents | Tier | pm |
|-------|-------|------|----|
| $0.50–$0.99 | 50–99 | 0 | 50 |
| $1.00–$1.49 | 100–149 | 1 | 38 |
| $1.50–$1.99 | 150–199 | 2 | 32 |
| $2.00–$2.99 | 200–299 | 3 | 27 |
| $3.00–$3.99 | 300–399 | 4 | 23 |
| $4.00–$4.99 | 400–499 | 5 | 17 |
| $5.00–$5.99 | 500–599 | 6 | 9 |
| $6.00+ | 600+ | 7 | 6 |

## Worked Examples

**Day 1, hot, radio, $3.00:**
`floor(3 × 15 × 27 × 25 / 100) = floor(303.75) = 303 customers`

**Day 1, rainy, no ads, $2.50:**
`floor(3 × 7 × 27 × 10 / 100) = floor(56.7) = 56 customers`

**Day 7, hot, radio, $0.50 (capacity hit):**
`floor(16 × 15 × 50 × 25 / 100) = floor(3000) = 3,000 customers`

## Per-Day Best-Case (Hot × radio × $3.50)
Formula: `floor(BASE × 15 × 27 × 25 / 100)` — wm=15 (hot), pm=27 ($3.50 tier), am=25 (radio)

| Day | BASE | Customers | Revenue | COGS (40¢/cup) | Ad Cost | Profit |
|-----|------|-----------|---------|----------------|---------|--------|
| 1 | 3 | 303 | $1,060 | $121 | $45 | $894 |
| 2 | 5 | 506 | $1,771 | $202 | $45 | $1,524 |
| 3 | 7 | 708 | $2,478 | $283 | $45 | $2,150 |
| 4 | 9 | 911 | $3,188 | $364 | $45 | $2,779 |
| 5 | 11 | 1,113 | $3,896 | $445 | $45 | $3,406 |
| 6 | 13 | 1,316 | $4,606 | $526 | $45 | $4,035 |
| 7 | 16 | 1,620 | $5,670 | $648 | $45 | $4,977 |
| **Total** | | | **$22,669** | | | **$19,765** |

## Cost Per Cup — Two Numbers

| Metric | Value | Calculation |
|--------|-------|-------------|
| `COST_PER_CUP` (ZK/demand) | **40 cents** | 1×50 + 2×4 + 12×1 |
| True purchase cost | **70 cents** | lemons $0.50 + sugar $0.08 + ice $0.12 |

The game UI buys at true cost ($0.70/cup). ZK proofs use `COST_PER_CUP = 40`.
Demand modeling uses 40 cents — this is what the PM/BASE curve was tuned for.

## Game Balance Summary (2026-05-09)

| Scenario | Expected Final Money |
|----------|---------------------|
| Rainy + no ads + $6.00 (worst) | ~$1,610 |
| Rainy + social + $3.50 | ~$5,684 |
| Hot + radio + $3.50 (optimal) | ~$16,908 |

$5,000 target is at the skill ceiling — requires good weather reading + social/radio ads.

## Historical Bug Record

- **AD_COSTS in dollars vs. cents (fixed 2026-05-08):** Circuit stored `[0, 90, 240, 450]` (dollars) while TypeScript stored `[0, 900, 2400, 4500]` (cents). ZK proofs rejected any ad spend >$0.
- **"none" ads at index 0 (fixed pre-2026-05-07):** Old `AD_MULTS=[10,12,18,25]` put "none" at index 0 with ×1.0 — but index 0 was also the selected index when switching away from a paid ad, causing demand to incorrectly use a ×1.0 tier multiplier instead of the base ×1.0.
