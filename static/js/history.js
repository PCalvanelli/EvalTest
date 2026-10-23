// History tab: every run in the MLflow store (#/history), and one past run on its own page (#/history?run=<id>).

import {$, esc, pct, fmtInt, fmtDate, fmtDur, fmtUSD, empty, loading, errorBox} from './ui.js';
import {app, modelName, contextName, cellLabel, freshState} from './config.js';
import {getJSON} from './api.js';
import {go} from './router.js';
import {showResults, prefill} from './run.js';

const split = (v) => (v ? v.split(',').filter(Boolean) : []);
function describe(m) {
  const p = m.params;
  const models = split(p.models).map(modelName).join(', ');
  const contexts = split(p.contexts).map(contextName).join(', ');
  const harness = split(p.harness_modes).map((h) => (h === 'on' ? 'harness' : 'baseline')).join(' / ');
  return {models, contexts, harness};
}
// The cell to headline: highest consistent accuracy (pass^k), then mean pass rate.
const bestCell = (m) => [...m.cells].filter((c) => c.metrics.mean_pass_rate != null)
  .sort((a, b) => (b.metrics.pass_k ?? 0) - (a.metrics.pass_k ?? 0) || (b.metrics.mean_pass_rate ?? 0) - (a.metrics.mean_pass_rate ?? 0))[0];
const STATUS = {FINISHED: ['Finished', 'badge-pass'], RUNNING: ['Running', 'badge-neutral'],
  KILLED: ['Stopped early', 'badge-warn'], FAILED: ['Failed', 'badge-fail']};
function statusPill(status) {
  const [label, tone] = STATUS[status] ?? [status, 'badge-neutral'];
  const dot = `<span class="h-1.5 w-1.5 rounded-full bg-current${status === 'RUNNING' ? ' animate-pulse' : ''}" aria-hidden="true"></span>`;
  return `<span class="badge ${tone} rounded-full px-2 font-medium">${dot}${esc(label)}</span>`;
}

export function showHistory(params = {}) {
  $('#historyList').hidden = !!params.run;
  $('#pastRun').hidden = !params.run;
  return params.run ? showPastRun(params.run) : showList(Math.max(1, Math.floor(+params.page) || 1));
}

const PAGE_SIZE = 25;
let listPage = 1;  // so "← History" on a past run returns to the page it was opened from

async function showList(page) {
  listPage = page;
  const body = $('#historyBody');
  body.innerHTML = loading('Loading runs');
  $('#historyRefresh').onclick = () => showList(page);
  let matrices, total;
  try {
    ({matrices, total} = await getJSON('/api/matrices', {limit: PAGE_SIZE, offset: (page - 1) * PAGE_SIZE}));
  } catch (err) {
    body.innerHTML = errorBox(err.message);
    return;
  }
  if (!total) {
    body.innerHTML = empty('No runs yet. <a class="link" href="#/run">Start one</a>.');
    return;
  }
  const pages = Math.ceil(total / PAGE_SIZE);
  if (page > pages) { go(`#/history?page=${pages}`); return; }
  body.innerHTML = `
    <div class="panel overflow-x-auto">
      <table class="data-table w-full text-sm">
        <thead><tr>
          <th>When</th><th>Models</th><th>Targets</th>
          <th class="!text-right">Answers</th><th class="!text-right">Cost</th><th class="!text-right" title="Base-10 log of the duration in milliseconds: 3 = 1 second, 4 = 10 seconds, 5 ≈ 1.7 minutes">Duration (log₁₀ ms)</th><th>Best setup (always passed · average)</th><th>Status</th><th></th>
        </tr></thead>
        <tbody>${matrices.map(row).join('')}</tbody>
      </table>
    </div>
    ${pages > 1 ? pager(page, pages, total, matrices.length) : ''}`;
}

function pager(page, pages, total, shown) {
  const first = (page - 1) * PAGE_SIZE + 1;
  const link = (p, label) => (p >= 1 && p <= pages
    ? `<a class="btn btn-outline px-2.5 py-1 text-xs" href="#/history?page=${p}">${label}</a>`
    : `<span class="btn btn-outline px-2.5 py-1 text-xs opacity-50" aria-disabled="true">${label}</span>`);
  return `
    <nav class="mt-3 flex items-center justify-between gap-3 text-sm" aria-label="Pages">
      <span class="tabular-nums text-ink-muted">${first}–${first + shown - 1} of ${total}</span>
      <div class="flex items-center gap-2">
        ${link(page - 1, '← Newer')}
        <span class="tabular-nums text-ink-muted">Page ${page} of ${pages}</span>
        ${link(page + 1, 'Older →')}
      </div>
    </nav>`;
}

// One number per run, the same "answers" the Run button counts: setups × questions × repeats.
const answers = (m) => (m.params.prompts ? fmtInt(m.cells.length * m.params.prompts * (m.params.repeats ?? 1)) : '—');
const plural = (n, word) => `${n} ${word}${+n === 1 ? '' : 's'}`;
const answersDetail = (m) =>
  `${plural(m.cells.length, 'setup')} × ${plural(m.params.prompts ?? '?', 'prompt')} × ${plural(m.params.repeats ?? 1, 'repeat')}`;

// Total spend at list prices: each setup's average cost per question × questions × repeats it completed.
// Setups with no completed repeat have no recorded cost, and runs from before cost tracking have none at all.
function costCell(m) {
  const costed = m.cells.filter((c) => c.metrics.mean_cost_per_question_usd != null);
  if (!costed.length) return '<td class="text-right text-ink-subtle" title="Not recorded for this run">—</td>';
  const total = costed.reduce((sum, c) =>
    sum + c.metrics.mean_cost_per_question_usd * (+m.params.prompts || 0) * (c.metrics.runs_completed ?? 1), 0);
  const partial = costed.length < m.cells.length || m.status !== 'FINISHED';
  const title = partial ? 'Completed repeats only; work cut short is not counted' : 'At list prices (app/pricing.py)';
  return `<td class="whitespace-nowrap text-right tabular-nums" title="${title}">${partial ? '≥ ' : ''}${fmtUSD(total)}</td>`;
}

// log10 of the duration in milliseconds, with the raw ms and the readable form (7m 01s) as the tooltip.
function durationCell(m) {
  const s = m.seconds ?? (m.status === 'RUNNING' ? (Date.now() - m.start_time) / 1000 : null);
  const soFar = m.seconds == null && s != null ? ' so far' : '';
  if (s == null) return '<td class="text-right">—</td>';
  const ms = Math.max(s * 1000, 1);
  return `<td class="whitespace-nowrap text-right tabular-nums" title="${esc(`${fmtInt(ms)} ms · ${fmtDur(s)}${soFar}`)}">${Math.log10(ms).toFixed(2)}${soFar}</td>`;
}

// Matrices graded under another benchmark version (before ids and frozen references: none recorded) don't compare.
function versionNote(m) {
  const v = m.params.benchmark_version ?? '1';
  if (v === String(app.config.benchmark_version)) return '';
  return `<span class="block text-xs text-ink-muted" title="Graded under benchmark v${esc(v)}; this app is v${esc(String(app.config.benchmark_version))}. Scores don't compare.">v${esc(v)} · not comparable</span>`;
}

function row(m) {
  const d = describe(m);
  const best = bestCell(m);
  return `<tr>
    <td class="whitespace-nowrap">${esc(fmtDate(m.start_time))}${versionNote(m)}</td>
    <td>${esc(d.models)}</td>
    <td class="max-w-[320px]">${esc(d.contexts)}<span class="block text-xs text-ink-muted">${esc(d.harness)}</span></td>
    <td class="text-right tabular-nums" title="${esc(answersDetail(m))}">${answers(m)}</td>
    ${costCell(m)}
    ${durationCell(m)}
    <td>${best ? `<span class="font-medium">${esc(cellLabel(best.id))}</span><span class="block text-xs tabular-nums text-ink-muted">${pct(best.metrics.pass_k)} · ${pct(best.metrics.mean_pass_rate)}</span>` : '<span class="text-ink-subtle">—</span>'}</td>
    <td class="whitespace-nowrap">${statusPill(m.status)}</td>
    <td class="whitespace-nowrap text-right">
      <a href="#/history?run=${encodeURIComponent(m.id)}" class="btn btn-outline px-2 py-1 text-xs">Open</a>
    </td>
  </tr>`;
}

// Finished runs don't change, so each is fetched once.
const pastRuns = new Map();

async function showPastRun(id) {
  const header = $('#pastHeader');
  const back = `<a href="#/history${listPage > 1 ? `?page=${listPage}` : ''}" class="text-sm text-ink-muted hover:text-ink">← History</a>`;
  let entry = pastRuns.get(id);
  if (!entry) {
    header.innerHTML = back + loading('Loading run');
    $('#results').hidden = true;
    let m;
    try {
      m = await getJSON(`/api/matrices/${encodeURIComponent(id)}`);
    } catch (err) {
      header.innerHTML = back + `<div class="mt-3">${errorBox(`Couldn't open that run: ${err.message}`)}</div>`;
      return;
    }
    entry = {m, state: toState(m)};
    if (m.status !== 'RUNNING') pastRuns.set(id, entry);
  }
  const {m, state} = entry;
  const d = describe(m);
  header.innerHTML = `${back}
    <div class="mt-2 flex flex-wrap items-end justify-between gap-3">
      <div>
        <div class="flex flex-wrap items-center gap-2">
          <h1 class="text-xl font-semibold tracking-tight">Run from ${esc(fmtDate(m.start_time))}</h1>${statusPill(m.status)}
        </div>
        <p class="hint">${esc([d.models, d.contexts, d.harness].filter(Boolean).join(' · '))} · ${answers(m)} answers</p>
      </div>
      <button id="runAgain" type="button" class="btn btn-outline">Run again with these settings</button>
    </div>`;
  $('#runAgain').onclick = () => { prefill(m.params); go('#/run'); };
  state.selected ??= state.cells.find((c) => Object.values(m.runs).some((r) => r[c.id]))?.id ?? null;
  showResults($('#pastRun'), state);
}

/** A past run (from /api/matrices/{id}) as results state, the same shape a live run builds. */
function toState(m) {
  return freshState({
    cells: m.cells.map(({id, model, harness_on, context}) => ({id, model, harness_on, context})),
    repeats: m.repeats,
    prompts: m.prompts.map((p) => p.text),
    runs: m.runs,
    errors: m.errors,
    runSeconds: m.run_seconds ?? {},
    totalSeconds: m.seconds,
    matrixId: m.id,
    mlflow: {matrix: m.mlflow_url,
             cells: Object.fromEntries(m.cells.map((c) => [c.id, c.mlflow_url]).filter(([, u]) => u))},
  });
}
