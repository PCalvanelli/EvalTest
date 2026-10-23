// History tab: every matrix in the MLflow store. Open one in the Run view.

import {$, $$, esc, pct, fmtDate, fmtDur, empty, loading, errorBox} from './ui.js';
import {modelName, contextName, cellLabel} from './config.js';
import {getJSON} from './api.js';
import {go} from './router.js';
import {loadMatrix} from './run.js';

const split = (v) => (v ? v.split(',').filter(Boolean) : []);
function describe(m) {
  const p = m.params;
  const models = split(p.models).map(modelName).join(', ');
  const contexts = split(p.contexts).map(contextName).join(', ');
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
  let matrices;
  try {
    matrices = await getJSON('/api/matrices');
  } catch (err) {
    body.innerHTML = errorBox(err.message);
    return;
  }
  if (!matrices.length) {
    body.innerHTML = empty('No matrices yet. <a class="link" href="#/run">Run one</a> and it shows up here.');
    return;
  }
  body.innerHTML = `
    <div class="panel overflow-x-auto">
      <table class="data-table w-full text-sm">
        <thead><tr>
          <th>When</th><th>Models</th><th>Target × harness</th>
          <th class="!text-right">Prompts × runs</th><th class="!text-right">Duration</th><th>Best cell (pass^k · mean)</th><th>Status</th><th></th>
        </tr></thead>
        <tbody>${matrices.map(row).join('')}</tbody>
      </table>
    </div>`;
  $$('[data-load]', body).forEach((b) => b.onclick = () => openMatrix(b.dataset.load, b));
}

function row(m) {
  const d = describe(m);
  const best = bestCell(m);
  return `<tr>
    <td class="whitespace-nowrap">${esc(fmtDate(m.start_time))}</td>
    <td>${esc(d.models)}</td>
    <td class="max-w-[320px]">${esc(d.contexts)}<span class="block text-xs text-ink-muted">${esc(d.harness)}</span></td>
    <td class="text-right tabular-nums">${esc(m.params.prompts ?? '—')} × ${esc(m.params.repeats ?? 1)}</td>
    <td class="whitespace-nowrap text-right tabular-nums">${m.seconds != null ? esc(fmtDur(m.seconds)) : m.status === 'RUNNING' ? `${esc(fmtDur((Date.now() - m.start_time) / 1000))} so far` : '—'}</td>
    <td>${best ? `<span class="font-medium">${esc(cellLabel(best.id))}</span><span class="block text-xs tabular-nums text-ink-muted">${pct(best.metrics.pass_k)} · ${pct(best.metrics.mean_pass_rate)}</span>` : '<span class="text-ink-subtle">—</span>'}</td>
    <td class="whitespace-nowrap ${m.status === 'FINISHED' ? '' : 'text-warning'}">${esc(statusText[m.status] ?? m.status)}</td>
    <td class="whitespace-nowrap text-right">
      <button type="button" data-load="${esc(m.id)}" class="btn btn-outline px-2 py-1 text-xs">Open</button>
    </td>
  </tr>`;
}

async function openMatrix(id, button) {
  button.disabled = true;
  button.textContent = 'Opening…';
  try {
    const m = await getJSON(`/api/matrices/${encodeURIComponent(id)}`);
    go('#/run');
    loadMatrix(m);
  } catch (err) {
    button.disabled = false;
    button.textContent = 'Open';
    $('#historyBody').insertAdjacentHTML('afterbegin', errorBox(`Couldn't open that matrix: ${err.message}`));
  }
}
