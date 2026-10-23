// Docs tab (read only): a small documentation site with a sidebar. What the app is for, how a run works, the
// targets, scoring and the HTTP API. Targets come from /api/config, so that page can't drift from the server.
// Section links are #/docs?s=<id>, so each section can be shared.

import {$, $$, esc} from './ui.js';
import {app} from './config.js';

const ext = (href, text) => `<a class="link whitespace-nowrap" href="${href}" target="_blank" rel="noopener">${text} ↗</a>`;
const table = (head, rows) => `<div class="my-4 overflow-x-auto rounded-panel border border-border">
  <table class="data-table w-full text-sm"><tr class="bg-surface-2">${head.map((h) => `<th class="!py-2">${h}</th>`).join('')}</tr>
  ${rows.map((r) => `<tr>${r.map((c) => `<td class="!py-2 align-top">${c}</td>`).join('')}</tr>`).join('')}
</table></div>`;
const p = (html) => `<p class="my-3 text-[15px] leading-7">${html}</p>`;
const code = (s) => `<code class="rounded bg-surface-2 px-1 py-0.5 text-[13px]">${esc(s)}</code>`;
const callout = (html) => `<div class="my-4 rounded-panel border border-border bg-surface px-4 py-3 text-sm leading-6">${html}</div>`;

// The site map: sidebar groups, each with its pages, in reading order.
function pages() {
  const targets = Object.values(app.config.contexts).map((c) => [`<span class="whitespace-nowrap font-medium">${esc(c.name)}</span>`, esc(c.summary)]);
  return [
    {group: 'Overview', items: [
      {id: 'intro', title: 'Introduction', body: `
        ${p('<strong>Which layer should an AI agent query to answer business questions?</strong> Semantic Layer Bench measures it. The same agent answers the same prompts over the same warehouse through different <em>targets</em>: raw SQL, raw SQL with a glossary, or a semantic layer.')}
        ${callout('<strong>Why it matters:</strong> semantic layers promise more accurate answers than raw SQL. This app puts numbers on accuracy, consistency, cost and speed, so you can choose a setup on evidence instead of claims.')}
        <h3 class="mt-6 text-base font-semibold">The warehouse</h3>
        ${p('A DuckDB warehouse: 52 tables (raw → staging → marts) with seed data from 2024-01 to 2025-09. It holds <em>no business metrics</em>: net revenue, MRR and churn are defined only in the semantic layers (and, in words, the glossary).')}
        ${p('It also has traps an agent must avoid: look-alike tables, a deprecated mart, cancelled orders and mixed currencies.')}`},
      {id: 'how', title: 'How a run works', body: `
        ${p('A run is a <strong>matrix</strong>. Each model × agent × target combination you pick is a <strong>setup</strong>, one cell in the results grid.')}
        ${table(['Step', 'What happens'], [
          ['<span class="whitespace-nowrap">1. Plan</span>', 'Every combination becomes a setup.'],
          ['2. Answer', 'Each setup answers every prompt in the prompt set. A fresh agent per prompt; six agents run at a time across the matrix.'],
          ['3. Grade', 'The agent’s final query runs and its rows are compared with a reference SQL answer.'],
          ['4. Repeat', 'Repeats run the whole matrix again, one after another, to measure consistency.'],
          ['5. Record', `Reports, traces and costs are logged to ${ext('https://mlflow.org/', 'MLflow')}, so every run reopens in History.`],
        ])}`},
      {id: 'using', title: 'Using the app', body: `
        ${table(['Tab', 'Use it to'], [
          ['<a class="link" href="#/run">Run</a>', 'Pick models, agents, targets, a prompt set and repeats, then watch results stream in.'],
          ['<a class="link" href="#/history">History</a>', 'Reopen a past run, or run it again with the same settings.'],
          ['<a class="link" href="#/harness">Harness</a>', 'See exactly what the agent is given, baseline vs. harness, for each target.'],
          ['MLflow', 'Inspect traces: every model call and tool call for each prompt.'],
        ])}
        <h3 class="mt-6 text-base font-semibold">Reading results</h3>
        ${table(['Results tab', 'Shows'], [
          ['Pass rate', 'The grid of setups, and the winner.'],
          ['Ranking', 'Every setup ranked by a weighted score, with adjustable weights.'],
          ['By prompt', 'Pass or fail for each prompt across setups.'],
          ['Answers', 'One setup’s answers: queries, explanations, rows and traces.'],
        ])}`},
    ]},
    {group: 'Concepts', items: [
      {id: 'targets', title: 'Targets', body: `
        ${p('The target is what the agent queries. Every target reads the same warehouse; they differ in how much business knowledge comes with it.')}
        ${table(['Target', 'What the agent gets'], targets)}`},
      {id: 'layers', title: 'Semantic layers', body: `
        ${p('A <strong>semantic layer</strong> defines business metrics (net revenue, MRR, churn) once, in code. Queries reuse those definitions instead of rewriting them in SQL, so every answer means the same thing.')}
        ${p('This app compares two. Both carry the same definitions, so the comparison is about the layer, not the metrics.')}
        <h3 class="mt-6 text-base font-semibold">dbt MetricFlow</h3>
        ${p(`The engine behind the dbt Semantic Layer. Metrics, dimensions and entities are declared in YAML; MetricFlow compiles a metric query into SQL and handles the joins. Here it’s defined in ${code('dbt/models/')}, and the agent sends metric queries through ${code('query_metrics')}. With the harness it can also define derived metrics from governed ones.`)}
        ${p(`${ext('https://docs.getdbt.com/docs/build/about-metricflow', 'dbt docs')} · ${ext('https://github.com/dbt-labs/metricflow', 'GitHub')}`)}
        <h3 class="mt-6 text-base font-semibold">Malloy</h3>
        ${p(`A modeling and query language. Sources declare joins and reusable measures, and Malloy queries compile to SQL. Here the model is ${code('app/malloy/warehouse.malloy')}, and the agent writes Malloy and runs it through ${code('run_malloy')}.`)}
        ${p(`${ext('https://www.malloydata.dev/', 'malloydata.dev')} · ${ext('https://github.com/malloydata/malloy', 'GitHub')}`)}`},
      {id: 'harness', title: 'Baseline vs. harness', body: `
        ${p('<strong>The baseline</strong> is the model with only the target’s query tools: a way to reach the data, nothing guiding or checking it.')}
        ${p('<strong>The harness</strong> adds <em>process</em> on top, never business knowledge:')}
        ${table(['Adds', 'What it does'], [
          ['Prompt caching', 'Reuses the instructions and tool definitions across calls, for lower cost.'],
          ['Tool guardrail', 'Checks each query before it runs: read-only SQL, or a query that compiles.'],
          ['Answer checklist', 'Tells the agent up front what a reviewer will check.'],
          ['Output checks', 'The final query must run and return data, and pass the target’s lints.'],
          ['Reviewer', 'A fresh agent checks the answer against the question and can send it back.'],
        ])}
        ${p('The <a class="link" href="#/harness">Harness</a> tab shows all of it, step by step, for each target.')}`},
      {id: 'scoring', title: 'Scoring', body: `
        ${table(['Measure', 'What it means'], [
          ['Pass', 'The final query runs and its rows match the reference SQL (order-insensitive, numbers within 0.5%).'],
          ['Always passed', 'Share of prompts passed in <em>every</em> repeat (pass^k). Use 3+ repeats: one prompt moves a 22-prompt score by 4.5 points.'],
          ['Flaky', 'Prompts passed in some repeats but not all.'],
          ['Cost', 'The agent’s tokens, reviewer included, at list prices, per prompt.'],
        ])}
        <h3 class="mt-6 text-base font-semibold">Prompt sets</h3>
        ${table(['Set', 'Use'], [
          ['Benchmark', '22 prompts, from simple totals to window functions.'],
          ['Held-out', '10 prompts written before any tuning. A score here shows whether improvements generalize.'],
        ])}`},
      {id: 'ranking', title: 'Ranking', body: `
        ${p('Each run names a winner: the setup with the best weighted score out of 100.')}
        ${table(['Factor', 'Scored as', 'Default weight'], [
          ['Accuracy', 'Average pass rate', '60%'],
          ['Cost', 'Cheapest setup’s cost per prompt ÷ this setup’s', '20%'],
          ['Speed', 'Fastest setup’s time per prompt ÷ this setup’s', '20%'],
        ])}
        ${p('Cost and speed are relative to the best setup in the run. Change the weights with the sliders on the Ranking tab; your browser remembers them.')}`},
    ]},
    {group: 'Reference', items: [
      {id: 'api', title: 'HTTP API', body: `
        ${p(`The UI is a thin client over these endpoints. Try them in the ${ext('/docs', 'interactive API reference')}, generated by FastAPI.`)}
        ${table(['Endpoint', 'What it does'], [
          [code('GET /api/config'), 'Targets (names and summaries), prompt sets and their sizes, whether an API key is set, and the MLflow URL.'],
          [code('POST /api/run'), `Runs a matrix and streams its progress as NDJSON, one JSON event per line. See <a class="link" href="#/docs?s=events" data-jump="events">Run events</a>.`],
          [code('GET /api/matrices'), `Past runs, newest first, with summary metrics. Paged with ${code('limit')} and ${code('offset')}.`],
          [code('GET /api/matrices/{id}'), 'One past run in full: setups, prompts, every report and answer, errors and timings.'],
          [code('GET /api/harness/{target}'), `What one target’s agent sees: instructions, tools and checks. ${code('?mode=on')} for the harness, ${code('off')} for the baseline. Calls no model.`],
          [code('/mlflow/'), 'MLflow’s own UI over the same store, for traces.'],
        ])}
        <h3 class="mt-6 text-base font-semibold">Run request body</h3>
        ${table(['Field', 'Type', 'Meaning'], [
          [code('models'), 'list of model ids', `e.g. ${code('claude-sonnet-5-5')}`],
          [code('harness_modes'), 'list of booleans', `${code('false')} = baseline, ${code('true')} = harness`],
          [code('context_modes'), 'list of target ids', 'Targets, as listed by /api/config'],
          [code('question_set'), 'string', `${code('benchmark')} or ${code('holdout')}`],
          [code('repeats'), 'integer, 1–10', 'Run everything this many times'],
        ])}`},
      {id: 'events', title: 'Run events', body: `
        ${p(`${code('POST /api/run')} streams these events in order. Within a repeat, events from different setups interleave.`)}
        ${table(['Event', 'Sent when'], [
          [code('plan'), 'First: the setups, prompts, repeats and MLflow links.'],
          [code('case_start'), 'A prompt takes an agent slot.'],
          [code('case'), 'A prompt is answered and graded.'],
          [code('cell'), 'A setup finishes a repeat. Carries its full report.'],
          [code('cell_error'), 'A setup failed outright.'],
          [code('run_end'), 'A repeat finishes. Carries its wall-clock time.'],
          [code('done'), 'Last line: the whole matrix is finished.'],
        ])}`},
      {id: 'cli', title: 'Command line', body: `
        ${table(['Task', 'Command'], [
          ['Run matrices without the UI', code('uv run python -m app.bench --harness on off --runs 3')],
          ['Check every prompt is expressible in both semantic layers', code('uv run python -m app.expressibility')],
          ['Rebuild the warehouse', code('uv run python -m app.db')],
        ])}
        ${p('Command-line runs are recorded in MLflow like the UI’s, so they show up in History.')}`},
    ]},
  ];
}

const navLink = (it) => `<a href="#/docs?s=${it.id}" data-jump="${it.id}"
  class="-ml-px block border-l border-transparent py-1 pl-3 text-sm text-ink-muted hover:border-border-strong hover:text-ink aria-[current=true]:border-ink aria-[current=true]:font-medium aria-[current=true]:text-ink">${it.title}</a>`;

let observer = null;

export function showDocs(params = {}) {
  const el = $('#docsBody');
  if (!el.childElementCount) render(el);
  if (params.s) requestAnimationFrame(() => jump(params.s, false));
}

function render(el) {
  const groups = pages();
  el.innerHTML = `
  <div class="lg:grid lg:grid-cols-[220px_minmax(0,1fr)] lg:gap-12">
    <aside class="border-b border-border py-4 lg:sticky lg:top-[49px] lg:h-[calc(100vh-49px)] lg:overflow-y-auto lg:border-b-0 lg:border-r lg:py-8 lg:pr-6">
      <nav aria-label="Docs" class="grid grid-cols-2 gap-x-6 gap-y-4 sm:grid-cols-3 lg:block lg:space-y-6">
        ${groups.map((g) => `<div>
          <p class="mb-2 text-xs font-semibold uppercase tracking-wide text-ink-subtle">${g.group}</p>
          <div class="border-l border-border">${g.items.map(navLink).join('')}</div>
        </div>`).join('')}
      </nav>
    </aside>
    <article class="max-w-[780px] pb-24 pt-8">
      <p class="text-sm text-ink-muted">Documentation</p>
      <h1 class="mb-2 text-3xl font-semibold tracking-tight">Semantic Layer Bench</h1>
      <p class="mb-8 text-[15px] leading-7 text-ink-muted">What this app is for, how a run works, and its HTTP API.</p>
      ${groups.map((g) => g.items.map((it) => `
        <section id="docs-${it.id}" data-section="${it.id}" class="scroll-mt-16 border-t border-border pt-8 mt-8 first:mt-0">
          <p class="mb-1 text-xs font-semibold uppercase tracking-wide text-ink-subtle">${g.group}</p>
          <h2 class="group text-2xl font-semibold tracking-tight">${it.title}
            <a href="#/docs?s=${it.id}" data-jump="${it.id}" class="ml-1 text-ink-subtle opacity-0 hover:text-ink group-hover:opacity-100" aria-label="Link to ${it.title}">#</a></h2>
          ${it.body}
        </section>`).join('')).join('')}
    </article>
  </div>`;
  $$('[data-jump]', el).forEach((a) => a.onclick = (e) => { e.preventDefault(); jump(a.dataset.jump); });

  // Highlight the section being read: the last one whose top has passed the upper part of the viewport.
  observer?.disconnect();
  const visible = new Set();
  observer = new IntersectionObserver((entries) => {
    for (const en of entries) en.isIntersecting ? visible.add(en.target.dataset.section) : visible.delete(en.target.dataset.section);
    const first = $$('[data-section]', el).find((s) => visible.has(s.dataset.section));
    if (first) setCurrent(first.dataset.section);
  }, {rootMargin: '-50px 0px -65% 0px'});
  $$('[data-section]', el).forEach((s) => observer.observe(s));
  setCurrent(groups[0].items[0].id);
}

function setCurrent(id) {
  $$('#docsBody nav [data-jump]').forEach((a) => a.setAttribute('aria-current', String(a.dataset.jump === id)));
}

function jump(id, smooth = true) {
  const target = $(`#docs-${id}`);
  if (!target) return;
  target.scrollIntoView({behavior: smooth ? 'smooth' : 'auto'});
  setCurrent(id);
  history.replaceState(null, '', `#/docs?s=${id}`);
}
