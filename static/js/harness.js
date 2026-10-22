// Harness tab (read only): for one target, what its agent is given with the default harness or with none. The server
// builds the real agent and captures its first model request, so the prompt and tool schemas are exactly what runs.

import {$, $$, esc, fmtInt, stat, loading, errorBox} from './ui.js';
import {app} from './config.js';
import {getJSON} from './api.js';
import {go} from './router.js';

const TARGETS = [['raw', 'Raw SQL'], ['metricflow', 'dbt MetricFlow'], ['malloy', 'Malloy']];
const MODES = [['on', 'With harness'], ['off', 'Without harness']];
const cache = new Map();

// Capability labels read "Name: what it does".
const capName = (key) => (app.config?.harness_capabilities?.[key]?.label ?? key).split(':')[0];
const capWhat = (label) => label.slice(label.indexOf(':') + 1).trim();
const sourceBadge = (key) => (key
  ? `<span class="badge bg-accent-soft text-accent" title="Added by the harness">${esc(capName(key))}</span>`
  : '<span class="badge badge-neutral" title="Part of the target itself, with or without the harness">Target</span>');
const approxTokens = (chars) => `≈ ${fmtInt(chars / 4)}`;

const pill = (attr, value, label, current) => `<button type="button" ${attr}="${value}" aria-pressed="${value === current}"
  class="rounded-control border px-3 py-1.5 text-sm ${value === current ? 'border-accent bg-accent text-accent-fg' : 'border-border bg-surface text-ink-muted hover:bg-surface-2 hover:text-ink'}">${label}</button>`;

export async function showHarness(params) {
  const target = TARGETS.some(([t]) => t === params.target) ? params.target : 'metricflow';
  const mode = params.mode === 'off' ? 'off' : 'on';
  $('#harnessControls').innerHTML = `
    <div class="flex flex-wrap gap-1" role="group" aria-label="Target">${TARGETS.map(([t, l]) => pill('data-target', t, l, target)).join('')}</div>
    <div class="flex flex-wrap gap-1" role="group" aria-label="Harness">${MODES.map(([m, l]) => pill('data-mode', m, l, mode)).join('')}</div>`;
  $$('[data-target]').forEach((b) => b.onclick = () => go(`#/harness?target=${b.dataset.target}&mode=${mode}`));
  $$('[data-mode]').forEach((b) => b.onclick = () => go(`#/harness?target=${target}&mode=${b.dataset.mode}`));

  const body = $('#harnessBody');
  const key = `${target}|${mode}`;
  if (!cache.has(key)) body.innerHTML = loading('Building the agent');
  try {
    if (!cache.has(key)) cache.set(key, await getJSON(`/api/harness/${target}`, {mode}));
  } catch (err) {
    body.innerHTML = errorBox(err.message);
    return;
  }
  body.innerHTML = render(cache.get(key));
}

function render(d) {
  const chars = d.instructions.reduce((n, p) => n + p.text.length, 0);
  const schemaChars = d.tools.reduce((n, t) => n + JSON.stringify(t.schema).length + t.description.length, 0);
  const on = d.capabilities.filter((c) => c.enabled).length;
  return `
    <div class="mb-4 grid grid-cols-2 gap-2 md:grid-cols-5">
      ${stat('Components on', `${on} of ${d.capabilities.length}`)}
      ${stat('Instructions', `${approxTokens(chars)} tokens`)}
      ${stat('Tool definitions', `${approxTokens(schemaChars)} tokens`)}
      ${stat('Tools the model sees', d.tools.length)}
      ${stat('Checks on the answer', d.answer_checks.length)}
    </div>
    <p class="hint -mt-2 mb-4">Token figures are estimates (characters ÷ 4). This fixed prompt is re-sent on every model turn.</p>
    <div class="grid gap-5 lg:grid-cols-[minmax(0,360px)_minmax(0,1fr)]">
      <aside class="min-w-0 space-y-4">${components(d)}</aside>
      <div class="min-w-0 space-y-5">
        ${section('What the model reads', 'The instructions, in the order they are sent. Each part says whether it belongs to the target or was added by a harness component.', instructions(d))}
        ${section('Tools the model can call', 'Names, descriptions and argument schemas exactly as the model receives them. The answer tool is how the model ends a case.', tools(d))}
        ${section('While the agent works', 'What happens to each query before and after it runs.', working(d))}
        ${section('Checks on the final answer', d.answer_checks.length ? 'Run in this order each time the agent answers. A failed check sends the answer back with a message; every check has a limit, so a case always ends with an answer.' : '', checks(d))}
      </div>
    </div>`;
}

const section = (title, hint, html) => `
  <section class="panel p-4">
    <h2 class="panel-title">${title}</h2>
    ${hint ? `<p class="hint mb-3 max-w-[80ch]">${hint}</p>` : ''}
    ${html}
  </section>`;

function components(d) {
  const group = (title, caps) => !caps.length ? '' : `
    <section class="panel p-4">
      <h2 class="panel-title mb-2">${title}</h2>
      <ul class="space-y-3">${caps.map((c) => `
        <li class="${c.enabled ? '' : 'opacity-60'}">
          <div class="flex items-start justify-between gap-2">
            <span class="text-sm font-medium">${esc(capName(c.key))}</span>
            ${c.enabled ? '<span class="badge badge-pass">On</span>' : '<span class="badge badge-neutral">Off</span>'}
          </div>
          <p class="hint mt-0.5">${esc(capWhat(c.label))}</p>
          ${c.default ? '' : '<p class="mt-0.5 text-xs text-ink-subtle">Optional: not in the default harness</p>'}
        </li>`).join('')}
      </ul>
    </section>`;
  const target = d.capabilities.filter((c) => c.target !== 'all');
  return `
    <section class="panel p-4">
      <h2 class="panel-title">${esc(d.target.name)}</h2>
      <p class="hint mt-1">${esc(d.target.summary)}</p>
      <dl class="mt-3 grid grid-cols-[auto_minmax(0,1fr)] gap-x-3 gap-y-1 text-sm">
        <dt class="text-ink-muted">Writes</dt><dd>${esc(d.target.writes)}</dd>
        <dt class="text-ink-muted">Tools</dt><dd class="font-mono text-xs leading-5">${esc(d.target.tools)}</dd>
      </dl>
      ${d.harness_on ? '' : '<p class="notice notice-info mt-3">Showing the agent with no harness: the baseline the harness is measured against.</p>'}
    </section>
    ${group('Shared harness components', d.capabilities.filter((c) => c.target === 'all'))}
    ${group(`${esc(d.target.name)} components`, target)}`;
}

function instructions(d) {
  return `<div class="space-y-2">${d.instructions.map((p, i) => `
    <details class="rounded-control border border-border" ${p.text.length < 4000 || i === 0 ? 'open' : ''}>
      <summary class="flex cursor-pointer flex-wrap items-center gap-2 px-3 py-2 text-sm">
        <span class="font-medium">${esc(p.title)}</span>${sourceBadge(p.source)}
        <span class="ml-auto text-xs tabular-nums text-ink-muted">${approxTokens(p.text.length)} tokens</span>
      </summary>
      <pre class="code mx-3 mb-3 max-h-[50vh] overflow-auto">${esc(p.text.trim())}</pre>
    </details>`).join('')}</div>`;
}

function tools(d) {
  return `<div class="space-y-2">${d.tools.map((t) => `
    <details class="rounded-control border border-border">
      <summary class="flex cursor-pointer flex-wrap items-center gap-2 px-3 py-2 text-sm">
        <span class="font-mono font-medium">${esc(t.name)}</span>
        ${t.kind === 'answer' ? `<span class="badge badge-neutral">Answer tool</span>${t.name === 'answer' ? sourceBadge('answer_by_ref') : ''}` : ''}
        <span class="ml-auto text-xs text-ink-muted">${Object.keys(t.schema.properties ?? {}).map(esc).join(', ') || 'no arguments'}</span>
      </summary>
      <div class="px-3 pb-3">
        ${t.description ? `<p class="mb-2 whitespace-pre-wrap text-sm text-ink-muted">${esc(t.description)}</p>` : ''}
        <pre class="code max-h-[50vh] overflow-auto">${esc(JSON.stringify(t.schema, null, 2))}</pre>
      </div>
    </details>`).join('')}</div>`;
}

function working(d) {
  const s = d.settings;
  const rows = [
    ['Before a query runs', d.tool_guard ?? 'Nothing: every call runs as sent.', d.tool_guard ? 'sql_guardrail' : null],
    ['Rows returned', `Up to ${s.max_rows} rows per query${s.sql_in_results ? ', plus the SQL it compiled to' : ''}.`, s.dedupe_repeats ? 'lean_results' : null],
    ['Repeated queries', s.dedupe_repeats ? 'Point at the earlier run instead of re-sending its rows.' : 'Run again and return their rows again.', s.dedupe_repeats ? 'lean_results' : null],
    ['Prompt caching', s.prompt_cache ? 'Instructions, tool definitions and the conversation are read from cache each turn (0.1× input price). They still count as input tokens.' : 'Off: everything is billed at full price every turn.', s.prompt_cache ? 'prompt_cache' : null],
    ['Retry budget', `${s.retries} retries per case, shared by tool errors and answer checks.`, null],
  ];
  return `<dl class="divide-y divide-border text-sm">${rows.map(([k, v, src]) => `
    <div class="grid gap-1 py-2 md:grid-cols-[180px_minmax(0,1fr)_auto] md:gap-3">
      <dt class="font-medium">${k}</dt><dd class="text-ink-muted">${esc(v)}</dd><dd>${src ? sourceBadge(src) : ''}</dd>
    </div>`).join('')}</dl>`;
}

function checks(d) {
  if (!d.answer_checks.length) return '<p class="text-sm text-ink-muted">None. The first answer the model gives is final.</p>';
  return `<ol class="space-y-3">${d.answer_checks.map((c, i) => `
    <li class="grid grid-cols-[1.75rem_minmax(0,1fr)] gap-2">
      <span class="mt-0.5 grid h-6 w-6 place-items-center rounded-full bg-surface-2 text-xs font-semibold tabular-nums">${i + 1}</span>
      <div>
        <div class="flex flex-wrap items-center gap-2 text-sm">
          <span class="font-medium">${esc(c.title)}</span>${sourceBadge(c.source)}
          <span class="text-xs text-ink-muted">Limit: ${esc(c.limit)}</span>
        </div>
        <p class="mt-0.5 text-sm text-ink-muted">${esc(c.text)}</p>
        ${c.instructions ? `<details class="mt-2"><summary class="cursor-pointer text-sm text-accent">Reviewer instructions</summary>
          <pre class="code mt-2 whitespace-pre-wrap">${esc(c.instructions)}</pre></details>` : ''}
      </div>
    </li>`).join('')}</ol>`;
}
