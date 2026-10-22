// History tab: every matrix in the MLflow store. Open one in the Run view, or compare cells across matrices.

import {$, $$, esc, pct, fmtUSD, fmtDate, fmtDur, empty, loading, errorBox, passTint} from './ui.js';
import {modelName, CONTEXT_SHORT} from './config.js';
import {getJSON} from './api.js';
import {go} from './router.js';
import {loadMatrix} from './run.js';
import {cellLabel} from './traces.js';

let matrices = [];
const chosen = new Set();
const expanded = new Set();

const split = (v) => (v ? v.split(',').filter(Boolean) : []);
function describe(m) {
  const p = m.params;
  const models = split(p.models).map(modelName).join(', ');
  const contexts = split(p.contexts).map((c) => CONTEXT_SHORT[c] ?? c).join(', ');
  const harness = split(p.harness_modes).map((h) => (h === 'on' ? 'harness' : 'no harness')).join(' / ');
  return {models, contexts, harness};
}
// The cell to headline: highest consistent accuracy (pass^k), then mean pass rate.
const bestCell = (m) => [...m.cells].filter((c) => c.metrics.mean_pass_rate != null)
  .sort((a, b) => (b.metrics.pass_k ?? 0) - (a.metrics.pass_k ?? 0) || (b.metrics.mean_pass_rate ?? 0) - (a.metrics.mean_pass_rate ?? 0))[0];
const statusText = {FINISHED: 'Finished', RUNNING: 'Running', KILLED: 'Stopped early', FAILED: 'Failed'};

export async function showHistory() {
  const body = $('#historyBody');
  body.innerHTML = loading('Loading matrices');
  $('#historyRefresh').onclick = showHistory;
  try {
    matrices = await getJSON('/api/matrices');
  } catch (err) {
    body.innerHTML = errorBox(err.message);
    return;
  }
  draw();
}

function draw() {
  const body = $('#historyBody');
  if (!matrices.length) {
    body.innerHTML = empty('No matrices yet. <a class="link" href="#/run">Run one</a> and it shows up here.');
    return;
  }
  body.innerHTML = `
    <div id="compare"></div>
    <div class="panel overflow-x-auto">
      <table class="data-table w-full text-sm">
        <thead><tr>
          <th class="w-8"><span class="sr-only">Compare</span></th><th>When</th><th>Models</th><th>Context × harness</th>
          <th class="!text-right">Prompts × runs</th><th class="!text-right">Duration</th><th>Best cell (pass^k · mean)</th><th>Status</th><th></th>
        </tr></thead>
        <tbody>${matrices.map(row).join('')}</tbody>
      </table>
    </div>`;
  $$('[data-pick]', body).forEach((el) => el.onchange = () => {
    el.checked ? chosen.add(el.dataset.pick) : chosen.delete(el.dataset.pick);
    drawCompare();
  });
  $$('[data-expand]', body).forEach((b) => b.onclick = () => {
    const id = b.dataset.expand;
    expanded.has(id) ? expanded.delete(id) : expanded.add(id);
    draw();
  });
  $$('[data-load]', body).forEach((b) => b.onclick = () => openMatrix(b.dataset.load, b));
  drawCompare();
}

function row(m) {
  const d = describe(m);
  const best = bestCell(m);
  const open = expanded.has(m.id);
  return `<tr class="${open ? 'bg-surface-2/60' : ''}">
    <td><input type="checkbox" data-pick="${esc(m.id)}" ${chosen.has(m.id) ? 'checked' : ''} aria-label="Compare ${esc(m.name)}" class="accent-[rgb(var(--accent))]"></td>
    <td class="whitespace-nowrap">${esc(fmtDate(m.start_time))}</td>
    <td>${esc(d.models)}</td>
    <td class="max-w-[320px]">${esc(d.contexts)}<span class="block text-xs text-ink-muted">${esc(d.harness)}</span></td>
    <td class="text-right tabular-nums">${esc(m.params.prompts ?? '—')} × ${esc(m.params.repeats ?? 1)}</td>
    <td class="whitespace-nowrap text-right tabular-nums">${m.seconds != null ? esc(fmtDur(m.seconds)) : m.status === 'RUNNING' ? `${esc(fmtDur((Date.now() - m.start_time) / 1000))} so far` : '—'}</td>
    <td>${best ? `<span class="font-medium">${esc(cellLabel(best.id))}</span><span class="block text-xs tabular-nums text-ink-muted">${pct(best.metrics.pass_k)} · ${pct(best.metrics.mean_pass_rate)}</span>` : '<span class="text-ink-subtle">—</span>'}</td>
    <td class="whitespace-nowrap ${m.status === 'FINISHED' ? '' : 'text-warning'}">${esc(statusText[m.status] ?? m.status)}</td>
    <td class="whitespace-nowrap text-right">
      <button type="button" data-expand="${esc(m.id)}" class="btn btn-quiet px-2 py-1 text-xs" aria-expanded="${open}">${open ? 'Hide cells' : 'Cells'}</button>
      <button type="button" data-load="${esc(m.id)}" class="btn btn-outline px-2 py-1 text-xs">Open</button>
    </td>
  </tr>
  ${open ? `<tr><td></td><td colspan="8" class="!pt-0">${cellsTable(m)}</td></tr>` : ''}`;
}

function cellsTable(m) {
  const cells = [...m.cells].sort((a, b) => (b.metrics.pass_k ?? -1) - (a.metrics.pass_k ?? -1));
  return `<table class="data-table my-2 w-full text-xs">
    <thead><tr><th>Cell</th><th class="!text-right">pass^k</th><th class="!text-right">Mean pass rate</th><th class="!text-right">Flaky</th>
      <th class="!text-right">$ / question</th><th class="!text-right">Runs</th><th></th></tr></thead>
    <tbody>${cells.map((c) => `<tr>
      <td>${esc(cellLabel(c.id))}</td>
      <td class="text-right font-medium tabular-nums" style="background:${passTint(c.metrics.pass_k)}">${pct(c.metrics.pass_k)}</td>
      <td class="text-right tabular-nums">${pct(c.metrics.mean_pass_rate)}</td>
      <td class="text-right tabular-nums">${c.metrics.flaky_prompts ?? '—'}</td>
      <td class="text-right tabular-nums">${c.metrics.mean_cost_per_question_usd != null ? fmtUSD(c.metrics.mean_cost_per_question_usd) : '—'}</td>
      <td class="text-right tabular-nums">${c.metrics.runs_completed ?? 0}</td>
      <td class="text-right"><a class="link" href="#/traces?${new URLSearchParams({matrix: m.id, run_id: c.run_id})}">Traces</a></td>
    </tr>`).join('')}</tbody>
  </table>`;
}

// Same cell id across the selected matrices, side by side.
function drawCompare() {
  const box = $('#compare');
  if (!box) return;
  const ms = matrices.filter((m) => chosen.has(m.id)).sort((a, b) => a.start_time - b.start_time);
  if (ms.length < 2) {
    box.innerHTML = ms.length === 1 ? '<p class="hint mb-3">Select one more matrix to compare.</p>' : '';
    return;
  }
  const ids = [...new Set(ms.flatMap((m) => m.cells.map((c) => c.id)))].sort();
  const cellOf = (m, id) => m.cells.find((c) => c.id === id);
  box.innerHTML = `
    <section class="panel mb-4 overflow-x-auto p-4">
      <div class="mb-2 flex items-baseline justify-between gap-2">
        <h2 class="panel-title">Compare ${ms.length} matrices</h2>
        <button type="button" id="clearCompare" class="btn btn-quiet px-2 py-1 text-xs">Clear selection</button>
      </div>
      <p class="hint mb-2">pass^k (share of prompts that passed in every run), then mean pass rate. A dash means the cell wasn't in that matrix.</p>
      <table class="data-table w-full text-xs">
        <thead><tr><th>Cell</th>${ms.map((m) => `<th class="!text-right">${esc(fmtDate(m.start_time))}</th>`).join('')}</tr></thead>
        <tbody>${ids.map((id) => `<tr><td class="whitespace-nowrap">${esc(cellLabel(id))}</td>${ms.map((m) => {
          const c = cellOf(m, id);
          return c ? `<td class="text-right tabular-nums" style="background:${passTint(c.metrics.pass_k)}"><span class="font-medium">${pct(c.metrics.pass_k)}</span> <span class="text-ink-muted">${pct(c.metrics.mean_pass_rate)}</span></td>`
            : '<td class="text-right text-ink-subtle">—</td>';
        }).join('')}</tr>`).join('')}</tbody>
      </table>
    </section>`;
  $('#clearCompare').onclick = () => { chosen.clear(); draw(); };
}

async function openMatrix(id, button) {
  button.disabled = true;
  button.textContent = 'Opening…';
  try {
    const m = await getJSON(`/api/matrices/${encodeURIComponent(id)}`);
    go('#/run');  // switch first: the chart measures the visible view
    loadMatrix(m);
  } catch (err) {
    button.disabled = false;
    button.textContent = 'Open';
    $('#historyBody').insertAdjacentHTML('afterbegin', errorBox(`Couldn't open that matrix: ${err.message}`));
  }
}
