"""A read-only description of one target's agent, with or without the harness: the instructions and tools the model
actually sees (captured from a real build of the agent, so it can't drift from the code), plus the checks that run on
tool calls and on the final answer. Nothing here calls Anthropic."""

from __future__ import annotations

from collections.abc import Sequence

from pydantic_ai.models.function import AgentInfo, FunctionModel

from . import agent as A


class _Captured(Exception):
    pass


async def _model_view(context: A.Context, harness: Sequence[str]) -> AgentInfo:
    """Build the agent and stop it at its first model request, keeping what that request would have sent."""
    agent = A.build_agent('claude-sonnet-5-5', 'not-used', harness, context=context)
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
    'raw': 'Blocks run_sql calls that are not a single read-only SELECT or WITH query.',
    'metricflow': "Compiles each query_metrics call (with its new definitions) first and blocks it if MetricFlow "
                  "rejects it, so the agent gets the compiler's error instead of a failed run.",
    'malloy': 'Compiles each run_malloy call first and blocks it if the Malloy compiler rejects it.',
}


def _answer_checks(context: A.Context, enabled: list[str]) -> list[dict]:
    """The output guard's steps, in the order _final_answer_guard runs them."""
    checks = []
    if 'output_check' in enabled or 'answer_review' in enabled:
        checks.append({'source': 'output_check' if 'output_check' in enabled else 'answer_review',
                       'title': 'Final query runs',
                       'text': 'The final query is compiled and executed. If it fails, the agent is told the error and '
                               'retries.', 'limit': f'{A.MAX_EXEC_RETRIES} retries'})
    if 'output_check' in enabled:
        checks.append({'source': 'output_check', 'title': 'Final query returns data',
                       'text': 'No rows, or a single row of NULLs, sends the answer back to check filters, date '
                               'ranges and joins (an empty result can stand if the explanation says so).',
                       'limit': f'{A.MAX_EMPTY_RETRIES} retry'})
    lint = {'raw': ('raw_deprecations', 'Deprecated tables',
                    'The final query must not read tables the warehouse documents as DEPRECATED.'),
            'malloy': ('malloy_flat_output', 'Flat result',
                       'The final result must not contain nested rows from nest:.')}.get(context)
    if lint and lint[0] in enabled:
        checks.append({'source': lint[0], 'title': lint[1], 'text': lint[2], 'limit': f'{A.MAX_LINT_RETRIES} retry'})
    if 'answer_review' in enabled:
        checks.append({'source': 'answer_review', 'title': 'Reviewer',
                       'text': 'A fresh agent on the same model, with no memory of the conversation, checks the '
                               'question, final query, explanation and first 10 rows'
                               + (', plus the documentation of every column the query uses' if 'raw_column_docs' in enabled else '')
                               + ', and sends concrete problems back.',
                       'limit': f'{A.MAX_REVIEWS} reviews',
                       'instructions': A.REVIEW_INSTRUCTIONS.format(target_rules=A.REVIEW_TARGET_RULES[context])})
    return checks


async def describe(context: A.Context, harness: Sequence[str]) -> dict:
    enabled = A.target_capabilities(harness, context)
    info = await _model_view(context, enabled)
    caps = A.HARNESS_CAPABILITIES
    guarded = 'output_check' in enabled or 'answer_review' in enabled
    return {
        'context': context,
        'target': A.CONTEXTS[context],
        'harness_on': bool(enabled),
        'capabilities': [{'key': k, **v, 'enabled': k in enabled}
                         for k, v in caps.items() if v['target'] in ('all', context)],
        'instructions': A.instruction_parts(context, enabled),
        'tools': [_tool(t, 'tool') for t in info.function_tools] + [_tool(t, 'answer') for t in info.output_tools],
        'tool_guard': TOOL_GUARDS[context] if 'sql_guardrail' in enabled else None,
        'answer_checks': _answer_checks(context, enabled),
        'settings': {
            'prompt_cache': 'prompt_cache' in enabled,
            'retries': A.MAX_EXEC_RETRIES + A.MAX_EMPTY_RETRIES + A.MAX_LINT_RETRIES + A.MAX_REVIEWS + 1 if guarded else 2,
            'max_rows': A.LEAN_MAX_ROWS if 'lean_results' in enabled else A.db.MAX_ROWS,
            'sql_in_results': context != 'raw' and 'lean_results' not in enabled,
            'dedupe_repeats': 'lean_results' in enabled,
        },
    }
