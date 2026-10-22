# Semantic Layer Bench

**Which layer should an AI agent query to answer business questions: raw SQL, Malloy, or dbt MetricFlow?** This app measures it.

**Why it matters:** semantic layers promise more accurate answers than raw SQL. This puts numbers on accuracy, consistency and cost.

## How it works

Each eval is a matrix. Every cell answers the same questions over the same DuckDB warehouse. Results stream in live and are recorded in MLflow.

| Axis | Options |
|---|---|
| Model | Opus / Sonnet / Haiku |
| Harness | Off / on |
| Target | Raw SQL / dbt MetricFlow / Malloy |

**The warehouse:** 52 tables (raw → staging → marts) with seed data from 2024-01 to 2025-09.
It holds **no business metrics**: *net revenue*, *MRR* and *churn* are defined only in the semantic layers.
It also has traps: look-alike tables, a deprecated mart, cancelled orders, mixed currencies.

| Target | The agent… | Tools |
|---|---|---|
| Raw SQL | Discovers the schema itself | `list_tables`, `describe_table`, `run_sql` |
| dbt MetricFlow | Queries governed metrics from `dbt/`, and can define derived metrics from them | `query_metrics` |
| Malloy | Writes Malloy against `app/malloy/warehouse.malloy`, which has the same definitions as dbt | `run_malloy` |

**The harness** adds *process*, never business knowledge. The **Harness** tab shows exactly what each agent sees.

| Applies to | What it adds |
|---|---|
| Every target | Prompt caching, answer review, an output check, cite-the-run answers, lean tool results |
| Raw SQL | Deprecated-table lint, column docs for the reviewer |
| dbt MetricFlow | A compact catalog, inline metric definitions, result steps (top-N, share of total) |
| Malloy | A flat-output check |

## Results so far

Sonnet 5.5 on 22 questions, harness on:

| Target | Runs | Tokens per run | Pass |
|---|---|---|---|
| dbt MetricFlow | 3 | ~265k | 21 / 22 |
| Malloy | 3 | ~362k | 21 / 22 |
| Raw SQL | 1 | 616k | 12 / 22 |

**The big picture:** raw SQL misses are mostly business definitions, the gap a semantic layer closes.
Both semantic layers can express every question (`uv run python -m app.expressibility`), so their misses are agent errors.

## Scoring

| Measure | What it means |
|---|---|
| Pass | The query runs, the rows match the reference (order-insensitive, numbers within 0.5%), and the LLM judge agrees |
| pass^k | Share of questions passed in *every* run. Use Runs ≥ 3: one question moves the score 4.5 points |
| Net value | Accuracy against token cost at list prices (`app/pricing.py`), with a Pareto frontier and bootstrap intervals |
| Judge agreement | Cohen's κ between the judge and your own labels from the Labels tab |

**Held-out set (10 questions):** written before tuning, so don't tune against it.

## Run it

```bash
uv sync
(cd malloy && npm ci)   # Malloy compiler, Node 18+
uv run uvicorn app.main:app --reload --reload-dir app --reload-dir static
# open http://localhost:8000 and enter an Anthropic API key
```

| Task | Command or setting |
|---|---|
| Benchmark from the command line | `uv run python -m app.bench --runs 3` (needs `ANTHROPIC_API_KEY`) |
| Check every question is expressible | `uv run python -m app.expressibility` |
| Rebuild the warehouse | `uv run python -m app.db` (it also rebuilds itself when its SQL changes) |
| Use an MLflow server | `MLFLOW_TRACKING_URI` (default: `data/mlflow.db`) |
| Turn off tracking | `EVAL_MLFLOW=off` |

| Tab | Use it to |
|---|---|
| Run | Launch a matrix and compare cells live |
| History | Reopen and compare past matrices |
| Traces | Inspect each model and tool call |
| Labels | Grade answers yourself to check the judge |
| Harness | See each agent's prompt, tools and checks |
| MLflow ↗ | Open MLflow's own UI at `/mlflow` |

Your API key is sent with each request and never logged or stored, unless you tick "remember".

## Where things live

| Path | What |
|---|---|
| `app/warehouse/` | Warehouse SQL |
| `dbt/`, `app/metricflow_bridge.py` | MetricFlow project and compiler |
| `app/malloy/`, `app/malloy_bridge.py`, `malloy/` | Malloy model and compiler |
| `app/agent.py` | Agents and harness |
| `app/evals.py` | Questions, graders, reports |
| `app/tracking.py`, `app/history.py` | MLflow recording and read API |
| `app/main.py` | FastAPI server |
| `static/` | UI (ES modules, no build). Design tokens in `static/css/tokens.css` |
