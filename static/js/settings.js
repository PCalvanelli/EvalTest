// Settings tab: the local config people change without editing code. Models on offer (with prices), concurrency and
// the labeler model are saved on the server in data/settings.json. API keys and the per-prompt limits are shown, never
// edited here: keys stay in environment variables, and limits define what a pass means, so they change in code.

import {$, $$, esc, errorBox, loading} from './ui.js';
import {app} from './config.js';
import {getJSON} from './api.js';
import {renderModelChoices} from './run.js';

const PRICE_KEYS = [['input', 'Input'], ['output', 'Output'], ['cache_read', 'Cache read'], ['cache_write', 'Cache write']];
let data = null;  // {settings, defaults, saved, info} from GET /api/settings; settings is edited in place

export async function showSettings() {
  const el = $('#settingsBody');
  if (data) return;
  el.innerHTML = loading('Loading settings');
  try {
    data = await getJSON('/api/settings');
    render();
  } catch (e) {
    el.innerHTML = errorBox(e.message);
  }
}

const num = (v) => (v == null ? '' : String(v));
const input = (attrs, cls = '') => `<input ${attrs} class="input mt-0 py-1.5 text-sm ${cls}">`;

function modelRow(m, i) {
  return `<tr data-row="${i}">
    <td>${input(`data-f="name" value="${esc(m.name)}" maxlength="60" aria-label="Display name"`, 'w-32')}</td>
    <td>${input(`data-f="id" value="${esc(m.id)}" spellcheck="false" aria-label="Model id"`, 'w-48 font-mono text-xs')}
      <p data-check-result class="mt-1 text-xs"></p></td>
    <td class="text-center"><input type="checkbox" data-f="checked" ${m.checked ? 'checked' : ''} aria-label="Picked by default"></td>
    ${PRICE_KEYS.map(([k, label]) => `<td>${input(`data-price="${k}" type="number" min="0" step="any" value="${num(m.prices?.[k])}" placeholder="—" aria-label="${label} price"`, 'w-20 tabular-nums')}</td>`).join('')}
    <td class="whitespace-nowrap">
      <button type="button" data-check class="btn btn-outline px-2 py-1 text-xs" title="Ask Anthropic whether this id exists (no tokens used)">Check</button>
      <button type="button" data-remove class="btn btn-outline px-2 py-1 text-xs" aria-label="Remove ${esc(m.name)}">Remove</button>
    </td>
  </tr>`;
}

function render() {
  const {settings: s, info, saved} = data;
  const ids = s.models.map((m) => m.id);
  const labelerChoices = [...new Set([...ids, s.labeler_model])];
  $('#settingsBody').innerHTML = `
  <section class="panel p-4">
    <h2 class="text-base font-semibold">Models</h2>
    <p class="hint">The models offered on the Run tab. Prices are USD per million tokens and turn token use into cost; leave
      them blank and cost isn’t tracked for that model. “Default” models start picked.</p>
    <div class="mt-3 overflow-x-auto">
      <table class="data-table w-full text-sm">
        <tr><th>Name</th><th>Model id</th><th class="!text-center">Default</th>
          ${PRICE_KEYS.map(([, label]) => `<th>${label} $/M</th>`).join('')}<th></th></tr>
        ${s.models.map(modelRow).join('')}
      </table>
    </div>
    <button type="button" id="addModel" class="btn btn-outline mt-3 px-3 py-1.5 text-sm">Add model</button>
  </section>

  <section class="panel mt-4 p-4">
    <h2 class="text-base font-semibold">Running</h2>
    <div class="mt-3 flex flex-wrap gap-x-8 gap-y-4">
      <label class="flex flex-col"><span class="group-label">Concurrency</span>
        ${input(`id="concurrency" type="number" min="1" max="20" step="1" value="${s.concurrency}"`, 'w-24 tabular-nums')}
        <span class="hint mt-1">Agents running at once across a run (1–20). Higher is faster, until rate limits hit.</span></label>
      <label class="flex flex-col"><span class="group-label">Failure labeler</span>
        <select id="labeler" class="input mt-0 w-auto self-start py-1.5 text-sm">
          ${labelerChoices.map((id) => `<option value="${esc(id)}" ${id === s.labeler_model ? 'selected' : ''}>${esc(s.models.find((m) => m.id === id)?.name ?? id)}</option>`).join('')}
        </select>
        <span class="hint mt-1">The model that labels why wrong answers failed (<code>python -m app.labels</code>).</span></label>
    </div>
  </section>

  <div class="mt-4 flex flex-wrap items-center gap-3">
    <button type="button" id="saveSettings" class="btn btn-primary px-5 py-2">Save</button>
    <button type="button" id="resetSettings" class="btn btn-outline px-4 py-2" ${saved ? '' : 'disabled'}
      title="Delete ${esc(info.path)} and use the defaults">Reset to defaults</button>
    <span id="settingsStatus" class="text-sm" role="status"></span>
  </div>
  <p class="hint mt-2">Saved on this machine in <code>${esc(info.path)}</code> (not in git). The next run uses them; a run already going keeps its settings.</p>

  <section class="panel mt-6 p-4">
    <h2 class="text-base font-semibold">Read-only</h2>
    <p class="hint">Set outside the app, so they can’t be changed by accident.</p>
    <table class="data-table mt-3 w-full text-sm">
      <tr><td class="w-56">Anthropic API key</td><td>${info.api_key_set ? '<span class="badge badge-pass">Set</span>' : '<span class="badge badge-fail">Not set</span>'}
        <span class="ml-2 text-ink-muted">From <code>ANTHROPIC_API_KEY</code>; set it and restart the server to change it. The value is never shown.</span></td></tr>
      <tr><td>MLflow</td><td>${info.mlflow ? `<code class="break-all">${esc(info.mlflow)}</code>` : 'Off (<code>EVAL_MLFLOW=off</code>)'}
        <span class="ml-2 text-ink-muted">From <code>MLFLOW_TRACKING_URI</code>.</span></td></tr>
      <tr><td>Per-prompt limits</td><td class="tabular-nums">${info.limits.tool_call_limit} tool calls · ${info.limits.token_limit.toLocaleString()} tokens · ${info.limits.case_timeout_s} s
        <span class="ml-2 text-ink-muted">In <code>app/evals.py</code>: changing them changes what a pass means, so it’s a code change with a new benchmark version.</span></td></tr>
      <tr><td>Benchmark version</td><td class="tabular-nums">v${info.benchmark_version}</td></tr>
    </table>
  </section>`;
  wire();
}

function readForm() {
  const s = data.settings;
  s.models = $$('#settingsBody [data-row]').map((row) => {
    const get = (f) => row.querySelector(`[data-f="${f}"]`);
    const prices = Object.fromEntries(PRICE_KEYS.map(([k]) => [k, row.querySelector(`[data-price="${k}"]`).value]));
    const priced = Object.values(prices).some((v) => v !== '');
    return {
      name: get('name').value.trim(), id: get('id').value.trim(), checked: get('checked').checked,
      prices: priced ? Object.fromEntries(Object.entries(prices).map(([k, v]) => [k, v === '' ? 0 : +v])) : null,
    };
  });
  s.concurrency = Math.round(+$('#concurrency').value);
  s.labeler_model = $('#labeler').value;
}

function status(msg, kind = 'muted') {
  const el = $('#settingsStatus');
  el.className = `text-sm ${{ok: 'text-success', error: 'text-danger', muted: 'text-ink-muted'}[kind]}`;
  el.textContent = msg;
}

// FastAPI validation errors arrive as a list; say where each one is in words.
function problems(detail) {
  if (!Array.isArray(detail)) return String(detail);
  return detail.map((d) => {
    const [, field, i, sub] = d.loc ?? [];
    const where = field === 'models' && i != null ? `Model ${+i + 1}${sub ? ` (${sub})` : ''}` : field ?? 'Settings';
    return `${where}: ${d.msg.replace(/^Value error, /, '')}`;
  }).join(' · ');
}

async function send(method) {
  const res = await fetch('/api/settings', {method, headers: {'Content-Type': 'application/json'},
    body: method === 'PUT' ? JSON.stringify(data.settings) : undefined});
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(problems(body.detail));
  data = body;
  app.config.models = data.settings.models.map(({id, name, checked}) => ({id, name, checked}));
  renderModelChoices();
}

function wire() {
  const el = $('#settingsBody');
  $('#addModel').onclick = () => {
    readForm();
    data.settings.models.push({id: '', name: '', checked: false, prices: null});
    render();
    $$('#settingsBody [data-row]').at(-1).querySelector('[data-f="name"]').focus();
  };
  $$('[data-remove]', el).forEach((b) => b.onclick = () => {
    readForm();
    data.settings.models.splice(+b.closest('[data-row]').dataset.row, 1);
    render();
    status('Removed. Save to keep the change.');
  });
  $$('[data-check]', el).forEach((b) => b.onclick = async () => {
    const row = b.closest('[data-row]');
    const out = row.querySelector('[data-check-result]');
    const id = row.querySelector('[data-f="id"]').value.trim();
    out.className = 'mt-1 text-xs text-ink-muted';
    out.textContent = 'Checking…';
    try {
      const res = await fetch('/api/settings/check-model', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({id})});
      const r = res.ok ? await res.json() : {ok: false, error: 'That isn’t a valid model id.'};
      out.className = `mt-1 text-xs ${r.ok ? 'text-success' : 'text-danger'}`;
      out.textContent = r.ok ? `✓ ${r.display_name}` : r.error;
    } catch (e) {
      out.className = 'mt-1 text-xs text-danger';
      out.textContent = e.message;
    }
  });
  $('#saveSettings').onclick = async () => {
    readForm();
    try {
      await send('PUT');
      render();
      status('Saved. The next run uses these settings.', 'ok');
    } catch (e) {
      status(e.message, 'error');
    }
  };
  $('#resetSettings').onclick = async () => {
    try {
      await send('DELETE');
      render();
      status('Back to the defaults.', 'ok');
    } catch (e) {
      status(e.message, 'error');
    }
  };
}
