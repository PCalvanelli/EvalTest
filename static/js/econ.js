// Economics: expected net value, bootstrap intervals, Pareto frontier, and the accuracy-vs-cost chart.

import {$, $$, esc, mean, pct, fmtUSD, stat} from './ui.js';
import {app, MODELS, CONTEXT_SHORT, modelName, cellName, modelSlot} from './config.js';
import {aggregate, reportsFor, selectCell, renderAll} from './results.js';
// Per question, with p = P(correct) and c = expected agent token cost in $:
//   U = p·V − (1 − p)·L − c        (V = value of a correct answer, L = loss from a wrong one)
// Uncertainty: cluster bootstrap over prompts (each draw keeps all runs of a resampled prompt). Every cell
// uses the SAME resampled prompt indices, so cell-vs-cell differences are paired.
const BOOT = 2000;
const scoring = () => ({
  V: Math.max(0, +$('#valueCorrect').value || 0),
  L: Math.max(0, +$('#lossWrong').value || 0),
  rankBy: $('#rankBy').value,
});

export function caseCost(c, model) {
  const p = app.pricing[model];
  if (!p) return NaN;  // unknown model price: cost can't be computed
  if (c.input_tokens == null) return 0;  // failed before any model call was billed
  const read = c.cache_read_tokens || 0, write = c.cache_write_tokens || 0;
  const fresh = Math.max(0, c.input_tokens - read - write);  // input_tokens includes cached tokens
  return (fresh * p.input + read * p.cache_read + write * p.cache_write + (c.output_tokens || 0) * p.output) / 1e6;
}

// Seeded RNG so intervals stay put between re-renders.
function mulberry32(seed) {
  return () => {
    seed = (seed + 0x6D2B79F5) | 0;
    let t = Math.imul(seed ^ (seed >>> 15), 1 | seed);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}
const bootCache = {};
function bootIndices(n) {
  if (!bootCache[n]) {
    const rnd = mulberry32(42);
    bootCache[n] = Array.from({length: BOOT}, () => Array.from({length: n}, () => Math.floor(rnd() * n)));
  }
  return bootCache[n];
}
function quantile(xs, q) {
  const v = [...xs].sort((a, b) => a - b);
  const i = (v.length - 1) * q, lo = Math.floor(i);
  return v[lo] + (v[Math.ceil(i)] - v[lo]) * (i - lo);
}
const utility = (p, c, {V, L}) => p * V - (1 - p) * L - c;

export function economics(id) {
  const cell = app.state.cells.find((c) => c.id === id);
  const rs = reportsFor(id);
  if (!cell || !rs.length) return null;
  // Per-prompt means over finished runs: the resampling unit.
  const ps = app.state.prompts.map((_, i) => {
    const cases = rs.map((r) => r.cases[i]).filter(Boolean);
    return {p: mean(cases.map((c) => (c.passed ? 1 : 0))), c: mean(cases.map((c) => caseCost(c, cell.model)))};
  });
  const s = scoring();
  const p = mean(ps.map((x) => x.p)), c = mean(ps.map((x) => x.c));
  const boots = bootIndices(ps.length).map((idx) => {
    const bp = mean(idx.map((i) => ps[i].p)), bc = mean(idx.map((i) => ps[i].c));
    return {p: bp, u: utility(bp, bc, s)};
  });
  return {
    id, cell, p, c, u: utility(p, c, s),
    costPerCorrect: p > 0 ? c / p : Infinity,
    pCI: [quantile(boots.map((b) => b.p), 0.025), quantile(boots.map((b) => b.p), 0.975)],
    uCI: [quantile(boots.map((b) => b.u), 0.025), quantile(boots.map((b) => b.u), 0.975)],
    boots,
  };
}

// Paired bootstrap difference in net value, a − b.
export function pairedDiff(a, b) {
  const d = a.boots.map((x, k) => x.u - b.boots[k].u);
  return {mean: a.u - b.u, ci: [quantile(d, 0.025), quantile(d, 0.975)], pBetter: mean(d.map((x) => (x > 0 ? 1 : 0)))};
}

export function econAll() {
  const out = {};
  for (const c of app.state.cells) { const e = economics(c.id); if (e) out[c.id] = e; }
  return out;
}

// Non-dominated cells: no other cell is at least as accurate AND no more expensive (one strictly).
export function paretoFrontier(econ) {
  const pts = Object.values(econ).filter((e) => isFinite(e.c));
  return pts.filter((a) => !pts.some((b) => b !== a && b.p >= a.p && b.c <= a.c && (b.p > a.p || b.c < a.c)))
    .sort((a, b) => a.c - b.c || a.p - b.p);
}

// What "best" means for the ★ and the ranking.
export function scoreOf(id, econ) {
  const a = aggregate(id), e = econ[id];
  if (!a) return -Infinity;
  const {rankBy} = scoring();
  if (rankBy === 'net_value') return e && isFinite(e.u) ? e.u : -Infinity;
  if (rankBy === 'pass_k') return app.state.repeats > 1 ? a.pass_k : a.pass_rate;
  return a.pass_rate;
}
export const rankLabel = () => ({net_value: 'expected net value', pass_k: app.state.repeats > 1 ? 'pass^k' : 'pass rate', pass_rate: 'mean pass rate'})[scoring().rankBy];


export function renderEconomics() {
  const econ = econAll();
  const all = Object.values(econ);
  if (!all.length) return;
  $('#econWrap').classList.remove('hidden');
  const frontier = paretoFrontier(econ);
  const onFrontier = new Set(frontier.map((e) => e.id));
  renderEconChart(all, frontier, onFrontier);
  renderEconTable(econ, frontier, onFrontier);
}

function renderEconChart(all, frontier, onFrontier) {
  const box = $('#econChart');
  const W = Math.max(320, box.clientWidth || 640), H = Math.round(Math.min(440, Math.max(300, W * 0.32))), m = {l: 48, r: 16, t: 12, b: 40};
  const pts = all.filter((e) => isFinite(e.c));
  const models = [...new Set(app.state.cells.map((c) => c.model))];
  $('#econLegend').innerHTML = models.map((id) =>
    `<span class="inline-flex items-center gap-1.5"><svg width="10" height="10"><circle cx="5" cy="5" r="4" fill="var(--series-${modelSlot(id)})"/></svg>${esc(modelName(id))}</span>`).join('') +
    `<span class="inline-flex items-center gap-1.5"><svg width="10" height="10"><circle cx="5" cy="5" r="4" fill="var(--text-secondary)"/></svg>harness on</span>` +
    `<span class="inline-flex items-center gap-1.5"><svg width="10" height="10"><circle cx="5" cy="5" r="3.5" fill="none" stroke="var(--text-secondary)" stroke-width="1.5"/></svg>harness off</span>` +
    `<span class="inline-flex items-center gap-1.5"><svg width="16" height="10"><path d="M0 5H16" stroke="var(--frontier)" stroke-width="2"/></svg>Pareto frontier</span>`;
  if (!pts.length) { box.innerHTML = '<p class="text-xs">No priced cells yet.</p>'; return; }

  // x: cost per question on a log scale (models differ ~40× in price); y: accuracy 0–100%.
  const positive = pts.map((e) => e.c).filter((c) => c > 0);
  let lo = positive.length ? Math.min(...positive) / 1.6 : 1e-4, hi = positive.length ? Math.max(...positive) * 1.6 : 1e-2;
  if (hi / lo < 10) { const mid = Math.sqrt(lo * hi); lo = mid / Math.sqrt(10); hi = mid * Math.sqrt(10); }
  const lx = (c) => Math.log10(Math.max(c, lo));
  const X = (c) => m.l + ((lx(c) - Math.log10(lo)) / (Math.log10(hi) - Math.log10(lo))) * (W - m.l - m.r);
  const Y = (p) => m.t + (1 - p) * (H - m.t - m.b);

  const xticks = [];
  for (let d = Math.floor(Math.log10(lo)); d <= Math.ceil(Math.log10(hi)); d++)
    for (const k of [1, 2, 5]) { const v = k * 10 ** d; if (v >= lo && v <= hi) xticks.push(v); }
  const grid = [0, 0.25, 0.5, 0.75, 1].map((p) =>
    `<line x1="${m.l}" x2="${W - m.r}" y1="${Y(p)}" y2="${Y(p)}" stroke="var(--grid)" stroke-width="1"/>
     <text x="${m.l - 6}" y="${Y(p) + 4}" text-anchor="end" font-size="11" fill="var(--text-muted)">${p * 100}%</text>`).join('') +
    xticks.map((v) => `<line x1="${X(v)}" x2="${X(v)}" y1="${m.t}" y2="${H - m.b}" stroke="var(--grid)" stroke-width="1"/>
     <text x="${X(v)}" y="${H - m.b + 16}" text-anchor="middle" font-size="11" fill="var(--text-muted)">${fmtUSD(v)}</text>`).join('') +
    `<text x="${(m.l + W - m.r) / 2}" y="${H - 6}" text-anchor="middle" font-size="11" fill="var(--text-secondary)">Agent token cost per question (log scale)</text>`;

  // Staircase frontier: between two frontier costs, the best attainable accuracy is the cheaper point's.
  let path = '';
  frontier.forEach((e, k) => {
    path += k === 0 ? `M${X(e.c)},${Y(e.p)}` : `H${X(e.c)}V${Y(e.p)}`;
  });
  const line = frontier.length > 1 ? `<path d="${path}" fill="none" stroke="var(--frontier)" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>` : '';

  // Direct labels on frontier points, skipping any that would collide with one already placed (the table names every cell).
  const placed = [];
  const fits = (x, y, w) => !placed.some((b) => x < b.x + b.w + 6 && b.x < x + w + 6 && Math.abs(y - b.y) < 14);
  const marks = pts.map((e) => {
    const col = `var(--series-${modelSlot(e.cell.model)})`, x = X(e.c), y = Y(e.p);
    const mark = e.cell.harness_on
      ? `<circle cx="${x}" cy="${y}" r="5" fill="${col}"/>`
      : `<circle cx="${x}" cy="${y}" r="4" fill="var(--surface-1)" stroke="${col}" stroke-width="2"/>`;
    const text = `${CONTEXT_SHORT[e.cell.context]}${e.cell.harness_on ? ' + harness' : ''}`;
    const w = text.length * 6;
    const lx = Math.min(x + 9, W - m.r - w);
    const show = onFrontier.has(e.id) && fits(lx, y - 8, w);
    if (show) placed.push({x: lx, y: y - 8, w});
    const label = show ? `<text x="${lx}" y="${y - 8}" font-size="11" fill="var(--text-secondary)">${esc(text)}</text>` : '';
    return `<g><circle cx="${x}" cy="${y}" r="7" fill="var(--surface-1)"/>${mark}${label}
      <circle cx="${x}" cy="${y}" r="12" fill="transparent" class="cursor-pointer" data-econ="${esc(e.id)}"/></g>`;
  }).join('');

  box.innerHTML = `<svg width="${W}" height="${H}" role="img" aria-label="Accuracy versus agent token cost per question, one point per cell">${grid}${line}${marks}</svg>
    <div id="econTip" class="pointer-events-none absolute hidden rounded-control border border-border bg-surface px-2.5 py-1.5 text-xs text-ink"></div>`;
  const tip = $('#econTip');
  const econ = Object.fromEntries(pts.map((e) => [e.id, e]));
  $$('#econChart [data-econ]').forEach((el) => {
    const e = econ[el.dataset.econ];
    el.onmouseenter = () => {
      const a = aggregate(e.id);
      tip.innerHTML = `<b>${esc(cellName(e.cell))}</b><br>Accuracy ${pct(e.p)} (95% CI ${pct(e.pCI[0])}–${pct(e.pCI[1])})` +
        `${app.state.repeats > 1 ? ` · pass^k ${pct(a.pass_k)}` : ''}<br>Cost ${fmtUSD(e.c)}/question · ${fmtUSD(e.costPerCorrect)}/correct<br>Net value ${fmtUSD(e.u, true)}/question`;
      tip.classList.remove('hidden');
      const x = +el.getAttribute('cx'), y = +el.getAttribute('cy');
      tip.style.left = `${Math.min(x + 14, W - 230)}px`; tip.style.top = `${Math.max(0, y - 20)}px`;
    };
    el.onmouseleave = () => tip.classList.add('hidden');
    el.onclick = () => selectCell(e.id);
  });
}

function renderEconTable(econ, frontier, onFrontier) {
  const {L} = scoring();
  const rows = Object.values(econ).sort((a, b) => scoreOf(b.id, econ) - scoreOf(a.id, econ));
  const leader = rows[0];
  const next = Object.fromEntries(frontier.slice(0, -1).map((e, k) => [e.id, frontier[k + 1]]));
  const breakEven = (a, b) => {
    const v = (b.c - a.c) / (b.p - a.p) - L;
    return v <= 0 ? 'any value' : fmtUSD(v);
  };
  $('#econTable').innerHTML = `<thead><tr>
      <th>Cell</th><th>Accuracy (95% CI)</th>${app.state.repeats > 1 ? '<th>pass^k</th>' : ''}
      <th>$ / question</th><th>$ / correct</th><th>Net value (95% CI)</th>
      <th>vs. top (paired)</th><th>Frontier · break-even to next</th></tr></thead>
    <tbody>${rows.map((e) => {
      const d = e === leader ? null : pairedDiff(e, leader);
      const nxt = next[e.id];
      return `<tr>
        <td class="whitespace-nowrap">${esc(cellName(e.cell))}</td>
        <td>${pct(e.p)} <span class="text-ink-subtle">${pct(e.pCI[0])}–${pct(e.pCI[1])}</span></td>
        ${app.state.repeats > 1 ? `<td>${pct(aggregate(e.id).pass_k)}</td>` : ''}
        <td>${fmtUSD(e.c)}</td><td>${fmtUSD(e.costPerCorrect)}</td>
        <td>${fmtUSD(e.u, true)} <span class="text-ink-subtle">${fmtUSD(e.uCI[0], true)} to ${fmtUSD(e.uCI[1], true)}</span></td>
        <td class="whitespace-nowrap">${d ? `${fmtUSD(d.mean, true)} <span class="text-ink-subtle">(${fmtUSD(d.ci[0], true)} to ${fmtUSD(d.ci[1], true)}) · P(better) ${pct(d.pBetter)}</span>` : 'top'}</td>
        <td class="whitespace-nowrap">${onFrontier.has(e.id) ? `✓${nxt ? ` · upgrade to ${esc(cellName(nxt.cell))} if a correct answer is worth > ${breakEven(e, nxt)}` : ''}` : '<span class="text-ink-subtle">dominated</span>'}</td>
      </tr>`;
    }).join('')}</tbody>`;
}

export function renderEconSummary(id) {
  const econ = econAll(), e = econ[id];
  if (!e) return '';
  const ranked = Object.values(econ).sort((x, y) => scoreOf(y.id, econ) - scoreOf(x.id, econ));
  const d = ranked[0] && ranked[0] !== e ? pairedDiff(e, ranked[0]) : null;
  const frontier = paretoFrontier(econ).some((f) => f.id === id);
  return `
  <section class="panel mb-4 p-4">
    <h3 class="mb-2 text-sm font-medium text-ink-muted">Cost and value · ${frontier ? 'on the Pareto frontier' : 'dominated by another cell'}</h3>
    <div class="grid grid-cols-2 md:grid-cols-5 gap-2">
      ${stat('$ / question', fmtUSD(e.c))}
      ${stat('$ / correct answer', fmtUSD(e.costPerCorrect))}
      ${stat('Net value / question', `${fmtUSD(e.u, true)} <span class="block text-xs font-normal text-ink-muted">95% CI ${fmtUSD(e.uCI[0], true)} to ${fmtUSD(e.uCI[1], true)}</span>`)}
      ${stat('Accuracy', `${pct(e.p)} <span class="block text-xs font-normal text-ink-muted">95% CI ${pct(e.pCI[0])}–${pct(e.pCI[1])}</span>`)}
      ${d ? stat('vs. top cell', `${fmtUSD(d.mean, true)} <span class="block text-xs font-normal text-ink-muted">P(better) ${pct(d.pBetter)}</span>`) : stat('Rank', 'top')}
    </div>
  </section>`;
}

// Scoring inputs or prices changed: re-score finished results (nothing is re-run).
export function rescore() {
  renderAll();
}
