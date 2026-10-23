"""A read-only description of one target's agent, as the baseline or with the harness: the instructions and tools the model
actually sees (captured from a real build of the agent, so it can't drift from the code), plus the checks that run on
tool calls and on the final answer. Nothing here calls Anthropic."""

from __future__ import annotations

from pydantic_ai.models.function import AgentInfo, FunctionModel

from . import agent as A


class _Captured(Exception):
    pass


async def _model_view(context: A.Context, harness: bool) -> AgentInfo:
    """Build the agent and stop it at its first model request, keeping what that request would have sent."""
    agent = A.build_agent('claude-sonnet-5-5', context, harness, api_key='not-used')
    seen: list[AgentInfo] = []

    def first_request(_messages, info: AgentInfo):  # noqa: ANN001
        seen.append(info)
        raise _Captured

    with agent.override(model=FunctionModel(first_request)):
        try:
            await agent.run('(inspect)')
        except _Captured:
            pass
    return seen[0]


def _tool(t, kind: str) -> dict:  # noqa: ANN001
    return {'name': t.name, 'kind': kind, 'description': t.description or '', 'schema': t.parameters_json_schema}


TOOL_GUARDS = {
    'raw': 'Only a single read-only SELECT or WITH runs',
    'glossary': 'Only a single read-only SELECT or WITH runs',
    'metricflow': 'Compiled first; MetricFlow errors go back to the agent',
    'malloy': 'Compiled first; Malloy errors go back to the agent',
}

LINTS = {
    'raw': ('Deprecated tables', 'The final query must not read tables the warehouse documents as DEPRECATED.'),
    'glossary': ('Deprecated tables', 'The final query must not read tables the warehouse documents as DEPRECATED.'),
    'malloy': ('Flat result', 'The final result must not contain nested rows from nest:.'),
}


def _answer_checks(context: A.Context) -> list[dict]:
    """The harness's output guard steps, in the order _final_answer_guard runs them."""
    checks = [
        {'title': 'Final query runs', 'limit': f'{A.MAX_EXEC_RETRIES} retries',
         'text': 'The final query is compiled and executed. If it fails, the agent is told the error and retries.'},
        {'title': 'Final query returns data', 'limit': f'{A.MAX_EMPTY_RETRIES} retry',
         'text': 'No rows, or a single row of NULLs, sends the answer back to check filters, date ranges and joins (an '
                 'empty result can stand if the explanation says so).'},
    ]
    if context in LINTS:
        title, text = LINTS[context]
        checks.append({'title': title, 'text': text, 'limit': f'{A.MAX_LINT_RETRIES} retry'})
    checks.append({'title': 'Reviewer', 'limit': f'{A.MAX_REVIEWS} reviews',
                   'text': 'A fresh agent on the same model, with no memory of the conversation, checks the question, '
                           'final query, explanation and first 10 rows'
                           + (', plus the documentation of every column the query uses' if context in A.RAW_CONTEXTS else '')
                           + ', and sends concrete problems back.',
                   'instructions': A.REVIEW_INSTRUCTIONS.format(target_rules=A.REVIEW_TARGET_RULES[context])})
    return checks


async def describe(context: A.Context, harness: bool) -> dict:
    info = await _model_view(context, harness)
    return {
        'context': context,
        'target': A.CONTEXTS[context],
        'harness_on': harness,
        'instructions': A.instruction_parts(context, harness),
        'tools': [_tool(t, 'tool') for t in info.function_tools] + [_tool(t, 'answer') for t in info.output_tools],
        'tool_guard': TOOL_GUARDS[context] if harness else None,
        'answer_checks': _answer_checks(context) if harness else [],
        'settings': {
            'prompt_cache': harness,
            'retries': A.HARNESS_RETRIES if harness else 2,
            'max_rows': A.LEAN_MAX_ROWS if harness else A.db.MAX_ROWS,
            'sql_in_results': context not in A.RAW_CONTEXTS and not harness,
            'dedupe_repeats': harness,
        },
    }
