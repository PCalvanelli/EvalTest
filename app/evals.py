"""Run a prompt set through the agent with Pydantic Evals and summarise the report."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Sequence
from dataclasses import dataclass, replace

import mlflow
from pydantic_evals import Case, Dataset
from pydantic_evals.evaluators import Evaluator, EvaluatorContext, LLMJudge

from . import db, tracking
from .agent import Context, HarnessLog, target_capabilities, MalloyAnswer, MetricFlowAnswer, answer_sql, build_agent, make_model


@dataclass
class ExecutesOK(Evaluator):
    """The agent produced SQL that runs."""

    def evaluate(self, ctx: EvaluatorContext) -> bool:
        return ctx.output['error'] is None


@dataclass
class ResultMatches(Evaluator):
    """When an expected SQL is given, the result sets match (row and column order ignored)."""

    def evaluate(self, ctx: EvaluatorContext) -> dict:
        if not ctx.expected_output:
            return {}
        if ctx.output['error'] is not None:
            return {'ResultMatches': False}
        try:
            expected = ctx.expected_output
            return {'ResultMatches': db.results_match(ctx.output['sql'], expected['sql'], expected.get('match', 'rows'))}
        except Exception:
            return {'ResultMatches': False}


# With a verified reference answer, correctness is settled by the reference: the judge checks that the answer agrees
# with it (allowing presentation differences) and that the explanation is honest. The company rubric is context only,
# so the judge doesn't re-derive definitions the reference already encodes.
REFERENCE_JUDGE_RUBRIC = """\
You grade a data analyst's answer to a business question against a verified reference result (the expected output:
its SQL, row count and first rows). PASS only if both hold:
1. The answer's rows agree with the reference. Allow presentation differences: column order and names, labels for
   the same groups (TRUE/FALSE vs 'first'/'repeat'), number formatting and small rounding, date formats. A different
   number, a missing or extra group or column, or a different grain is a disagreement.
2. The explanation accurately describes what the query does: no claimed filter, exclusion or definition the query
   doesn't apply.
The output shows the agent's first rows and its total row_count. Where the answer agrees with the reference, do not
fail it on your own reading of the company definitions below: the reference already encodes them.

Company definitions, for context:
{rubric}"""


@dataclass
class TracedLLMJudge(LLMJudge):
    """LLMJudge whose calls are traced to the cell's MLflow run. Reports under the plain LLMJudge name."""

    run_id: str | None = None
    trace_tags: dict[str, str] | None = None

    @classmethod
    def get_serialization_name(cls) -> str:
        return 'LLMJudge'

    async def evaluate(self, ctx: EvaluatorContext):
        # The judge sees the same output it always has: the trace id is bookkeeping, not part of the answer.
        ctx = replace(ctx, output={k: v for k, v in ctx.output.items() if k != 'trace_id'})
        with tracking.span(self.run_id, f'judge {ctx.name}', tracking.SpanType.EVALUATOR, {'case': ctx.name}) as span:
            if span is not None:
                mlflow.update_current_trace(tags={**(self.trace_tags or {}), 'kind': 'judge', 'case': ctx.name})
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
    trace_tags: dict[str, str] | None = None,
) -> dict:
    async def task(prompt: str) -> dict:
        # One MLflow trace per case: the agent, model and tool spans nest under this root span.
        with tracking.span(mlflow_run_id, label, tracking.SpanType.AGENT, {'prompt': prompt}) as span:
            if span is not None:
                mlflow.update_current_trace(tags={**(trace_tags or {}), 'kind': 'agent'})
            output = await run_case(prompt)
            if span is not None:
                span.set_outputs({k: output[k] for k in ('sql', 'metricflow_query', 'malloy_query', 'rows', 'error')})
        return {**output, 'trace_id': span.trace_id if span is not None else None}

    async def run_case(prompt: str) -> dict:
        # Fresh agent per case so capability state never leaks. The limiter is shared by every
        # cell in the matrix, so `seconds` is measured after acquiring it (excludes queueing).
        log = HarnessLog()
        agent = build_agent(model, api_key, harness, workspace_id, context, log)
        async with limiter:
            start = time.perf_counter()
            try:
                result = await agent.run(prompt)
            except Exception as e:
                return {'sql': None, 'metricflow_query': None, 'malloy_query': None, 'explanation': None, 'rows': None,
                        'error': f'{type(e).__name__}: {e}', 'seconds': time.perf_counter() - start, 'usage': None}
            seconds = time.perf_counter() - start
        answer = result.output
        metricflow_query = answer.query.model_dump(exclude_defaults=True) if isinstance(answer, MetricFlowAnswer) else None
        if metricflow_query is not None and getattr(answer, 'metric_definitions', None):
            metricflow_query['metric_definitions'] = [d.model_dump(exclude_none=True, exclude_defaults=True)
                                                      for d in answer.metric_definitions]
        malloy_query = answer.malloy if isinstance(answer, MalloyAnswer) else None
        try:
            sql = await answer_sql(answer)
            rows, error = db.run_readonly(sql, max_rows=10), None  # {columns, rows (first 10), row_count}
        except Exception as e:
            sql, rows, error = getattr(answer, 'sql', None), None, str(e)
        u, r = result.usage, log.reviewer_usage  # reviewer calls are part of the case's cost
        return {
            'sql': sql,
            'metricflow_query': metricflow_query,
            'malloy_query': malloy_query,
            'explanation': answer.explanation,
            'rows': rows,
            'error': error,
            'seconds': seconds,
            'usage': {'input_tokens': u.input_tokens + r.get('input_tokens', 0),
                      'output_tokens': u.output_tokens + r.get('output_tokens', 0),
                      'cache_read_tokens': u.cache_read_tokens + r.get('cache_read_tokens', 0),
                      'cache_write_tokens': u.cache_write_tokens + r.get('cache_write_tokens', 0),
                      'requests': u.requests + r.get('requests', 0), 'tool_calls': u.tool_calls,
                      'reviews': log.reviews, 'sent_back': log.sent_back,
                      'defined_metrics': log.defined_metrics, 'lints': log.lints},
        }

    judge = make_model(judge_model, api_key, workspace_id)

    def case(i: int, p: dict) -> Case:
        """A case with the judge that fits it: graded against the reference when there is one, else on the rubric."""
        reference = None
        if p.get('expected_sql'):
            ref = db.run_readonly(p['expected_sql'], max_rows=20)
            reference = {'sql': p['expected_sql'], 'match': p.get('match', 'rows'), 'row_count': ref['row_count'],
                         'columns': ref['columns'], 'rows': ref['rows']}
        judge_rubric = REFERENCE_JUDGE_RUBRIC.format(rubric=rubric) if reference else rubric
        return Case(name=f'q{i + 1}', inputs=p['text'], expected_output=reference, evaluators=[
            TracedLLMJudge(rubric=judge_rubric, model=judge, include_input=True, include_expected_output=reference is not None,
                           run_id=mlflow_run_id, trace_tags=trace_tags)])

    dataset = Dataset(name=label, cases=[case(i, p) for i, p in enumerate(prompts)],
                      evaluators=[ExecutesOK(), ResultMatches()])

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
            'expected_sql': (c.expected_output or {}).get('sql'),
            'sql': c.output['sql'],
            'metricflow_query': c.output['metricflow_query'],
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
        'harness': target_capabilities(harness, context),  # what applied to this target
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
        'reviews': sum(c.get('reviews', 0) for c in cases),
        'sent_back': sum(c.get('sent_back', 0) for c in cases),
        'defined_metrics': sum(c.get('defined_metrics', 0) for c in cases),
        # The two graders, reported separately: when they disagree it's usually the question or the grading.
        'rows_match': sum(c['checks'].get('ResultMatches', {}).get('passed', False) for c in cases),
        'rows_checked': sum('ResultMatches' in c['checks'] for c in cases),
        'judge_pass': sum(c['checks'].get('LLMJudge', {}).get('passed', False) for c in cases),
        'graders_disagree': sum('ResultMatches' in c['checks'] and 'LLMJudge' in c['checks']
                                and c['checks']['ResultMatches']['passed'] != c['checks']['LLMJudge']['passed'] for c in cases),
        'lint_retries': sum(len(c.get('lints') or []) for c in cases),
        'cache_read_tokens': sum(c.get('cache_read_tokens', 0) for c in cases),
        'input_tokens': sum(c.get('input_tokens', 0) for c in cases),
        'cases': cases,
    }
