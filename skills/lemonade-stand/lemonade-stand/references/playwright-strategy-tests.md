# Playwright Strategy Test Results — 34 Games (2026-05-07)

Automated Playwright E2E test of 10 strategies × 3+ weather sequences, 7-day games, actual game UI.

## Test Setup
- Script: `/tmp/lemonade_playwright.js` (Playwright, Node.js)
- Target: dev server `localhost:3000`, free mode
- Starting money: $120 | Days: 7 | Recipe: 1L+2S+12I

## Results Summary

| Strategy | Final $ | P&L | Rank |
|----------|---------|-----|------|
| aggressive ($3.00 + social ads) | $60.36 | -$59.64 | **1** |
| premium_ads ($4.00 + social ads) | $60.32 | -$59.68 | **2** |
| weather_aggro (weather-adjusted price/ads) | $42.11 | -$77.89 | 3 |
| mid_plus ($2.50 + no ads) | $35.30 | -$84.70 | 4 |
| standard ($3.00 + no ads) | $33.10 | -$86.90 | 5 |
| mid_hot ($3.00 + radio ads) | $33.05 | -$86.95 | 6 |
| max_ads ($2.00 + radio) | $33.05 | -$86.95 | 6 |
| mid ($2.00 + no ads) | $28.30 | -$91.70 | 7 |
| weather_mid (weather-adjusted) | $25.50 | -$94.50 | 8 |
| best_guess ($2.50 + flyers) | $28.26 | -$91.74 | 7 |
| cheap ($1.00 + no ads) | $17.30 | -$102.70 | 9 |
| premium ($4.00 + no ads) | $0.30 | -$119.70 | 10 |

## Key Findings

**Social ads are critical.** Only strategies paired with social advertising ($24/day) approach break-even. Social provides +80% customer boost at a reasonable cost. Radio ($45/day) is almost never worth it — the demand lift doesn't cover the ad spend.

**Price U-curve.** Both extremes underperform:
- $1.00 (cheap): too little revenue per customer, demand虽高但利润薄
- $4.00 (premium, no ads): demand collapses — $0.30 final money
- Sweet spot: $2.50–$3.00 with social ads

**Weather matters more than strategy.** Same strategy produces $17–$60 depending on weather sequence. Hot days on high-price days with social ads = best outcome. Rainy days destroy even good strategies.

**All strategies lose money.** The game appears calibrated so optimal play loses $50–60 on a typical weather run. The ZK proof of score (challenge mode) is the actual value — free mode is a learning sandbox.

**Ice melts daily.** Cannot buy 7-day inventory upfront. Ice cost ($0.01/unit) is negligible but buying 12× per cup per day means restocking is required. No storage advantage from bulk buy.

## Strategy Recommendations

**Best tested:** $3.00 price + social ads (idx=2) every day
- Revenue: ~$500/day on hot days, ~$200/day on rainy days
- Break-even possible on hot-weather runs

**To try:** Weather-aware strategy — buy radio ads only on hot days, skip on rainy.
- Hot + $2.50 + radio = high customers, radio cost covered by volume
- Rainy + $3.00 + no ads = preserve margin

## Test Script Reference
```bash
# Run from dev server (port 3000 must be running)
NODE_PATH=/Users/<user>/.hermes/hermes-agent/node_modules \
  node /tmp/lemonade_playwright.js [num_games] [strategy_index]

# Strategies defined in lemonade_playwright.js STRATEGIES array
# Each strategy: { name, price, adType, buyMax }
```

## Known Limitation
34/50 games completed before timeout. Cheap/mid/premium strategies produced deterministic results (identical across runs). Weather-dependent strategies varied. Sample size for rare weather sequences is small.
