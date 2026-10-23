# Semantic Layer × Harness Bench

**How much of an AI analytics agent's accuracy comes from the model, from the data interface it queries, and from the harness around it, and how do the three interact?** This app measures all three at once, against an eval with frozen correct answers.

**What kind of benchmark this is:** a configuration benchmark, not a model leaderboard or a semantic-layer shootout. A *setup* is one model × interface × harness. Every setup answers the same prompts over the same warehouse, under the same budget, graded the same way. Comparing setups that differ in one factor isolates that factor.

| Benchmark kind | Holds fixed | Varies | Answers |
|---|---|---|---|
| Text-to-SQL / model benchmarks (e.g. Spider) | The interface (a raw schema) and the scaffolding | The model | Which model writes the best SQL? |
| Semantic-layer benchmarks | The agent | The interface | Does a semantic layer help? |
| **This bench** | Prompts, warehouse, grading, budgets | Model × interface × harness | How much each factor adds, and whether they interact: does a semantic layer matter less with a good harness, or a harness less with a strong model? |

**Why it matters:** a semantic layer and a harness both promise accuracy, at different costs. One is modeling work up front; the other is tokens and latency on every question. Knowing which buys more, for which model, tells you where to invest.

## The three factors

| Factor | Levels | What it isolates |
|---|---|---|
| Model | Opus / Sonnet / Haiku 5.5 | Raw model capability |
| Data interface (*target*) | Raw SQL / Raw SQL + glossary / dbt MetricFlow / Malloy | Business knowledge: absent, given in words, or enforced by a semantic layer |
| Harness | Baseline / harness | Process around the model (guardrails, review, retries), never business knowledge |

Each run is a matrix of setups. Within a repeat, every setup's prompts run concurrently, six agents at a time across the matrix; repeats run one after another. Results stream in per prompt and are recorded in MLflow. To read one factor's effect, compare two setups that differ only in it: the **Compare** panel gives the paired difference with a 95% interval (see Scoring).

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

**How it's built:** agents run on [Pydantic AI](https://pydantic.dev/docs/ai/overview/), the harness checks are [Pydantic AI Harness](https://pydantic.dev/docs/ai/harness/) guardrails, our own runner grades, and MLflow records. The Docs tab's *How the evals work* page explains each, and how this maps to [Pydantic Evals](https://pydantic.dev/docs/ai/evals/evals/).

## Results so far

Sonnet 5.5 on 22 prompts, harness on: one model and one harness level, so this shows the interface effect only. These runs were graded by rows match *and* an LLM judge, which has since been removed:

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
| Pass | The final query runs and its rows match the prompt's frozen reference rows (order-insensitive, numbers within 0.5%) |
| Partial | A fail, labeled separately: every reference column matches, but the answer adds columns nobody asked for |
| 95% CI | Likely range of a pass rate on another set of similar prompts (bootstrap over prompts). About ±18 points with 22 prompts |
| Compare | Two setups prompt by prompt: difference with its 95% interval, plus a McNemar test. *Clear* when the interval leaves out zero |
| pass^k | Share of prompts passed in *every* run. Use Runs ≥ 3: one prompt moves the score 4.5 points |
| Cost | The agent's tokens (reviewer included) at each model's prices from Settings (defaults: Anthropic list prices), per prompt |
| Budget | Every setup gets the same caps per prompt: 15 tool calls, 250k tokens (agent and reviewer together) and 180 s. A case over a cap fails as *over budget* or *timed out* |
| Failure label | Why each failed answer failed. Rules label budget, never-ran, made-up names, extra columns and ambiguous references at grading time; `app.labels` has an LLM (Haiku) label wrong rows: join path, fan-out, metric definition, filter/time/grain. Labels never change pass/fail |
| Infra error | A provider outage (rate limit, overload, 5xx, connection) or a Malloy compiler crash. Retried twice, then left out of pass rate and pass^k, and counted separately |

**Prompt sets:** `app/questions.py`. Each prompt has a stable id (`b01`, `h01`), a difficulty and tags. The held-out set (10 prompts) was written before tuning, so don't tune against it.

**Frozen references:** `gold/gold.json` holds each prompt's reference rows, the warehouse build they came from and a checksum. A matrix won't start if it's missing, hand-edited, from another build, or older than a prompt's reference SQL. Re-freeze after changing either.

**Benchmark version:** `BENCHMARK_VERSION` in `app/evals.py`. Bump it when what a pass means changes (including `REL_TOL` or anything else in the comparator, `app/db.compare_rows`); History marks matrices from other versions as not comparable. Every matrix also logs its git SHA, warehouse build, reference checksum and tool versions.

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
| Benchmark from the command line | `uv run python -m app.bench --harness on off --runs 3` (each run shuffles the prompt order; `--seed` fixes it, and the seed is recorded) |
| Check every prompt is expressible, that MetricFlow and Malloy agree on every metric, and that the glossary defines every governed metric | `uv run python -m app.expressibility` |
| Rebuild the warehouse | `uv run python -m app.db` (it also rebuilds itself when its SQL changes) |
| Freeze reference results (after changing prompts or the warehouse) | `uv run python -m app.gold freeze` |
| Check the frozen results still match the reference SQL | `uv run python -m app.gold verify` |
| Label why a past matrix's wrong-rows answers failed (LLM, about $0.01 per answer) | `uv run python -m app.labels <matrix id>` (`--relabel` redoes them) |
| Run the tests | `uv run pytest` and `node --test tests/*.test.mjs` |
| Use an MLflow server | `MLFLOW_TRACKING_URI` (default: `data/mlflow.db`) |
| Turn off tracking | `EVAL_MLFLOW=off` |

| Tab | Use it to |
|---|---|
| Run | Launch a matrix and compare cells live |
| History | Reopen and compare past matrices |
| Harness | See each agent's prompt, tools and checks |
| Settings | Pick the models on offer and their prices, concurrency and the failure labeler (saved locally in `data/settings.json`). API keys and limits show read-only |
| MLflow ↗ | Inspect traces (each model and tool call) in MLflow's own UI at `/mlflow` |

## Where things live

| Path | What |
|---|---|
| `app/warehouse/` | Warehouse SQL |
| `dbt/`, `app/metricflow_bridge.py` | MetricFlow project and compiler |
| `app/malloy/`, `app/malloy_bridge.py`, `malloy/` | Malloy model and compiler |
| `app/agent.py` | Agents and harness |
| `app/questions.py`, `app/gold.py`, `gold/` | Prompt sets, reference SQL and frozen reference results |
| `app/evals.py` | Running and grading a prompt set, and running a matrix (UI and CLI) |
| `app/labels.py` | Failure labels: rules, and the LLM labeler |
| `app/settings.py` | Local settings and their defaults (models, prices, concurrency, labeler) |
| `app/tracking.py`, `app/history.py` | MLflow recording and read API |
| `app/main.py` | FastAPI server |
| `static/` | UI (ES modules, no build). Design tokens in `static/css/tokens.css` |
