// Traces: the trace detail (span waterfall, span inspector, eval checks, your label), the Traces tab, and
// the drawer that shows the same detail over the Run tab.

import {$, $$, esc, fmtMs, fmtInt, fmtDate, passBadge, checkLine, empty, loading, errorBox} from './ui.js';
import {modelName, comboLabel} from './config.js';
import {getJSON, postJSON} from './api.js';
import {renderQuery} from './results.js';

const HUMAN = 'human_correct';
const CHECK_ORDER = ['ExecutesOK', 'ResultMatches', 'LLMJudge', 'passed'];

export function cellLabel(cellId) {
  if (!cellId) return 'Unknown cell';
  const [model, harness, context] = cellId.split('|');
  return `${modelName(model)} · ${comboLabel({harness: harness === 'harness', context})}`;
}

const kindBadge = (kind) => (kind === 'judge' ? '<span class="badge badge-neutral">Judge</span>' : '');
function outcomeBadge(t) {
  const passed = t.assessments?.passed?.value;
  return passed == null ? (t.kind === 'judge' ? kindBadge('judge') : '<span class="badge badge-neutral">No result</span>') : passBadge(passed);
}
function humanBadge(t) {
  const h = t.assessments?.[HUMAN];
  if (!h) return '';
  return `<span class="badge ${h.value ? 'badge-pass' : 'badge-fail'}" title="Your label">You: ${h.value ? 'correct' : 'wrong'}</span>`;
}

// ─────────────────────────── Trace detail (shared) ───────────────────────────

/** Render a trace into `root`. `onLabeled(assessments)` runs after you label it. */
export async function renderTraceDetail(root, traceId, {onLabeled} = {}) {
  root.innerHTML = loading('Loading trace');
  let t;
  try {
    t = await getJSON(`/api/traces/${encodeURIComponent(traceId)}`);
  } catch (err) {
    root.innerHTML = errorBox(`Couldn't load trace ${traceId}: ${err.message}`);
    return;
  }
  const tree = spanTree(t.spans);
  const total = Math.max(...t.spans.map((s) => s.start_ms + s.duration_ms), 1);
  root.innerHTML = `
  <div class="space-y-4">
    <section class="panel p-4">
      <div class="flex flex-wrap items-start justify-between gap-2">
        <div class="min-w-0">
          <div class="mb-1 flex flex-wrap items-center gap-2">${outcomeBadge(t)}${t.kind === 'agent' ? kindBadge(t.kind) : ''}${humanBadge(t)}</div>
          <h2 class="text-lg font-semibold tracking-tight">${esc(t.prompt || (t.kind === 'judge' ? `Judge call for ${t.case ?? 'a case'}` : 'Trace'))}</h2>
          <p class="hint">${esc(cellLabel(t.cell))}${t.case ? ` · ${esc(t.case)}` : ''}${t.run ? ` · run ${t.run}` : ''} · ${esc(fmtDate(t.request_time))}</p>
        </div>
        <dl class="grid grid-cols-3 gap-x-5 text-right text-xs">
          <div><dt class="text-ink-muted">Duration</dt><dd class="text-sm font-semibold tabular-nums">${fmtMs(t.duration_ms)}</dd></div>
          <div><dt class="text-ink-muted">Tokens</dt><dd class="text-sm font-semibold tabular-nums">${fmtInt(t.tokens)}</dd></div>
          <div><dt class="text-ink-muted">Spans</dt><dd class="text-sm font-semibold tabular-nums">${t.spans.length}</dd></div>
        </dl>
      </div>
      <p class="mt-2 break-all font-mono text-[11px] text-ink-subtle">${esc(t.trace_id)}</p>
    </section>

    ${t.kind === 'agent' ? `<section class="grid gap-4 xl:grid-cols-2">
      <div class="panel p-4" data-slot="checks"></div>
      <div class="panel p-4" data-slot="label"></div>
    </section>` : ''}

    ${t.output ? `<section class="panel p-4">
      <h3 class="panel-title">Answer</h3>
      ${t.output.error ? `<p class="mt-2 break-words rounded-control bg-danger-soft p-2 font-mono text-xs">${esc(t.output.error)}</p>` : ''}
      ${renderQuery(t.output)}
    </section>` : ''}

    <section class="panel p-4">
      <div class="mb-2 flex items-baseline justify-between">
        <h3 class="panel-title">Spans</h3>
        <p class="hint">Select a span to inspect its inputs and outputs.</p>
      </div>
      <div class="overflow-x-auto"><div class="min-w-[560px]" role="tree" data-slot="waterfall"></div></div>
      <div class="mt-3" data-slot="span"></div>
    </section>
  </div>`;

  const slot = (name) => $(`[data-slot="${name}"]`, root);
  if (t.kind === 'agent') {
    renderChecks(slot('checks'), t.assessments);
    renderLabelForm(slot('label'), t, onLabeled, root);
  }
  renderWaterfall(slot('waterfall'), slot('span'), tree, total);
}

function renderChecks(el, assessments) {
  const names = Object.keys(assessments).filter((n) => n !== HUMAN)
    .sort((a, b) => (CHECK_ORDER.indexOf(a) + 99) % 99 - (CHECK_ORDER.indexOf(b) + 99) % 99);
  el.innerHTML = `<h3 class="panel-title mb-2">Eval checks</h3>` + (names.length
    ? `<ul class="space-y-1.5 text-sm">${names.map((n) => checkLine(n === 'passed' ? 'All checks (case result)' : n, assessments[n].value === true, assessments[n].rationale)).join('')}</ul>`
    : '<p class="hint">No checks were recorded on this trace.</p>');
}

function renderLabelForm(el, t, onLabeled, root) {
  const h = t.assessments[HUMAN];
  el.innerHTML = `
    <h3 class="panel-title">Your label</h3>
    <p class="hint mb-2">Is the agent's answer actually correct? Labels show how far to trust each automated check (see Labels).</p>
    ${h ? `<p class="mb-2 text-sm">You marked this <span class="font-semibold ${h.value ? 'text-success' : 'text-danger'}">${h.value ? 'correct' : 'wrong'}</span>${h.rationale ? `: ${esc(h.rationale)}` : ''}.</p>` : ''}
    <label class="field-label">Note <span class="font-normal text-ink-subtle">(optional)</span>
      <input data-note class="input" placeholder="What's right or wrong about it" value="${esc(h?.rationale ?? '')}">
    </label>
    <div class="mt-3 flex gap-2">
      <button type="button" data-label="true" class="btn btn-outline">Mark correct</button>
      <button type="button" data-label="false" class="btn btn-outline">Mark wrong</button>
    </div>
    <p data-label-status class="hint mt-2" role="status"></p>`;
  $$('[data-label]', el).forEach((b) => b.onclick = async () => {
    const status = $('[data-label-status]', el);
    $$('[data-label]', el).forEach((x) => (x.disabled = true));
    try {
      const res = await postJSON(`/api/traces/${encodeURIComponent(t.trace_id)}/label`, {correct: b.dataset.label === 'true', note: $('[data-note]', el).value});
      t.assessments = await res.json();
      renderLabelForm(el, t, onLabeled, root);
      $('[data-label-status]', el).textContent = 'Saved.';
      onLabeled?.(t.assessments, t.trace_id);
    } catch (err) {
      status.textContent = `Couldn't save the label: ${err.message}`;
      $$('[data-label]', el).forEach((x) => (x.disabled = false));
    }
  });
}

// Depth-first order with depth, so the waterfall reads top to bottom like a call stack.
function spanTree(spans) {
  const kids = {};
  for (const s of spans) (kids[s.parent_id ?? 'root'] ??= []).push(s);
  const out = [];
  const walk = (parent, depth) => {
    for (const s of (kids[parent] ?? []).sort((a, b) => a.start_ms - b.start_ms)) {
      out.push({...s, depth, hasKids: Boolean(kids[s.id]?.length)});
      walk(s.id, depth + 1);
    }
  };
  walk('root', 0);
  return out;
}

const SPAN_BAR = {LLM: 'bg-accent', TOOL: 'bg-ink-muted', AGENT: 'bg-border-strong', EVALUATOR: 'bg-border-strong'};

function renderWaterfall(el, inspector, rows, total) {
  const collapsed = new Set();
  let selected = rows.find((r) => r.type === 'LLM')?.id ?? rows[0]?.id;
  const hiddenBy = (r) => {  // a row is hidden when any ancestor is collapsed
    let p = r.parent_id;
    while (p) {
      if (collapsed.has(p)) return true;
      p = rows.find((x) => x.id === p)?.parent_id;
    }
    return false;
  };
  const draw = () => {
    el.innerHTML = rows.filter((r) => !hiddenBy(r)).map((r) => {
      const left = (r.start_ms / total) * 100, width = Math.max((r.duration_ms / total) * 100, 0.4);
      const err = r.status === 'ERROR';
      return `<div role="treeitem" aria-selected="${r.id === selected}" aria-level="${r.depth + 1}" ${r.hasKids ? `aria-expanded="${!collapsed.has(r.id)}"` : ''}
        class="grid grid-cols-[minmax(0,260px)_minmax(0,1fr)_64px] items-center gap-2 rounded-control px-1 py-1 text-xs ${r.id === selected ? 'bg-accent-soft' : 'hover:bg-surface-2'}">
        <div class="flex min-w-0 items-center gap-1" style="padding-left:${r.depth * 14}px">
          ${r.hasKids ? `<button type="button" data-toggle="${esc(r.id)}" class="w-4 shrink-0 text-ink-muted" aria-label="${collapsed.has(r.id) ? 'Expand' : 'Collapse'}">${collapsed.has(r.id) ? '▸' : '▾'}</button>` : '<span class="w-4 shrink-0"></span>'}
          <button type="button" data-span="${esc(r.id)}" class="min-w-0 truncate text-left ${err ? 'text-danger' : ''}" title="${esc(r.name)}">${esc(r.name)}</button>
          <span class="shrink-0 text-[10px] text-ink-subtle">${esc((r.type || '').toLowerCase())}</span>
        </div>
        <div class="relative h-2.5 rounded-sm bg-surface-2">
          <div class="absolute inset-y-0 rounded-sm ${err ? 'bg-danger' : SPAN_BAR[r.type] ?? 'bg-border-strong'}" style="left:${left}%;width:${width}%"></div>
        </div>
        <div class="text-right tabular-nums text-ink-muted">${fmtMs(r.duration_ms)}</div>
      </div>`;
    }).join('');
    $$('[data-toggle]', el).forEach((b) => b.onclick = () => {
      collapsed.has(b.dataset.toggle) ? collapsed.delete(b.dataset.toggle) : collapsed.add(b.dataset.toggle);
      draw();
    });
    $$('[data-span]', el).forEach((b) => b.onclick = () => { selected = b.dataset.span; draw(); });
    inspect(inspector, rows.find((r) => r.id === selected));
  };
  draw();
}

function payload(label, p) {
  if (!p || p.text === 'null') return '';
  return `<div class="min-w-0"><p class="mb-1 text-xs font-medium text-ink-muted">${label}${p.truncated ? ' <span class="font-normal text-ink-subtle">(truncated)</span>' : ''}</p>
    <pre class="code max-h-[420px] overflow-auto whitespace-pre-wrap break-words">${esc(p.text)}</pre></div>`;
}

function inspect(el, s) {
  if (!s) { el.innerHTML = ''; return; }
  const usage = s.tokens ? ` · ${fmtInt(s.tokens.input_tokens)} in / ${fmtInt(s.tokens.output_tokens)} out tokens` : '';
  el.innerHTML = `
    <div class="rounded-control border border-border p-3">
      <p class="text-sm font-medium">${esc(s.name)} <span class="font-normal text-ink-muted">· ${esc((s.type || '').toLowerCase())} · ${fmtMs(s.duration_ms)}${s.model ? ` · ${esc(s.model)}` : ''}${usage}</span></p>
      ${s.error ? `<p class="mt-2 break-words rounded-control bg-danger-soft p-2 font-mono text-xs">${esc(s.error)}</p>` : ''}
      <div class="mt-2 grid gap-3 2xl:grid-cols-2">${payload('Inputs', s.inputs)}${payload('Outputs', s.outputs)}</div>
    </div>`;
}

// ─────────────────────────── Drawer (over the Run tab) ───────────────────────────

let lastFocus = null;
export function openTraceDrawer(traceId) {
  const drawer = $('#drawer');
  lastFocus = document.activeElement;
  drawer.classList.remove('hidden');
  drawer.setAttribute('aria-hidden', 'false');
  $('#drawerOpen').href = `#/traces?trace=${encodeURIComponent(traceId)}`;
  renderTraceDetail($('#drawerBody'), traceId);
  $('#drawer [data-close]:not(.absolute)')?.focus();
}
export function closeTraceDrawer() {
  const drawer = $('#drawer');
  if (drawer.classList.contains('hidden')) return;
  drawer.classList.add('hidden');
  drawer.setAttribute('aria-hidden', 'true');
  $('#drawerBody').innerHTML = '';
  lastFocus?.focus?.();
}
export function initDrawer() {
  $$('#drawer [data-close]').forEach((el) => el.onclick = closeTraceDrawer);
  $('#drawerOpen').addEventListener('click', closeTraceDrawer);
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeTraceDrawer(); });
}

// ─────────────────────────── Traces tab ───────────────────────────

let matrices = null;   // cached /api/matrices for the filter row
let rows = [];

const select = (name, label, options, value) => `
  <label class="field-label min-w-[140px]">${label}
    <select data-filter="${name}" class="input">${options.map(([v, l]) => `<option value="${esc(v)}" ${String(v) === String(value ?? '') ? 'selected' : ''}>${esc(l)}</option>`).join('')}</select>
  </label>`;

/** Show the Traces tab for hash params {matrix, run_id, case, kind, outcome, trace}. */
export async function showTraces(params) {
  const filters = $('#traceFilters'), list = $('#traceList'), detail = $('#traceDetail');
  if (!matrices) {
    filters.innerHTML = loading('Loading matrices');
    try { matrices = await getJSON('/api/matrices'); } catch (err) { filters.innerHTML = errorBox(err.message); list.innerHTML = ''; return; }
  }
  // Default scope: the newest matrix (a direct trace link stays unscoped so the trace is found).
  const p = {kind: 'agent', ...params};
  if (!('matrix' in params) && !params.trace && matrices.length) p.matrix = matrices[0].id;
  const m = matrices.find((x) => x.id === p.matrix);
  filters.innerHTML =
    select('matrix', 'Matrix', [['', 'All matrices'], ...matrices.map((x) => [x.id, `${x.name}`])], p.matrix) +
    select('run_id', 'Cell', [['', 'All cells'], ...(m?.cells ?? []).map((c) => [c.run_id, cellLabel(c.id)])], p.run_id) +
    select('kind', 'Kind', [['agent', 'Agent answers'], ['judge', 'Judge calls'], ['', 'Both']], p.kind) +
    select('outcome', 'Result', [['', 'Any'], ['pass', 'Passed'], ['fail', 'Failed']], p.outcome) +
    `<label class="field-label w-24">Case<input data-filter="case" class="input" placeholder="q3" value="${esc(p.case ?? '')}"></label>`;
  $$('[data-filter]', filters).forEach((el) => el.onchange = () => {
    const next = Object.fromEntries($$('[data-filter]', filters).map((f) => [f.dataset.filter, f.value]));
    if (el.dataset.filter === 'matrix') next.run_id = '';
    // An empty matrix or kind is a real choice ("All"), so keep those keys; drop other empty filters.
    location.hash = '#/traces?' + new URLSearchParams(Object.entries(next).filter(([k, v]) => v !== '' || k === 'matrix' || k === 'kind'));
  });

  if (!matrices.length) {
    list.innerHTML = '';
    detail.innerHTML = empty('No traces yet. <a class="link" href="#/run">Run a matrix</a> and its traces appear here.');
    return;
  }
  list.innerHTML = loading('Loading traces');
  try {
    rows = await getJSON('/api/traces', {matrix: p.matrix, run_id: p.run_id, case: p.case, kind: p.kind, outcome: p.outcome});
  } catch (err) {
    list.innerHTML = errorBox(err.message);
    return;
  }
  const current = p.trace ?? rows[0]?.trace_id;
  if (current) openInPane(current, p);
  else { drawList(list, null, p); detail.innerHTML = empty('No traces match these filters.'); }
}

function openInPane(traceId, p) {
  drawList($('#traceList'), traceId, p);
  renderTraceDetail($('#traceDetail'), traceId, {onLabeled: (a, id) => {
    const r = rows.find((x) => x.trace_id === id);
    if (r) { r.assessments = a; drawList($('#traceList'), id, p); }
  }});
}

function drawList(list, current, p) {
  if (!rows.length) { list.innerHTML = '<p class="p-4 text-sm text-ink-muted">No traces match these filters.</p>'; return; }
  list.innerHTML = `<p class="border-b border-border px-3 py-2 text-xs text-ink-muted">${rows.length} trace${rows.length === 1 ? '' : 's'}</p>` +
    rows.map((t) => `<button type="button" data-open="${esc(t.trace_id)}" aria-current="${t.trace_id === current}"
      class="block w-full border-b border-border px-3 py-2.5 text-left last:border-b-0 ${t.trace_id === current ? 'bg-accent-soft' : 'hover:bg-surface-2'}">
      <div class="flex items-center gap-2">${outcomeBadge(t)}${humanBadge(t)}<span class="ml-auto text-xs tabular-nums text-ink-muted">${t.case ?? ''}${t.run ? ` · run ${t.run}` : ''}</span></div>
      <p class="mt-1 line-clamp-2 text-sm">${esc(t.prompt || (t.kind === 'judge' ? `Judge call for ${t.case ?? 'a case'}` : t.trace_id))}</p>
      <p class="mt-0.5 text-xs text-ink-muted">${esc(cellLabel(t.cell))} · ${fmtMs(t.duration_ms)} · ${fmtInt(t.tokens)} tok</p>
    </button>`).join('');
  $$('[data-open]', list).forEach((b) => b.onclick = () => {
    // Keep the URL shareable without re-running the search: replaceState doesn't fire hashchange.
    const next = {...p, trace: b.dataset.open};
    history.replaceState(null, '', '#/traces?' + new URLSearchParams(Object.entries(next).filter(([k, v]) => v != null && (v !== '' || k === 'matrix' || k === 'kind'))));
    openInPane(b.dataset.open, next);
  });
}

export function invalidateTraceCache() { matrices = null; }
