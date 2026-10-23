// Run tab: the setup form and the streaming run. The results block is shared with History, which shows past runs
// in it; the Run tab only ever shows the latest run started here.

import {$, $$, esc} from './ui.js';
import {app, freshState, models} from './config.js';
import {postJSON, readNdjson} from './api.js';
import {renderAll, renderProgress, renderRunTimes, selectCell} from './results.js';
import {modelIcon, targetIcon} from './icons.js';

const check = '<svg viewBox="0 0 12 12" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M2.5 6.5l2.5 2.5 4.5-5"/></svg>';

function chip(group, value, label, checked, title = '', icon = '') {
  return `<label${title ? ` title="${esc(title)}"` : ''}><input type="checkbox" name="${group}" value="${esc(value)}"
    class="peer sr-only" ${checked ? 'checked' : ''}><span class="chip"><span class="chip-box" aria-hidden="true">${check}</span>${icon}${esc(label)}</span></label>`;
}

const checkedValues = (group) => $$(`input[name=${group}]:checked`).map((i) => i.value);
const repeats = () => Math.max(1, Math.min(10, Math.round(+$('#repeats').value || 1)));

function updateCount() {
  const cells = checkedValues('model').length * checkedValues('harness').length * checkedValues('context').length;
  const n = app.config.question_sets[$('#questionSet').value];
  const runs = repeats();
  $('#runBtn').title = `${cells} setup${cells === 1 ? '' : 's'} × ${n} prompts` +
    `${runs > 1 ? ` × ${runs} repeats` : ''} = ${cells * n * runs} answers`;
}

function showStatus(msg, kind = 'info') {
  const el = $('#status');
  el.classList.remove('hidden', 'notice-info', 'notice-ok', 'notice-error');
  el.classList.add(`notice-${kind}`);
  el.textContent = msg;
}

/** The model chips, from the settings. Re-run after Settings saves; choices still on offer stay as picked. */
export function renderModelChoices() {
  const picked = new Set(checkedValues('model'));
  const keep = picked.size > 0;
  $('#modelChoices').innerHTML = models().map((m) => chip('model', m.id, m.name, keep ? picked.has(m.id) : m.checked, '', modelIcon())).join('');
  $$('input[name=model]').forEach((i) => i.onchange = updateCount);
  if (keep && !checkedValues('model').length) $$('input[name=model]')[0].checked = true;
  updateCount();
}

export function initRun(cfg) {
  renderModelChoices();
  $('#harnessChoices').innerHTML = chip('harness', 'false', 'Baseline', true, 'The model with only the target’s query tools: nothing guiding or checking it') + chip('harness', 'true', 'Harness', true, 'The baseline plus the harness: extra guidance, query and answer checks, a reviewer, more retries');
  $('#contextChoices').innerHTML = Object.entries(cfg.contexts)
    .map(([id, c]) => chip('context', id, c.name, true, c.summary, targetIcon(id))).join('');
  $('#questionSet').innerHTML = Object.entries(cfg.question_sets).map(([name, n]) =>
    `<option value="${esc(name)}">${name === 'holdout' ? 'Held-out' : 'Benchmark'} (${n} prompts)</option>`).join('');
  $$('input[name=harness], input[name=context]').forEach((i) => i.onchange = updateCount);
  $('#repeats').oninput = updateCount;
  $('#questionSet').onchange = updateCount;
  updateCount();
  if (!cfg.api_key_set) showStatus('Set ANTHROPIC_API_KEY and restart the server to run.', 'error');
  $('#form').onsubmit = submit;
  $('#stopBtn').onclick = () => stopping?.abort();
}

// Stops the live run. Dropping its stream is enough: the server cancels the matrix when the client disconnects, and
// records it as stopped early. Repeats that finished are kept.
let stopping = null;

// The latest run started from this page. It keeps streaming while another page shows a past run.
let live = freshState();

/** Move the results block into `container` and render `state` in it. */
export function showResults(container, state) {
  container.append($('#results'));
  $('#results').hidden = false;
  if (app.state === state && state.cells.length) return;
  app.state = state;
  $('#detail').innerHTML = '';
  for (const id of ['#matrixWrap', '#perPromptWrap']) $(id).classList.add('hidden');
  $('#resultTabs').classList.replace('flex', 'hidden');
  $('#empty').classList.toggle('hidden', state.cells.length > 0);
  renderAll();
}

export const showRun = () => showResults($('#view-run'), live);

/** Copy a past run's settings into the form. */
export function prefill(params) {
  const pick = (group, values) => $$(`input[name=${group}]`).forEach((i) => i.checked = values.includes(i.value));
  const split = (v) => (v ? v.split(',') : []);
  pick('model', split(params.models));
  pick('context', split(params.contexts));
  pick('harness', split(params.harness_modes).map((h) => String(h === 'on')));
  if (params.question_set in app.config.question_sets) $('#questionSet').value = params.question_set;
  $('#repeats').value = params.repeats ?? 1;
  updateCount();
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
    return showStatus('Pick at least one model, agent and target.', 'error');

  live = freshState({repeats: body.repeats});
  showRun();
  $('#runBtn').disabled = true;
  stopping = new AbortController();
  $('#stopBtn').classList.remove('hidden');
  $('#empty').classList.add('hidden');
  const t0 = performance.now();
  const elapsed = () => ((performance.now() - t0) / 1000).toFixed(1) + 's';
  const progress = () => {
    const state = live;
    const cells = Object.values(state.progress).flatMap((r) => Object.values(r));
    const answered = cells.reduce((n, p) => n + Object.keys(p.done).length, 0);
    const running = cells.reduce((n, p) => n + p.active.size, 0);
    const total = state.cells.length * state.prompts.length * state.repeats;
    const current = Math.min(Object.keys(state.runSeconds).length + 1, state.repeats);
    const run = state.repeats > 1 ? `Repeat ${current}/${state.repeats} · ` : '';
    showStatus(`Running… ${run}${answered}/${total || '?'} answers · ${running} running · ${elapsed()}`);
  };
  const timer = setInterval(progress, 200);
  try {
    const res = await postJSON('/api/run', body, {signal: stopping.signal});
    await readNdjson(res, handleEvent);
    clearInterval(timer);
    live.totalSeconds = (performance.now() - t0) / 1000;
    if (app.state === live) renderRunTimes();
    const runs = live.repeats > 1 ? ` × ${live.repeats} repeats` : '';
    showStatus(`Done: ${live.cells.length} setups${runs} in ${elapsed()}.`, 'ok');
  } catch (err) {
    clearInterval(timer);
    if (stopping.signal.aborted) {
      live.stopped = true;
      for (const cells of Object.values(live.progress)) for (const p of Object.values(cells)) p.active.clear();  // cancelled
      if (app.state === live) renderProgress();
      const kept = Object.keys(live.runSeconds).length;
      showStatus(`Stopped after ${elapsed()}. ${kept ? `${kept} finished repeat${kept > 1 ? 's' : ''} saved` : 'No repeat finished'}; History lists this run as stopped early.`);
    } else {
      showStatus(err.message, 'error');
    }
    if (!live.cells.length && app.state === live) $('#empty').classList.remove('hidden');
  } finally {
    $('#runBtn').disabled = false;
    $('#stopBtn').classList.add('hidden');
    stopping = null;
  }
}

// Question events can arrive dozens a second: batch their re-render into one per frame.
let progressFrame = null;
function scheduleProgress(state) {
  progressFrame ??= requestAnimationFrame(() => {
    progressFrame = null;
    if (app.state === state) renderProgress();
  });
}

function handleEvent(ev) {
  const state = live;
  if (ev.type === 'case_start' || ev.type === 'case') {
    const p = ((state.progress[ev.run] ??= {})[ev.id] ??= {active: new Set(), done: {}});
    if (ev.type === 'case_start') p.active.add(ev.case);
    else { p.active.delete(ev.case); p.done[ev.case] = ev.status === 'infra_error' ? null : ev.passed; }  // null: not scored
    return scheduleProgress(state);
  }
  if (ev.type === 'plan') Object.assign(state, {cells: ev.cells, repeats: ev.repeats, mlflow: ev.mlflow, prompts: ev.prompts, matrixId: ev.matrix_id});
  else if (ev.type === 'run_end') state.runSeconds[ev.run] = ev.seconds;
  else if (ev.type === 'cell') (state.runs[ev.run] ??= {})[ev.report.id] = ev.report;
  else if (ev.type === 'cell_error') state.errors[ev.id] = `Repeat ${ev.run}: ${ev.error}`;
  else return;
  if (app.state !== state) { if (ev.type === 'cell') state.selected ??= ev.report.id; return; }
  renderAll();
  if (ev.type === 'cell' && !state.selected) selectCell(ev.report.id);
}
