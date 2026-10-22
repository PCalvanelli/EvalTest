# Text-to-SQL Eval Playground

**Which layer should an AI agent query to answer business questions: raw SQL, Malloy, or dbt MetricFlow?** This app measures it.

**Why it matters:** semantic layers promise more accurate answers than raw SQL. This puts numbers on accuracy, consistency and cost.

## How it works

Each eval is a matrix: **model** (Opus / Sonnet / Haiku) × **harness** (off / on) × **target** (raw SQL / MetricFlow / Malloy).
Every cell answers the same questions over the same DuckDB warehouse. Results stream in live and are recorded in MLflow.

- **Warehouse:** 52 tables (raw → staging → marts) with seed data from 2024-01 to 2025-09.
  It holds **no business metrics**: *net revenue*, *MRR* and *churn* are defined only in the semantic layers.
  It also has traps: look-alike tables, a deprecated mart, cancelled orders, mixed currencies.
- **Raw SQL agent:** discovers the schema with `list_tables`, `describe_table` and `run_sql`.
- **MetricFlow agent:** queries governed metrics from `dbt/` with `query_metrics`, and can define derived metrics from them.
- **Malloy agent:** writes Malloy against `app/malloy/warehouse.malloy`, which has the same definitions as dbt.
- **Harness:** adds *process* (caching, answer review, checks, leaner tool results) but never business knowledge.
  Each target also gets its own checks. The **Harness** tab shows exactly what each agent sees.

## Results so far

Sonnet 5.5, 22 questions, harness on, 3 runs:

| Target | Tokens per run | Pass |
|---|---|---|
| dbt MetricFlow | ~265k | 21 / 22 |
| Malloy | ~362k | 21 / 22 |
| Raw SQL (1 run) | 616k | 12 / 22 |

**The big picture:** raw SQL misses are mostly business definitions, the gap a semantic layer closes.
Both semantic layers can express every question (`uv run python -m app.expressibility`), so their misses are agent errors.

## Scoring

- **A case passes** only if the query runs, the rows match the reference (order-insensitive, numbers within 0.5%), and
  an LLM judge agrees.
- **pass^k:** the share of questions passed in *every* run. Use Runs ≥ 3; a single question moves the score 4.5 points.
- **Cost:** tokens are priced per model (`app/pricing.py`). The UI shows net value per question, a Pareto frontier,
  and bootstrap intervals.
- **Held-out set (10 questions):** written before tuning. Don't tune against it.
- **Labels tab:** grade answers yourself to measure how far the judge can be trusted (Cohen's κ).

## Run it

```bash
uv sync
(cd malloy && npm ci)   # Malloy compiler, Node 18+
uv run uvicorn app.main:app --reload --reload-dir app --reload-dir static
# open http://localhost:8000 and enter an Anthropic API key
```

From the command line: `uv run python -m app.bench --runs 3` (needs `ANTHROPIC_API_KEY`).

- **Tabs:** Run, History, Traces, Labels, Harness, plus MLflow's own UI at `/mlflow`.
- **Data:** the warehouse rebuilds itself into `data/` when its SQL changes, and MLflow uses `data/mlflow.db`.
- **Settings:** `MLFLOW_TRACKING_URI`, `MLFLOW_EXPERIMENT_NAME`, `EVAL_MLFLOW=off`.
- **API key:** sent per request and never logged or stored, unless you tick "remember".

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
