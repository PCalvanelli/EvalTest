// Harness tab (read only): per target, what the harness changes from the baseline. The server builds the real agents
// and captures their first model request, so the instructions and tool schemas are exactly what runs.

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

// The harness as a diff: what both sides get, once, then one row per difference.
function render(off, on) {
  const key = (x) => JSON.stringify(x);
  const split = (list, other, name) => {
    const theirs = new Map(other.map((x) => [x[name], key(x)]));
    return {same: list.filter((x) => theirs.get(x[name]) === key(x)), only: list.filter((x) => theirs.get(x[name]) !== key(x))};
  };
  const ins = split(off.instructions, on.instructions, 'title'), insOn = split(on.instructions, off.instructions, 'title');
  const tls = split(off.tools, on.tools, 'name'), tlsOn = split(on.tools, off.tools, 'name');
  const s = off.settings, h = on.settings;
  const rows = [
    ['Instructions', ins.only.map(instruction), insOn.only.map(instruction)],
    ['Tools', tls.only.map(tool), tlsOn.only.map(tool)],
    ['Query check', [off.tool_guard], [on.tool_guard]],
    ['Rows per query', [s.max_rows + (s.sql_in_results ? ', with SQL' : '')], [h.max_rows + (h.sql_in_results ? ', with SQL' : '')]],
    ['Repeated query', [s.dedupe_repeats ? 'Points back' : 'Runs again'], [h.dedupe_repeats ? 'Points back' : 'Runs again']],
    ['Answer checks', off.answer_checks.map(check), on.answer_checks.map(check)],
    ['Retries', [s.retries], [h.retries]],
    ['Prompt cache', [s.prompt_cache ? 'On' : 'Off'], [h.prompt_cache ? 'On' : 'Off']],
  ].filter(([, a, b]) => key(a) !== key(b));
  const cell = (xs) => (xs.filter((x) => x != null && x !== '').length ? stack(xs.map((x) => (typeof x === 'string' && x.startsWith('<') ? x : `<p class="text-sm">${esc(String(x))}</p>`))) : '<span class="text-ink-subtle">—</span>');
  return `
    <div class="mb-4 flex flex-wrap items-center gap-1.5 text-sm"><span class="mr-1 text-ink-muted">Both get</span>
      ${[...ins.same.map(instruction), ...tls.same.map(tool)].join('')}</div>
    <div class="panel max-w-[1100px] overflow-x-auto">
      <table class="data-table w-full text-sm">
        <tr><th class="w-36"></th><th>Baseline</th><th>Harness</th></tr>
        ${rows.map(([name, a, b]) => `<tr><td class="align-top font-medium">${name}</td><td class="align-top">${cell(a)}</td><td class="align-top">${cell(b)}</td></tr>`).join('')}
      </table>
    </div>`;
}

// One expandable chip: a name, a note on the right, and the full text inside.
const item = (name, note, inner) => `<details class="rounded-control border border-border bg-surface open:basis-full">
    <summary class="flex cursor-pointer items-center gap-2 px-2 py-1 text-sm"><span>${name}</span>
      ${note ? `<span class="ml-auto shrink-0 text-xs tabular-nums text-ink-muted">${note}</span>` : ''}</summary>
    <div class="px-2 pb-2">${inner}</div>
  </details>`;
const stack = (items) => `<div class="flex flex-wrap items-start gap-1">${items.join('')}</div>`;

const instruction = (p) => item(esc(p.title), `<span title="characters ÷ 4">≈ ${fmtInt(p.text.length / 4)} tok</span>`,
  `<pre class="code max-h-[50vh] overflow-auto">${esc(p.text.trim())}</pre>`);

const tool = (t) => item(`<span class="font-mono">${esc(t.name)}</span>`, t.kind === 'answer' ? 'answers' : '', `
  ${t.description ? `<p class="mb-2 whitespace-pre-wrap text-sm text-ink-muted">${esc(t.description)}</p>` : ''}
  <pre class="code max-h-[50vh] overflow-auto">${esc(JSON.stringify(t.schema, null, 2))}</pre>`);

const check = (c, i) => item(`${i + 1}. ${esc(c.title)}`, `<span title="up to ${esc(c.limit)}">≤ ${esc(c.limit.split(' ')[0])}</span>`, `
  <p class="text-sm text-ink-muted">${esc(c.text)}</p>
  ${c.instructions ? `<pre class="code mt-2 whitespace-pre-wrap">${esc(c.instructions)}</pre>` : ''}`);
