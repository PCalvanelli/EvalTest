// Uncertainty for pass rates. A setup's score depends on which prompts it was asked, so intervals come from a
// bootstrap over prompts (not repeats): with 22 prompts, one prompt is 4.5 points. Two setups are compared prompt by
// prompt, since they answered the same prompts. Every function takes per-prompt outcomes: one array per prompt of
// 1/0 for each scored repeat (an empty array when no repeat was scored). Resampling is seeded, so numbers don't
// jitter between renders.

const B = 2000;  // bootstrap resamples

function rng(seed) {  // mulberry32
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

const mean = (xs) => xs.reduce((s, x) => s + x, 0) / xs.length;

/** The q-quantile (0–1) of xs, linearly interpolated. */
export function quantile(xs, q) {
  if (!xs.length) return null;
  const s = [...xs].sort((a, b) => a - b);
  const i = (s.length - 1) * q;
  const lo = Math.floor(i);
  return s[lo] + (s[Math.min(lo + 1, s.length - 1)] - s[lo]) * (i - lo);
}

function bootstrap(values, statistic, seed) {
  const next = rng(seed);
  const out = [];
  for (let b = 0; b < B; b++) {
    const sample = values.map(() => values[Math.floor(next() * values.length)]);
    out.push(statistic(sample));
  }
  return [quantile(out, 0.025), quantile(out, 0.975)];
}

/** 95% intervals for the mean pass rate and pass^k, or null when no prompt was scored. */
export function passRateCI(perPrompt) {
  const scored = perPrompt.filter((xs) => xs.length);
  if (!scored.length) return null;
  const rows = scored.map((xs) => ({rate: mean(xs), always: xs.every(Boolean) ? 1 : 0}));
  return {
    rate: bootstrap(rows, (s) => mean(s.map((r) => r.rate)), 1),
    passK: bootstrap(rows, (s) => mean(s.map((r) => r.always)), 2),
  };
}

// Exact two-sided McNemar test: of the prompts where the two setups disagree, is the split further from 50/50 than
// chance would make it?
function mcnemar(aOnly, bOnly) {
  const n = aOnly + bOnly;
  if (!n) return 1;
  let tail = 0;
  for (let k = 0; k <= Math.min(aOnly, bOnly); k++) tail += binom(n, k) / 2 ** n;
  return Math.min(1, 2 * tail);
}

function binom(n, k) {
  let c = 1;
  for (let i = 1; i <= k; i++) c = (c * (n - k + i)) / i;
  return c;
}

/**
 * Setup A against setup B on the prompts both scored. `diff`: A's pass rate minus B's (−1 to 1), with a paired
 * bootstrap 95% interval. `aOnly` / `bOnly`: prompts only one of them gets right by majority of repeats, and `p`, the
 * McNemar p-value on those. `clear`: the interval excludes 0. Null when they share no scored prompt.
 */
export function pairedCompare(a, b) {
  const pairs = a.map((xs, i) => [xs, b[i] ?? []]).filter(([x, y]) => x.length && y.length)
    .map(([x, y]) => ({a: mean(x), b: mean(y)}));
  if (!pairs.length) return null;
  const right = (r) => r > 0.5;  // majority of repeats
  const aOnly = pairs.filter((p) => right(p.a) && !right(p.b)).length;
  const bOnly = pairs.filter((p) => !right(p.a) && right(p.b)).length;
  const diff = mean(pairs.map((p) => p.a - p.b));
  const ci = bootstrap(pairs, (s) => mean(s.map((p) => p.a - p.b)), 3);
  return {n: pairs.length, diff, ci, aOnly, bOnly, p: mcnemar(aOnly, bOnly), clear: ci[0] > 0 || ci[1] < 0};
}
