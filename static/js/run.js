// Run tab: the matrix form, the streaming run, and loading a past matrix (from History) into the results view.

import {$, $$, esc, store, fmtDate} from './ui.js';
import {app, freshState, MODELS, CONTEXTS, SAMPLE_PROMPTS, DEFAULT_RUBRIC, modelName} from './config.js';
import {postJSON, readNdjson} from './api.js';
import {renderAll, selectCell} from './results.js';
import {renderEconomics, rescore} from './econ.js';

function chip(group, value, label, checked, describedBy = '') {
  return `<label><input type="checkbox" name="${group}" value="${esc(value)}" class="peer sr-only" ${checked ? 'checked' : ''}${
    describedBy ? ` aria-describedby="${describedBy}"` : ''}>
    <span class="chip">${esc(label)}</span></label>`;
}

// What a business context gives the agent, from /api/config. Shared by the tooltip and the reference tab.
const contextFacts = (info) => [['Agent writes', info.writes], ['Tools', info.tools]];

function contextChips(cfg) {
  $('#contextChoices').innerHTML = CONTEXTS.map((c) => {
    const info = cfg.contexts[c.id];
    const desc = `${info.summary} Agent writes: ${info.writes}. Tools: ${info.tools}.`;
    return `<div data-context-tip="${esc(c.id)}">${chip('context', c.id, c.name, true, `ctxdesc-${c.id}`)}
      <span id="ctxdesc-${esc(c.id)}" class="sr-only">${esc(desc)}</span></div>`;
  }).join('');

  // Hover or keyboard focus shows one shared panel under the chip, kept inside the viewport.
  const tip = $('#contextTip');
  const show = (holder) => {
    const info = cfg.contexts[holder.dataset.contextTip];
    tip.innerHTML = `<p class="font-semibold">${esc(info.name)}</p><p class="mt-1 text-ink-muted">${esc(info.summary)}</p>
      <dl class="mt-2 grid grid-cols-[auto_minmax(0,1fr)] gap-x-3 gap-y-1">${contextFacts(info).map(([k, v]) =>
        `<dt class="text-ink-muted">${k}</dt><dd>${k === 'Tools' ? `<code class="font-mono text-xs">${esc(v)}</code>` : esc(v)}</dd>`).join('')}</dl>`;
    tip.classList.remove('hidden');
    const r = holder.getBoundingClientRect();
    const left = Math.max(16, Math.min(r.left, window.innerWidth - tip.offsetWidth - 16));
    const below = r.bottom + 8 + tip.offsetHeight <= window.innerHeight;
    tip.style.left = `${left}px`;
    tip.style.top = `${below ? r.bottom + 8 : r.top - tip.offsetHeight - 8}px`;
  };
  const hide = () => tip.classList.add('hidden');
  $$('[data-context-tip]').forEach((holder) => {
    holder.addEventListener('pointerenter', (e) => { if (e.pointerType === 'mouse') show(holder); });
    holder.addEventListener('pointerleave', hide);
    holder.addEventListener('focusin', () => { if (holder.querySelector(':focus-visible')) show(holder); });
    holder.addEventListener('focusout', hide);
  });
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape') hide(); });
  window.addEventListener('scroll', hide, {passive: true});
  $('#contextExplain').onclick = () => { showRunTab('reference'); showReference('contexts'); };
}
const checkedValues = (group) => $$(`input[name=${group}]:checked`).map((i) => i.value);

function addPrompt(p = {}) {
  const node = $('#promptTpl').content.firstElementChild.cloneNode(true);
  node.querySelector('.p-text').value = p.text || '';
  node.querySelector('.p-sql').value = p.expected_sql || '';
  node.querySelector('.p-remove').onclick = () => { node.remove(); renumber(); updateCount(); };
  node.querySelector('.p-text').oninput = updateCount;
  $('#prompts').appendChild(node);
  renumber();
  updateCount();
}

const renumber = () => $$('.prompt-row .p-num').forEach((el, i) => { el.textContent = i + 1; });

function readPrompts() {
  return $$('.prompt-row')
    .map((r) => ({text: r.querySelector('.p-text').value.trim(), expected_sql: r.querySelector('.p-sql').value.trim() || null}))
    .filter((p) => p.text);
}

function updateCount() {
  const cells = checkedValues('model').length * checkedValues('harness').length * checkedValues('context').length;
  const n = readPrompts().length;
  const runs = Math.max(1, Math.min(10, Math.round(+$('#repeats').value || 1)));
  const total = cells * n * runs;
  $('#matrixCount').textContent = `${cells} cell${cells === 1 ? '' : 's'} × ${n} prompt${n === 1 ? '' : 's'}` +
    `${runs > 1 ? ` × ${runs} runs` : ''} = ${total} agent runs + ${total} judge calls`;
  $('#promptCount').textContent = n;
  $('#harnessBox').classList.toggle('opacity-40', !checkedValues('harness').includes('true'));
}

export function showStatus(msg, kind = 'info') {
  const el = $('#status');
  el.classList.remove('hidden', 'notice-info', 'notice-ok', 'notice-error');
  el.classList.add(`notice-${kind}`);
  el.textContent = msg;
}

// Sub-tabs of the Run view. Results live outside the form; the other panels are parts of it.
export function showRunTab(name) {
  for (const t of $$('[data-subtab]')) t.setAttribute('aria-selected', t.dataset.subtab === name);
  for (const p of $$('#view-run .subpanel')) p.hidden = p.id !== `panel-${name}`;
  if (name === 'results') renderEconomics();  // the chart measures its container, which was hidden
}

function showReference(name) {
  for (const b of $$('[data-ref]')) b.setAttribute('aria-pressed', b.dataset.ref === name);
  for (const p of $$('[data-refpanel]')) p.hidden = p.dataset.refpanel !== name;
}

export function initRun(cfg) {
  $('#modelChoices').innerHTML = MODELS.map((m) => chip('model', m.id, m.name, m.checked)).join('');
  $('#harnessChoices').innerHTML = chip('harness', 'false', 'Off', true) + chip('harness', 'true', 'On', true);
  contextChips(cfg);
  $('#judgeModel').innerHTML = MODELS.map((m) => `<option value="${m.id}" ${m.id === 'claude-haiku-5-5' ? 'selected' : ''}>${m.name}</option>`).join('');
  $$('input[name=model], input[name=harness], input[name=context]').forEach((i) => i.onchange = updateCount);
  $('#repeats').oninput = updateCount;

  SAMPLE_PROMPTS.forEach(addPrompt);
  $('#rubric').value = DEFAULT_RUBRIC;
  const saved = store.get('apiKey');
  if (saved) { $('#apiKey').value = saved; $('#rememberKey').checked = true; }
  $('#workspaceId').value = store.get('workspaceId') || '';
  $('#addPrompt').onclick = () => addPrompt();

  $('#schema').textContent = cfg.schema;
  $('#tableCount').textContent = `(${cfg.table_count} tables)`;
  $('#catalog').textContent = cfg.metricflow_catalog;
  $('#metricflowYaml').textContent = cfg.metricflow_yaml;
  $('#malloyModel').textContent = cfg.malloy_model;
  app.pricing = structuredClone(cfg.pricing);
  $('#pricesAsOf').textContent = `(list prices as of ${cfg.prices_as_of})`;
  $('#priceRows').innerHTML = Object.entries(app.pricing).map(([id, pr]) => `<tr><td class="pr-2">${esc(modelName(id))}</td>${
    ['input', 'output', 'cache_read', 'cache_write'].map((k) => `<td><input type="number" min="0" step="any" value="${pr[k]}"
      data-model="${esc(id)}" data-key="${k}" aria-label="${esc(modelName(id))} ${k.replace('_', ' ')} price"
      class="price-input w-full rounded border border-border bg-surface px-1 py-0.5 text-right"></td>`).join('')}</tr>`).join('');
  $$('.price-input').forEach((el) => el.oninput = () => { app.pricing[el.dataset.model][el.dataset.key] = Math.max(0, +el.value || 0); rescore(); });
  $$('.score-input').forEach((el) => el.oninput = rescore);
  window.addEventListener('resize', () => { if (!$('#view-run').hidden) renderEconomics(); });
  $('#contextHelp').innerHTML = CONTEXTS.map((c) => {
    const info = cfg.contexts[c.id];
    return `<div class="grid gap-1 px-4 py-3 sm:grid-cols-[180px_minmax(0,1fr)] sm:gap-4">
      <dt class="font-medium">${esc(info.name)}</dt>
      <dd><p class="text-ink-muted">${esc(info.summary)}</p>
        <p class="mt-1 text-xs">${contextFacts(info).map(([k, v]) => `<span class="text-ink-muted">${k}:</span> ${
          k === 'Tools' ? `<code class="font-mono">${esc(v)}</code>` : esc(v)}`).join('<span class="mx-2 text-ink-subtle">|</span>')}</p></dd></div>`;
  }).join('');
  // One group per target: the shared core first, then each business context's own harness.
  $('#harnessList').innerHTML = Object.entries(cfg.harness_targets).map(([target, name]) => {
    const caps = Object.entries(cfg.harness_capabilities).filter(([, cap]) => cap.target === target);
    return `<fieldset class="space-y-2"><legend class="mb-1.5 text-[13px] font-medium text-ink-muted">${esc(name)}</legend>${
      caps.map(([k, cap]) => `<label class="flex items-start gap-2"><input type="checkbox" class="harness-cap mt-1 accent-[rgb(var(--accent))]" value="${esc(k)}" ${cap.default ? 'checked' : ''}>
        <span>${esc(cap.label)}</span></label>`).join('')}</fieldset>`;
  }).join('');
  updateCount();
  $$('[data-subtab]').forEach((t) => t.onclick = () => showRunTab(t.dataset.subtab));
  $$('[data-ref]').forEach((b) => b.onclick = () => showReference(b.dataset.ref));
  showReference('contexts');
  showRunTab('results');

  // A browser-blocked submit is otherwise silent when the bad field sits in a hidden tab: show it and say why.
  $('#form').addEventListener('invalid', (e) => {
    const el = e.target;
    const panel = el.closest('.subpanel');
    if (panel?.hidden) showRunTab(panel.id.replace('panel-', ''));
    const name = el.labels?.[0]?.textContent.trim().split('\n')[0] || el.dataset.key || el.id || 'a field';
    showStatus(`Can't run yet: ${name}: ${el.validationMessage}`, 'error');
  }, true);
  $('#form').onsubmit = submit;
}

function resetResults(extra) {
  app.state = freshState(extra);
  $('#detail').innerHTML = '';
  $('#loadedBanner').classList.add('hidden');
  for (const id of ['#matrixWrap', '#econWrap', '#perPromptWrap']) $(id).classList.add('hidden');
}

async function submit(e) {
  e.preventDefault();
  const apiKey = $('#apiKey').value.trim();
  const workspaceId = $('#workspaceId').value.trim();
  if ($('#rememberKey').checked) { store.set('apiKey', apiKey); store.set('workspaceId', workspaceId); }
  else { store.del('apiKey'); store.del('workspaceId'); }

  const prompts = readPrompts();
  const body = {
    api_key: apiKey,
    workspace_id: workspaceId || null,
    judge_model: $('#judgeModel').value,
    models: checkedValues('model'),
    harness_modes: checkedValues('harness').map((v) => v === 'true'),
    context_modes: checkedValues('context'),
    harness: $$('.harness-cap:checked').map((c) => c.value),
    prompts,
    rubric: $('#rubric').value,
    repeats: Math.max(1, Math.min(10, Math.round(+$('#repeats').value || 1))),
  };
  if (!prompts.length) return showStatus('Add at least one prompt.', 'error');
  if (!body.models.length || !body.harness_modes.length || !body.context_modes.length)
    return showStatus('Pick at least one option in each matrix dimension.', 'error');

  resetResults({repeats: body.repeats, prompts});
  showRunTab('results');
  $('#runBtn').disabled = true;
  $('#empty').classList.add('hidden');
  const t0 = performance.now();
  const elapsed = () => ((performance.now() - t0) / 1000).toFixed(1) + 's';
  const progress = () => {
    const {state} = app;
    const done = Object.values(state.runs).reduce((n, r) => n + Object.keys(r).length, 0) + Object.keys(state.errors).length;
    const run = state.repeats > 1 ? `Run ${state.currentRun || 1}/${state.repeats} · ` : '';
    showStatus(`Running… ${run}${done}/${(state.cells.length * state.repeats) || '?'} cells done · ${elapsed()}`);
  };
  const timer = setInterval(progress, 200);
  try {
    const res = await postJSON('/api/run', body);
    await readNdjson(res, handleEvent);
    clearInterval(timer);
    const runs = app.state.repeats > 1 ? ` × ${app.state.repeats} runs` : '';
    showStatus(`Done: ${app.state.cells.length} cells${runs} in ${elapsed()} (wall clock, including judging).`, 'ok');
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
  if (ev.type === 'plan') { state.cells = ev.cells; state.repeats = ev.repeats; state.mlflow = ev.mlflow; }
  else if (ev.type === 'run_start') state.currentRun = ev.run;
  else if (ev.type === 'cell') (state.runs[ev.run] ??= {})[ev.report.id] = ev.report;
  else if (ev.type === 'cell_error') state.errors[ev.id] = `Run ${ev.run}: ${ev.error}`;
  else return;
  renderAll();
  if (ev.type !== 'cell') return;
  if (!state.selected) selectCell(ev.report.id);
}

/** Show a past matrix (from /api/matrices/{id}) in the results view, exactly as if it had just run. */
export function loadMatrix(m) {
  const cells = m.cells.map(({id, model, harness_on, context, run_id}) => ({id, model, harness_on, context, run_id}));
  resetResults({
    cells,
    repeats: m.repeats,
    prompts: m.prompts,
    runs: m.runs,
    errors: m.errors,
    loadedFrom: m.id,
    mlflow: {matrix_id: m.id, matrix: m.mlflow_url, cell_runs: Object.fromEntries(cells.map((c) => [c.id, c.run_id])),
             cells: Object.fromEntries(m.cells.map((c) => [c.id, c.mlflow_url]).filter(([, u]) => u))},
  });
  $('#status').classList.add('hidden');
  showRunTab('results');
  const banner = $('#loadedBanner');
  banner.innerHTML = `<span>Showing <span class="font-medium">${esc(m.name)}</span> from ${esc(fmtDate(m.start_time))}. Your matrix settings above are unchanged.</span>
    <span class="flex gap-2"><button type="button" id="usePrompts" class="btn btn-outline px-2 py-1 text-xs">Use its prompts and rubric</button>
    <a class="btn btn-quiet px-2 py-1 text-xs" href="#/history">Back to History</a></span>`;
  banner.classList.remove('hidden');
  $('#usePrompts').onclick = () => {
    $('#prompts').innerHTML = '';
    m.prompts.forEach(addPrompt);
    if (m.rubric) $('#rubric').value = m.rubric;
    showStatus(`Loaded ${m.prompts.length} prompts and the rubric from ${m.name}.`, 'ok');
  };
  const first = cells.find((c) => Object.values(m.runs).some((r) => r[c.id]));
  renderAll();
  if (first) selectCell(first.id);
}
