// Labels tab: label agent answers yourself, then see how often each automated check agrees with you.

import {$, $$, esc, pct, empty, loading, errorBox, passBadge} from './ui.js';
import {getJSON} from './api.js';
import {renderTraceDetail, cellLabel} from './traces.js';

const CHECK_NAMES = {LLMJudge: 'LLM judge', ResultMatches: 'Result match', passed: 'Case result (all checks)'};
const CHECK_HELP = {
  LLMJudge: 'The rubric-based judge model.',
  ResultMatches: 'Rows equal the expected SQL\'s rows (prompts with expected SQL only).',
  passed: 'What the grid counts as a pass: every check passed.',
};
const PRIORITY = ['Result match and judge disagree', 'No expected SQL: judge only', 'Checks agree'];

// Landis & Koch's conventional reading of kappa.
function kappaWord(k) {
  if (k == null) return 'not enough variety to measure';
  if (k < 0) return 'worse than chance';
  return k <= 0.2 ? 'slight' : k <= 0.4 ? 'fair' : k <= 0.6 ? 'moderate' : k <= 0.8 ? 'substantial' : 'almost perfect';
}

let data = null;
let scope = null;
let cursor = 0;

export async function showLabels(params) {
  const body = $('#labelsBody');
  body.innerHTML = loading('Loading labels');
  let matrices;
  try {
    matrices = await getJSON('/api/matrices');
  } catch (err) {
    body.innerHTML = errorBox(err.message);
    return;
  }
  scope = params.matrix ?? '';
  $('#labelScope').innerHTML = `<label class="field-label">Scope
    <select id="labelMatrix" class="input">${[['', 'All matrices'], ...matrices.map((m) => [m.id, m.name])]
      .map(([v, l]) => `<option value="${esc(v)}" ${v === scope ? 'selected' : ''}>${esc(l)}</option>`).join('')}</select></label>`;
  $('#labelMatrix').onchange = (e) => { location.hash = '#/labels?' + new URLSearchParams(e.target.value ? {matrix: e.target.value} : {}); };
  if (!matrices.length) {
    body.innerHTML = empty('Nothing to label yet. <a class="link" href="#/run">Run a matrix</a> first.');
    return;
  }
  cursor = 0;
  await refresh();
}

async function refresh(keepTrace = null) {
  const body = $('#labelsBody');
  try {
    data = await getJSON('/api/labels', {matrix: scope});
  } catch (err) {
    body.innerHTML = errorBox(err.message);
    return;
  }
  if (keepTrace) cursor = Math.max(0, data.queue.findIndex((q) => q.trace_id === keepTrace));
  cursor = Math.min(cursor, Math.max(data.queue.length - 1, 0));
  body.innerHTML = `
    <section class="mb-5">
      <div class="mb-2 flex flex-wrap items-baseline justify-between gap-2">
        <h2 class="panel-title">Agreement with your labels</h2>
        <p class="hint">${data.labeled} of ${data.total} agent answers labeled</p>
      </div>
      <div class="grid gap-3 md:grid-cols-3">${Object.entries(data.checks).map(([name, c]) => agreementCard(name, c)).join('')}</div>
    </section>
    <section class="mb-5 grid gap-4 lg:grid-cols-[minmax(0,360px)_minmax(0,1fr)]">
      <div>
        <div class="mb-2 flex items-baseline justify-between">
          <h2 class="panel-title">To label (${data.queue.length})</h2>
          <p class="hint"><kbd>y</kbd> correct · <kbd>n</kbd> wrong · <kbd>j</kbd>/<kbd>k</kbd> next/previous</p>
        </div>
        <div id="queueList" class="panel max-h-[70vh] overflow-y-auto"></div>
      </div>
      <div id="queueDetail" class="min-w-0"></div>
    </section>
    <section>
      <h2 class="panel-title mb-2">Where checks disagree with you (${data.disagreements.length})</h2>
      ${disagreementTable()}
    </section>`;
  drawQueue();
}

function agreementCard(name, c) {
  const confusion = c.n ? `
    <table class="mt-3 w-full text-xs tabular-nums" aria-label="${esc(CHECK_NAMES[name])} versus your labels">
      <thead><tr class="text-ink-muted"><th></th><th class="pb-1 text-right font-medium">Check passed</th><th class="pb-1 text-right font-medium">Check failed</th></tr></thead>
      <tbody>
        <tr><th class="py-0.5 text-left font-medium text-ink-muted">You: correct</th><td class="text-right">${c.tp}</td><td class="text-right ${c.fn ? 'font-semibold text-danger' : ''}">${c.fn}</td></tr>
        <tr><th class="py-0.5 text-left font-medium text-ink-muted">You: wrong</th><td class="text-right ${c.fp ? 'font-semibold text-danger' : ''}">${c.fp}</td><td class="text-right">${c.tn}</td></tr>
      </tbody>
    </table>` : '';
  return `<div class="panel p-4">
    <p class="text-sm font-medium">${esc(CHECK_NAMES[name] ?? name)}</p>
    <p class="hint">${esc(CHECK_HELP[name] ?? '')}</p>
    ${c.n ? `<p class="mt-2 text-[26px] font-semibold leading-tight tracking-tight tabular-nums">${pct(c.agreement)} <span class="text-sm font-normal text-ink-muted">agree</span></p>
      <p class="text-xs text-ink-muted">κ ${c.kappa == null ? '—' : c.kappa.toFixed(2)} · ${kappaWord(c.kappa)} · ${c.n} labeled</p>`
      : '<p class="mt-2 text-sm text-ink-muted">No labeled answers with this check yet.</p>'}
    ${confusion}
  </div>`;
}

function drawQueue() {
  const list = $('#queueList'), detail = $('#queueDetail');
  if (!data.queue.length) {
    list.innerHTML = '<p class="p-4 text-sm text-ink-muted">Everything in this scope is labeled.</p>';
    detail.innerHTML = empty('Nothing left to label here. Pick another scope, or run another matrix.');
    return;
  }
  list.innerHTML = data.queue.map((q, i) => `<button type="button" data-q="${i}" aria-current="${i === cursor}"
    class="block w-full border-b border-border px-3 py-2.5 text-left last:border-b-0 ${i === cursor ? 'bg-accent-soft' : 'hover:bg-surface-2'}">
    <div class="flex items-center gap-2">${q.assessments.passed ? passBadge(q.assessments.passed.value) : ''}
      <span class="text-xs ${q.priority === 0 ? 'font-medium text-warning' : 'text-ink-muted'}">${PRIORITY[q.priority]}</span></div>
    <p class="mt-1 line-clamp-2 text-sm">${esc(q.prompt ?? q.trace_id)}</p>
    <p class="mt-0.5 text-xs text-ink-muted">${esc(cellLabel(q.cell))}${q.case ? ` · ${esc(q.case)}` : ''}${q.run ? ` · run ${q.run}` : ''}</p>
  </button>`).join('');
  $$('[data-q]', list).forEach((b) => b.onclick = () => { cursor = +b.dataset.q; drawQueue(); });
  $(`[data-q="${cursor}"]`, list)?.scrollIntoView({block: 'nearest'});
  const item = data.queue[cursor];
  renderTraceDetail(detail, item.trace_id, {onLabeled: () => {
    // Labeled: it leaves the queue; stay at the same position, which is now the next item.
    refresh();
  }});
}

function disagreementTable() {
  if (!data.disagreements.length) {
    return `<p class="hint">${data.labeled ? 'None: every check agrees with your labels so far.' : 'Label some answers to see where the checks disagree with you.'}</p>`;
  }
  return `<div class="panel overflow-x-auto"><table class="data-table w-full text-sm">
    <thead><tr><th>Question</th><th>Cell</th><th>You</th><th>Disagreeing checks</th><th></th></tr></thead>
    <tbody>${data.disagreements.map((d) => {
      const you = d.assessments.human_correct;
      return `<tr>
        <td class="max-w-[360px]">${esc(d.prompt ?? '')}${you.rationale ? `<span class="block text-xs text-ink-muted">${esc(you.rationale)}</span>` : ''}</td>
        <td class="text-xs">${esc(cellLabel(d.cell))}${d.case ? ` · ${esc(d.case)}` : ''}</td>
        <td class="whitespace-nowrap font-medium ${you.value ? 'text-success' : 'text-danger'}">${you.value ? 'Correct' : 'Wrong'}</td>
        <td>${d.disagrees_with.map((c) => esc(CHECK_NAMES[c] ?? c)).join(', ')}</td>
        <td class="text-right"><a class="link" href="#/traces?${new URLSearchParams({trace: d.trace_id, matrix: '', kind: 'agent'})}">Trace</a></td>
      </tr>`;
    }).join('')}</tbody></table></div>`;
}

// Keyboard: only while the Labels view is showing and focus isn't in a text field.
document.addEventListener('keydown', (e) => {
  if ($('#view-labels').hidden || !data?.queue.length || e.metaKey || e.ctrlKey || e.altKey) return;
  if (e.target.closest('input, textarea, select')) return;
  if (e.key === 'j' || e.key === 'k') {
    cursor = Math.max(0, Math.min(data.queue.length - 1, cursor + (e.key === 'j' ? 1 : -1)));
    drawQueue();
  } else if (e.key === 'y' || e.key === 'n') {
    $(`#queueDetail [data-label="${e.key === 'y'}"]`)?.click();
  }
});
