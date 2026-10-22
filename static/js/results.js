// Run tab results: the matrix grid, per-prompt table, and cell / case detail. Works the same for a live
// matrix and one loaded from History (both fill app.state).

import {$, $$, esc, mean, fmtS, pct, fmtUSD, passTint, spinner, stat, passBadge, checkLine} from './ui.js';
import {app, COMBOS, cellId, modelName, comboLabel} from './config.js';
import {econAll, scoreOf, rankLabel, renderEconomics, renderEconSummary} from './econ.js';
import {openTraceDrawer} from './traces.js';

// All finished reports for a cell, in run order.
export const reportsFor = (id) => Object.keys(app.state.runs).map(Number).sort((a, b) => a - b)
  .map((run) => app.state.runs[run][id]).filter(Boolean);

export function aggregate(id) {
  const rs = reportsFor(id);
  if (!rs.length) return null;
  const rates = rs.map((r) => r.pass_rate);
  return {
    n: rs.length,
    pass_rate: mean(rates),
    min: Math.min(...rates),
    max: Math.max(...rates),
    seconds: mean(rs.map((r) => r.total_seconds)),
    tokens: mean(rs.map((r) => r.total_tokens)),
    tool_calls: mean(rs.map((r) => r.tool_calls)),
    ...consistency(rs),
  };
}

// pass^k: share of prompts that passed in EVERY finished run. Flaky: passed in some runs but not all.
function consistency(rs) {
  const outcomes = app.state.prompts.map((_, i) => rs.map((r) => Boolean(r.cases[i]?.passed)));
  return {
    pass_k: outcomes.filter((xs) => xs.every(Boolean)).length / Math.max(outcomes.length, 1),
    flaky: outcomes.flatMap((xs, i) => (xs.some(Boolean) && !xs.every(Boolean) ? [i] : [])),
  };
}

function planDims() {
  const {cells} = app.state;
  const models = [...new Set(cells.map((c) => c.model))];
  const combos = COMBOS.filter((k) => cells.some((c) => c.harness_on === k.harness && c.context === k.context));
  return {models, combos};
}

// Links from results into the Traces tab (and MLflow's own UI as a fallback).
const matrixId = () => app.state.mlflow?.matrix_id;
const cellRunId = (id) => app.state.mlflow?.cell_runs?.[id];
const tracesHref = (params) => '#/traces?' + new URLSearchParams(Object.entries(params).filter(([, v]) => v));

/** Re-render everything that depends on app.state (after an event, a load, or a scoring change). */
export function renderAll() {
  const {state} = app;
  if (!state.cells.length) return;
  $('#empty').classList.add('hidden');
  renderMatrix();
  renderEconomics();
  renderPerPrompt();
  if (state.selected) selectCell(state.selected, state.selectedRun);
}

export function renderMatrix() {
  const {state} = app;
  const {models, combos} = planDims();
  const econ = econAll();
  const best = Math.max(...state.cells.map((c) => scoreOf(c.id, econ)));
  const head = `<tr><th></th>${combos.map((c) =>
    `<th class="px-1 pb-1 text-left text-xs font-medium text-ink-muted">${comboLabel(c)}</th>`).join('')}</tr>`;
  const rows = models.map((m) => `<tr>
    <th class="whitespace-nowrap pr-3 text-left text-sm font-medium">${esc(modelName(m))}</th>
    ${combos.map((c) => renderCell(cellId(m, c.harness, c.context), best, econ)).join('')}
  </tr>`).join('');
  $('#matrix').innerHTML = head + rows;
  $('#matrixWrap').classList.remove('hidden');
  const mid = matrixId();
  $('#matrixTraces').classList.toggle('hidden', !mid);
  if (mid) $('#matrixTraces').href = tracesHref({matrix: mid});
  $('#mlflowLink').classList.toggle('hidden', !state.mlflow?.matrix);
  if (state.mlflow?.matrix) $('#mlflowLink').href = state.mlflow.matrix;
  $$('#matrix [data-cell]').forEach((b) => b.onclick = () => selectCell(b.dataset.cell));
}

function renderCell(id, best, econ) {
  const {state} = app;
  const a = aggregate(id);
  const selected = state.selected === id;
  const err = state.errors[id] ? `<div class="mt-1 break-words text-[11px] text-danger">${esc(state.errors[id])}</div>` : '';
  if (!a) {
    return err ? `<td class="align-top"><div class="rounded-control border border-danger/30 bg-danger-soft p-3 text-xs">${err}</div></td>`
      : `<td class="align-top"><div class="flex min-h-[92px] min-w-[150px] items-center gap-2 rounded-control border border-dashed border-border-strong p-3 text-xs text-ink-subtle">${spinner}Running…</div></td>`;
  }
  const multi = state.repeats > 1;
  const e = econ[id];
  const star = isFinite(best) && scoreOf(id, econ) === best ? ` <span class="text-base text-accent" title="Best ${rankLabel()}">★</span>` : '';
  const money = e ? `<div class="text-xs tabular-nums text-ink">${fmtUSD(e.c)}/q · net ${fmtUSD(e.u, true)}/q</div>` : '';
  const range = multi && a.n > 1 ? ` <span class="text-xs font-normal text-ink-muted" title="Min–max pass rate across runs">${pct(a.min)}–${pct(a.max)}</span>` : '';
  const runs = multi ? `<div class="mt-0.5 text-xs tabular-nums"><span class="font-semibold" title="Share of prompts that passed in every run">pass^k ${pct(a.pass_k)}</span>` +
    `${a.flaky.length ? ` · <span class="text-warning">${a.flaky.length} flaky</span>` : ''}</div>` +
    `<div class="flex items-center gap-1 text-[11px] text-ink-muted">${a.n < state.repeats ? spinner : ''}${a.n}/${state.repeats} runs · mean shown</div>` : '';
  const counts = multi ? '' : (() => { const r = reportsFor(id)[0]; return ` <span class="text-xs font-normal text-ink-muted">${r.passed}/${r.total}</span>`; })();
  return `<td class="align-top"><button type="button" data-cell="${esc(id)}" aria-pressed="${selected}"
    class="w-full min-w-[150px] rounded-control border p-3 text-left transition-colors hover:border-border-strong ${selected ? 'border-accent ring-2 ring-accent/40' : 'border-border'}"
    style="background:${passTint(a.pass_rate)}">
    <div class="whitespace-nowrap text-[26px] font-semibold leading-tight tracking-tight tabular-nums">${pct(a.pass_rate)}${star}${counts}${range}</div>
    <div class="text-xs tabular-nums text-ink-muted">${fmtS(a.seconds)} · ${Math.round(a.tokens).toLocaleString()} tok · ${+a.tool_calls.toFixed(1)} tools</div>
    ${money}${runs}${err}
  </button></td>`;
}

export function renderPerPrompt() {
  const {state} = app;
  const {models, combos} = planDims();
  const cols = models.flatMap((m) => combos.map((c) => ({id: cellId(m, c.harness, c.context), m, c})));
  const head = `<tr><th>Prompt</th>${cols.map(({m, c}) =>
    `<th class="whitespace-nowrap !text-center">${esc(modelName(m))}<br><span class="font-normal">${comboLabel(c)}</span></th>`).join('')}</tr>`;
  const rows = state.prompts.map((p, i) => `<tr>
    <td class="max-w-[280px]">${esc(p.text)}</td>
    ${cols.map(({id}) => {
      const cases = reportsFor(id).map((r) => r.cases[i]).filter(Boolean);
      if (!cases.length) return '<td class="text-center text-ink-subtle">·</td>';
      if (state.repeats === 1) {
        return `<td class="text-center font-semibold ${cases[0].passed ? 'text-success' : 'text-danger'}" title="${cases[0].passed ? 'Passed' : 'Failed'}">${cases[0].passed ? '✓' : '✗'}</td>`;
      }
      const k = cases.filter((c) => c.passed).length;
      return `<td class="text-center font-medium tabular-nums" style="background:${passTint(k / cases.length)}"
        title="Passed in ${k} of ${cases.length} runs">${k}/${cases.length}</td>`;
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
  $('#detail').innerHTML = renderEconSummary(id) + (state.repeats > 1 ? renderRunSummary(id, rs, r.run) : '') + renderReport(r);
  $$('#detail [data-run]').forEach((b) => b.onclick = () => selectCell(id, +b.dataset.run));
  $$('#detail [data-trace]').forEach((b) => b.onclick = () => openTraceDrawer(b.dataset.trace));
}

function renderRunSummary(id, rs, activeRun) {
  const {state} = app;
  const a = aggregate(id);
  const sd = Math.sqrt(mean(rs.map((r) => (r.pass_rate - a.pass_rate) ** 2)));
  const tabs = rs.map((r) => `<button type="button" data-run="${r.run}" aria-pressed="${r.run === activeRun}"
    class="rounded-control border px-2.5 py-1 text-xs tabular-nums ${r.run === activeRun ? 'border-accent bg-accent text-accent-fg' : 'border-border hover:bg-surface-2'}">
    Run ${r.run} · ${pct(r.pass_rate)}</button>`).join('');
  return `
  <section class="panel mb-4 p-4">
    <h3 class="mb-2 text-sm font-medium text-ink-muted">Across ${a.n} of ${state.repeats} runs</h3>
    <div class="mb-3 grid grid-cols-2 gap-2 md:grid-cols-6">
      ${stat('pass^k', pct(a.pass_k))}
      ${stat('Mean pass rate', pct(a.pass_rate))}
      ${stat('Range', `${pct(a.min)}–${pct(a.max)}`)}
      ${stat('Std dev', (sd * 100).toFixed(1) + ' pts')}
      ${stat('Mean cell time', fmtS(a.seconds))}
      ${stat('Mean tokens', Math.round(a.tokens).toLocaleString())}
    </div>
    ${a.flaky.length ? `<p class="mb-3 rounded-control bg-warning-soft p-2 text-xs"><span class="font-semibold">Flaky prompts</span> (passed in some runs, not all): ${a.flaky.map((i) => esc(state.prompts[i].text)).join(' · ')}</p>` : ''}
    <div class="flex flex-wrap gap-2">${tabs}</div>
  </section>`;
}

function renderReport(r) {
  const {state} = app;
  const runId = cellRunId(r.id);
  return `
  <section class="panel min-w-0 p-4">
    <div class="mb-1 flex flex-wrap items-baseline justify-between gap-2">
      <h2 class="text-lg font-semibold tracking-tight">${esc(modelName(r.model))} · ${comboLabel({harness: r.harness_on, context: r.context})}${state.repeats > 1 ? ` · run ${r.run}` : ''}</h2>
      <div class="flex items-center gap-3 text-xs">
        ${runId ? `<a class="link" href="${tracesHref({matrix: matrixId(), run_id: runId})}">Traces for this cell</a>` : ''}
        ${state.mlflow?.cells?.[r.id] ? `<a href="${esc(state.mlflow.cells[r.id])}" target="_blank" rel="noopener" class="text-ink-muted hover:text-ink"
          title="This cell's run in MLflow's own UI">in MLflow ↗</a>` : ''}
      </div>
    </div>
    ${r.harness?.length ? `<p class="hint mb-3">Capabilities: ${r.harness.map(esc).join(', ')}</p>` : '<div class="mb-3"></div>'}
    <div class="mb-4 grid grid-cols-3 gap-2 md:grid-cols-6">
      ${stat('Pass rate', `${pct(r.pass_rate)} <span class="text-sm font-normal text-ink-muted">(${r.passed}/${r.total})</span>`)}
      ${stat('Cell time', fmtS(r.total_seconds))}
      ${stat('Avg / prompt', fmtS(r.avg_case_seconds))}
      ${stat('Tokens', (r.total_tokens ?? 0).toLocaleString())}
      ${stat('Requests', r.requests)}
      ${stat('Tool calls', r.tool_calls)}
      ${r.context === 'glossary_search' ? stat('Glossary searches', r.glossary_searches) : ''}
    </div>
    <div class="grid gap-3 xl:grid-cols-2">${r.cases.map((c) => renderCase(c, r.context)).join('')}</div>
  </section>`;
}

const queryLabel = (text) => `<p class="mt-2.5 text-xs font-medium text-ink-muted">${text}</p>`;

export function renderQuery(c) {
  return `
    ${c.malloy_query ? `${queryLabel('Malloy query')}<pre class="code mt-1">${esc(c.malloy_query)}</pre>${queryLabel('Compiled SQL')}` : ''}
    ${c.metricflow_query ? `${queryLabel('MetricFlow query')}<pre class="code mt-1">${esc(JSON.stringify(c.metricflow_query, null, 2))}</pre>${queryLabel('Compiled SQL')}` : ''}
    ${c.semantic_query ? `${queryLabel('Semantic query (retired YAML layer)')}<pre class="code mt-1">${esc(JSON.stringify(c.semantic_query, null, 2))}</pre>${queryLabel('Compiled SQL')}` : ''}
    ${c.sql ? `<pre class="code mt-1">${esc(c.sql)}</pre>` : ''}
    ${c.explanation ? `<p class="mt-2 text-xs text-ink-muted">${esc(c.explanation)}</p>` : ''}
    ${c.expected_sql ? `${queryLabel('Expected')}<pre class="mt-1 overflow-x-auto rounded-control bg-surface-2 p-2 font-mono text-xs">${esc(c.expected_sql)}</pre>` : ''}
    ${c.rows ? renderRows(c.rows) : ''}`;
}

function renderCase(c, context) {
  const checks = Object.entries(c.checks || {}).map(([name, v]) => checkLine(name, v.passed, v.reason)).join('');
  const meta = c.seconds != null
    ? `${fmtS(c.seconds)} · ${((c.input_tokens || 0) + (c.output_tokens || 0)).toLocaleString()} tok · ${c.requests ?? 0} req · ${c.tool_calls ?? 0} tool calls` +
      (context === 'glossary_search' ? ` · ${c.glossary_searches ?? 0} glossary searches` : '')
    : '';
  return `
  <article class="min-w-0 rounded-control border p-3 ${c.passed ? 'border-success/30' : 'border-danger/30'}">
    <div class="flex items-start gap-2">
      ${passBadge(c.passed)}
      <div class="min-w-0 flex-1">
        <p class="text-sm font-medium">${esc(c.prompt)}</p>
        <p class="text-xs tabular-nums text-ink-subtle">${meta}</p>
      </div>
      ${c.trace_id ? `<button type="button" data-trace="${esc(c.trace_id)}" class="btn btn-outline shrink-0 px-2 py-1 text-xs">View trace</button>` : ''}
    </div>
    ${c.error ? `<p class="mt-2 break-words rounded-control bg-danger-soft p-2 font-mono text-xs">${esc(c.error)}</p>` : ''}
    <ul class="mt-2 space-y-1 text-xs">${checks}</ul>
    ${c.sql ? `<details class="mt-2"><summary class="cursor-pointer text-xs text-accent">Query and results</summary>${renderQuery(c)}</details>` : ''}
  </article>`;
}

export function renderRows({columns, rows, row_count}) {
  return `
  <div class="mt-2 overflow-x-auto">
    <table class="data-table w-full text-xs">
      <thead><tr>${columns.map((c) => `<th>${esc(c)}</th>`).join('')}</tr></thead>
      <tbody>${rows.map((r) => `<tr>${r.map((v) => `<td class="tabular-nums">${esc(v)}</td>`).join('')}</tr>`).join('')}</tbody>
    </table>
    ${row_count > rows.length ? `<p class="mt-1 text-[11px] text-ink-subtle">Showing ${rows.length} of ${row_count} rows</p>` : ''}
  </div>`;
}
