// Docs tab (read only): what the app is for, how a run works, the targets and the HTTP API. Targets come from
// /api/config, so the table can't drift from the server.

import {$, esc} from './ui.js';
import {app} from './config.js';

const ext = (href, text) => `<a class="link whitespace-nowrap" href="${href}" target="_blank" rel="noopener">${text} ↗</a>`;
const section = (id, title, body) => `<section id="docs-${id}" class="panel scroll-mt-20 p-5">
  <h2 class="panel-title mb-3">${title}</h2>${body}</section>`;
const table = (head, rows) => `<div class="overflow-x-auto"><table class="data-table w-full text-sm">
  <tr>${head.map((h) => `<th>${h}</th>`).join('')}</tr>
  ${rows.map((r) => `<tr>${r.map((c) => `<td class="align-top">${c}</td>`).join('')}</tr>`).join('')}
</table></div>`;
const p = (html) => `<p class="mb-3 text-sm leading-relaxed">${html}</p>`;
const code = (s) => `<code class="rounded bg-surface-2 px-1 py-0.5 text-[12px]">${esc(s)}</code>`;

const TOC = [['purpose', 'Purpose'], ['how', 'How a run works'], ['targets', 'Targets'], ['layers', 'dbt MetricFlow and Malloy'],
  ['harness', 'Baseline vs. harness'], ['scoring', 'Scoring and ranking'], ['api', 'HTTP API']];

export function showDocs() {
  const el = $('#docsBody');
  if (el.childElementCount) return;
  const targets = Object.values(app.config.contexts).map((c) => [`<span class="whitespace-nowrap font-medium">${esc(c.name)}</span>`, esc(c.summary)]);
  el.innerHTML = `
  <nav class="mb-4 flex flex-wrap gap-x-4 gap-y-1 text-sm" aria-label="On this page">
    ${TOC.map(([id, t]) => `<a class="link" href="#/docs" data-jump="${id}">${t}</a>`).join('')}
  </nav>
  <div class="space-y-4">
  ${section('purpose', 'Purpose', `
    ${p('<strong>Which layer should an AI agent query to answer business questions?</strong> This app measures it. The same agent answers the same prompts over the same warehouse through different <em>targets</em>: raw SQL, raw SQL with a glossary, or a semantic layer.')}
    ${p('<strong>Why it matters:</strong> semantic layers promise more accurate answers than raw SQL. This puts numbers on accuracy, consistency, cost and speed, so you can choose a setup on evidence.')}
    ${p(`<strong>The warehouse</strong> is DuckDB: 52 tables (raw → staging → marts) with seed data from 2024-01 to 2025-09. It holds <em>no business metrics</em>: net revenue, MRR and churn are defined only in the semantic layers (and, in words, the glossary). It also has traps: look-alike tables, a deprecated mart, cancelled orders, mixed currencies.`)}`)}

  ${section('how', 'How a run works', `
    ${p('A run is a <strong>matrix</strong>. Pick models, agents and targets on the Run tab; each combination is a <strong>setup</strong> (a cell in the grid).')}
    ${table(['Step', 'What happens'], [
      ['1. Plan', 'Every model × agent × target combination becomes a setup.'],
      ['2. Answer', 'Each setup answers every prompt in the chosen prompt set. A fresh agent per prompt; six agents at a time across the matrix.'],
      ['3. Grade', 'The agent’s final query runs and its rows are compared with a reference SQL answer.'],
      ['4. Repeat', `Repeats run the whole matrix again, one after another, to measure consistency.`],
      ['5. Record', `Everything (reports, traces, costs) is logged to ${ext('https://mlflow.org/', 'MLflow')}, so past runs reopen in History.`],
    ])}
    <div class="mt-3"></div>
    ${table(['Tab', 'Use it to'], [
      ['Run', 'Launch a matrix and watch results stream in: Pass rate, Ranking, By prompt and Answers.'],
      ['History', 'Reopen past runs, or run one again with the same settings.'],
      ['Harness', 'See exactly what the agent is given, baseline vs. harness, per target.'],
      ['MLflow', 'Inspect traces: every model call and tool call per prompt.'],
    ])}`)}

  ${section('targets', 'Targets', `
    ${p('The target is what the agent queries. Every target reads the same warehouse.')}
    ${table(['Target', 'What the agent gets'], targets)}`)}

  ${section('layers', 'dbt MetricFlow and Malloy', `
    ${p('Both are <strong>semantic layers</strong>: business metrics (net revenue, MRR, churn) are defined once, in code, and queries reuse those definitions instead of rewriting them in SQL. Here both carry the same definitions, so the comparison is about the layer, not the metrics.')}
    ${table(['Layer', 'What it is', 'In this app', 'Learn more'], [
      ['<span class="whitespace-nowrap font-medium">dbt MetricFlow</span>', 'The engine behind the dbt Semantic Layer. Metrics, dimensions and entities are declared in YAML; MetricFlow compiles a metric query into SQL and handles the joins.',
        `Defined in ${code('dbt/models/')}. The agent sends metric queries through ${code('query_metrics')} and can define derived metrics from governed ones.`,
        `${ext('https://docs.getdbt.com/docs/build/about-metricflow', 'dbt docs')}<br>${ext('https://github.com/dbt-labs/metricflow', 'GitHub')}`],
      ['<span class="whitespace-nowrap font-medium">Malloy</span>', 'A modeling and query language from the Malloy project. Sources declare joins and reusable measures; Malloy queries compile to SQL.',
        `Defined in ${code('app/malloy/warehouse.malloy')}. The agent writes Malloy and runs it through ${code('run_malloy')}.`,
        `${ext('https://www.malloydata.dev/', 'malloydata.dev')}<br>${ext('https://github.com/malloydata/malloy', 'GitHub')}`],
    ])}`)}

  ${section('harness', 'Baseline vs. harness', `
    ${p('<strong>The baseline</strong> is the model with only the target’s query tools: a way to reach the data, nothing guiding or checking it.')}
    ${p('<strong>The harness</strong> adds <em>process</em> on top, never business knowledge: prompt caching, a tool guardrail, an answer checklist, a check that the final query runs and returns data, target-specific lints and a reviewer agent that can send an answer back. The <a class="link" href="#/harness">Harness</a> tab shows it step by step.')}`)}

  ${section('scoring', 'Scoring and ranking', `
    ${table(['Measure', 'What it means'], [
      ['Pass', 'The final query runs and its rows match the reference SQL (order-insensitive, numbers within 0.5%).'],
      ['Always passed (pass^k)', 'Share of prompts passed in <em>every</em> repeat. Use 3+ repeats: one prompt moves a 22-prompt score by 4.5 points.'],
      ['Flaky', 'Prompts passed in some repeats but not all.'],
      ['Cost', 'The agent’s tokens (reviewer included) at list prices, per prompt.'],
      ['Score (Ranking)', 'One number out of 100: accuracy, cost and speed, weighted 60 / 20 / 20 by default. Cost and speed are scored against the best setup in the run. Adjust the weights on the Ranking tab.'],
    ])}
    <div class="mt-3"></div>
    ${p(`<strong>Prompt sets:</strong> <em>Benchmark</em> (22 prompts) and <em>Held-out</em> (10 prompts, written before any tuning, so a score there shows whether improvements generalize).`)}`)}

  ${section('api', 'HTTP API', `
    ${p(`The UI is a thin client over these endpoints. ${ext('/docs', 'Interactive API reference')} (generated by FastAPI) lets you try them.`)}
    ${table(['Endpoint', 'What it does'], [
      [code('GET /api/config'), 'Targets (names and summaries), prompt sets with their sizes, whether an API key is set, and the MLflow URL.'],
      [code('POST /api/run'), `Runs a matrix and streams its progress as NDJSON (one JSON event per line). Body: ${code('models')}, ${code('harness_modes')}, ${code('context_modes')}, ${code('question_set')}, ${code('repeats')}.`],
      [code('GET /api/matrices'), `Past runs, newest first, with summary metrics. Paged with ${code('limit')} and ${code('offset')}.`],
      [code('GET /api/matrices/{id}'), 'One past run in full: setups, prompts, every report and answer, errors and timings.'],
      [code('GET /api/harness/{target}'), `What one target’s agent sees: instructions, tools and checks. ${code('?mode=on')} for the harness, ${code('off')} for the baseline. Calls no model.`],
      [code('/mlflow/'), 'MLflow’s own UI over the same store, for traces.'],
    ])}
    <h3 class="mb-2 mt-4 text-sm font-medium">Run events (${code('POST /api/run')})</h3>
    ${table(['Event', 'Sent when'], [
      [code('plan'), 'First: the setups, prompts, repeats and MLflow links.'],
      [code('case_start'), 'A prompt takes an agent slot.'],
      [code('case'), 'A prompt is answered and graded (passed or not).'],
      [code('cell'), 'A setup finishes a repeat; carries its full report.'],
      [code('cell_error'), 'A setup failed outright.'],
      [code('run_end'), 'A repeat finishes; carries its wall-clock time.'],
      [code('done'), 'Last line: the whole matrix is finished.'],
    ])}
    <div class="mt-3"></div>
    ${p(`From the command line, ${code('uv run python -m app.bench')} runs the same matrices without the UI; see the README.`)}`)}
  </div>`;
  el.querySelectorAll('[data-jump]').forEach((a) => a.onclick = (e) => {
    e.preventDefault();
    $(`#docs-${a.dataset.jump}`).scrollIntoView({behavior: 'smooth'});
  });
}
