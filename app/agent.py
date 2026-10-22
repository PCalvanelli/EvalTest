"""Text-to-SQL agent, built with or without Pydantic AI Harness capabilities, over raw SQL, dbt MetricFlow or Malloy."""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Literal

from anthropic import AsyncAnthropic
from pydantic import BaseModel, Field
from pydantic_ai import Agent
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.providers.anthropic import AnthropicProvider
from pydantic_ai_harness import GuardrailResult, OutputGuardrail, Planning, SystemReminders, ToolGuardrail
from pydantic_ai_harness.guardrails import ToolCallInfo
from pydantic_ai_harness.repair_tool_arguments import RepairToolArguments
from pydantic_ai_harness.system_reminders import Reminder

from . import db, malloy_bridge, metricflow_bridge
from .metricflow_bridge import MetricFlowQuery


class SQLAnswer(BaseModel):
    sql: str = Field(description='The final DuckDB SELECT query that answers the question.')
    explanation: str = Field(description='One or two sentences on how the query answers the question.')


class MetricFlowAnswer(BaseModel):
    query: MetricFlowQuery = Field(description='The final MetricFlow query that answers the question.')
    explanation: str = Field(description='One or two sentences on how the query answers the question.')


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

<metrics_catalog>
{metricflow_bridge.catalog_text()}
</metrics_catalog>
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

# Capabilities the harness mode can switch on. Keys are what the UI sends.
HARNESS_CAPABILITIES = {
    'sql_guardrail': 'Tool guardrail: block non-SELECT SQL / queries that don\'t compile before they run',
    'output_check': 'Output guardrail: final query must execute, otherwise the model retries',
    'reminders': 'System reminders: re-inject "verify by running the query" each turn',
    'planning': 'Planning: a write_plan tool to outline the query before writing it',
    'repair_args': 'Repair tool arguments: fix malformed JSON tool args',
}


def _block_writes(call: ToolCallInfo) -> GuardrailResult:
    sql = str(call.args.get('sql', ''))
    stripped = sql.strip().lower()
    if not stripped.startswith(('select', 'with')) or ';' in stripped.rstrip(';'):
        return GuardrailResult.block('Only a single read-only SELECT/WITH query is allowed.')
    return GuardrailResult.allow()


def _block_uncompilable_metricflow(call: ToolCallInfo) -> GuardrailResult:
    try:
        metricflow_bridge.compile(MetricFlowQuery.model_validate(call.args['query']))
    except Exception as e:
        return GuardrailResult.block(f'MetricFlow query does not compile: {e}')
    return GuardrailResult.allow()


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
    return metricflow_bridge.compile(output.query)


async def _final_query_executes(output: object) -> GuardrailResult:
    assert isinstance(output, (SQLAnswer, MetricFlowAnswer, MalloyAnswer))
    try:
        db.run_readonly(await answer_sql(output), max_rows=1)
    except Exception as e:
        return GuardrailResult.retry(f'Your final query failed to execute: {e}. Fix it and try again.')
    return GuardrailResult.allow()


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


def _build_capabilities(enabled: Sequence[str], context: Context) -> list[AbstractCapability]:
    tool, guard = {
        'metricflow': ('query_metrics', _block_uncompilable_metricflow),
        'malloy': ('run_malloy', _block_uncompilable_malloy),
    }.get(context, ('run_sql', _block_writes))
    factories = {
        'sql_guardrail': lambda: ToolGuardrail(guard=guard, tools=[tool]),
        'output_check': lambda: OutputGuardrail(guard=_final_query_executes),
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
    return [factories[name]() for name in enabled if name in factories]


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
) -> Agent[None, SQLAnswer] | Agent[None, MetricFlowAnswer] | Agent[None, MalloyAnswer]:
    """Build a fresh agent. `harness` lists enabled capability keys (empty = baseline). `context` picks what
    business knowledge it gets: none (raw SQL), or a semantic layer (dbt MetricFlow or Malloy) instead of SQL."""
    agent = Agent(
        make_model(model_name, api_key, workspace_id),
        output_type={'metricflow': MetricFlowAnswer, 'malloy': MalloyAnswer}.get(context, SQLAnswer),
        instructions={
            'raw': INSTRUCTIONS,
            'metricflow': METRICFLOW_INSTRUCTIONS,
            'malloy': MALLOY_INSTRUCTIONS,
        }[context],
        capabilities=_build_capabilities(harness, context),
        retries=2,
    )

    if context == 'metricflow':
        @agent.tool_plain
        def query_metrics(query: MetricFlowQuery) -> dict | str:
            """Compile a MetricFlow query to SQL, run it, and return the SQL, columns, rows (capped) and total row count."""
            try:
                sql = metricflow_bridge.compile(query)
                return {'sql': sql, **db.run_readonly(sql)}
            except Exception as e:
                return f'ERROR: {e}'
    elif context == 'malloy':
        @agent.tool_plain
        async def run_malloy(query: str) -> dict | str:
            """Compile a Malloy query (a `run:` statement) against the model, run it, and return the generated SQL, columns, rows (capped) and total row count."""
            try:
                sql = await malloy_bridge.compile(query)
                return {'sql': sql, **db.run_readonly(sql)}
            except Exception as e:
                return f'ERROR: {e}'
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
            try:
                return db.run_readonly(sql)
            except Exception as e:
                return f'ERROR: {e}'

    return agent
