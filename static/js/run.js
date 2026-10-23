// Run tab: the matrix form, the streaming run, and loading a past matrix (from History) into the results view.

import {$, $$, esc, fmtDate} from './ui.js';
import {app, freshState, MODELS} from './config.js';
import {postJSON, readNdjson} from './api.js';
import {renderAll, renderRunTimes, selectCell} from './results.js';

function chip(group, value, label, checked, title = '') {
  return `<label${title ? ` title="${esc(title)}"` : ''}><input type="checkbox" name="${group}" value="${esc(value)}"
    class="peer sr-only" ${checked ? 'checked' : ''}><span class="chip">${esc(label)}</span></label>`;
}

const checkedValues = (group) => $$(`input[name=${group}]:checked`).map((i) => i.value);
const repeats = () => Math.max(1, Math.min(10, Math.round(+$('#repeats').value || 1)));

function updateCount() {
  const cells = checkedValues('model').length * checkedValues('harness').length * checkedValues('context').length;
  const n = app.config.question_sets[$('#questionSet').value];
  const runs = repeats();
  $('#matrixCount').textContent = `${cells} cell${cells === 1 ? '' : 's'} × ${n} questions` +
    `${runs > 1 ? ` × ${runs} runs` : ''} = ${cells * n * runs} agent runs`;
}

function showStatus(msg, kind = 'info') {
  const el = $('#status');
  el.classList.remove('hidden', 'notice-info', 'notice-ok', 'notice-error');
  el.classList.add(`notice-${kind}`);
  el.textContent = msg;
}

export function initRun(cfg) {
  $('#modelChoices').innerHTML = MODELS.map((m) => chip('model', m.id, m.name, m.checked)).join('');
  $('#harnessChoices').innerHTML = chip('harness', 'false', 'Off', true) + chip('harness', 'true', 'On', true);
  $('#contextChoices').innerHTML = Object.entries(cfg.contexts)
    .map(([id, c]) => chip('context', id, c.name, true, c.summary)).join('');
  $('#questionSet').innerHTML = Object.entries(cfg.question_sets).map(([name, n]) =>
    `<option value="${esc(name)}">${name === 'holdout' ? 'Held-out' : 'Benchmark'} (${n})</option>`).join('');
  $$('input[name=model], input[name=harness], input[name=context]').forEach((i) => i.onchange = updateCount);
  $('#repeats').oninput = updateCount;
  $('#questionSet').onchange = updateCount;
  updateCount();
  if (!cfg.api_key_set) showStatus('Set ANTHROPIC_API_KEY and restart the server to run evals.', 'error');
  $('#form').onsubmit = submit;
}

function resetResults(extra) {
  app.state = freshState(extra);
  $('#detail').innerHTML = '';
  $('#loadedBanner').classList.add('hidden');
  for (const id of ['#matrixWrap', '#perPromptWrap']) $(id).classList.add('hidden');
}

async function submit(e) {
  e.preventDefault();
  const body = {
    models: checkedValues('model'),
    harness_modes: checkedValues('harness').map((v) => v === 'true'),
    context_modes: checkedValues('context'),
    question_set: $('#questionSet').value,
    repeats: repeats(),
  };
  if (!body.models.length || !body.harness_modes.length || !body.context_modes.length)
    return showStatus('Pick at least one option in each matrix dimension.', 'error');

  resetResults({repeats: body.repeats});
  $('#runBtn').disabled = true;
  $('#empty').classList.add('hidden');
  const t0 = performance.now();
  const elapsed = () => ((performance.now() - t0) / 1000).toFixed(1) + 's';
  const progress = () => {
    const {state} = app;
    const done = Object.values(state.runs).reduce((n, r) => n + Object.keys(r).length, 0) + Object.keys(state.errors).length;
    const current = Math.min(Object.keys(state.runSeconds).length + 1, state.repeats);
    const run = state.repeats > 1 ? `Run ${current}/${state.repeats} · ` : '';
    showStatus(`Running… ${run}${done}/${(state.cells.length * state.repeats) || '?'} cells done · ${elapsed()}`);
  };
  const timer = setInterval(progress, 200);
  try {
    const res = await postJSON('/api/run', body);
    await readNdjson(res, handleEvent);
    clearInterval(timer);
    app.state.totalSeconds = (performance.now() - t0) / 1000;
    renderRunTimes();
    const runs = app.state.repeats > 1 ? ` × ${app.state.repeats} runs` : '';
    showStatus(`Done: ${app.state.cells.length} cells${runs} in ${elapsed()}.`, 'ok');
  } catch (err) {
    clearInterval(timer);
    showStatus(err.message, 'error');
    if (!app.state.cells.length) $('#empty').classList.remove('hidden');
  } finally {
    $('#runBtn').disabled = false;
  }
}

function handleEvent(ev) {
  const {state} = app;
  if (ev.type === 'plan') Object.assign(state, {cells: ev.cells, repeats: ev.repeats, mlflow: ev.mlflow, prompts: ev.prompts});
  else if (ev.type === 'run_end') state.runSeconds[ev.run] = ev.seconds;
  else if (ev.type === 'cell') (state.runs[ev.run] ??= {})[ev.report.id] = ev.report;
  else if (ev.type === 'cell_error') state.errors[ev.id] = `Run ${ev.run}: ${ev.error}`;
  else return;
  renderAll();
  if (ev.type === 'cell' && !state.selected) selectCell(ev.report.id);
}

/** Show a past matrix (from /api/matrices/{id}) in the results view, exactly as if it had just run. */
export function loadMatrix(m) {
  const cells = m.cells.map(({id, model, harness_on, context}) => ({id, model, harness_on, context}));
  resetResults({
    cells,
    repeats: m.repeats,
    prompts: m.prompts.map((p) => p.text),
    runs: m.runs,
    errors: m.errors,
    runSeconds: m.run_seconds ?? {},
    totalSeconds: m.seconds,
    mlflow: {matrix: m.mlflow_url,
             cells: Object.fromEntries(m.cells.map((c) => [c.id, c.mlflow_url]).filter(([, u]) => u))},
  });
  $('#status').classList.add('hidden');
  const banner = $('#loadedBanner');
  banner.innerHTML = `<span>Showing <span class="font-medium">${esc(m.name)}</span> from ${esc(fmtDate(m.start_time))}.</span>
    <a class="btn btn-quiet px-2 py-1 text-xs" href="#/history">Back to History</a>`;
  banner.classList.remove('hidden');
  const first = cells.find((c) => Object.values(m.runs).some((r) => r[c.id]));
  renderAll();
  if (first) selectCell(first.id);
}
