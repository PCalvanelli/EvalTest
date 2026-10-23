// Ranking: one weighted score per setup from accuracy, cost and speed, so a run names a winner. Accuracy is the pass
// rate as is; cost and speed are scored against the best setup in the run (best / this), so the cheapest scores 1 and
// one costing twice as much scores 0.5. Weights are a per-viewer preference, kept in localStorage.

import {$, esc, pct, fmtS, fmtUSD} from './ui.js';

export const FACTORS = [
  {key: 'accuracy', label: 'Accuracy', hint: 'Average pass rate'},
  {key: 'cost', label: 'Cost', hint: 'Average cost per prompt, against the cheapest setup'},
  {key: 'speed', label: 'Speed', hint: 'Average agent time per prompt (not time waiting for a slot), against the fastest setup'},
];
const DEFAULT_WEIGHTS = {accuracy: 60, cost: 20, speed: 20};
const STORE = 'ranking-weights';

let weights = load();

function load() {
  try {
    const saved = JSON.parse(localStorage.getItem(STORE));
    if (saved && FACTORS.every((f) => Number.isFinite(saved[f.key]))) return saved;
  } catch { /* storage unavailable: use the defaults */ }
  return {...DEFAULT_WEIGHTS};
}

function save() {
  try { localStorage.setItem(STORE, JSON.stringify(weights)); } catch { /* not persisted; fine */ }
}

/**
 * Rank setups. `entries`: [{id, label, accuracy, cost (USD per prompt or null), seconds (per prompt)}].
 * Returns {ranked: entries best first, each with score (0–1) and parts {accuracy, cost, speed} (0–1), and
 * noCost: true when cost was left out because some setup has none}.
 */
export function rank(entries) {
  const noCost = entries.some((e) => e.cost == null);
  const w = {...weights, ...(noCost ? {cost: 0} : {})};
  const total = FACTORS.reduce((s, f) => s + w[f.key], 0) || 1;
  const best = (xs) => Math.min(...xs.filter((x) => x > 0));
  const minCost = noCost ? null : best(entries.map((e) => e.cost));
  const minSeconds = best(entries.map((e) => e.seconds));
  const relative = (min, x) => (x > 0 && isFinite(min) ? min / x : 1);
  const ranked = entries.map((e) => {
    const parts = {accuracy: e.accuracy, cost: noCost ? 0 : relative(minCost, e.cost), speed: relative(minSeconds, e.seconds)};
    const score = FACTORS.reduce((s, f) => s + (w[f.key] / total) * parts[f.key], 0);
    return {...e, parts, score};
  }).sort((a, b) => b.score - a.score || b.accuracy - a.accuracy);
  return {ranked, noCost};
}

const share = (key, noCost) => {
  const w = {...weights, ...(noCost ? {cost: 0} : {})};
  const total = FACTORS.reduce((s, f) => s + w[f.key], 0);
  return total ? Math.round((100 * w[key]) / total) : Math.round(100 / FACTORS.length);
};
const weightSummary = (noCost) => FACTORS.filter((f) => !(noCost && f.key === 'cost'))
  .map((f) => `${f.label.toLowerCase()} ${share(f.key, noCost)}%`).join(', ');
const score100 = (s) => Math.round(100 * s);

/** The winner callout above the grid. `final`: every setup has finished. `note`: trusted HTML shown under it. */
export function winnerHtml({ranked, noCost}, final, note = '') {
  if (!ranked.length) return '';
  const w = ranked[0];
  return `<div class="notice notice-ok flex flex-wrap items-baseline gap-x-3 gap-y-1">
    <span class="font-semibold">${final ? 'Winner' : 'Leading so far'}: ${esc(w.label)}</span>
    <span class="tabular-nums text-ink-muted">score ${score100(w.score)} · ${pct(w.accuracy)} pass · ${noCost ? '' : `${fmtUSD(w.cost)} / prompt · `}${fmtS(w.seconds)} / prompt</span>
    <button type="button" data-goto="ranking" class="link ml-auto text-xs">Weighted ${esc(weightSummary(noCost))} · see ranking</button>
    ${note ? `<span class="basis-full text-xs tabular-nums text-ink-muted">${note}</span>` : ''}
  </div>`;
}

/** The ranking table. */
export function tableHtml({ranked, noCost}) {
  const head = `<tr><th>#</th><th>Setup</th><th class="!text-right">Score</th><th class="!text-right">Pass rate</th>
    ${noCost ? '' : '<th class="!text-right">Cost / prompt</th>'}<th class="!text-right">Time / prompt</th></tr>`;
  const sub = (x) => `<span class="ml-1 text-xs text-ink-subtle">${score100(x)}</span>`;
  const rows = ranked.map((e, i) => `<tr class="${i === 0 ? 'font-semibold' : ''}">
    <td class="tabular-nums text-ink-muted">${i + 1}</td>
    <td>${esc(e.label)}${i === 0 ? ' <span class="badge badge-pass ml-1">Winner</span>' : ''}</td>
    <td class="text-right tabular-nums">${score100(e.score)}</td>
    <td class="text-right tabular-nums">${pct(e.accuracy)}</td>
    ${noCost ? '' : `<td class="text-right tabular-nums">${fmtUSD(e.cost)}${sub(e.parts.cost)}</td>`}
    <td class="text-right tabular-nums">${fmtS(e.seconds)}${sub(e.parts.speed)}</td>
  </tr>`).join('');
  return head + rows;
}

/** The weight sliders, rendered once; `onChange` re-ranks. */
export function initWeights(onChange) {
  const el = $('#weights');
  if (el.childElementCount) return;
  el.innerHTML = FACTORS.map((f) => `<label class="flex flex-col" title="${esc(f.hint)}">
      <span class="group-label">${f.label} <span data-share="${f.key}" class="tabular-nums text-ink"></span></span>
      <input type="range" min="0" max="100" step="5" data-weight="${f.key}" class="w-40 accent-[rgb(var(--accent))]">
    </label>`).join('') + '<button type="button" id="weightsReset" class="btn btn-quiet">Reset</button>';
  const sync = () => FACTORS.forEach((f) => { el.querySelector(`[data-weight=${f.key}]`).value = weights[f.key]; });
  el.querySelectorAll('[data-weight]').forEach((i) => i.oninput = () => {
    weights = {...weights, [i.dataset.weight]: +i.value};
    save();
    onChange();
  });
  $('#weightsReset').onclick = () => { weights = {...DEFAULT_WEIGHTS}; save(); sync(); onChange(); };
  sync();
}

/** Show each factor's share of the total weight next to its slider. */
export function renderShares(noCost) {
  for (const f of FACTORS) {
    const el = $(`[data-share=${f.key}]`);
    if (el) el.textContent = noCost && f.key === 'cost' ? '(not available)' : `${share(f.key, noCost)}%`;
  }
}
