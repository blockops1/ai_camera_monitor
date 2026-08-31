/**
 * Lemonade Stand Simulation — 20 runs, fixed strategy
 * Usage: npx tsx test-simulation.ts
 *
 * Plays 20 full 7-day games using the actual LemonadeStand class.
 * Derives weather deterministically via WeatherOracle.deriveWeatherLocal.
 * Strategy is fixed (30 cups/day inventory, $0.30 price, no ads).
 */

import { LemonadeStand } from './src/game/LemonadeStand';
import { WeatherOracle } from './src/game/WeatherOracle';

// ─── Constants ───────────────────────────────────────────────────────────────
const STARTING_MONEY = 1200; // $120
const MAX_DAYS = 7;
const RECIPE = { lemons: 2, sugar: 1, ice: 3 };

// ─── Strategy ───────────────────────────────────────────────────────────────
function playGame(sessionId: string): {
  finalScore: number;
  finalMoney: number;
  won: boolean;
  daysPlayed: number;
  totalSales: number;
  totalRevenue: number;
  dayByDay: { day: number; weather: number; base: number; wm: number; demand: number; sales: number; revenue: number; money: number }[];
} {
  const game = new LemonadeStand();
  const dayByDay: { day: number; weather: number; base: number; wm: number; demand: number; sales: number; revenue: number; money: number }[] = [];

  // Fixed strategy: buy 30 cups worth of ingredients each day
  const cupsWanted = 30;

  for (let day = 1; day <= MAX_DAYS; day++) {
    const state = game.getState();
    if (state.gameOver) break;

    // Derive today's weather deterministically (matches game logic)
    const weather = WeatherOracle.deriveWeatherLocal(sessionId, day - 1);

    // Buy ingredients at start of day
    game.buyIngredients('lemons', cupsWanted * RECIPE.lemons);
    game.buyIngredients('sugar', cupsWanted * RECIPE.sugar);
    game.buyIngredients('ice', cupsWanted * RECIPE.ice);

    // Fixed price: $0.30 (tier 0 = mult 30)
    game.setLemonadePrice(30);

    // No advertising (mult = 8)
    game.setAdvertising('none');

    // Simulate the day
    const result = game.simulateDay(weather);

    // Recompute demand for logging (mirrors LemonadeStand.ts formula)
    const WEATHER_MULTS = [7, 10, 12, 15];
    const wm = WEATHER_MULTS[weather] ?? 12;
    const BASE_CUSTOMERS_PER_DAY = Array.from({ length: 7 }, (_, i) => Math.round(20 * Math.pow(i + 1, 1.2)));
    const dayBase = BASE_CUSTOMERS_PER_DAY[day - 1] ?? 200;
    const demand = Math.min(Math.floor((dayBase * wm * 30 * 8) / 1000), 200);

    dayByDay.push({
      day,
      weather,
      base: dayBase,
      wm,
      demand,
      sales: result.sales,
      revenue: result.newMoney - state.money + result.advertisingCost,
      money: result.newMoney,
    });

    if (result.gameOver) break;
  }

  const summary = game.getGameSummary();
  return {
    finalScore: summary.finalScore ?? 0,
    finalMoney: summary.finalScore ?? 0,
    won: summary.won,
    daysPlayed: summary.totalDays,
    totalSales: summary.totalCustomers,
    totalRevenue: summary.totalRevenue,
    dayByDay,
  };
}

// ─── Run 20 simulations ──────────────────────────────────────────────────────
const NUM_RUNS = 20;
const results: ReturnType<typeof playGame>[] = [];

for (let i = 0; i < NUM_RUNS; i++) {
  const sessionId = `sim-run-${i + 1}-${Date.now()}`;
  const result = playGame(sessionId);
  results.push(result);
}

// ─── Summary statistics ──────────────────────────────────────────────────────
const scores = results.map(r => r.finalScore);
const mean = scores.reduce((a, b) => a + b, 0) / NUM_RUNS;
const sorted = [...scores].sort((a, b) => a - b);
const median = NUM_RUNS % 2 === 0
  ? (sorted[NUM_RUNS / 2 - 1] + sorted[NUM_RUNS / 2]) / 2
  : sorted[Math.floor(NUM_RUNS / 2)];

const wins = results.filter(r => r.won).length;

// ─── Print results ───────────────────────────────────────────────────────────
console.log('\n══════════════════════════════════════════════');
console.log('  LEMONADE STAND — 20 RUN SIMULATION');
console.log('  Strategy: 30 cups/day ingredients, $0.30 price, no ads');
console.log('══════════════════════════════════════════════\n');

console.log(`  Starting money: $${(STARTING_MONEY / 10).toFixed(2)}`);
console.log(`  ZK_MAX_CUSTOMERS: 200`);
console.log(`  Base demand curve: 20 * day^1.2 = [20, 45, 77, 106, 135, 163, 200]`);
console.log(`  Weather: deterministic (WeatherOracle.deriveWeatherLocal)\n`);
console.log(`  Mean:   $${(mean / 10).toFixed(2)}`);
console.log(`  Median: $${(median / 10).toFixed(2)}`);
console.log(`  Min:    $${(sorted[0] / 10).toFixed(2)}`);
console.log(`  Max:    $${(sorted[sorted.length - 1] / 10).toFixed(2)}`);
console.log(`  Wins:   ${wins}/20 (final money > $120)\n`);

// Histogram
const buckets: Record<string, number> = {};
for (const s of scores) {
  const bucket = Math.floor(s / 100) * 100;
  const key = `$${(bucket / 10).toFixed(0)}–$${((bucket + 100) / 10).toFixed(0)}`;
  buckets[key] = (buckets[key] ?? 0) + 1;
}
console.log('  Histogram (final money):');
for (const [range, count] of Object.entries(buckets).sort((a, b) =>
  parseInt(a[0]) - parseInt(b[0])
)) {
  console.log(`    ${range.padEnd(12)} ${'█'.repeat(count)} (${count})`);
}

console.log('\n  Per-run results:\n');
console.log('  Run  Score    Won  Days  Cups  Revenue');
console.log('  ───  ───────  ───  ───  ────  ───────');
results.forEach((r, i) => {
  const scoreStr = `$${(r.finalScore / 10).toFixed(2)}`.padStart(8);
  console.log(
    `  ${String(i + 1).padStart(3)}  ${scoreStr}  ${r.won ? '✓' : '✗'}   ${r.daysPlayed}    ${r.totalSales.toString().padStart(4)}  $${(r.totalRevenue / 10).toFixed(2)}`
  );
});

console.log('\n  Day-by-day for run 1:\n');
console.log('  Day  Weather   Base   wm   Demand  Cups   Revenue   Money');
console.log('  ──  ────────  ────  ───  ──────  ─────  ────────  ───────');
results[0].dayByDay.forEach(d => {
  const weatherNames = ['Rainy', 'Cloudy', 'Sunny', 'Hot'];
  const wName = weatherNames[d.weather] ?? `W${d.weather}`;
  console.log(
    `   ${d.day}   ${wName.padEnd(8)}  ${String(d.base).padStart(4)}  ${String(d.wm).padStart(3)}  ${String(d.demand).padStart(6)}  ${String(d.sales).padStart(5)}  $${(d.revenue / 10).toFixed(2).padStart(7)}  $${(d.money / 10).toFixed(2)}`
  );
});

console.log('\n');
