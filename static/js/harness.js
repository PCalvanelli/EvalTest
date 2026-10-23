// Harness tab (read only): one tab per target, comparing how its agent answers a question in the baseline (the model
// with only the target's query tools) and with the harness, step by step. The server builds the real agent and captures its first model request, so the instructions
// and tool schemas are exactly what runs.

import {$, $$, esc, fmtInt, loading, errorBox} from './ui.js';
import {getJSON} from './api.js';
import {go} from './router.js';
import {contextIds, contextName} from './config.js';
import {targetIcon, withIcon} from './icons.js';

const cache = new Map();
const fetchAgent = (target, mode) => {
  const key = `${target}|${mode}`;
  if (!cache.has(key)) cache.set(key, getJSON(`/api/harness/${target}`, {mode}).catch((err) => { cache.delete(key); throw err; }));
  return cache.get(key);
};

export async function showHarness(params) {
  const target = contextIds().includes(params.target) ? params.target : contextIds()[0];
  $('#harnessControls').innerHTML = `
    <div class="flex gap-5 border-b border-border" role="tablist" aria-label="Target">
      ${contextIds().map((t) => `<button type="button" role="tab" data-target="${t}" aria-selected="${t === target}"
        class="-mb-px border-b-2 px-0.5 pb-2 text-sm ${t === target ? 'border-ink font-medium text-ink' : 'border-transparent text-ink-muted hover:text-ink'}">${withIcon(targetIcon(t), esc(contextName(t)))}</button>`).join('')}
    </div>`;
  $$('[data-target]').forEach((b) => b.onclick = () => go(`#/harness?target=${b.dataset.target}`));

  const body = $('#harnessBody');
  body.innerHTML = loading('Building the agents');
  let off, on;
  try {
    [off, on] = await Promise.all([fetchAgent(target, 'off'), fetchAgent(target, 'on')]);
  } catch (err) {
    body.innerHTML = errorBox(err.message);
    return;
  }
  if (params.target && !location.hash.includes(`target=${target}`)) return;  // switched tabs while this one loaded
  body.innerHTML = render(off, on);
}

function render(off, on) {
  const steps = [
    ['What the agent is told', 'Instructions sent with every model call.', told(off, on, false), told(on, off, true)],
    ['What the agent can use', 'The tools it calls to query data and to answer.', tools(off, on, false), tools(on, off, true)],
    ['When it runs a query', 'What happens before and after each query.', querying(off), querying(on, off)],
    ['When it gives an answer', 'Whether the answer is checked before it counts.', answering(off), answering(on)],
    ['Retries and caching', 'How much room it has to recover, and how input is billed.', limits(off), limits(on, off)],
  ];
  return `
    <p class="mb-4 max-w-[80ch] text-sm text-ink-muted">${esc(on.target.summary)}</p>
    <div class="panel max-w-[1200px] overflow-hidden">
      <div class="hidden border-b border-border bg-surface-2 text-xs font-medium text-ink-muted md:grid md:grid-cols-[200px_minmax(0,1fr)_minmax(0,1fr)]">
        <div class="px-4 py-2">Step</div><div class="px-4 py-2">Baseline</div><div class="px-4 py-2">With harness</div>
      </div>
      ${steps.map(([title, hint, without, withH]) => `
        <div class="grid border-b border-border last:border-b-0 md:grid-cols-[200px_minmax(0,1fr)_minmax(0,1fr)]">
          <div class="px-4 pb-1 pt-3 md:py-3"><div class="text-sm font-medium">${title}</div><div class="hint">${hint}</div></div>
          <div class="px-4 py-2 md:py-3"><div class="mb-1 text-xs font-medium text-ink-muted md:hidden">Baseline</div>${without}</div>
          <div class="px-4 pb-3 pt-2 md:py-3"><div class="mb-1 text-xs font-medium text-ink-muted md:hidden">With harness</div>${withH}</div>
        </div>`).join('')}
    </div>
    <p class="hint mt-2">Token counts are estimates (characters ÷ 4). <span class="font-medium text-ink">New</span> marks what the harness adds; <span class="line-through">struck</span> items are replaced by it.</p>`;
}

const tokens = (chars) => `≈ ${fmtInt(chars / 4)} tokens`;
const isNew = '<span class="badge bg-accent-soft text-accent">New</span>';

// One expandable line: name, an optional tag, a note on the right, and the full text inside.
const item = (name, note, inner, {tag = '', struck = false} = {}) => `
  <details class="rounded-control border border-border">
    <summary class="flex cursor-pointer items-center gap-2 px-2.5 py-1.5 text-sm">
      <span class="${struck ? 'text-ink-muted line-through' : ''}">${name}</span>${tag}
      <span class="ml-auto shrink-0 text-xs tabular-nums text-ink-muted">${note}</span>
    </summary>
    <div class="px-2.5 pb-2.5">${inner}</div>
  </details>`;
const stack = (items) => `<div class="space-y-1.5">${items.join('')}</div>`;
// A plain sentence; `changed` marks it as different from the baseline.
const line = (text, changed = false) => `<p class="text-sm ${changed ? 'text-ink' : 'text-ink-muted'}">${changed ? '<span class="mr-1.5 inline-block h-1.5 w-1.5 rounded-full bg-accent align-middle" aria-hidden="true"></span>' : ''}${text}</p>`;
const lines = (rows) => `<div class="space-y-1">${rows.join('')}</div>`;

// `d` is the side being drawn, `other` the opposite side; `withSide` says which column this is.
function told(d, other, withSide) {
  const has = (p) => other.instructions.some((q) => q.title === p.title);
  return stack(d.instructions.map((p) => item(esc(p.title), tokens(p.text.length),
    `<pre class="code max-h-[50vh] overflow-auto">${esc(p.text.trim())}</pre>`,
    {tag: withSide && !has(p) ? isNew : '', struck: !withSide && !has(p)})));
}

function tools(d, other, withSide) {
  const has = (t) => other.tools.some((u) => u.name === t.name);
  return stack(d.tools.map((t) => item(`<span class="font-mono">${esc(t.name)}</span>`,
    t.kind === 'answer' ? 'gives the answer' : '', `
    ${t.description ? `<p class="mb-2 whitespace-pre-wrap text-sm text-ink-muted">${esc(t.description)}</p>` : ''}
    <pre class="code max-h-[50vh] overflow-auto">${esc(JSON.stringify(t.schema, null, 2))}</pre>`,
    {tag: withSide && !has(t) ? isNew : '', struck: !withSide && !has(t)})));
}

// `base` is passed for the with-harness side, so rows that differ from the baseline get a marker.
function querying(d, base) {
  const s = d.settings, b = base?.settings;
  return lines([
    line(d.tool_guard ? `${esc(d.tool_guard)}.` : 'Runs exactly as written.', !!base && d.tool_guard !== base.tool_guard),
    line(`Returns up to ${s.max_rows} rows${s.sql_in_results ? ', with the SQL it compiled to' : ''}.`, !!b && (s.max_rows !== b.max_rows || s.sql_in_results !== b.sql_in_results)),
    line(s.dedupe_repeats ? 'A repeated query points back to the earlier result.' : 'A repeated query runs again.', !!b && s.dedupe_repeats !== b.dedupe_repeats),
  ]);
}

function answering(d) {
  if (!d.answer_checks.length) return line('The first answer is final. Nothing checks it.');
  return `${line(`Checked ${d.answer_checks.length} ways, in order. A failed check sends the answer back to fix.`, true)}
    <div class="mt-2">${stack(d.answer_checks.map((c, i) => item(`${i + 1}. ${esc(c.title)}`, `up to ${esc(c.limit)}`, `
      <p class="text-sm text-ink-muted">${esc(c.text)}</p>
      ${c.instructions ? `<p class="mt-2 text-xs font-medium text-ink-muted">Reviewer instructions</p>
        <pre class="code mt-1 whitespace-pre-wrap">${esc(c.instructions)}</pre>` : ''}`)))}</div>`;
}

function limits(d, base) {
  const s = d.settings, b = base?.settings;
  return lines([
    line(`Up to ${s.retries} retries per prompt.`, !!b && s.retries !== b.retries),
    line(s.prompt_cache ? 'Prompt caching on: repeated input costs 0.1×.' : 'No prompt caching: all input at full price.', !!b && s.prompt_cache !== b.prompt_cache),
  ]);
}
