"""Text-to-SQL agent, built with or without Pydantic AI Harness capabilities, over raw SQL, dbt MetricFlow or Malloy."""

from __future__ import annotations

import inspect
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Literal

from anthropic import AsyncAnthropic
from pydantic import BaseModel, Field, create_model
from pydantic_ai import Agent, ModelRetry, RunContext, ToolOutput
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.models.anthropic import AnthropicModel, AnthropicModelSettings
from pydantic_ai.providers.anthropic import AnthropicProvider
from pydantic_ai_harness import GuardrailResult, OutputGuardrail, Planning, SystemReminders, ToolGuardrail
from pydantic_ai_harness.guardrails import ToolCallInfo
from pydantic_ai_harness.repair_tool_arguments import RepairToolArguments
from pydantic_ai_harness.system_reminders import Reminder

from . import db, malloy_bridge, metricflow_bridge
from .metricflow_bridge import MetricDefinition, MetricFlowQuery, MetricFlowQueryWithSteps


class SQLAnswer(BaseModel):
    sql: str = Field(description='The final DuckDB SELECT query that answers the question.')
    explanation: str = Field(description='One or two sentences on how the query answers the question.')


class MetricFlowAnswer(BaseModel):
    query: MetricFlowQuery = Field(description='The final MetricFlow query that answers the question.')
    explanation: str = Field(description='One or two sentences on how the query answers the question.')


def _metricflow_answer_type(define: bool, steps: bool) -> type[BaseModel]:
    """The MetricFlow answer for the enabled harness tools: query-time metric definitions and post-processing steps
    travel with the query, so the compiled SQL is reproducible and reviewable. Baseline gets the plain answer."""
    if not define and not steps:
        return MetricFlowAnswer
    fields: dict = {'query': (MetricFlowQueryWithSteps if steps else MetricFlowQuery,
                              Field(description='The final MetricFlow query that answers the question.'))}
    if define:
        fields['metric_definitions'] = (list[MetricDefinition], Field(
            default_factory=list, description='Every metric you defined with define_metric that the query uses.'))
    return create_model('MetricFlowAnswerPlus', __base__=MetricFlowAnswer, **fields)


class MalloyAnswer(BaseModel):
    malloy: str = Field(description='The final Malloy query (a `run:` statement) that answers the question.')
    explanation: str = Field(description='One or two sentences on how the query answers the question.')


INSTRUCTIONS = f"""\
You answer questions by writing DuckDB SQL against the company's data warehouse ({len(db.list_tables())} tables
across several schemas). The schema is not given to you up front. Use the tools:
- `list_tables(schema=None)`: every table with its row count and documentation.
- `describe_table(table)`: columns, types, column documentation and a few sample rows.
- `run_sql(sql)`: run a read-only query; returns up to {db.MAX_ROWS} rows.
Always use schema-qualified table names (schema.table). Return the final query and a short explanation.
"""

# The semantic layer's private views (app/warehouse/04_semantic.sql) are off limits to the raw-SQL agent.
_SEMANTIC_SCHEMA = re.compile(r'\bsemantic\s*\.', re.IGNORECASE)

METRICFLOW_INSTRUCTIONS = f"""\
You answer natural-language questions using the company's dbt Semantic Layer (MetricFlow). You cannot write SQL and
you cannot see the warehouse tables: express every answer as a MetricFlow query of governed metrics, grouped and
filtered by dimensions. You have a `query_metrics` tool that compiles a query with MetricFlow, runs it, and returns
the generated SQL plus up to {db.MAX_ROWS} rows (or MetricFlow's error, with suggestions). Return the final query and a
short explanation. If the question cannot be expressed with these metrics, return the closest query and say so.

MetricFlow quick reference:
- `metrics`: metric names from the catalog. Metrics in one query must share the group-bys you ask for.
- `group_by`: `entity__dimension` names from the catalog (e.g. `customer__customer_segment`), or
  `metric_time__<grain>` (day, week, month, quarter, year) for the metric's own time axis.
- `where`: SQL expressions with Jinja references, ANDed together:
  `{{{{ Dimension('order__sales_channel') }}}} = 'web'`,
  `{{{{ TimeDimension('metric_time', 'day') }}}} >= '2025-01-01'`,
  `{{{{ TimeDimension('metric_time', 'month') }}}} = '2025-06-01'`, `{{{{ Entity('customer') }}}} = 42`.
  You may filter on any dimension in the metric's list, whether or not you group by it.
- `order_by`: metric or group-by names, `-name` for descending. `limit`: a row count.
"""
# The catalog goes last, in full or (with the harness's compact catalog) with each entity's dimensions described once.
METRICFLOW_CATALOG = '\n<metrics_catalog>\n{catalog}\n</metrics_catalog>\n'

# Added to the MetricFlow instructions when the harness gives the agent define_metric.
POST_STEPS_INSTRUCTIONS = """
For per-group winners (the top product per category, the lowest day per warehouse) or a share of a total, add
`post_process` steps to the query: `rank_filter` (partition_by, order_by with '-' for highest first, keep; ties are
kept), `share_of_total` (metric, optional partition_by, name) and `select` (columns, to drop columns the question
didn't ask for, such as a metric_time that an offset metric requires) and `drop_nulls` (columns; for example, drop an
unknown-customer row after computing shares over all rows). Steps run on the result rows in order and can only use the
query's own group-bys and metrics.
"""

DEFINE_METRIC_INSTRUCTIONS = """
When no governed metric answers the question directly, define one in the `define` list of `query_metrics`, built only
from existing metrics: `derived` (an expression over inputs; `offset_window` reads an input from an earlier period, for
period-over-period comparisons), `cumulative` (running totals, rolling `window`s such as '3 months', or
`grain_to_date` such as year for year-to-date) or `ratio` (numerator / denominator, each optionally filtered), and use
it in the same query's metrics. MetricFlow validates it first; it stays defined for later queries.{listing}
Offset and cumulative metrics need `metric_time` in the group_by. Cumulative windows are anchored on days, so for a
trailing N-month figure at month grain, use a derived metric over offset inputs instead (e.g. the current month plus
`offset_window` '1 month' and '2 months').
"""
DEFINE_LISTING = ' List every metric you defined that the final query uses in `metric_definitions` of your final answer.'

# Added for every target when the harness's answer-by-reference is on: the final answer cites a run instead of
# rewriting the query, and can go in the same response as the query.
ANSWER_BY_REF_INSTRUCTIONS = """
Every `{tool}` result has a run number. Finish by calling `answer` with the run that answers the question and a short
explanation of what that query does; don't rewrite the query. When you are confident of the query, send it and
`answer` (omit `run` to cite your latest query) in the same response: the harness runs the query first, checks the
result, and shows you the rows if anything needs fixing. Don't quote numbers in the explanation that you haven't seen,
and don't re-run a query you've already run.
"""

# Added for every target with the harness's answer checklist: the reviewer's fit checks, given to the agent up front so
# the first answer passes review instead of costing a round trip.
ANSWER_CHECKLIST_INSTRUCTIONS = """
Before you answer, check your query against the question the way a reviewer will:
- Columns: exactly what the question asks for. No id or key columns, helper columns or extra metrics nobody asked
  for, and nothing missing.
- Rows: only the requested grain, groups, time window and top-N. When the question asks about one item or segment,
  filter or limit to it instead of returning every group.
- Fields: when the catalog or model already has a field or measure for a concept in the question (a flag, a
  sequence, a metric), use it instead of rebuilding the concept from other fields.
- Explanation: it describes only the final query. Every filter, exclusion or definition it mentions is in that query.
"""

MALLOY_INSTRUCTIONS = f"""\
You answer natural-language questions by writing Malloy queries against the company's Malloy semantic model
(below). You cannot write SQL and you cannot see the warehouse tables. The model's measures carry the company's
governed business definitions: prefer them over re-deriving metrics from raw fields.
You have a `run_malloy(query)` tool that compiles a Malloy query, runs it, and returns the generated SQL plus up
to {db.MAX_ROWS} rows (or the compiler error). Return the final Malloy query and a short explanation.

Malloy quick reference:
- A query: `run: <source> -> {{ group_by: a, b; aggregate: m1, m2; where: ...; order_by: m1 desc; limit: 5 }}`
- Fields of joined sources use dots: `customer.customer_segment`, `products.category`, `orders.net_revenue`.
- Ad-hoc fields: `group_by: m is order_date.month` (also .day/.week/.quarter/.year); `aggregate: n is count()`,
  `x is sum(a - b)`, `y is amount.sum() {{ where: status = 'paid' }}`.
- Time literals and ranges: `@2025`, `@2025-Q3`, `@2025-06`, `@2025-06-30`; `where: order_date ? @2024` keeps
  2024, `where: d >= @2025-01-01 and d < @2025-04-01`. Alternatives: `status = 'solved' | 'closed'`.
- Nulls: `x is null`, `x is not null`. Case: `pick 'a' when cond else 'b'`.
- Filter on an aggregate with `having: m1 > 100`. Nest sub-results: `nest: by_plan is {{ group_by: plan_name; aggregate: mrr }}`.
- Pipelines run a second stage on the first stage's output: `run: s -> {{ group_by: k; aggregate: m }} -> {{ aggregate: total is m.sum() }}`.
- Names that are Malloy keywords (e.g. date, month) must be backticked: `\`date\``.

<malloy_model>
{malloy_bridge.MODEL_TEXT}
</malloy_model>
"""

# Capabilities the harness mode can switch on. Keys are what the UI sends; `default` ones are pre-selected.
# `target` says which business context a capability serves: 'all' is the shared core, the others are the
# target-specific harnesses (each target has its own failure modes and rules). A cell gets the core plus its own
# target's group. The harness adds process (caching, checks, review, tools, retries), never business knowledge:
# definitions belong to the business-context axis, so mixing them in here would confound the comparison.
LEAN_MAX_ROWS = 20
HARNESS_CAPABILITIES: dict[str, dict] = {
    'prompt_cache': {'target': 'all', 'default': True, 'label': 'Prompt caching: re-read the instructions, tool '
                     'definitions and conversation from cache each turn (0.1x input price) instead of re-billing them'},
    'answer_review': {'target': 'all', 'default': True, 'label': 'Answer review: before answering, a fresh reviewer '
                      'checks the final query, explanation and rows against the question with a checklist for the '
                      'target, and sends concrete problems back (max 2)'},
    'output_check': {'target': 'all', 'default': True, 'label': 'Output check: the final query must execute and '
                     'return rows, otherwise the model retries'},
    'sql_guardrail': {'target': 'all', 'default': True, 'label': "Tool guardrail: block non-SELECT SQL / queries "
                      "that don't compile before they run"},
    'answer_by_ref': {'target': 'all', 'default': True, 'label': 'Cite the run: query results are numbered and the '
                      'final answer cites a run instead of rewriting the query, so it can go in the same response as '
                      'the query (one turn for a straightforward question)'},
    'answer_checklist': {'target': 'all', 'default': True, 'label': "Answer checklist: the agent gets the reviewer's "
                         'fit checks (columns, rows, explanation) up front, so the first answer passes review'},
    'lean_results': {'target': 'all', 'default': True, 'label': 'Lean tool results: no generated SQL, at most '
                     f'{LEAN_MAX_ROWS} rows, and a repeated query points at its earlier run instead of re-sending rows'},
    'reminders': {'target': 'all', 'default': False, 'label': 'System reminders: re-inject "verify by running the '
                  'query" each turn (adds tokens)'},
    'planning': {'target': 'all', 'default': False, 'label': 'Planning: a write_plan tool to outline the query first '
                 '(adds a turn)'},
    'repair_args': {'target': 'all', 'default': False, 'label': 'Repair tool arguments: fix malformed JSON tool args'},
    'raw_deprecations': {'target': 'raw', 'default': True, 'label': 'Deprecation lint: flag tables the warehouse '
                         'documents as DEPRECATED, in run_sql and in the final query'},
    'raw_column_docs': {'target': 'raw', 'default': True, 'label': 'Column documentation: the reviewer sees the '
                        'documentation of every column the final query uses (works with answer review)'},
    'mf_compact_catalog': {'target': 'metricflow', 'default': True, 'label': 'Compact catalog: the same metrics '
                           'catalog with each entity\'s dimensions described once instead of under every metric group'},
    'mf_define_metric': {'target': 'metricflow', 'default': True, 'label': 'Define metrics: query_metrics can '
                         'define a validated derived, cumulative or ratio metric from governed metrics when none fits '
                         '(period-over-period, running and rolling totals, filtered shares)'},
    'mf_post_steps': {'target': 'metricflow', 'default': True, 'label': 'Result steps: per-group top-N and share of '
                      'total applied to the governed result rows (like a BI tool on the semantic layer)'},
    'malloy_flat_output': {'target': 'malloy', 'default': True, 'label': 'Flat output: the final result must not '
                           'contain nested rows (from nest:); the model flattens it'},
}
TARGET_NAMES = {'all': 'Every target', 'raw': 'Raw SQL', 'metricflow': 'dbt MetricFlow', 'malloy': 'Malloy'}
MAX_REVIEWS = 2
MAX_EMPTY_RETRIES = 1
MAX_EXEC_RETRIES = 2
MAX_LINT_RETRIES = 1

REVIEW_INSTRUCTIONS = """\
You review a data analyst's final answer before it goes to the person who asked the question. You do not know the
company's business definitions and must not invent or assume any. Check only:
1. Consistency: does the explanation claim anything the query does not actually do (a filter, an exclusion, a join,
   a time window, a definition)?
2. Fit: do the result columns and rows answer the question as asked: the requested grain and grouping, time window,
   ordering or top-N, and the requested columns with no extra ones (including id or key columns nobody asked for)
   and none missing?
3. Plausibility: are the rows sensible for the question (not empty, no unexpected NULLs, believable signs and
   magnitudes)?
{target_rules}
If the explanation says the question cannot be fully expressed with the tools available and the query is the closest
available answer, accept it: sending it back cannot fix that.
Report only concrete, fixable problems, one sentence each, saying what to change. If the answer is acceptable,
return ok=true and no issues. Do not ask for stylistic changes."""

# The target-specific part of the reviewer's checklist.
REVIEW_TARGET_RULES = {
    'raw': """\
4. Column use: when column documentation is given, does the query use each column consistently with its
   documentation and with what the explanation says it computes?
5. Window functions: is any filter applied before a window function that needs rows outside the filter (for
   example, filtering to 2025 before LAG removes December 2024, so January has no prior month)?""",
    'metricflow': """\
4. Query-time metrics: if the answer defines metrics, does each definition compute what the question asks (offset
   direction and size, window length, period-to-date grain, ratio numerator and denominator and their filters)?
5. Result steps: does each post_process step rank or total within the right partition, in the right direction,
   keeping the right number of rows?""",
    'malloy': """\
4. Calculations across periods: is any where-filter applied in the same stage as a calculation that needs earlier
   periods (a filter to 2025 removes December 2024, so January has no prior month)?""",
}


class Review(BaseModel):
    ok: bool = Field(description='True when the answer is acceptable as is.')
    issues: list[str] = Field(default_factory=list, description='Concrete, fixable problems; empty when ok.')


@dataclass
class HarnessLog:
    """What the harness did during one case, for reporting and cost: reviews run, answers sent back, reviewer usage."""

    reviews: int = 0
    sent_back: int = 0
    empty_retries: int = 0
    exec_retries: int = 0
    lint_retries: int = 0
    lints: list[str] = field(default_factory=list)
    defined_metrics: int = 0
    reviewer_usage: dict[str, int] = field(default_factory=dict)

    def add_usage(self, u) -> None:  # noqa: ANN001
        for k in ('input_tokens', 'output_tokens', 'cache_read_tokens', 'cache_write_tokens', 'requests'):
            self.reviewer_usage[k] = self.reviewer_usage.get(k, 0) + (getattr(u, k, 0) or 0)


def _block_writes(call: ToolCallInfo) -> GuardrailResult:
    sql = str(call.args.get('sql', ''))
    stripped = sql.strip().lower()
    if not stripped.startswith(('select', 'with')) or ';' in stripped.rstrip(';'):
        return GuardrailResult.block('Only a single read-only SELECT/WITH query is allowed.')
    return GuardrailResult.allow()


def _block_uncompilable_metricflow(definitions: list[MetricDefinition]):
    def guard(call: ToolCallInfo) -> GuardrailResult:
        try:
            new = [MetricDefinition.model_validate(d) for d in call.args.get('define') or []]
        except Exception:
            return GuardrailResult.allow()  # the tool reports bad definitions itself
        names = {d.name for d in new}
        try:
            metricflow_bridge.compile(MetricFlowQueryWithSteps.model_validate(call.args['query']),
                                      [d for d in definitions if d.name not in names] + new)
        except Exception as e:
            return GuardrailResult.block(f'MetricFlow query does not compile: {e}')
        return GuardrailResult.allow()

    return guard


async def _block_uncompilable_malloy(call: ToolCallInfo) -> GuardrailResult:
    try:
        await malloy_bridge.compile(str(call.args.get('query', '')))
    except Exception as e:
        return GuardrailResult.block(f'Malloy query does not compile: {e}')
    return GuardrailResult.allow()


async def answer_sql(output: SQLAnswer | MetricFlowAnswer | MalloyAnswer) -> str:
    """The SQL an answer runs, compiling it first when it came from MetricFlow or Malloy."""
    if isinstance(output, SQLAnswer):
        return output.sql
    if isinstance(output, MalloyAnswer):
        return await malloy_bridge.compile(output.malloy)
    return metricflow_bridge.compile(output.query, getattr(output, 'metric_definitions', ()))


def _answer_text(output: SQLAnswer | MetricFlowAnswer | MalloyAnswer, sql: str) -> str:
    """The final query as the agent wrote it, plus the SQL it compiles to when that differs."""
    if isinstance(output, MetricFlowAnswer):
        defs = [d.model_dump(exclude_none=True, exclude_defaults=True) for d in getattr(output, 'metric_definitions', [])]
        defined = f'\n\nMetrics defined at query time:\n{json.dumps(defs, indent=2)}' if defs else ''
        return (f'MetricFlow query:\n{json.dumps(output.query.model_dump(exclude_defaults=True), indent=2)}{defined}'
                f'\n\nCompiled SQL:\n{sql}')
    if isinstance(output, MalloyAnswer):
        return f'Malloy query:\n{output.malloy}\n\nCompiled SQL:\n{sql}'
    return f'SQL:\n{sql}'


def _tables_and_columns(sql: str) -> tuple[set[str], set[str]]:
    """Warehouse tables (schema.table) and column names a query references, CTE names excluded."""
    import sqlglot
    from sqlglot import exp

    try:
        tree = sqlglot.parse_one(sql, read='duckdb')
    except Exception:
        return set(), set()
    ctes = {c.alias_or_name.lower() for c in tree.find_all(exp.CTE)}
    tables = {f'{t.db}.{t.name}'.lower() for t in tree.find_all(exp.Table) if t.db and t.name.lower() not in ctes}
    return tables, {c.name.lower() for c in tree.find_all(exp.Column)}


def _deprecated_tables(tables: set[str]) -> dict[str, str]:
    """Tables among these that the warehouse documents as deprecated, with their documentation."""
    docs = {t['table']: t['description'] for t in db.list_tables()}
    return {t: docs[t] for t in sorted(tables) if docs.get(t, '').upper().startswith('DEPRECATED')}


def _column_docs(sql: str) -> str:
    """Documentation of every documented column the query uses, from the tables it reads."""
    tables, columns = _tables_and_columns(sql)
    lines = []
    for table in sorted(tables):
        try:
            described = db.describe_table(table)
        except Exception:
            continue
        lines += [f'- {table}.{c["name"]}: {c["description"]}' for c in described['columns']
                  if c['name'].lower() in columns and c['description']]
    return '\n'.join(lines)


def _has_nested(rows: list) -> bool:
    return any(isinstance(v, (list, dict)) or (isinstance(v, str) and v[:1] in '[{' and v[-1:] in ']}')
               for row in rows for v in row)


def _final_answer_guard(enabled: Sequence[str], context: Context, reviewer: AnthropicModel | None, log: HarnessLog):
    """One output guard for the enabled checks, in order: the final query must run and return rows, then the target's
    lints, then a reviewer with fresh context checks it against the question. Every step is bounded, so a stubborn
    case ends with an answer on record, not a run error."""
    check, review = 'output_check' in enabled, 'answer_review' in enabled and reviewer is not None

    async def guard(ctx: RunContext, output: object) -> GuardrailResult:
        assert isinstance(output, (SQLAnswer, MetricFlowAnswer, MalloyAnswer))
        try:
            sql = await answer_sql(output)
            result = db.run_readonly(sql, max_rows=10)
        except Exception as e:
            if log.exec_retries >= MAX_EXEC_RETRIES:
                return GuardrailResult.allow()  # recorded as a failed answer, like the baseline, not a run error
            log.exec_retries += 1
            return GuardrailResult.retry(f'Your final query failed to execute: {e}. Fix it and try again.')
        rows = result['rows']
        blank = not rows or (len(rows) == 1 and all(v is None for v in rows[0]))
        if check and blank and log.empty_retries < MAX_EMPTY_RETRIES:
            log.empty_retries += 1
            return GuardrailResult.retry(
                'Your final query returns no data (no rows, or a single row of NULLs). Check filter values, date '
                'ranges and joins against the actual data. If an empty result really is the answer, keep the query '
                'and say so in the explanation.'
            )
        if lint := _target_lint(enabled, context, output, sql, rows, log):
            return GuardrailResult.retry(lint)
        if review and log.reviews < MAX_REVIEWS:
            log.reviews += 1
            docs = _column_docs(sql) if context == 'raw' and 'raw_column_docs' in enabled else ''
            instructions = REVIEW_INSTRUCTIONS.format(target_rules=REVIEW_TARGET_RULES[context])
            verdict = await Agent(reviewer, output_type=Review, instructions=instructions).run(
                f'Question:\n{ctx.prompt}\n\n{_answer_text(output, sql)}\n\nExplanation:\n{output.explanation}\n\n'
                + (f'Documentation of the columns the query uses:\n{docs}\n\n' if docs else '')
                + f'Result: {result["row_count"]} rows, columns {result["columns"]}. First rows:\n'
                f'{json.dumps(rows, default=str)}'
            )
            log.add_usage(verdict.usage)
            if not verdict.output.ok and verdict.output.issues:
                log.sent_back += 1
                issues = '\n'.join(f'- {i}' for i in verdict.output.issues)
                return GuardrailResult.retry(
                    f'A reviewer found problems with your answer:\n{issues}\nFix them, re-run the query, and answer '
                    'again. If you are sure an issue is mistaken, keep your answer and address it in the explanation.'
                )
        return GuardrailResult.allow()

    return guard


def _target_lint(enabled: Sequence[str], context: Context, output: object, sql: str, rows: list, log: HarnessLog) -> str | None:
    """The target's own rules for a final answer; returns a retry message, at most once per case."""
    if log.lint_retries >= MAX_LINT_RETRIES:
        return None
    message = None
    if context == 'raw' and 'raw_deprecations' in enabled and isinstance(output, SQLAnswer):
        if deprecated := _deprecated_tables(_tables_and_columns(sql)[0]):
            message = 'Your final query reads tables the warehouse documents as deprecated:\n' + '\n'.join(
                f'- {t}: {doc}' for t, doc in deprecated.items()) + '\nUse the current tables instead.'
    elif context == 'malloy' and 'malloy_flat_output' in enabled and _has_nested(rows):
        message = ('Your final result has nested rows (from nest:). Return a flat result: one row per group with '
                   'plain columns, using group_by and aggregate (or a pipeline stage) instead of nest.')
    if message:
        log.lint_retries += 1
        log.lints.append(message.splitlines()[0])
    return message


Context = Literal['raw', 'metricflow', 'malloy']
# What each context gives the agent. The UI shows these in the context tooltips and the reference tab.
CONTEXTS: dict[str, dict[str, str]] = {
    'raw': {
        'name': 'Raw SQL',
        'summary': 'No business context. The agent explores the warehouse and decides what business terms mean itself.',
        'writes': 'DuckDB SQL',
        'tools': 'list_tables, describe_table, run_sql',
    },
    'metricflow': {
        'name': 'dbt MetricFlow',
        'summary': 'The dbt Semantic Layer: governed metrics and dimensions, with joins handled by MetricFlow. '
                   'No warehouse tables, no SQL.',
        'writes': 'A MetricFlow query (metrics, group by, where, order, limit)',
        'tools': 'query_metrics',
    },
    'malloy': {
        'name': 'Malloy',
        'summary': 'A Malloy semantic model with the same governed definitions, joined sources and reusable measures. '
                   'No SQL.',
        'writes': 'A Malloy query',
        'tools': 'run_malloy',
    },
}


def target_capabilities(enabled: Sequence[str], context: Context) -> list[str]:
    """The enabled capabilities that apply to this target: the shared core plus the target's own group."""
    return [k for k in enabled if HARNESS_CAPABILITIES.get(k, {}).get('target') in ('all', context)]


def _build_capabilities(
    enabled: Sequence[str], context: Context, reviewer: AnthropicModel | None, log: HarnessLog,
    definitions: list[MetricDefinition],
) -> list[AbstractCapability]:
    tool, guard = {
        'metricflow': ('query_metrics', _block_uncompilable_metricflow(definitions)),
        'malloy': ('run_malloy', _block_uncompilable_malloy),
    }.get(context, ('run_sql', _block_writes))
    factories = {
        'sql_guardrail': lambda: ToolGuardrail(guard=guard, tools=[tool]),
        'reminders': lambda: SystemReminders(
            reminders=[
                Reminder(
                    f'Before answering, run your final query with {tool} and check the rows actually answer '
                    'the question (right grain, filters, ordering, and limit).'
                )
            ]
        ),
        'planning': lambda: Planning(tools=['write_plan']),
        'repair_args': lambda: RepairToolArguments(),
    }
    caps = [factories[name]() for name in enabled if name in factories]
    if 'output_check' in enabled or 'answer_review' in enabled:
        caps.append(OutputGuardrail(guard=_final_answer_guard(enabled, context, reviewer, log)))
    return caps


@dataclass
class QueryRuns:
    """The queries one case ran, numbered, so the final answer can cite one instead of rewriting it (answer by
    reference), and a repeated query can point at its earlier run (lean results)."""

    runs: list[dict] = field(default_factory=list)  # {'fields': answer fields, 'error': str | None}
    seen: dict[str, int] = field(default_factory=dict)  # query key -> run number of its first successful run
    latest: int | None = None  # the run the last tool result showed

    def add(self, key: str, fields: dict, error: str | None = None) -> int:
        self.runs.append({'fields': fields, 'error': error})
        self.latest = len(self.runs)
        if error is None:
            self.seen.setdefault(key, self.latest)
        return self.latest

    def repeat_of(self, key: str) -> int | None:
        if (n := self.seen.get(key)) is not None:
            self.latest = n
        return n

    def cite(self, run: int | None) -> dict:
        n = self.latest if run is None else run
        if n is None:
            raise ModelRetry('No query has run yet. Run the query that answers the question, then cite it.')
        if not 1 <= n <= len(self.runs):
            raise ModelRetry(f'There is no run {n}; runs so far: 1 to {len(self.runs)}.')
        if error := self.runs[n - 1]['error']:
            raise ModelRetry(f'Run {n} failed ({error[:300]}). Fix the query, run it, and cite the run that works.')
        return self.runs[n - 1]['fields']


def _answer_tool(answer_type: type[BaseModel], runs: QueryRuns, tool: str) -> ToolOutput:
    def answer(explanation: str, run: int | None = None) -> BaseModel:
        """Give your final answer: the query run that answers the question, and a short explanation of it.

        Args:
            explanation: One or two sentences on how the query answers the question.
            run: The run number shown in the query result. Omit it to cite your latest query.
        """
        return answer_type(**runs.cite(run), explanation=explanation)

    answer.__doc__ = answer.__doc__.replace('the query run', f'the {tool} run')
    return ToolOutput(answer, name='answer')


def _query_key(value: object) -> str:
    return json.dumps(value, sort_keys=True, default=str)


def instruction_parts(context: Context, harness: Sequence[str]) -> list[dict]:
    """The agent's instructions in order, each part tagged with the capability that adds it (None for the target's
    own base instructions and catalog). build_agent joins them; the Harness view shows them apart."""
    harness = target_capabilities(harness, context)
    by_ref = 'answer_by_ref' in harness
    tool = {'metricflow': 'query_metrics', 'malloy': 'run_malloy'}.get(context, 'run_sql')
    parts = [{'source': None, 'title': 'Base instructions',
              'text': {'raw': INSTRUCTIONS, 'metricflow': METRICFLOW_INSTRUCTIONS, 'malloy': MALLOY_INSTRUCTIONS}[context]}]
    if context == 'metricflow':
        if 'mf_define_metric' in harness:
            parts.append({'source': 'mf_define_metric', 'title': 'Defining metrics',
                          'text': DEFINE_METRIC_INSTRUCTIONS.format(listing='' if by_ref else DEFINE_LISTING)})
        if 'mf_post_steps' in harness:
            parts.append({'source': 'mf_post_steps', 'title': 'Result steps', 'text': POST_STEPS_INSTRUCTIONS})
        compact = 'mf_compact_catalog' in harness
        catalog = metricflow_bridge.compact_catalog_text() if compact else metricflow_bridge.catalog_text()
        parts.append({'source': 'mf_compact_catalog' if compact else None,
                      'title': 'Metrics catalog (compact)' if compact else 'Metrics catalog',
                      'text': METRICFLOW_CATALOG.format(catalog=catalog)})
    if by_ref:
        parts.append({'source': 'answer_by_ref', 'title': 'Cite the run',
                      'text': ANSWER_BY_REF_INSTRUCTIONS.format(tool=tool)})
    if 'answer_checklist' in harness:
        parts.append({'source': 'answer_checklist', 'title': 'Answer checklist', 'text': ANSWER_CHECKLIST_INSTRUCTIONS})
    return parts


def make_model(model_name: str, api_key: str, workspace_id: str | None = None) -> AnthropicModel:
    """Anthropic model; keys not scoped to a workspace need the workspace id header."""
    headers = {'anthropic-workspace-id': workspace_id} if workspace_id else None
    client = AsyncAnthropic(api_key=api_key, default_headers=headers)
    return AnthropicModel(model_name, provider=AnthropicProvider(anthropic_client=client))


def build_agent(
    model_name: str,
    api_key: str,
    harness: Sequence[str] = (),
    workspace_id: str | None = None,
    context: Context = 'raw',
    log: HarnessLog | None = None,
) -> Agent[None, SQLAnswer] | Agent[None, MetricFlowAnswer] | Agent[None, MalloyAnswer]:
    """Build a fresh agent. `harness` lists enabled capability keys (empty = baseline). `context` picks what
    business knowledge it gets: none (raw SQL), or a semantic layer (dbt MetricFlow or Malloy) instead of SQL.
    `log` collects what the harness did (reviews, reviewer tokens) for the case report."""
    log = log if log is not None else HarnessLog()
    harness = target_capabilities(harness, context)
    reviewer = make_model(model_name, api_key, workspace_id) if 'answer_review' in harness else None
    cache = 'prompt_cache' in harness
    guarded = 'output_check' in harness or 'answer_review' in harness
    by_ref, lean = 'answer_by_ref' in harness, 'lean_results' in harness
    can_define = context == 'metricflow' and 'mf_define_metric' in harness
    steps = context == 'metricflow' and 'mf_post_steps' in harness
    definitions: list[MetricDefinition] = []  # metrics this case defined in query_metrics
    runs = QueryRuns()
    tool = {'metricflow': 'query_metrics', 'malloy': 'run_malloy'}.get(context, 'run_sql')
    answer_type = (_metricflow_answer_type(can_define, steps) if context == 'metricflow' else
                   {'malloy': MalloyAnswer}.get(context, SQLAnswer))
    instructions = ''.join(p['text'] for p in instruction_parts(context, harness))
    agent = Agent(
        make_model(model_name, api_key, workspace_id),
        output_type=_answer_tool(answer_type, runs, tool) if by_ref else answer_type,
        instructions=instructions,
        capabilities=_build_capabilities(harness, context, reviewer, log, definitions),
        model_settings=AnthropicModelSettings(
            anthropic_cache_instructions=True, anthropic_cache_tool_definitions=True, anthropic_cache_messages=True
        ) if cache else None,
        # Output checks retry through the same budget as tool errors; leave room so they never turn into hard failures.
        retries=MAX_EXEC_RETRIES + MAX_EMPTY_RETRIES + MAX_LINT_RETRIES + MAX_REVIEWS + 1 if guarded else 2,
    )

    def run_query(key: str, fields: dict, sql: str | None, error: str | None = None) -> dict | str:
        """Run one query for a query tool: numbered when answers cite runs, lean when results are lean."""
        if lean and (earlier := runs.repeat_of(key)) is not None:
            return {'run': earlier, 'note': f'Same query as run {earlier}; its rows are above.'}
        result = None
        if error is None:
            try:
                result = db.run_readonly(sql, max_rows=LEAN_MAX_ROWS if lean else db.MAX_ROWS)
            except Exception as e:
                error = str(e)
        n = runs.add(key, fields, error)
        if error is not None:
            return f'ERROR (run {n}): {error}' if by_ref else f'ERROR: {error}'
        if context != 'raw' and not lean:
            result = {'sql': sql, **result}
        return {'run': n, **result} if by_ref else result

    if context == 'metricflow':
        def query_metrics(query: MetricFlowQuery, define: list[MetricDefinition] | None = None) -> dict | str:
            """Compile a MetricFlow query to SQL, run it, and return the columns, rows (capped) and total row count.
            `define`: metrics to define first, from existing metrics; they stay defined for later queries."""
            for metric in define or []:
                try:
                    metricflow_bridge.check_definition(metric, definitions)
                except Exception as e:
                    return f'ERROR in the definition of {metric.name}: {e}'
                definitions[:] = [d for d in definitions if d.name != metric.name] + [metric]
                log.defined_metrics += 1
            # A cited run carries the definitions in effect when it ran, so the answer compiles the same SQL.
            fields = {'query': query} | ({'metric_definitions': list(definitions)} if can_define else {})
            key = _query_key([query.model_dump(), [d.model_dump() for d in definitions]])
            try:
                sql, error = metricflow_bridge.compile(query, definitions), None
            except Exception as e:
                sql, error = None, str(e)
            return run_query(key, fields, sql, error)

        # The tool takes the query type the harness enables (its schema is what the model sees), and `define` only
        # when the harness lets the agent define metrics.
        query_metrics.__annotations__['query'] = MetricFlowQueryWithSteps if steps else MetricFlowQuery
        if not can_define:
            signature = inspect.signature(query_metrics)
            query_metrics.__signature__ = signature.replace(parameters=[signature.parameters['query']])
            query_metrics.__doc__ = query_metrics.__doc__.split('`define`')[0].rstrip()
        agent.tool_plain(query_metrics)
    elif context == 'malloy':
        @agent.tool_plain
        async def run_malloy(query: str) -> dict | str:
            """Compile a Malloy query (a `run:` statement) against the model, run it, and return the columns, rows (capped) and total row count."""
            try:
                sql, error = await malloy_bridge.compile(query), None
            except Exception as e:
                sql, error = None, str(e)
            return run_query(_query_key(query), {'malloy': query}, sql, error)
    else:
        @agent.tool_plain
        def list_tables(schema: str | None = None) -> list[dict]:
            """List warehouse tables (optionally only one schema) with row counts and descriptions."""
            return db.list_tables(schema)

        @agent.tool_plain
        def describe_table(table: str) -> dict | str:
            """Columns, types, column descriptions and sample rows for a schema-qualified table, e.g. marts.fct_orders."""
            try:
                return db.describe_table(table)
            except Exception as e:
                return f'ERROR: {e}'

        @agent.tool_plain
        def run_sql(sql: str) -> dict | str:
            """Execute a read-only DuckDB query and return columns, rows (capped) and the total row count."""
            if _SEMANTIC_SCHEMA.search(sql):
                return 'ERROR: the semantic schema is not available; query the warehouse tables from list_tables.'
            result = run_query(_query_key(sql.strip()), {'sql': sql}, sql)
            if (isinstance(result, dict) and 'raw_deprecations' in harness
                    and (deprecated := _deprecated_tables(_tables_and_columns(sql)[0]))):
                result = {'warning': 'Deprecated tables: ' + '; '.join(f'{t}: {d}' for t, d in deprecated.items()), **result}
            return result

    return agent
