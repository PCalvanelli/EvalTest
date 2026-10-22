# Text-to-SQL Eval Playground

Small local app for evaluating a text-to-SQL agent over a seeded, dbt-style DuckDB warehouse, as a matrix:

**model** (Opus / Sonnet / Haiku) × **harness** ([Pydantic AI Harness](https://pydantic.dev/docs/ai/harness/) off / on) × **business context** (none / glossary in prompt / glossary search / YAML semantic layer / Malloy)

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
only in the semantic layer, as they would be in a real dbt + MetricFlow/Cube stack. A raw-SQL agent has to work out what
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
- **Glossary mode:** raw SQL mode plus `app/glossary.md`, a plain-language business glossary in the agent's instructions.
  It has the same definitions as the semantic layer, written the way Finance would write them, with no table names, column names or SQL.
  This tests whether written definitions alone close the gap without a governed, compiled layer.
- **Glossary search mode:** raw SQL mode plus a `search_glossary(query)` tool over the same glossary (keyword search
  returning the top 3 entries, `app/glossary.py`). The glossary is not in the prompt, so the agent has to decide to look
  terms up, as it would with a wiki or docs search. Each case and cell reports how many searches the agent made.
- **Semantic mode:** the agent sees only the semantic catalog (`app/semantic/warehouse.yaml`). It has six models (`orders`, `order_lines`, `subscriptions`, `support`, `marketing` and `inventory`), and they hold the governed metric definitions. A model can be built on a SQL subquery: for example, MRR evaluates every subscription at each month end, and first response time uses the first public agent comment. Its only tool is `query_metrics`. A query targets one model, and `app/semantic/layer.py` compiles it to SQL, so every cell is scored the same way. Cross-domain questions (e.g. customers with both orders and tickets) can't be expressed, and they fail on purpose.
- **Malloy mode:** the agent writes [Malloy](https://www.malloydata.dev/) queries against `app/malloy/warehouse.malloy` and has a single tool, `run_malloy`.
  The model has the **same governed definitions** as the YAML layer, but it's built the idiomatic Malloy way: sources are joined, and a `customers`
  hub reaches orders, tickets and memberships. Queries can cross domains, nest, pipeline and compute ad-hoc fields. Queries compile through a Node
  sidecar (`malloy/compile.mjs`, the official `@malloydata/malloy` compiler) and execute in Python like every other context.
  `uv run python -m app.malloy_bridge` checks parity: every gold answer and every governed metric must match the YAML layer.
- **Harness capabilities** (each one can be toggled in the UI):
  - `ToolGuardrail`: SQL must be SELECT-only, and semantic queries must compile.
  - `OutputGuardrail`: the final query must execute or the model retries.
  - `SystemReminders`, `Planning` and `RepairToolArguments`.

## Scoring

The eval framework is [Pydantic Evals](https://pydantic.dev/docs/ai/evals/). Each prompt is a `Case`, checked by:
- `ExecutesOK`: the query runs;
- `ResultMatches`: only when you give an expected SQL; compares result values ignoring row order. The sample prompts' expected SQL encodes the semantic layer's definitions and returns the same results;
- `LLMJudge`: grades against your rubric.

A case passes only if every check passes.

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

## MLflow

Every matrix is also recorded in MLflow, so results outlive the browser tab. To browse them:

```bash
uv run mlflow ui --backend-store-uri sqlite:///data/mlflow.db --port 5000
# open http://127.0.0.1:5000, experiment "text-to-sql-evals"
```

The two UIs split the work. This page launches matrices and compares cells live: the grid, per-prompt k/N, cost and the Pareto frontier.
MLflow keeps the history and is where you investigate:

- **One parent run per matrix**, holding the parameters, `prompts.json`, `rubric.md` and `summary.json`.
- **One child run per cell.** Each repeat logs pass rate, tokens, time and cost at step = run number. When the matrix finishes, the cell logs
  `pass_k`, `mean_pass_rate`, `flaky_prompts` and `mean_cost_per_question_usd`. Use MLflow's run comparison to chart these across matrices.
- **One trace per case**, linked to its cell's run, with the agent, model and tool spans. The eval checks (`ExecutesOK`, `ResultMatches`,
  `LLMJudge`, `passed`) are attached as feedback. Judge calls get their own traces (tag `kind=judge`). Add your own labels to traces in
  MLflow's UI to check how often the judge agrees with you.

The page links to the matrix run above the grid and to each cell's run in its detail header. MLflow's cost metrics use list prices from
`app/pricing.py`; prices edited in the page apply to the page only.

Settings (environment variables): `MLFLOW_TRACKING_URI` to use a tracking server instead of `data/mlflow.db`, `MLFLOW_UI_URL` if the UI isn't
at `http://127.0.0.1:5000`, `MLFLOW_EXPERIMENT_NAME`, and `EVAL_MLFLOW=off` to disable tracking. A tracking failure is logged and never
fails an eval. The API key is never logged.

## Files

- `app/warehouse/`: `01_raw.sql`, `02_staging.sql`, `03_marts.sql`
- `app/db.py`: warehouse build, read-only query helper, `list_tables` / `describe_table`
- `app/semantic/`: semantic models YAML and compiler
- `app/glossary.md`, `app/glossary.py`: business glossary and its keyword search (`uv run python -m app.glossary` shows the parsed entries and sample searches)
- `app/malloy/warehouse.malloy`: Malloy semantic model
- `app/malloy_bridge.py`, `malloy/compile.mjs`: Malloy → SQL compilation via the Node sidecar (schema-only in-memory DuckDB)
- `app/agent.py`: agent factory (raw / glossary / glossary search / semantic / Malloy) and the harness capability set
- `app/evals.py`: Pydantic Evals dataset, evaluators, report summary
- `app/tracking.py`: MLflow runs, traces and feedback for each matrix
- `app/main.py`: FastAPI routes (`/`, `/api/config`, and `/api/run`, which streams NDJSON)
- `static/index.html`: Tailwind UI
