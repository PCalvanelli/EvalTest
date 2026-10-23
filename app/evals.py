"""Run a question set through one agent and grade each answer against its reference result, and run a whole matrix
of (model, harness, context) cells. The server and the CLI bench both run matrices through run_matrix."""

from __future__ import annotations

import asyncio
import itertools
import random
import time
from collections.abc import AsyncIterator, Callable, Sequence

import mlflow
from pydantic_ai.exceptions import (ModelConnectionError, ModelQuotaExceededError, ModelRateLimitError,
                                    ModelRequestTimeout, ModelServerError, ModelUnavailableError, RunTimedOut,
                                    UsageLimitExceeded)
from pydantic_ai.usage import RunUsage, UsageLimits

from . import db, gold, tracking
from .labels import count_labels, rule_label
from .agent import Context, HarnessLog, MalloyAnswer, MetricFlowAnswer, answer_sql, build_agent
from .malloy_bridge import SidecarError
from .pricing import case_cost

MAX_CONCURRENT_AGENT_RUNS = 6
# Bump when what a pass means changes (grading, budgets, targets, questions), so History can tell which matrices compare.
BENCHMARK_VERSION = 2

# Every cell gets the same budget per question, so no setup wins by spending more. Set from past runs, where the 95th
# percentile case used 9 tool calls, 78k tokens and 32 s: the caps only stop runaways.
TOOL_CALL_LIMIT = 15
TOKEN_LIMIT = 250_000  # the agent's and the reviewer's tokens together
CASE_TIMEOUT_S = 180
LIMITS = {'tool_call_limit': TOOL_CALL_LIMIT, 'token_limit': TOKEN_LIMIT, 'case_timeout_s': CASE_TIMEOUT_S}
# Provider and compiler outages are not the agent's misses: retried with a fresh agent, then excluded from scores.
INFRA_ERRORS = (ModelRateLimitError, ModelQuotaExceededError, ModelUnavailableError, ModelServerError,
                ModelConnectionError, ModelRequestTimeout, SidecarError)
INFRA_RETRIES = 2


def grade(sql: str | None, error: str | None, question: dict) -> tuple[str, str | None]:
    """'pass' when the final query runs and returns the question's frozen reference rows; 'partial' (a fail) when it
    returns them plus columns nobody asked for; otherwise 'fail'. With the reason when it isn't a pass."""
    if error is not None:
        return 'fail', f'Query failed: {error}'
    try:
        outcome = db.compare_rows(db.all_rows(sql), gold.rows(question['id']), question.get('match', 'rows'),
                                  order_matters=question.get('order_matters', False))
    except Exception as e:
        return 'fail', f'Could not compare with the reference: {e}'
    if outcome == 'match':
        return 'pass', None
    if outcome == 'partial':
        return 'partial', 'Partial: every reference column matches, but the answer adds columns nobody asked for.'
    return 'fail', "Rows don't match the reference."


async def run_case(question: dict, model: str, context: Context, harness: bool) -> dict:
    """One question, answered by a fresh agent so capability state never leaks between cases. An infrastructure
    failure is retried; if it persists, the case's status is infra_error. Otherwise it is 'answered', ready to grade."""
    for attempt in range(1, INFRA_RETRIES + 2):
        case = await _attempt(question, model, context, harness)
        if case['status'] != 'infra_error' or attempt > INFRA_RETRIES:
            return {**case, 'attempts': attempt}
        await asyncio.sleep(2 * 4 ** (attempt - 1))
    raise AssertionError('unreachable')


def _usage(u: RunUsage | None, r: dict) -> dict:
    """Token and request counts for a case: the agent's run plus its reviewer's calls (part of the case's cost)."""
    u = u or RunUsage()
    return {
        'input_tokens': u.input_tokens + r.get('input_tokens', 0),
        'output_tokens': u.output_tokens + r.get('output_tokens', 0),
        'cache_read_tokens': u.cache_read_tokens + r.get('cache_read_tokens', 0),
        'cache_write_tokens': u.cache_write_tokens + r.get('cache_write_tokens', 0),
        'requests': u.requests + r.get('requests', 0),
        'tool_calls': u.tool_calls,
    }


async def _attempt(question: dict, model: str, context: Context, harness: bool) -> dict:
    log = HarnessLog()
    agent = build_agent(model, context, harness, log)
    start = time.perf_counter()

    def no_answer(error: str, *, status: str = 'answered', failure: str | None = None, usage: RunUsage | None = None) -> dict:
        return {'sql': None, 'metricflow_query': None, 'malloy_query': None, 'explanation': None, 'rows': None,
                'error': error, 'status': status, 'failure': failure, 'seconds': time.perf_counter() - start,
                **_usage(usage, log.reviewer_usage)}

    limits = UsageLimits(request_limit=None, tool_calls_limit=TOOL_CALL_LIMIT, total_tokens_limit=TOKEN_LIMIT)
    try:
        result = await agent.run(question['text'], usage_limits=limits, timeout=CASE_TIMEOUT_S)
    except INFRA_ERRORS as e:
        return no_answer(f'{type(e).__name__}: {e}', status='infra_error')
    except UsageLimitExceeded as e:
        return no_answer(f'Over budget: {e}', failure='budget')
    except RunTimedOut as e:
        return no_answer(f'Timed out after {CASE_TIMEOUT_S} s', failure='timeout', usage=e.usage)
    except Exception as e:
        return no_answer(f'{type(e).__name__}: {e}')
    seconds = time.perf_counter() - start
    answer = result.output
    metricflow_query = answer.query.model_dump(exclude_defaults=True) if isinstance(answer, MetricFlowAnswer) else None
    if metricflow_query is not None and getattr(answer, 'metric_definitions', None):
        metricflow_query['metric_definitions'] = [d.model_dump(exclude_none=True, exclude_defaults=True)
                                                  for d in answer.metric_definitions]
    status, failure = 'answered', None
    try:
        sql = await answer_sql(answer)
        rows, error = await asyncio.to_thread(db.run_readonly, sql, max_rows=10), None  # {columns, rows (first 10), row_count}
    except SidecarError as e:
        sql, rows, error, status = None, None, f'{type(e).__name__}: {e}', 'infra_error'
    except Exception as e:
        sql, rows, error = getattr(answer, 'sql', None), None, str(e)
    usage = _usage(result.usage, log.reviewer_usage)
    # The run's limit covers the agent alone; the reviewer's tokens count against the same budget.
    if error is None and (spent := usage['input_tokens'] + usage['output_tokens']) > TOKEN_LIMIT:
        error, failure = f'Over budget: the agent and its reviewer used {spent:,} tokens (limit {TOKEN_LIMIT:,})', 'budget'
    return {
        'sql': sql,
        'metricflow_query': metricflow_query,
        'malloy_query': answer.malloy if isinstance(answer, MalloyAnswer) else None,
        'explanation': answer.explanation,
        'rows': rows,
        'error': error,
        'status': status,
        'failure': failure,
        'seconds': seconds,
        **usage,
        'reviews': log.reviews,
        'sent_back': log.sent_back,
        'defined_metrics': log.defined_metrics,
        'lints': log.lints,
    }


async def run_eval(
    *,
    label: str,
    model: str,
    questions: Sequence[dict],
    harness: bool,
    context: Context,
    limiter: asyncio.Semaphore,
    mlflow_run_id: str | None = None,
    trace_tags: dict[str, str] | None = None,
    emit: Callable[[dict], None] = lambda event: None,
    order: Sequence[int] | None = None,
) -> dict:
    """Run every question through the agent and grade it. `order` is the order the questions take agent slots in
    (default: as listed); cases come back as listed either way. Infrastructure errors are left out of the scores."""
    async def case(i: int, question: dict) -> dict:
        name = question['id']
        # One MLflow trace per case: the agent, model and tool spans nest under this root span. The limiter is shared
        # by every cell in the matrix, so `seconds` excludes queueing.
        async with limiter:
            emit({'type': 'case_start', 'case': i})
            with tracking.span(mlflow_run_id, label, {'prompt': question['text']}) as span:
                if span is not None:
                    mlflow.update_current_trace(tags={**(trace_tags or {}), 'kind': 'agent', 'case': name})
                output = await run_case(question, model, context, harness)
                if span is not None:
                    span.set_outputs({k: output[k] for k in ('sql', 'metricflow_query', 'malloy_query', 'rows', 'error')})
            if output['status'] == 'infra_error':
                passed, reason = False, f'Infrastructure error, left out of the scores: {output["error"]}'
            else:
                outcome, reason = await asyncio.to_thread(grade, output['sql'], output['error'], question)
                passed = outcome == 'pass'
                output['status'], output['partial'] = 'pass' if passed else 'fail', outcome == 'partial'
                output['failure_label'] = rule_label({**output, 'name': name})  # the LLM labels the rest later
                output['label_source'] = 'rule' if output['failure_label'] else None
            emit({'type': 'case', 'case': i, 'passed': passed, 'status': output['status'], 'seconds': output['seconds']})
        return {'name': name, 'prompt': question['text'], 'expected_sql': question['expected_sql'], **output,
                'passed': passed, 'reason': reason, 'cost_usd': case_cost(output, model),
                'trace_id': span.trace_id if span is not None else None,
                'trace_url': tracking.trace_url(span.trace_id) if span is not None else None}

    start = time.perf_counter()
    # Tasks start in creation order, and so queue for the shared limiter in that order.
    tasks = {i: asyncio.ensure_future(case(i, questions[i])) for i in (order or range(len(questions)))}
    cases = list(await asyncio.gather(*(tasks[i] for i in range(len(questions)))))
    total_seconds = time.perf_counter() - start

    scored = [c for c in cases if c['status'] != 'infra_error']
    n = max(len(cases), 1)
    passed = sum(c['passed'] for c in scored)
    total = lambda key: sum(c.get(key) or 0 for c in cases)  # noqa: E731
    return {
        'model': model,
        'harness_on': harness,
        'context': context,
        'total_seconds': total_seconds,
        'avg_case_seconds': total('seconds') / n,
        'passed': passed,
        'total': len(scored),
        'pass_rate': passed / max(len(scored), 1),
        'infra_errors': len(cases) - len(scored),
        'over_budget': sum(c.get('failure') == 'budget' for c in cases),
        'timeouts': sum(c.get('failure') == 'timeout' for c in cases),
        'partial': sum(bool(c.get('partial')) for c in cases),
        'failure_labels': count_labels(cases),
        'total_tokens': total('input_tokens') + total('output_tokens'),
        'input_tokens': total('input_tokens'),
        'cache_read_tokens': total('cache_read_tokens'),
        'tool_calls': total('tool_calls'),
        'requests': total('requests'),
        'reviews': total('reviews'),
        'sent_back': total('sent_back'),
        'defined_metrics': total('defined_metrics'),
        'lint_retries': sum(len(c.get('lints') or []) for c in cases),
        'cost_usd': total('cost_usd'),
        'cases': cases,
    }


def matrix_cells(models: Sequence[str], harness_modes: Sequence[bool], contexts: Sequence[Context]) -> list[dict]:
    """Every (model, harness, context) combination, duplicates dropped."""
    return [{'id': f'{m}|{"harness" if h else "no-harness"}|{c}', 'model': m, 'harness_on': h, 'context': c}
            for m, h, c in itertools.product(dict.fromkeys(models), dict.fromkeys(harness_modes), dict.fromkeys(contexts))]


async def run_matrix(cells: list[dict], questions: Sequence[dict], repeats: int, params: dict,
                     seed: int | None = None) -> AsyncIterator[dict]:
    """Run every cell `repeats` times, one run after another, recording it all in MLflow. Yields a plan event; then,
    within each run, a case_start and a case event per question as it takes an agent slot and is graded, a cell (or
    cell_error) event as each cell finishes, and a run_end event. Close it (contextlib.aclosing) so an interrupted matrix
    is recorded as KILLED. Each run starts the questions in its own shuffled order, drawn from `seed` (random if
    omitted, and recorded), so no question always queues last behind rate limits."""
    gold.check(questions)  # before any MLflow run is opened, so a stale reference never starts a matrix
    seed = random.randrange(2 ** 31) if seed is None else seed
    params = {'benchmark_version': BENCHMARK_VERSION, 'seed': seed, **LIMITS, 'models': ','.join(dict.fromkeys(c['model'] for c in cells)),
              'harness_modes': ','.join(dict.fromkeys('on' if c['harness_on'] else 'off' for c in cells)),
              'contexts': ','.join(dict.fromkeys(c['context'] for c in cells)),
              'repeats': repeats, 'prompts': len(questions), **tracking.provenance(), **params}
    tracker = await asyncio.to_thread(tracking.MatrixTracker.start, cells=cells, questions=questions, params=params)
    limiter = asyncio.Semaphore(MAX_CONCURRENT_AGENT_RUNS)

    async def run_cell(cell: dict, run_no: int, order: list[int], events: asyncio.Queue) -> dict:
        try:
            report = await run_eval(
                label=cell['id'], model=cell['model'], questions=questions, harness=cell['harness_on'],
                context=cell['context'], limiter=limiter,
                mlflow_run_id=tracker.cell_runs[cell['id']] if tracker else None,
                trace_tags=tracker.trace_tags(cell['id'], run_no) if tracker else None,
                emit=lambda event: events.put_nowait({**event, 'id': cell['id'], 'run': run_no}), order=order,
            )
        except Exception as e:
            error = f'{type(e).__name__}: {e}'
            if tracker:
                await asyncio.to_thread(tracker.log_cell_error, cell['id'], run_no, error)
            return {'type': 'cell_error', 'run': run_no, 'id': cell['id'], 'error': error}
        if tracker:
            await asyncio.to_thread(tracker.log_cell, cell['id'], run_no, report)
        return {'type': 'cell', 'run': run_no, 'report': {**report, 'id': cell['id'], 'run': run_no}}

    yield {'type': 'plan', 'cells': cells, 'prompts': [q['text'] for q in questions], 'repeats': repeats,
           'mlflow': tracker.urls() if tracker else None, 'matrix_id': tracker.parent_id if tracker else None}
    status = 'KILLED'  # stays KILLED if the matrix is interrupted, so History doesn't show it running forever
    tasks: list[asyncio.Task] = []
    try:
        for run_no in range(1, repeats + 1):
            started = time.perf_counter()
            events: asyncio.Queue = asyncio.Queue()
            order = random.Random(seed + run_no).sample(range(len(questions)), len(questions))
            tasks = [asyncio.create_task(run_cell(c, run_no, order, events)) for c in cells]
            for task in tasks:  # each cell's final event goes on the queue after its case events
                task.add_done_callback(lambda t: t.cancelled() or events.put_nowait(t.result()))
            remaining = len(tasks)
            while remaining:
                event = await events.get()
                remaining -= event['type'] in ('cell', 'cell_error')
                yield event
            seconds = time.perf_counter() - started  # wall clock for the whole run, every cell included
            if tracker:
                await asyncio.to_thread(tracker.log_run_time, run_no, seconds)
            yield {'type': 'run_end', 'run': run_no, 'seconds': seconds}
        status = 'FINISHED'
    finally:
        for task in tasks:
            task.cancel()
        if tracker:
            await asyncio.shield(asyncio.to_thread(tracker.finish, status))
