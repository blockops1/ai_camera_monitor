# Advertising Cost Bug — Radio Unbuyable

**Date:** 2026-05-01  \
**Severity:** Game design blocker  \
**Status:** ✅ RESOLVED — all three systems now aligned

## Problem

Radio advertising is impossible to purchase with a $120 starting bankroll.

```
AD_COSTS.radio = 4500  (10-cent units = $450.00)
Player bankroll: $120.00
```

Every day the player selects "Radio" and the game silently logs:
```
[WARN] Cannot afford advertising {"type":"radio","cost":4500,"money":1105}
```
Then falls back to no advertising (ad_type="none").

This means the radio ad tier is completely untestable in free mode and effectively dead code.

## Root Cause

`AD_COSTS` in `LemonadeStand.ts` defines radio cost as 4500 (10-cent units). Either:
- The value is wrong (should be 450 = $45?) and was accidentally scaled by 10x, OR
- The radio cost is intentional but starting money ($120) is too low for any advertising

## Affected Files
- `~/staging_apps/lemonade-stand/src/game/LemonadeStand.ts`

## Resolution

Selected Option 1 (scale down radio cost to match $120 bankroll):

| Ad tier | Internal cost | Display | Status |
|---------|--------------|---------|--------|
| None | 0 | $0 | ✅ always available |
| Flyers | 90 | $9 | ✅ always affordable |
| Social | 240 | $24 | ✅ affordable Day 1 |
| Radio | 450 | $45 | ⚠️ only affordable Day 1 with >$50 remaining |

**Three systems updated:**
1. `LemonadeStand.ts`: `AD_COSTS = [0, 90, 240, 450]` (was [0, 900, 2400, 4500])
2. `GameState.ts`: `setAdvertising()` costs → [0, 90, 240, 450] (was [0, 30, 80, 150])
3. Circuit `lib.nr`: `global AD_COSTS: [u64; 4] = [0, 90, 240, 450]` (was [0, 900, 2400, 4500])

Also recalculated ZK_MAX_DAILY_PROFIT (+7020/day cheaper ads) and ZK_MAX_TOTAL_PROFIT (4,887,300 vs old 4,835,660).

## Verification

`e2e-test.js` shows `Cannot afford advertising` warning for radio on Day 2+ when money drops below $45. This is correct behavior — the game should force strategic decisions about ad spend.

## Verification

After fixing, `e2e-test.js` should show no `[WARN] Cannot afford` messages for the ad type being tested.
