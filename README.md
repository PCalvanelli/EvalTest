# Text-to-SQL Eval Playground

Small local app for evaluating a text-to-SQL agent over a seeded, dbt-style DuckDB warehouse, as a matrix:

**model** (Opus / Sonnet / Haiku) × **harness** ([Pydantic AI Harness](https://pydantic.dev/docs/ai/harness/) off / on) × **business context** (raw text-to-SQL / dbt MetricFlow / Malloy)

The question this playground answers: *which intermediate representation between a natural-language question and the warehouse gives the highest, most consistent accuracy?*

Cells run concurrently, limited to 6 agent runs at a time across the whole matrix, and stream into the UI as they finish.
The **Runs** slider (1–10) repeats the whole matrix one run after another. A run starts only after the previous one finishes, and cells show the mean pass rate, the min–max range, and per-run detail.

## The warehouse (52 tables)

Built from `app/warehouse/*.sql`, with deterministic seed data from 2024-01 to 2025-09. Every table and most columns carry
documentation (`COMMENT ON`, like dbt `persist_docs`).

| Layer | Schemas | Tables | What it looks like |
|---|---|---|---|
| raw | `raw_shopify`, `raw_stripe`, `raw_zendesk`, `raw_hubspot`, `raw_ads`, `raw_erp`, `raw_finance` | 21 | Source-native columns: string money, cents, micros, epoch seconds/ms, soft deletes, test orders, multi-currency |
| staging | `staging` | 16 | `stg_<source>__<entity>`: one per source table, renamed, typed and cleaned |
| marts | `marts` | 15 | `fct_*` / `dim_*`: cleaned entities and events in USD, conformed `customer_key`, plus a deprecated `fct_orders_legacy` |

**The warehouse holds no business metrics.** Tables contain system and entity facts only: amounts, statuses, timestamps, flags and keys.
Terms like *net revenue*, *MRR*, *active customers*, *churn*, *first response time* and *customer segment* are defined
only in the semantic layers (dbt MetricFlow and Malloy), as they would be in a real dbt stack. A raw-SQL agent has to work out what
the business means by them, which is the gap the semantic layer is meant to close.

Other deliberate traps:
- look-alike tables at every layer;
- a deprecated orders mart;
- cancelled orders still present in the orders table;
- refunds in a separate table;
- non-USD Shopify and Facebook amounts in raw/staging;
- Google `cost_micros`;
- internal notes vs. public replies;
- semi-additive inventory snapshots;
- an SCD2 price history.

## Agents

- **Raw SQL mode:** the agent doesn't get the schema in its prompt. It has to discover the warehouse with `list_tables`, `describe_table` (columns, docs and sample rows) and `run_sql`, the way an agent would on a real warehouse.
- **dbt MetricFlow mode:** the agent gets a metrics catalog generated from the dbt project in `dbt/` (`models/semantic_models.yml`, `models/metrics.yml`) and one tool, `query_metrics` (metrics, group by, Jinja `where`, order, limit, like `mf query`). `app/metricflow_bridge.py` compiles queries in-process with MetricFlow (`dbt parse` builds the manifest on first use) and runs the SQL like every other context. The semantic models sit on views in a `semantic` schema (`app/warehouse/04_semantic.sql`) that the raw-SQL agent can't see or query.
- **Malloy mode:** the agent writes [Malloy](https://www.malloydata.dev/) queries against `app/malloy/warehouse.malloy` and has a single tool, `run_malloy`.
  The model has the **same governed definitions** as the dbt metrics, but it's built the idiomatic Malloy way: sources are joined, and a `customers`
  hub reaches orders, tickets and memberships. Queries can cross domains, nest, pipeline and compute ad-hoc fields. Queries compile through a Node
  sidecar (`malloy/compile.mjs`, the official `@malloydata/malloy` compiler) and execute in Python like every other context.
  `uv run python -m app.metricflow_bridge` checks parity: every gold answer must match in MetricFlow and Malloy, and the two must agree on every governed metric.
- **Harness: a shared core plus one harness per target.** Each target has its own failure modes and rules, so each
  gets its own harness: a cell with the harness on gets the core plus its own target's group. The harness adds *process*
  (caching, checks, review, tools, retries), never business knowledge, which would overlap with the business-context axis.
  - **Core (every target)**:
    - **Prompt caching**: the instructions, tool definitions and conversation are re-read from cache at the cache-read
      price.
    - **Answer review**: a fresh call to the same model checks the final query, explanation and first rows against the
      question. Its checklist is target-specific, it has no rubric and no definitions, and it accepts answers that say
      the question can't be expressed. It sends problems back at most twice.
    - **Output check**: the final query must run and return data.
    - **Tool guardrail**.
    - Every step is bounded, so a case always ends with an answer on record. Reviewer tokens count toward the case's
      cost.
  - **Raw SQL**:
    - **Deprecation lint**: tables documented as `DEPRECATED` are flagged in `run_sql` and, once, in the final query.
    - **Column documentation**: the reviewer sees the warehouse's own documentation for every column the final query
      uses.
  - **dbt MetricFlow**: **`define_metric`**. When no governed metric fits, the agent defines a derived (with
    `offset_window` for prior periods), cumulative (rolling `window` or `grain_to_date`) or ratio (filtered) metric from
    governed metrics.
    - MetricFlow validates the metric in-process against a per-question copy of the semantic manifest, and the
      definitions travel with the final answer.
    - New semantic models or dimensions are out of scope: they would mean writing SQL.
    - **Result steps** (`post_process`): `rank_filter` (per-group top-N, ties kept), `share_of_total`, `select`. They
      run on MetricFlow's result rows, like a BI tool on the semantic layer, and never on warehouse tables.
  - **Malloy**: **flat output**. A result with nested rows (`nest:`) is sent back once to be flattened.
  - Off by default (they added tokens without helping): `SystemReminders`, `Planning`, `RepairToolArguments`.

  Judge harness vs no harness with **Runs ≥ 3**. With 22 prompts, a single prompt is a 4.5-point swing.

## Benchmark sets and the ceiling

- **Sample sets:** the Prompts tab offers two, **Benchmark (22)** and **Held-out (10)**.
- **Held-out:** written and verified before the semantic layers and harness were tuned on the benchmark, so a score
  there shows whether those improvements generalize. Don't tune against it.
- **The ceiling:** `uv run python -m app.expressibility` runs a hand-written MetricFlow and Malloy answer for every
  question in both sets, graded with the eval's own matcher. Today every question is expressible in both, so any
  score below that is agent error, not a missing capability in the layer.

## Scoring

The eval framework is [Pydantic Evals](https://pydantic.dev/docs/ai/evals/). Each prompt is a `Case`, checked by:
- `ExecutesOK`: the query runs;
- `ResultMatches`: only when you give an expected SQL; compares result values ignoring row and column order, with numbers equal within 0.5% (extra or missing columns still fail). A prompt with `match: 'numbers'` compares only numeric columns, for answers whose labels can vary (TRUE/FALSE vs 'first'/'repeat'). The sample prompts' expected SQL encodes the governed metric definitions and returns the same results;
- `LLMJudge` (Sonnet 5.5 by default):
  - **With an expected SQL**, it gets the verified reference (its SQL, row count and first rows). It checks that the
    answer agrees with the reference, allowing presentation differences, and that the explanation honestly describes
    the query. The rubric is context only.
  - **Without one**, it grades on the rubric.
  - The rubric is a list of company definitions, each scoped to the metrics it names.
  - Claude 4.7+ models only accept default sampling settings, so the judge runs at the default temperature.

A case passes only if every check passes. Each cell also reports the two graders separately (rows match, judge pass) and how often they disagree, which usually points at the question or the grading. To check the judge itself, label cases in the Labels tab: it reports the judge's agreement (Cohen's κ) with your labels.

**Consistency:** with Runs > 1, each cell reports **pass^k**, the share of prompts that passed in *every* run, plus the prompts that are **flaky** (passed in some runs but not all).
The ★ goes to the best pass^k. Mean pass rate measures average accuracy; pass^k measures accuracy you can rely on.

**Cost-weighted scoring.** Tokens are converted to dollars at each model's list price (`app/pricing.py`, editable in the UI), since token
prices differ ~40× between models. Each cell's **expected net value per question** is

    U = p·V − (1 − p)·L − c

where p = P(correct) (pooled over prompts and runs), V = value of a correct answer, L = loss from a wrong answer, and c = expected
agent token cost per question (judge tokens are excluded). V and L are explicit exchange rates, so no hidden weighting is involved.
Alongside U, the UI shows:
- **Pareto frontier** of accuracy vs. cost (parameter-free: a dominated cell loses for every choice of V and L);
- **break-even values** between neighboring frontier cells, V* = Δc / Δp − L: the value of a correct answer above which the pricier, more
  accurate cell is worth it;
- **95% cluster-bootstrap intervals** (prompts resampled with all their runs) and **paired** differences vs. the top cell, with P(better).
The ★ ranking can be switched between net value, pass^k and mean pass rate.

## Run

```bash
uv sync
(cd malloy && npm ci)        # Malloy compiler sidecar (Node 18+), needed for the Malloy context
uv run uvicorn app.main:app --reload --reload-dir app --reload-dir static
# open http://localhost:8000
```

Enter your Anthropic API key (and a workspace ID if the key isn't workspace-scoped), choose the matrix dimensions, edit the prompts and judge criteria, then click Run.
Cell time is the wall-clock time for that cell's prompt set, including judging and queueing behind other cells. Per-prompt times don't include queueing.
The API key goes to the server once per request. It isn't stored, unless you tick "remember", which saves it in your browser's localStorage.

The database file is `data/warehouse-<hash>.duckdb`. Its name changes whenever the build SQL changes, which triggers an automatic rebuild.
`uv run python -m app.db` rebuilds it and lists the tables. `uv run python -m app.semantic.layer` compiles and runs sample semantic queries.

## The app: Run, History, Traces, Labels

Everything runs on the one server. Every matrix is recorded in MLflow (a local SQLite store, `data/mlflow.db`), and the app reads it back:

- **Run**: configure and launch a matrix, then compare cells live: the grid, per-prompt k/N, cost and the Pareto frontier. Each case card has
  **View trace**, which opens the trace in a drawer without leaving the results.
- **History**: every matrix you've run. **Open** loads one into the Run view (grid, economics and case detail all work on past runs);
  **Cells** shows each cell's pass^k, mean pass rate and cost; select two or more matrices to compare the same cells across them.
- **Traces**: one trace per agent answer and one per judge call, filtered by matrix, cell, kind, result and case. The detail shows the span
  waterfall (each model call and tool call, with inputs and outputs), the eval checks with the judge's reasoning, and your label.
- **Labels**: mark answers correct or wrong yourself (keys `y` / `n`, `j` / `k` to move). The queue puts first the answers where the result
  check and the judge disagree, then judge-only prompts. For each check (LLM judge, result match, the overall case result) it shows
  agreement with you, Cohen's κ and a confusion matrix, plus the list of answers where a check disagrees with you. Low agreement means the
  grid's pass rates can't be trusted yet.
- **MLflow ↗** (top right) opens MLflow's own UI, served by the same server at `/mlflow`, for anything the app doesn't cover.

What MLflow stores, per matrix:

- **One parent run**, holding the parameters, `prompts.json`, `rubric.md` and `summary.json`.
- **One child run per cell.** Each repeat logs pass rate, tokens, time and cost at step = run number, plus `run-N/report.json` (what History
  re-renders). When the matrix finishes, the cell logs `pass_k`, `mean_pass_rate`, `flaky_prompts` and `mean_cost_per_question_usd`.
- **One trace per case**, linked to its cell's run and tagged with matrix, cell, case and run, with the eval checks attached as feedback.
  Judge calls get their own traces (tag `kind=judge`). Your labels are stored as `human_correct` feedback from a HUMAN source.

MLflow's cost metrics use list prices from `app/pricing.py`; prices edited in the page apply to the page only.

Settings (environment variables): `MLFLOW_TRACKING_URI` to use a tracking server instead of `data/mlflow.db` (then `/mlflow` isn't served
and `MLFLOW_UI_URL` points at that server's UI), `MLFLOW_EXPERIMENT_NAME`, and `EVAL_MLFLOW=off` to disable tracking. A tracking failure is
logged and never fails an eval. The API key is never logged.

## Design system

`static/css/tokens.css` defines the tokens: surfaces, ink, the accent (interaction only), status colors, radii and type (Instrument Sans,
JetBrains Mono for code), each with a light and a dark value. Dark mode follows the OS unless the header's theme toggle pins it.
`static/index.html` maps the tokens into Tailwind (`bg-surface`, `text-ink-muted`, `border-border`, `bg-accent`, ...) and defines the few
shared components (`.panel`, `.btn`, `.input`, `.chip`, `.badge-*`, `.stat`, `.code`, `.data-table`, `.notice-*`). Use those, not raw
Tailwind colors. Chart series colors were validated for color-vision deficiency against both surfaces; keep their order.

## Files

- `app/warehouse/`: `01_raw.sql`, `02_staging.sql`, `03_marts.sql`
- `app/db.py`: warehouse build, read-only query helper, `list_tables` / `describe_table`
- `dbt/`: dbt project for MetricFlow (semantic models, metrics, time spine); `app/metricflow_bridge.py` compiles queries and runs the parity check
- `app/malloy/warehouse.malloy`: Malloy semantic model
- `app/malloy_bridge.py`, `malloy/compile.mjs`: Malloy → SQL compilation via the Node sidecar (schema-only in-memory DuckDB)
- `app/agent.py`: agent factory (raw SQL / dbt MetricFlow / Malloy) and the harness capability set
- `app/evals.py`: Pydantic Evals dataset, evaluators, report summary
- `app/tracking.py`: MLflow runs, traces and feedback for each matrix
- `app/history.py`: read API over the MLflow store (`/api/matrices`, `/api/traces`, `/api/labels`) and labeling
- `app/mlflow_ui.py`: MLflow's own UI mounted at `/mlflow`
- `app/main.py`: FastAPI routes (`/`, `/api/config`, and `/api/run`, which streams NDJSON)
- `static/index.html`: page shell, Tailwind token mapping and components; `static/css/tokens.css`: design tokens
- `static/js/`: ES modules, no build step. `app.js` (entry), `router.js`, `run.js`, `results.js`, `econ.js`, `history.js`, `traces.js`,
  `labels.js`, `ui.js`, `api.js`, `config.js`
