"""Run a prompt set through the agent with Pydantic Evals and summarise the report."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Sequence
from dataclasses import dataclass, replace

import mlflow
from pydantic_ai.messages import ToolCallPart
from pydantic_evals import Case, Dataset
from pydantic_evals.evaluators import Evaluator, EvaluatorContext, LLMJudge

from . import db, tracking
from .agent import Context, MalloyAnswer, SemanticAnswer, answer_sql, build_agent, make_model


@dataclass
class ExecutesOK(Evaluator):
    """The agent produced SQL that runs."""

    def evaluate(self, ctx: EvaluatorContext) -> bool:
        return ctx.output['error'] is None


@dataclass
class ResultMatches(Evaluator):
    """When an expected SQL is given, the result sets match (order-insensitive)."""

    def evaluate(self, ctx: EvaluatorContext) -> dict:
        if not ctx.expected_output:
            return {}
        if ctx.output['error'] is not None:
            return {'ResultMatches': False}
        try:
            return {'ResultMatches': db.result_set(ctx.output['sql']) == db.result_set(ctx.expected_output)}
        except Exception:
            return {'ResultMatches': False}


@dataclass
class TracedLLMJudge(LLMJudge):
    """LLMJudge whose calls are traced to the cell's MLflow run. Reports under the plain LLMJudge name."""

    run_id: str | None = None

    @classmethod
    def get_serialization_name(cls) -> str:
        return 'LLMJudge'

    async def evaluate(self, ctx: EvaluatorContext):
        # The judge sees the same output it always has: the trace id is bookkeeping, not part of the answer.
        ctx = replace(ctx, output={k: v for k, v in ctx.output.items() if k != 'trace_id'})
        with tracking.span(self.run_id, f'judge {ctx.name}', tracking.SpanType.EVALUATOR, {'case': ctx.name}) as span:
            if span is not None:
                mlflow.update_current_trace(tags={'kind': 'judge', 'case': ctx.name})
            verdict = await super().evaluate(ctx)
            if span is not None:
                span.set_outputs(verdict)
            return verdict


async def run_eval(
    *,
    label: str,
    api_key: str,
    workspace_id: str | None,
    model: str,
    judge_model: str,
    prompts: Sequence[dict],
    rubric: str,
    harness: Sequence[str],
    context: Context,
    limiter: asyncio.Semaphore,
    mlflow_run_id: str | None = None,
) -> dict:
    async def task(prompt: str) -> dict:
        # One MLflow trace per case: the agent, model and tool spans nest under this root span.
        with tracking.span(mlflow_run_id, label, tracking.SpanType.AGENT, {'prompt': prompt}) as span:
            output = await run_case(prompt)
            if span is not None:
                span.set_outputs({k: output[k] for k in ('sql', 'semantic_query', 'malloy_query', 'rows', 'error')})
        return {**output, 'trace_id': span.trace_id if span is not None else None}

    async def run_case(prompt: str) -> dict:
        # Fresh agent per case so capability state never leaks. The limiter is shared by every
        # cell in the matrix, so `seconds` is measured after acquiring it (excludes queueing).
        agent = build_agent(model, api_key, harness, workspace_id, context)
        async with limiter:
            start = time.perf_counter()
            try:
                result = await agent.run(prompt)
            except Exception as e:
                return {'sql': None, 'semantic_query': None, 'malloy_query': None, 'explanation': None, 'rows': None,
                        'error': f'{type(e).__name__}: {e}', 'seconds': time.perf_counter() - start, 'usage': None}
            seconds = time.perf_counter() - start
        answer = result.output
        semantic_query = answer.query.model_dump(exclude_defaults=True) if isinstance(answer, SemanticAnswer) else None
        malloy_query = answer.malloy if isinstance(answer, MalloyAnswer) else None
        try:
            sql = await answer_sql(answer)
            rows, error = db.run_readonly(sql, max_rows=10), None
        except Exception as e:
            sql, rows, error = getattr(answer, 'sql', None), None, str(e)
        u = result.usage
        glossary_searches = sum(
            isinstance(p, ToolCallPart) and p.tool_name == 'search_glossary' for m in result.all_messages() for p in m.parts
        )
        return {
            'sql': sql,
            'semantic_query': semantic_query,
            'malloy_query': malloy_query,
            'explanation': answer.explanation,
            'rows': rows,
            'error': error,
            'seconds': seconds,
            'usage': {'input_tokens': u.input_tokens, 'output_tokens': u.output_tokens,
                      'cache_read_tokens': u.cache_read_tokens, 'cache_write_tokens': u.cache_write_tokens,
                      'requests': u.requests, 'tool_calls': u.tool_calls, 'glossary_searches': glossary_searches},
        }

    judge = make_model(judge_model, api_key, workspace_id)
    dataset = Dataset(
        name=label,
        cases=[
            Case(name=f'q{i + 1}', inputs=p['text'], expected_output=p.get('expected_sql') or None)
            for i, p in enumerate(prompts)
        ],
        evaluators=[ExecutesOK(), ResultMatches(), TracedLLMJudge(rubric=rubric, model=judge, include_input=True, run_id=mlflow_run_id)],
    )

    start = time.perf_counter()
    report = await dataset.evaluate(task, name=label, progress=False)
    total_seconds = time.perf_counter() - start

    cases = []
    for c in report.cases:
        checks = {name: {'passed': bool(r.value), 'reason': r.reason} for name, r in c.assertions.items()}
        for f in c.evaluator_failures:
            checks[f.name] = {'passed': False, 'reason': f.error_message}
        usage = c.output['usage'] or {}
        cases.append({
            'name': c.name,
            'prompt': c.inputs,
            'expected_sql': c.expected_output,
            'sql': c.output['sql'],
            'semantic_query': c.output['semantic_query'],
            'malloy_query': c.output['malloy_query'],
            'explanation': c.output['explanation'],
            'rows': c.output['rows'],
            'error': c.output['error'],
            'checks': checks,
            'passed': bool(checks) and all(v['passed'] for v in checks.values()),
            'seconds': c.output['seconds'],
            'total_seconds': c.total_duration,
            'trace_id': c.output['trace_id'],
            **usage,
        })
    for f in report.failures:  # task crashed outside our try/except
        cases.append({'name': f.name, 'prompt': f.inputs, 'error': f.error_message, 'checks': {}, 'passed': False})
    cases.sort(key=lambda c: int(c['name'][1:]))

    passed = sum(c['passed'] for c in cases)
    tokens = sum(c.get('input_tokens', 0) + c.get('output_tokens', 0) for c in cases)
    return {
        'label': label,
        'model': model,
        'harness': list(harness),
        'harness_on': bool(harness),
        'context': context,
        'total_seconds': total_seconds,
        'avg_case_seconds': sum(c.get('seconds') or 0 for c in cases) / max(len(cases), 1),
        'passed': passed,
        'total': len(cases),
        'pass_rate': passed / max(len(cases), 1),
        'total_tokens': tokens,
        'tool_calls': sum(c.get('tool_calls', 0) for c in cases),
        'requests': sum(c.get('requests', 0) for c in cases),
        'glossary_searches': sum(c.get('glossary_searches', 0) for c in cases),
        'cases': cases,
    }
