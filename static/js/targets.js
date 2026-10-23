// Harness tab: the four targets compared. Where each keeps the business meaning (a fixed table), where it sits on
// expressiveness × determinism, and a task-type heatmap: expected strengths, with measured pass rates from History
// (current benchmark version, one model and harness mode) where runs exist.

import {$, $$, esc, pct, passTint, loading, errorBox} from './ui.js';
import {getJSON} from './api.js';
import {contextName, modelName} from './config.js';
import {targetIcon, withIcon} from './icons.js';

// Left to right: more modeling up front buys correctness, at the cost of coverage.
const ORDER = ['raw', 'glossary', 'malloy', 'metricflow'];

const loud = (s) => `<span class="text-success">${s}</span>`;
const silent = (s) => `<span class="text-danger">${s}</span>`;
const PROFILE = [
  ['Meaning lives in', 'The model’s priors', 'The prompt', 'A modeling language', 'Governed metrics'],
  ['SQL written by', 'The model', 'The model', 'Malloy’s compiler', 'MetricFlow'],
  ['When wrong', silent('Silently'), silent('Silently, as the glossary drifts'), loud('Mostly loudly: compile errors'), loud('Loudly: not modeled')],
  ['Setup', 'None', 'A glossary', 'A Malloy model', 'dbt metrics'],
  ['Changes by', '—', 'Editing text', 'Editing the model', 'A dbt PR'],
  ['Main risk', 'Meaning re-decided per query', 'Definitions drift from tables', 'Agent’s Malloy fluency', 'Unmodeled questions'],
];

// Task types: [label, measured key (a question tag, 'all', 'consistent' or null), expected strength per target in ORDER].
// Expected: 2 strong, 1 partial, 0 weak; a working hypothesis until runs measure it.
const TASKS = [
  ['All prompts', 'all', null],
  ['Governed KPI lookup', null, [0, 1, 2, 2]],
  ['Ambiguous business terms (if defined)', 'ambiguity', [0, 1, 2, 2]],
  ['Aggregating across one-to-many joins', 'fan-out-risk', [0, 1, 2, 2]],
  ['Nested or multi-step analysis', 'window', [1, 1, 2, 1]],
  ['Exploring unmodeled tables', null, [2, 2, 1, 0]],
  ['Same answer on repeated asks', 'consistent', [0, 1, 2, 2]],
  ['First answer on a new dataset', null, [2, 2, 1, 0]],
  ['Auditability', null, [1, 1, 2, 2]],
];
const GLYPH = ['○', '◐', '●'];
const MIN_N = 3;  // fewer measured answers than this: not shown

let pick = {model: null, harness: true};

export async function showTargets() {
  const el = $('#targetsCompare');
  if (el.childElementCount) return;  // built once; the pickers re-render the heatmap
  el.innerHTML = `
    <div class="grid gap-4 lg:grid-cols-[minmax(0,1fr)_320px]">
      <section class="panel overflow-x-auto p-4">
        <h2 class="panel-title">The four targets</h2>
        <p class="hint">Left to right, more modeling up front buys correctness, at the cost of coverage.</p>
        <table class="data-table mt-2 w-full text-sm">
          <tr><th></th>${ORDER.map((t) => `<th>${withIcon(targetIcon(t), esc(contextName(t)))}</th>`).join('')}</tr>
          ${PROFILE.map(([label, ...cells]) => `<tr><td class="whitespace-nowrap text-ink-muted">${label}</td>${cells.map((c) => `<td>${c}</td>`).join('')}</tr>`).join('')}
        </table>
      </section>
      <section class="panel p-4">
        <h2 class="panel-title">Trade-off</h2>
        ${quadrant()}
      </section>
    </div>
    <section class="panel mt-4 overflow-x-auto p-4">
      <div class="flex flex-wrap items-baseline justify-between gap-3">
        <h2 class="panel-title">By task type</h2>
        <div id="measureControls" class="flex flex-wrap items-center gap-2 text-sm"></div>
      </div>
      <div id="heatmap" class="mt-2"></div>
      <p class="hint mt-2">● strong ◐ partial ○ weak: expected, before measuring. Percentages: measured pass rate on this
        benchmark version’s runs (${MIN_N}+ answers; hover for counts).</p>
    </section>`;
  await renderHeatmap();
}

// Expressiveness (what can be asked) × determinism (same, correct number every time).
function quadrant() {
  const name = (t) => `<span class="block">${withIcon(targetIcon(t), esc(contextName(t)))}</span>`;
  const box = (html, cls = '') => `<div class="flex min-h-[84px] flex-col justify-center gap-1 rounded-control border border-border p-2 text-xs ${cls}">${html}</div>`;
  return `
    <div class="mt-2 grid grid-cols-[auto_minmax(0,1fr)_minmax(0,1fr)] gap-1 text-xs">
      <div class="row-span-2 flex items-center [writing-mode:vertical-rl] rotate-180 text-ink-muted">Determinism →</div>
      ${box(name('metricflow'), 'bg-surface-2')}${box(name('malloy'), 'bg-surface-2')}
      ${box('<span class="text-ink-subtle">—</span>')}${box(name('glossary') + name('raw'))}
      <div></div><div class="col-span-2 text-center text-ink-muted">Expressiveness →</div>
    </div>
    <p class="hint mt-2">Malloy pays elsewhere: modeling effort, and LLMs have seen little Malloy.</p>`;
}

async function renderHeatmap() {
  const el = $('#heatmap');
  el.innerHTML = loading('Loading measured results');
  let data;
  try {
    data = await getJSON('/api/targets/measured', {model: pick.model, harness: pick.harness});
  } catch (err) {
    el.innerHTML = errorBox(err.message);
    return;
  }
  pick.model = data.model;
  const models = Object.keys(data.models);
  $('#measureControls').innerHTML = models.length ? `
    <span class="text-ink-muted">Measured with</span>
    <select id="measureModel" class="input mt-0 w-auto py-1">${models.map((m) => `<option value="${esc(m)}" ${m === data.model ? 'selected' : ''}>${esc(modelName(m))}</option>`).join('')}</select>
    <select id="measureHarness" class="input mt-0 w-auto py-1"><option value="true" ${pick.harness ? 'selected' : ''}>Harness</option><option value="false" ${pick.harness ? '' : 'selected'}>Baseline</option></select>` : '';
  if (models.length) {
    $('#measureModel').onchange = (e) => { pick.model = e.target.value; renderHeatmap(); };
    $('#measureHarness').onchange = (e) => { pick.harness = e.target.value === 'true'; renderHeatmap(); };
  }
  const cell = (key, expected, t) => {
    const m = key && data.cells[t]?.[key];
    const measured = m && m.n >= MIN_N ? m : null;
    const glyph = expected ? `<span class="text-base leading-none" title="Expected">${GLYPH[expected[ORDER.indexOf(t)]]}</span>` : '';
    const rate = measured ? `<span class="tabular-nums" title="${measured.passed} of ${measured.n} passed">${pct(measured.passed / measured.n)}</span>` : '';
    return `<td class="text-center" style="${measured ? `background:${passTint(measured.passed / measured.n)}` : ''}">
      <div class="flex items-center justify-center gap-2">${glyph}${rate}</div></td>`;
  };
  el.innerHTML = `<table class="data-table w-full text-sm">
    <tr><th></th>${ORDER.map((t) => `<th class="!text-center">${withIcon(targetIcon(t), esc(contextName(t)))}</th>`).join('')}</tr>
    ${TASKS.map(([label, key, expected]) => `<tr><td class="whitespace-nowrap">${label}</td>${ORDER.map((t) => cell(key, expected, t)).join('')}</tr>`).join('')}
  </table>`;
}
