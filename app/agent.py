"""Text-to-SQL agent, built with or without Pydantic AI Harness capabilities and a semantic layer."""

from __future__ import annotations

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

from . import db, glossary, malloy_bridge
from .semantic import layer
from .semantic.layer import SemanticQuery


class SQLAnswer(BaseModel):
    sql: str = Field(description='The final DuckDB SELECT query that answers the question.')
    explanation: str = Field(description='One or two sentences on how the query answers the question.')


class SemanticAnswer(BaseModel):
    query: SemanticQuery = Field(description='The final semantic-layer query that answers the question.')
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

GLOSSARY = glossary.TEXT

GLOSSARY_INSTRUCTIONS = f"""\
{INSTRUCTIONS}
The company's business glossary is below. Business terms in questions follow these definitions. The
glossary is written for business users and doesn't name tables or columns: map it onto the warehouse yourself.

<glossary>
{GLOSSARY}
</glossary>
"""

# Same as raw SQL mode, but the glossary is only reachable through a search tool: the agent has to
# decide to look terms up, as with a wiki or docs search in a real deployment.
GLOSSARY_SEARCH_INSTRUCTIONS = INSTRUCTIONS.replace(
    '- `run_sql(sql)`',
    "- `search_glossary(query)`: search the company's business glossary for definitions of business terms.\n- `run_sql(sql)`",
)

SEMANTIC_INSTRUCTIONS = f"""\
You answer natural-language questions using a semantic layer. You cannot write SQL and you cannot see
the warehouse tables: express every answer as a semantic query against ONE model, made of metrics,
dimensions, filters, segments, ordering and limit. You have a `query_metrics` tool that compiles and runs
a semantic query and returns the generated SQL plus up to {db.MAX_ROWS} rows. Return the final semantic query
and a short explanation. If the question cannot be expressed with this layer, return the closest query
and say so in the explanation.

{layer.catalog_text()}
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
    'sql_guardrail': 'Tool guardrail: block non-SELECT SQL / uncompilable semantic queries before they run',
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


def _block_uncompilable(call: ToolCallInfo) -> GuardrailResult:
    try:
        layer.compile(SemanticQuery.model_validate(call.args['query']))
    except Exception as e:
        return GuardrailResult.block(f'Semantic query is invalid: {e}')
    return GuardrailResult.allow()


async def _block_uncompilable_malloy(call: ToolCallInfo) -> GuardrailResult:
    try:
        await malloy_bridge.compile(str(call.args.get('query', '')))
    except Exception as e:
        return GuardrailResult.block(f'Malloy query does not compile: {e}')
    return GuardrailResult.allow()


async def answer_sql(output: SQLAnswer | SemanticAnswer | MalloyAnswer) -> str:
    """The SQL an answer runs, compiling it first when it came from the semantic layer or Malloy."""
    if isinstance(output, SQLAnswer):
        return output.sql
    if isinstance(output, MalloyAnswer):
        return await malloy_bridge.compile(output.malloy)
    return layer.compile(output.query)


async def _final_query_executes(output: object) -> GuardrailResult:
    assert isinstance(output, (SQLAnswer, SemanticAnswer, MalloyAnswer))
    try:
        db.run_readonly(await answer_sql(output), max_rows=1)
    except Exception as e:
        return GuardrailResult.retry(f'Your final query failed to execute: {e}. Fix it and try again.')
    return GuardrailResult.allow()


Context = Literal['raw', 'glossary', 'glossary_search', 'semantic', 'malloy']
CONTEXTS: dict[str, str] = {
    'raw': 'No business context: discover tables and write SQL',
    'glossary': 'Raw SQL + a plain-language business glossary',
    'glossary_search': 'Raw SQL + a search_glossary tool (glossary not in the prompt)',
    'semantic': 'Semantic layer only (governed metrics, no SQL)',
    'malloy': 'Malloy model (write Malloy queries, no SQL)',
}


def _build_capabilities(enabled: Sequence[str], context: Context) -> list[AbstractCapability]:
    tool, guard = {
        'semantic': ('query_metrics', _block_uncompilable),
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
) -> Agent[None, SQLAnswer] | Agent[None, SemanticAnswer] | Agent[None, MalloyAnswer]:
    """Build a fresh agent. `harness` lists enabled capability keys (empty = baseline). `context` picks what
    business knowledge it gets: none (raw), a prose glossary on top of raw SQL, or a semantic model (YAML layer or
    Malloy) instead of SQL."""
    semantic = context == 'semantic'
    agent = Agent(
        make_model(model_name, api_key, workspace_id),
        output_type={'semantic': SemanticAnswer, 'malloy': MalloyAnswer}.get(context, SQLAnswer),
        instructions={
            'raw': INSTRUCTIONS,
            'glossary': GLOSSARY_INSTRUCTIONS,
            'glossary_search': GLOSSARY_SEARCH_INSTRUCTIONS,
            'semantic': SEMANTIC_INSTRUCTIONS,
            'malloy': MALLOY_INSTRUCTIONS,
        }[context],
        capabilities=_build_capabilities(harness, context),
        retries=2,
    )

    if semantic:
        @agent.tool_plain
        def query_metrics(query: SemanticQuery) -> dict | str:
            """Compile a semantic query to SQL, run it, and return the SQL, columns, rows (capped) and total row count."""
            try:
                sql = layer.compile(query)
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
        if context == 'glossary_search':
            @agent.tool_plain
            def search_glossary(query: str) -> list[dict] | str:
                """Search the company's business glossary by keywords (e.g. 'MRR', 'net revenue'). Returns the best-matching entries."""
                return glossary.search(query) or 'No matching glossary entries. Try different keywords.'

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
            try:
                return db.run_readonly(sql)
            except Exception as e:
                return f'ERROR: {e}'

    return agent
