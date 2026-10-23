# Semantic Layer Bench

**Which layer should an AI agent query to answer business questions: raw SQL (with or without a glossary), Malloy, or dbt MetricFlow?** This app measures it.

**Why it matters:** semantic layers promise more accurate answers than raw SQL. This puts numbers on accuracy, consistency and cost.

## How it works

Each eval is a matrix. Every cell answers the same prompt set over the same DuckDB warehouse. Within a repeat, every cell's prompts run concurrently, six agents at a time across the matrix; repeats run one after another. Results stream in per prompt and are recorded in MLflow.

| Axis | Options |
|---|---|
| Model | Opus / Sonnet / Haiku |
| Agent | Baseline / harness |
| Target | Raw SQL / Raw SQL + glossary / dbt MetricFlow / Malloy |

**The warehouse:** 52 tables (raw → staging → marts) with seed data from 2024-01 to 2025-09.
It holds **no business metrics**: *net revenue*, *MRR* and *churn* are defined only in the semantic layers (and, in words, the glossary).
It also has traps: look-alike tables, a deprecated mart, cancelled orders, mixed currencies.

| Target | The agent… | Tools |
|---|---|---|
| Raw SQL | Discovers the schema itself | `list_tables`, `describe_table`, `run_sql` |
| Raw SQL + glossary | Same, plus a short plain-language glossary of business terms (`app/glossary.md`), with no table or column names | Same as raw SQL |
| dbt MetricFlow | Queries governed metrics from `dbt/`, and can define derived metrics from them | `query_metrics` |
| Malloy | Writes Malloy against `app/malloy/warehouse.malloy`, which has the same definitions as dbt | `run_malloy` |

**Why the glossary target:** it separates *knowing the definitions* from *having a semantic layer enforce them*. If it closes most of the raw-SQL gap, the definitions mattered more than the query engine.

**The baseline** is the model with only the target's query tools: a way to reach the data, nothing guiding or checking it. **The harness** adds *process* on top, never business knowledge. It's one fixed set. The **Harness** tab compares the two step by step.

| Applies to | What it adds |
|---|---|
| Every target | Prompt caching, a tool guardrail, answer review, an output check, an answer checklist, cite-the-run answers, lean tool results |
| Raw SQL (both) | Deprecated-table lint, column docs for the reviewer |
| dbt MetricFlow | A compact catalog, inline metric definitions, result steps (top-N, share of total) |
| Malloy | A flat-output check |

## Results so far

Sonnet 5.5 on 22 prompts, harness on. These runs were graded by rows match *and* an LLM judge, which has since been removed:

| Target | Runs | Tokens per run | Pass |
|---|---|---|---|
| dbt MetricFlow | 3 | ~265k | 21 / 22 |
| Malloy | 3 | ~362k | 21 / 22 |
| Raw SQL | 1 | 616k | 12 / 22 |

**The big picture:** raw SQL misses are mostly business definitions, the gap a semantic layer closes.
Both semantic layers can express every prompt (`uv run python -m app.expressibility`), so their misses are agent errors.

## Scoring

| Measure | What it means |
|---|---|
| Pass | The final query runs and its rows match the reference SQL (order-insensitive, numbers within 0.5%) |
| pass^k | Share of prompts passed in *every* run. Use Runs ≥ 3: one prompt moves the score 4.5 points |
| Cost | The agent's tokens (reviewer included) at list prices (`app/pricing.py`), per prompt |

**Prompt sets:** `app/questions.py`. The held-out set (10 prompts) was written before tuning, so don't tune against it.

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
| Check every prompt is expressible, and that MetricFlow and Malloy agree on every metric | `uv run python -m app.expressibility` |
| Rebuild the warehouse | `uv run python -m app.db` (it also rebuilds itself when its SQL changes) |
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
| `app/questions.py` | Prompt sets and reference SQL |
| `app/evals.py` | Running and grading a prompt set, and running a matrix (UI and CLI) |
| `app/tracking.py`, `app/history.py` | MLflow recording and read API |
| `app/main.py` | FastAPI server |
| `static/` | UI (ES modules, no build). Design tokens in `static/css/tokens.css` |
