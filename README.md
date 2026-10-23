# Semantic Layer Bench

**Which layer should an AI agent query to answer business questions: raw SQL, Malloy, or dbt MetricFlow?** This app measures it.

**Why it matters:** semantic layers promise more accurate answers than raw SQL. This puts numbers on accuracy, consistency and cost.

## How it works

Each eval is a matrix. Every cell answers the same questions over the same DuckDB warehouse. Within a repeat, every cell's questions run concurrently, six agents at a time across the matrix; repeats run one after another. Results stream in per question and are recorded in MLflow.

| Axis | Options |
|---|---|
| Model | Opus / Sonnet / Haiku |
| Agent | Baseline / harness |
| Target | Raw SQL / dbt MetricFlow / Malloy |

**The warehouse:** 52 tables (raw → staging → marts) with seed data from 2024-01 to 2025-09.
It holds **no business metrics**: *net revenue*, *MRR* and *churn* are defined only in the semantic layers.
It also has traps: look-alike tables, a deprecated mart, cancelled orders, mixed currencies.

| Target | The agent… | Tools |
|---|---|---|
| Raw SQL | Discovers the schema itself | `list_tables`, `describe_table`, `run_sql` |
| dbt MetricFlow | Queries governed metrics from `dbt/`, and can define derived metrics from them | `query_metrics` |
| Malloy | Writes Malloy against `app/malloy/warehouse.malloy`, which has the same definitions as dbt | `run_malloy` |

**The baseline** is the model with only the target's query tools: a way to reach the data, nothing guiding or checking it. **The harness** adds *process* on top, never business knowledge. It's one fixed set. The **Harness** tab compares the two step by step.

| Applies to | What it adds |
|---|---|
| Every target | Prompt caching, a tool guardrail, answer review, an output check, an answer checklist, cite-the-run answers, lean tool results |
| Raw SQL | Deprecated-table lint, column docs for the reviewer |
| dbt MetricFlow | A compact catalog, inline metric definitions, result steps (top-N, share of total) |
| Malloy | A flat-output check |

## Results so far

Sonnet 5.5 on 22 questions, harness on. These runs were graded by rows match *and* an LLM judge, which has since been removed:

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
| Pass | The final query runs and its rows match the question's frozen reference rows (order-insensitive, numbers within 0.5%) |
| pass^k | Share of questions passed in *every* run. Use Runs ≥ 3: one question moves the score 4.5 points |
| Cost | The agent's tokens (reviewer included) at list prices (`app/pricing.py`), per question |

**Questions:** `app/questions.py`. Each has a stable id (`b01`, `h01`), a difficulty and tags. The held-out set (10 questions) was written before tuning, so don't tune against it.

**Frozen references:** `gold/gold.json` holds each question's reference rows, the warehouse build they came from and a checksum. A matrix won't start if it's missing, hand-edited, from another build, or older than a question's SQL. Re-freeze after changing either.

**Benchmark version:** `BENCHMARK_VERSION` in `app/evals.py`. Bump it when what a pass means changes; History marks matrices from other versions as not comparable. Every matrix also logs its git SHA, warehouse build, reference checksum and tool versions.

## Run it

```bash
uv sync
(cd malloy && npm ci)   # Malloy compiler, Node 18+
export ANTHROPIC_API_KEY=sk-ant-...
uv run uvicorn app.main:app --reload --reload-dir app --reload-dir static
# open http://localhost:8000
```

| Task | Command or setting |
|---|---|
| Benchmark from the command line | `uv run python -m app.bench --harness on off --runs 3` |
| Check every question is expressible, and that MetricFlow and Malloy agree on every metric | `uv run python -m app.expressibility` |
| Rebuild the warehouse | `uv run python -m app.db` (it also rebuilds itself when its SQL changes) |
| Freeze reference results (after changing questions or the warehouse) | `uv run python -m app.gold freeze` |
| Check the frozen results still match the reference SQL | `uv run python -m app.gold verify` |
| Use an MLflow server | `MLFLOW_TRACKING_URI` (default: `data/mlflow.db`) |
| Turn off tracking | `EVAL_MLFLOW=off` |

| Tab | Use it to |
|---|---|
| Run | Launch a matrix and compare cells live |
| History | Reopen and compare past matrices |
| Harness | See each agent's prompt, tools and checks |
| MLflow ↗ | Inspect traces (each model and tool call) in MLflow's own UI at `/mlflow` |

## Where things live

| Path | What |
|---|---|
| `app/warehouse/` | Warehouse SQL |
| `dbt/`, `app/metricflow_bridge.py` | MetricFlow project and compiler |
| `app/malloy/`, `app/malloy_bridge.py`, `malloy/` | Malloy model and compiler |
| `app/agent.py` | Agents and harness |
| `app/questions.py`, `app/gold.py`, `gold/` | Questions, reference SQL and frozen reference results |
| `app/evals.py` | Running and grading a question set, and running a matrix (UI and CLI) |
| `app/tracking.py`, `app/history.py` | MLflow recording and read API |
| `app/main.py` | FastAPI server |
| `static/` | UI (ES modules, no build). Design tokens in `static/css/tokens.css` |
