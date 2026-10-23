// Run tab results: the matrix grid, per-prompt table, and cell / case detail. Works the same for a live
// matrix and one loaded from History (both fill app.state).

import {$, $$, esc, mean, fmtS, fmtDur, pct, fmtUSD, passTint, spinner, stat, passBadge} from './ui.js';
import {app, contextIds, contextName, cellId, cellLabel, modelName, comboLabel} from './config.js';
import {rank, winnerHtml, tableHtml, initWeights, renderShares} from './ranking.js';
import {passRateCI, pairedCompare, quantile} from './stats.js';
import {modelIcon, targetIcon, withIcon} from './icons.js';

// All finished reports for a cell, in run order.
const reportsFor = (id) => Object.keys(app.state.runs).map(Number).sort((a, b) => a - b)
  .map((run) => app.state.runs[run][id]).filter(Boolean);

// Infrastructure errors (provider or compiler failures) are retried, then left out of every score.
const scored = (c) => c && c.status !== 'infra_error';

// A setup's outcomes per prompt: 1/0 for each scored repeat, in prompt order (what stats.js works on).
const perPrompt = (id) => app.state.prompts.map((_, i) => reportsFor(id).map((r) => r.cases[i]).filter(scored).map((c) => (c.passed ? 1 : 0)));
const range = ([lo, hi]) => `${pct(lo)}–${pct(hi)}`;
const points = (x) => `${x > 0 ? '+' : x < 0 ? '−' : ''}${Math.abs(100 * x).toFixed(1)}`;
// Intervals change only when a setup finishes a repeat, but results re-render on every progress frame: cache them.
const ciCache = new WeakMap();  // state -> {'<setup>@<repeats>': interval}
function cachedCI(id, rs) {
  const cache = ciCache.get(app.state) ?? ciCache.set(app.state, {}).get(app.state);
  const key = `${id}@${rs.map((r) => r.run).join(',')}`;
  if (!(key in cache)) cache[key] = passRateCI(perPrompt(id));
  return cache[key];
}
const CI_TITLE = 'Likely range of the pass rate on another set of similar prompts (95% bootstrap interval over prompts)';

// Question-level progress for a cell's runs that have no report yet (live runs only).
const pendingProgress = (id) => Object.entries(app.state.progress ?? {})
  .filter(([run, cells]) => cells[id] && !app.state.runs[run]?.[id]).map(([, cells]) => cells[id]);

function aggregate(id) {
  const rs = reportsFor(id);
  if (!rs.length) return null;
  const rates = rs.map((r) => r.pass_rate);
  return {
    n: rs.length,
    pass_rate: mean(rates),
    min: Math.min(...rates),
    max: Math.max(...rates),
    seconds: mean(rs.map((r) => r.total_seconds)),
    case_seconds: mean(rs.map((r) => r.avg_case_seconds)),  // agent time per prompt, excluding queueing
    tokens: mean(rs.map((r) => r.total_tokens)),
    tool_calls: mean(rs.map((r) => r.tool_calls)),
    // Matrices logged before costs were computed server side have no cost_usd.
    cost: rs.every((r) => r.cost_usd != null) ? mean(rs.map((r) => r.cost_usd / Math.max(r.total, 1))) : null,
    infra: rs.reduce((n, r) => n + (r.infra_errors ?? 0), 0),
    ci: cachedCI(id, rs),
    ...consistency(rs),
  };
}

// pass^k: share of prompts that passed in EVERY finished run. Flaky: passed in some runs but not all. Runs with an
// infrastructure error don't count for that prompt; a prompt with no scored run drops out.
function consistency(rs) {
  const outcomes = app.state.prompts.map((_, i) => rs.map((r) => r.cases[i]).filter(scored).map((c) => Boolean(c.passed)));
  const counted = outcomes.filter((xs) => xs.length);
  return {
    pass_k: counted.filter((xs) => xs.every(Boolean)).length / Math.max(counted.length, 1),
    flaky: outcomes.flatMap((xs, i) => (xs.some(Boolean) && !xs.every(Boolean) ? [i] : [])),
  };
}

function planDims() {
  const {cells} = app.state;
  const models = [...new Set(cells.map((c) => c.model))];
  // Column order: target, then harness.
  const combos = contextIds().flatMap((context) => [false, true].map((harness) => ({context, harness})))
    .filter((k) => cells.some((c) => c.harness_on === k.harness && c.context === k.context));
  return {models, combos};
}

/** Wall clock for each finished run and for the whole matrix. A live matrix's elapsed time is in the status line. */
export function renderRunTimes() {
  const {state} = app;
  const el = $('#runTimes');
  const time = (label, s) => `${label}: <span class="font-medium text-ink">${fmtDur(s)}</span>`;
  const runs = state.repeats > 1 ? Object.entries(state.runSeconds).map(([n, s]) => time(`Repeat ${n}`, s)) : [];
  const parts = [...runs, ...(state.totalSeconds != null ? [time('Total', state.totalSeconds)] : [])];
  el.innerHTML = parts.join('<span aria-hidden="true" class="mx-2 text-ink-subtle">·</span>');
  el.classList.toggle('hidden', !parts.length);
}

// Every setup with a finished report, ranked by the weighted score. Final once every setup has all its repeats (or
// has failed).
function ranking() {
  const {state} = app;
  const entries = state.cells.map((c) => [c.id, aggregate(c.id)]).filter(([, a]) => a)
    .map(([id, a]) => ({id, label: cellLabel(id), accuracy: a.pass_rate, cost: a.cost, seconds: a.case_seconds}));
  const final = state.cells.every((c) => state.errors[c.id] || aggregate(c.id)?.n === state.repeats);
  return {...rank(entries), final};
}

function renderRanking() {
  const r = ranking();
  $('#winner').innerHTML = winnerHtml(r, r.final, edgeNote(r.ranked));
  $('#winner').classList.toggle('hidden', !r.ranked.length);
  $$('#winner [data-goto]').forEach((b) => b.onclick = () => showTab(b.dataset.goto));
  $('#ranking').innerHTML = r.ranked.length ? tableHtml(r) : '';
  $('#rankingNote').innerHTML = r.noCost
    ? '<p class="hint mt-3">Cost isn’t recorded for this run, so the ranking uses accuracy and speed only.</p>' : '';
  renderShares(r.noCost);
  return r;
}

// How sure the winner is ahead of the runner-up on accuracy (the ranking also weighs cost and speed).
function edgeNote([first, second]) {
  if (!first || !second) return '';
  const c = pairedCompare(perPrompt(first.id), perPrompt(second.id));
  if (!c) return '';
  const verdict = c.clear ? (c.diff > 0 ? 'clearly more accurate' : 'clearly less accurate') : 'no clear accuracy difference';
  return `Vs. ${esc(second.label)}: ${verdict} (${points(c.diff)} points, 95% CI ${points(c.ci[0])} to ${points(c.ci[1])})`;
}

/** Show one results tab: overview (pass rate), ranking, questions or detail (the selected setup's answers). */
export function showTab(name) {
  app.state.tab = name;
  $$('#resultTabs [data-pane]').forEach((b) => b.setAttribute('aria-selected', String(b.dataset.pane === name)));
  $$('#results > [data-pane]').forEach((p) => p.hidden = p.dataset.pane !== name);
}

/** Re-render everything that depends on app.state (after an event, a load, or a scoring change). */
export function renderAll() {
  const {state} = app;
  if (!state.cells.length) return;
  $('#empty').classList.add('hidden');
  $('#resultTabs').classList.replace('hidden', 'flex');
  $$('#resultTabs [data-pane]').forEach((b) => b.onclick = () => showTab(b.dataset.pane));
  showTab(state.tab);
  initWeights(() => renderMatrix());
  renderMatrix();
  renderRunTimes();
  renderPerPrompt();
  if (state.selected) selectCell(state.selected, state.selectedRun);
}

/** Re-render just the live parts (matrix and per-question table) after question-level progress. */
export function renderProgress() {
  if (!app.state.cells.length) return;
  renderMatrix();
  renderPerPrompt();
}

// The unfinished run's answered count, running count and pass rate so far, or null if nothing has started.
function liveCell(id) {
  const p = pendingProgress(id)[0];
  if (!p) return null;
  const done = Object.values(p.done);
  const passed = done.filter((x) => x !== null);  // null: an infrastructure error, not scored
  const total = app.state.prompts.length;
  const bar = `<div class="mt-2 h-1 overflow-hidden rounded-full bg-surface-2" aria-hidden="true">
    <div class="h-full rounded-full bg-accent transition-[width]" style="width:${(100 * done.length) / Math.max(total, 1)}%"></div></div>`;
  return {
    answered: `${done.length}/${total} answered${p.active.size ? ` · ${p.active.size} running` : ''}`,
    soFar: passed.length ? `${pct(passed.filter(Boolean).length / passed.length)} passing so far` : '',
    bar,
  };
}

function renderMatrix() {
  const {state} = app;
  const {models, combos} = planDims();
  const winner = renderRanking().ranked[0]?.id;
  // Two header rows: each target over its baseline / harness columns. Columns share the width equally.
  const targets = [...new Set(combos.map((c) => c.context))];
  const head = `<tr><th class="w-px"></th>${targets.map((t) =>
    `<th colspan="${combos.filter((c) => c.context === t).length}" class="border-b border-border px-1 pb-1 text-left text-xs font-medium text-ink">${withIcon(targetIcon(t), esc(contextName(t)))}</th>`).join('')}</tr>
    <tr><th class="w-px"></th>${combos.map((c) =>
    `<th style="width:${100 / combos.length}%" class="px-1 text-left text-xs font-normal text-ink-muted">${c.harness ? 'Harness' : 'Baseline'}</th>`).join('')}</tr>`;
  const rows = models.map((m) => `<tr>
    <th class="w-px whitespace-nowrap pr-3 text-left text-sm font-medium">${withIcon(modelIcon(), esc(modelName(m)))}</th>
    ${combos.map((c) => renderCell(cellId(m, c.harness, c.context), winner)).join('')}
  </tr>`).join('');
  $('#matrix').innerHTML = head + rows;
  $('#matrixWrap').classList.remove('hidden');
  $('#mlflowLink').classList.toggle('hidden', !state.mlflow?.matrix);
  if (state.mlflow?.matrix) $('#mlflowLink').href = state.mlflow.matrix;
  $$('#matrix [data-cell]').forEach((b) => b.onclick = () => { selectCell(b.dataset.cell); showTab('detail'); });
  renderCompare(winner);
}

// Compare two setups prompt by prompt. The pickers are rebuilt only when the setups with results change, so live
// progress doesn't reset a choice being made.
function renderCompare(winner) {
  const {state} = app;
  const ids = state.cells.map((c) => c.id).filter((id) => aggregate(id));
  $('#compareWrap').classList.toggle('hidden', ids.length < 2);
  if (ids.length < 2) return;
  const ranked = ranking().ranked.map((e) => e.id);
  state.compare ??= [winner ?? ranked[0], ranked.find((id) => id !== (winner ?? ranked[0]))];
  for (const [i, sel] of [$('#cmpA'), $('#cmpB')].entries()) {
    const html = ids.map((id) => `<option value="${esc(id)}">${esc(cellLabel(id))}</option>`).join('');
    if (sel.dataset.options !== html) { sel.innerHTML = html; sel.dataset.options = html; }
    if (!ids.includes(state.compare[i])) state.compare[i] = ids[i];
    sel.value = state.compare[i];
    sel.onchange = () => { state.compare[i] = sel.value; renderCompare(winner); };
  }
  const [a, b] = state.compare;
  const c = a === b ? null : pairedCompare(perPrompt(a), perPrompt(b));
  $('#cmpResult').innerHTML = a === b ? '<span class="text-ink-muted">Pick two different setups.</span>' : !c ? '<span class="text-ink-muted">No prompt scored by both yet.</span>' : `
    <p><span class="font-semibold">${c.clear ? (c.diff > 0 ? 'A is clearly more accurate.' : 'B is clearly more accurate.') : 'No clear difference in accuracy.'}</span>
      <span class="tabular-nums">A minus B: ${points(c.diff)} points (95% CI ${points(c.ci[0])} to ${points(c.ci[1])}).</span></p>
    <p class="mt-1 text-xs tabular-nums text-ink-muted">Over ${c.n} prompts both answered: A gets ${c.aOnly} right that B misses, B gets ${c.bOnly} right that A misses (by majority of repeats) · McNemar ${c.p < 0.001 ? 'p < 0.001' : `p = ${c.p.toFixed(3)}`}</p>`;
}

function renderCell(id, winner) {
  const {state} = app;
  const a = aggregate(id);
  const selected = state.selected === id;
  const err = state.errors[id] ? `<div class="mt-1 break-words text-[11px] text-danger">${esc(state.errors[id])}</div>` : '';
  const live = liveCell(id);
  if (!a) {
    if (err) return `<td class="align-top"><div class="rounded-control border border-danger/30 bg-danger-soft p-3 text-xs">${err}</div></td>`;
    if (state.stopped) {
      return `<td class="h-px align-top"><div class="flex h-full min-h-[92px] min-w-[104px] flex-col justify-center rounded-control border border-dashed border-border p-3 text-xs text-ink-muted"
        title="Stopped before this setup finished a repeat"><span class="font-medium text-ink">Stopped</span>${live ? `<span class="mt-1 tabular-nums">${live.answered}</span>` : ''}</div></td>`;
    }
    if (!live) {
      return `<td class="h-px align-top"><div class="flex h-full min-h-[92px] min-w-[104px] items-center rounded-control border border-dashed border-border p-3 text-xs text-ink-subtle"
        title="Waiting for an agent slot">Waiting</div></td>`;
    }
    return `<td class="h-px align-top"><div class="h-full min-h-[92px] min-w-[104px] rounded-control border border-dashed border-border-strong p-3 text-xs text-ink-muted">
      <div class="flex items-center gap-2 font-medium text-ink">${spinner}Running…</div>
      <div class="mt-1.5 tabular-nums">${live.answered}</div>
      ${live.soFar ? `<div class="tabular-nums text-ink-subtle">${live.soFar}</div>` : ''}
      ${live.bar}
    </div></td>`;
  }
  const multi = state.repeats > 1;
  const money = a.cost != null ? `<div class="whitespace-nowrap text-xs tabular-nums text-ink-muted" title="Average cost per prompt at list prices">${fmtUSD(a.cost)} / prompt</div>` : '';
  const runs = multi ? `<div class="mt-0.5 text-xs font-semibold tabular-nums" title="Share of prompts that passed in every repeat">${pct(a.pass_k)} always passed</div>` +
    (a.flaky.length ? `<div class="text-xs tabular-nums text-warning" title="Passed in some repeats, not all">${a.flaky.length} flaky</div>` : '') +
    (a.n < state.repeats ? `<div class="flex items-center gap-1 text-[11px] text-ink-muted">${state.stopped ? 'Stopped at ' : spinner}${a.n}/${state.repeats} repeats${live && !state.stopped ? ` · ${live.answered}` : ''}</div>` : '') : '';
  const details = [multi && a.n > 1 ? `${pct(a.min)}–${pct(a.max)} across repeats (average shown)` : '',
    `${fmtS(a.seconds)} · ${Math.round(a.tokens).toLocaleString()} tokens · ${+a.tool_calls.toFixed(1)} tool calls`].filter(Boolean).join('\n');
  const first = reportsFor(id)[0];
  const counts = multi ? '' : ` <span class="text-xs font-normal text-ink-muted">${first.passed}/${first.total}</span>`;
  const infra = a.infra ? `<div class="text-xs tabular-nums text-warning" title="Provider or compiler failures, retried and then left out of the scores">${a.infra} infra error${a.infra > 1 ? 's' : ''}</div>` : '';
  return `<td class="h-px align-top"><button type="button" data-cell="${esc(id)}" aria-pressed="${selected}"
    title="${esc(details)}" class="flex h-full w-full min-w-[104px] flex-col rounded-control border p-3 text-left transition-colors hover:border-border-strong ${selected ? 'border-accent ring-2 ring-accent/40' : 'border-border'}"
    style="background:${passTint(a.pass_rate)}">
    <div class="whitespace-nowrap text-2xl font-semibold leading-tight tracking-tight tabular-nums">${pct(a.pass_rate)}${counts}</div>
    ${a.ci ? `<div class="whitespace-nowrap text-[11px] tabular-nums text-ink-muted" title="${CI_TITLE}">95% CI ${range(a.ci.rate)}</div>` : ''}
    ${money}${runs}${infra}${err}
    ${id === winner ? '<div class="mt-auto pt-2"><span class="badge badge-pass" title="Best weighted score: see Ranking">Winner</span></div>' : ''}
  </button></td>`;
}

function renderPerPrompt() {
  const {state} = app;
  const {models, combos} = planDims();
  const cols = models.flatMap((m) => combos.map((c) => ({id: cellId(m, c.harness, c.context), m, c})));
  const head = `<tr><th>Prompt</th>${cols.map(({m, c}) =>
    `<th class="whitespace-nowrap !text-center">${esc(modelName(m))}<br><span class="font-normal">${comboLabel(c)}</span></th>`).join('')}</tr>`;
  const rows = state.prompts.map((p, i) => `<tr>
    <td class="max-w-[280px]">${esc(p)}</td>
    ${cols.map(({id}) => {
      const pending = pendingProgress(id);
      const all = [...reportsFor(id).map((r) => r.cases[i]).filter(Boolean),
        ...pending.filter((p) => i in p.done).map((p) => (p.done[i] === null ? {status: 'infra_error'} : {passed: p.done[i]}))];
      const cases = all.filter(scored);
      const running = pending.some((p) => p.active.has(i));
      if (!cases.length) {
        if (running) return `<td class="text-center" title="Running">${spinner}</td>`;
        return all.length ? '<td class="text-center font-semibold text-warning" title="Infrastructure error: not scored">!</td>'
          : '<td class="text-center text-ink-subtle">·</td>';
      }
      if (state.repeats === 1) {
        return `<td class="text-center font-semibold ${cases[0].passed ? 'text-success' : 'text-danger'}" title="${cases[0].passed ? 'Passed' : 'Failed'}">${cases[0].passed ? '✓' : '✗'}</td>`;
      }
      const k = cases.filter((c) => c.passed).length;
      return `<td class="text-center font-medium tabular-nums" style="background:${passTint(k / cases.length)}"
        title="Passed in ${k} of ${cases.length} repeats">${k}/${cases.length}</td>`;
    }).join('')}
  </tr>`).join('');
  $('#perPrompt').innerHTML = head + rows;
  $('#perPromptWrap').classList.remove('hidden');
}

export function selectCell(id, run = null) {
  const {state} = app;
  state.selected = id;
  renderMatrix();
  const rs = reportsFor(id);
  if (!rs.length) { $('#detail').innerHTML = ''; return; }
  const r = rs.find((x) => x.run === run) ?? rs[rs.length - 1];
  state.selectedRun = r.run;
  $('#detail').innerHTML = (state.repeats > 1 ? renderRunSummary(id, rs, r.run) : '') + renderReport(r);
  $$('#detail [data-run]').forEach((b) => b.onclick = () => selectCell(id, +b.dataset.run));
}

function renderRunSummary(id, rs, activeRun) {
  const {state} = app;
  const a = aggregate(id);
  const tabs = rs.map((r) => `<button type="button" data-run="${r.run}" aria-pressed="${r.run === activeRun}"
    class="rounded-control border px-2.5 py-1 text-xs tabular-nums ${r.run === activeRun ? 'border-accent bg-accent text-accent-fg' : 'border-border hover:bg-surface-2'}">
    Repeat ${r.run} · ${pct(r.pass_rate)}</button>`).join('');
  return `
  <section class="panel mb-4 p-4">
    <h3 class="mb-2 text-sm font-medium text-ink-muted">Across ${a.n} of ${state.repeats} repeats</h3>
    <div class="mb-3 grid grid-cols-2 gap-2 md:grid-cols-5">
      ${stat('Always passed', `${pct(a.pass_k)}${a.ci ? `<span class="block text-xs font-normal text-ink-muted">95% CI ${range(a.ci.passK)}</span>` : ''}`)}
      ${stat('Average pass rate', `${pct(a.pass_rate)}${a.ci ? `<span class="block text-xs font-normal text-ink-muted">95% CI ${range(a.ci.rate)}</span>` : ''}`, CI_TITLE)}
      ${stat('Range', `${pct(a.min)}–${pct(a.max)}`)}
      ${stat('Average time', fmtS(a.seconds))}
      ${stat('Average tokens', Math.round(a.tokens).toLocaleString())}
    </div>
    ${a.flaky.length ? `<p class="mb-3 rounded-control bg-warning-soft p-2 text-xs"><span class="font-semibold">Flaky prompts</span> (passed in some repeats, not all): ${a.flaky.map((i) => esc(state.prompts[i])).join(' · ')}</p>` : ''}
    ${failureBreakdown(rs.flatMap((x) => x.cases)).replace('Why it failed (', 'Why it failed, all repeats (')}
    <div class="flex flex-wrap gap-2">${tabs}</div>
  </section>`;
}

function renderReport(r) {
  const {state} = app;
  return `
  <section class="panel min-w-0 p-4">
    <div class="mb-1 flex flex-wrap items-baseline justify-between gap-2">
      <h2 class="text-lg font-semibold tracking-tight">${esc(modelName(r.model))} · ${comboLabel({harness: r.harness_on, context: r.context})}${state.repeats > 1 ? ` · repeat ${r.run}` : ''}</h2>
      <div class="flex items-center gap-3 text-xs">
        ${state.mlflow?.cells?.[r.id] ? `<a href="${esc(state.mlflow.cells[r.id])}" target="_blank" rel="noopener" class="text-ink-muted hover:text-ink"
          title="Open in MLflow">in MLflow ↗</a>` : ''}
      </div>
    </div>
    <div class="mt-3 grid grid-cols-2 gap-2 md:grid-cols-4">
      ${stat('Pass rate', `${pct(r.pass_rate)} <span class="text-sm font-normal text-ink-muted">(${r.passed}/${r.total})</span>` +
        (r.infra_errors ? `<span class="block text-xs font-normal text-warning">${r.infra_errors} infra error${r.infra_errors > 1 ? 's' : ''} not scored</span>` : ''))}
      ${r.cost_usd != null ? stat('Cost', `${fmtUSD(r.cost_usd)} <span class="text-sm font-normal text-ink-muted">(${fmtUSD(r.cost_usd / Math.max(r.total, 1))} / prompt)</span>`) : ''}
      ${stat('Time', fmtS(r.total_seconds))}
      ${stat('Tokens', (r.total_tokens ?? 0).toLocaleString())}
    </div>
    <details class="mb-4 mt-2">
      <summary class="cursor-pointer select-none text-xs text-ink-muted hover:text-ink">More stats</summary>
      <div class="mt-2 grid grid-cols-2 gap-2 md:grid-cols-6">
        ${stat('Per prompt', fmtS(r.avg_case_seconds))}
        ${caseStats(r)}
        ${r.cache_read_tokens ? stat('Input cached', pct(r.cache_read_tokens / Math.max(r.input_tokens, 1))) : ''}
        ${stat('Requests', r.requests)}
        ${stat('Tool calls', r.tool_calls)}
        ${r.reviews ? stat('Reviews', `${r.reviews} <span class="text-sm font-normal text-ink-muted">(${r.sent_back} sent back)</span>`) : ''}
        ${r.defined_metrics ? stat('Metrics defined', r.defined_metrics) : ''}
        ${r.lint_retries ? stat('Lint retries', r.lint_retries) : ''}
        ${r.over_budget ? stat('Over budget', r.over_budget) : ''}
        ${r.timeouts ? stat('Timed out', r.timeouts) : ''}
        ${r.partial ? stat('Partial', r.partial) : ''}
      </div>
    </details>
    ${failureBreakdown(r.cases)}
    <div class="grid gap-3 xl:grid-cols-2">${r.cases.map(renderCase).join('')}</div>
  </section>`;
}

// Why a setup's answers failed: label counts over the given cases, most common first, plus failures not labeled yet
// (wrong rows wait for an LLM label: python -m app.labels <matrix id>).
function failureBreakdown(cases) {
  const failed = cases.filter((c) => c.status === 'fail');
  if (!failed.length) return '';
  const counts = {};
  for (const c of failed) if (c.failure_label) counts[c.failure_label] = (counts[c.failure_label] ?? 0) + 1;
  const names = app.config.failure_labels ?? {};
  const rows = Object.entries(counts).sort((a, b) => b[1] - a[1])
    .map(([k, n]) => `<li class="flex justify-between gap-4"><span>${esc(names[k] ?? k)}</span><span class="tabular-nums">${n}</span></li>`);
  const open = failed.length - Object.values(counts).reduce((a, b) => a + b, 0);
  return `<div class="mb-4 rounded-control border border-border p-3 text-sm">
    <p class="mb-1.5 text-xs font-medium text-ink-muted">Why it failed (${failed.length} failed answer${failed.length > 1 ? 's' : ''})</p>
    <ul class="space-y-0.5">${rows.join('')}</ul>
    ${open ? `<p class="mt-1.5 text-xs text-ink-subtle" title="Wrong rows need reading to label">${open} wrong-rows answer${open > 1 ? 's' : ''} not labeled yet: <code>uv run python -m app.labels ${esc(app.state.matrixId ?? '<matrix id>')}</code></p>` : ''}
  </div>`;
}

// Spread across one repeat's answered prompts: time and tool calls at the median and 95th percentile, and cost per
// correct answer (the run's whole cost over its passes).
function caseStats(r) {
  const answered = r.cases.filter((c) => c.seconds != null);
  if (!answered.length) return '';
  const q = (key, p) => quantile(answered.map((c) => c[key] ?? 0), p);
  return stat('Time p50 / p95', `${fmtS(q('seconds', 0.5))} / ${fmtS(q('seconds', 0.95))}`, 'Agent time per prompt, excluding waiting for a slot') +
    stat('Tool calls p50 / p95', `${+q('tool_calls', 0.5).toFixed(1)} / ${+q('tool_calls', 0.95).toFixed(1)}`) +
    (r.cost_usd != null ? stat('Cost per correct', r.passed ? fmtUSD(r.cost_usd / r.passed) : '—', 'Total cost of this repeat ÷ prompts passed') : '');
}

const queryLabel = (text) => `<p class="mt-2.5 text-xs font-medium text-ink-muted">${text}</p>`;

function renderQuery(c) {
  return `
    ${c.malloy_query ? `${queryLabel('Malloy query')}<pre class="code mt-1">${esc(c.malloy_query)}</pre>${queryLabel('Compiled SQL')}` : ''}
    ${c.metricflow_query ? `${queryLabel('MetricFlow query')}<pre class="code mt-1">${esc(JSON.stringify(c.metricflow_query, null, 2))}</pre>${queryLabel('Compiled SQL')}` : ''}
    ${c.sql ? `<pre class="code mt-1">${esc(c.sql)}</pre>` : ''}
    ${c.explanation ? `<p class="mt-2 text-xs text-ink-muted">${esc(c.explanation)}</p>` : ''}
    ${c.expected_sql ? `${queryLabel('Expected')}<pre class="mt-1 overflow-x-auto rounded-control bg-surface-2 p-2 font-mono text-xs">${esc(c.expected_sql)}</pre>` : ''}
    ${c.rows ? renderRows(c.rows) : ''}`;
}

const plural = (n, word) => `${n} ${word}${n === 1 ? '' : 's'}`;

function renderCase(c) {
  const meta = c.seconds != null
    ? `${fmtS(c.seconds)} · ${((c.input_tokens || 0) + (c.output_tokens || 0)).toLocaleString()} tokens · ${plural(c.requests ?? 0, 'request')} · ${plural(c.tool_calls ?? 0, 'tool call')}` +
      (c.reviews ? ` · ${c.reviews} review${c.reviews > 1 ? 's' : ''}${c.sent_back ? `, ${c.sent_back} sent back` : ''}` : '') +
      (c.defined_metrics ? ` · ${c.defined_metrics} metric${c.defined_metrics > 1 ? 's' : ''} defined` : '') +
      (c.lints?.length ? ` · lint: ${esc(c.lints.join('; '))}` : '') +
      (c.attempts > 1 ? ` · ${c.attempts} attempts` : '')
    : '';
  return `
  <article class="min-w-0 rounded-control border p-3 ${c.status === 'infra_error' ? 'border-warning/30' : c.passed ? 'border-success/30' : 'border-danger/30'}">
    <div class="flex items-start gap-2">
      ${passBadge(c.passed, c.status)}
      <div class="min-w-0 flex-1">
        <p class="text-sm font-medium">${esc(c.prompt)}</p>
      </div>
      ${c.trace_url ? `<a href="${esc(c.trace_url)}" target="_blank" rel="noopener" class="btn btn-outline shrink-0 px-2 py-1 text-xs">Trace ↗</a>` : ''}
    </div>
    ${c.error ? `<p class="mt-2 break-words rounded-control bg-danger-soft p-2 font-mono text-xs">${esc(c.error)}</p>` : ''}
    ${c.reason && !c.error ? `<p class="mt-2 text-xs text-ink-muted">${esc(c.reason)}</p>` : ''}
    ${c.failure_label ? `<p class="mt-2 text-xs"><span class="badge badge-neutral" title="${c.label_source === 'llm' ? 'Labeled by an LLM' : 'Labeled by a rule'}">${esc(app.config.failure_labels?.[c.failure_label] ?? c.failure_label)}</span>${c.label_rationale ? ` <span class="text-ink-muted">${esc(c.label_rationale)}</span>` : ''}</p>` : ''}
    ${c.sql || meta ? `<details class="mt-2"><summary class="cursor-pointer select-none text-xs text-ink-muted hover:text-ink">Details</summary>
      ${meta ? `<p class="mt-1.5 text-xs tabular-nums text-ink-subtle">${meta}</p>` : ''}${c.sql ? renderQuery(c) : ''}</details>` : ''}
  </article>`;
}

function renderRows({columns = [], rows = [], row_count = rows.length}) {
  return `
  <div class="mt-2 overflow-x-auto">
    <table class="data-table w-full text-xs">
      ${columns.length ? `<thead><tr>${columns.map((c) => `<th>${esc(c)}</th>`).join('')}</tr></thead>` : ''}
      <tbody>${rows.map((r) => `<tr>${r.map((v) => `<td class="tabular-nums">${esc(v)}</td>`).join('')}</tr>`).join('')}</tbody>
    </table>
    ${row_count > rows.length ? `<p class="mt-1 text-[11px] text-ink-subtle">Showing ${rows.length} of ${row_count} rows</p>` : ''}
  </div>`;
}
