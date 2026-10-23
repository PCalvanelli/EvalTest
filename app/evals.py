"""Run a question set through one agent and grade each answer against its reference result, and run a whole matrix
of (model, harness, context) cells. The server and the CLI bench both run matrices through run_matrix."""

from __future__ import annotations

import asyncio
import itertools
import time
from collections.abc import AsyncIterator, Callable, Sequence

import mlflow

from . import db, gold, tracking
from .agent import Context, HarnessLog, MalloyAnswer, MetricFlowAnswer, answer_sql, build_agent
from .pricing import case_cost

MAX_CONCURRENT_AGENT_RUNS = 6
# Bump when what a pass means changes (grading, budgets, targets, questions), so History can tell which matrices compare.
BENCHMARK_VERSION = 2


def grade(sql: str | None, error: str | None, question: dict) -> tuple[bool, str | None]:
    """Pass when the final query runs and returns the question's frozen reference rows; otherwise say why not."""
    if error is not None:
        return False, f'Query failed: {error}'
    try:
        if db.rows_match(db.all_rows(sql), gold.rows(question['id']), question.get('match', 'rows')):
            return True, None
    except Exception as e:
        return False, f'Could not compare with the reference: {e}'
    return False, "Rows don't match the reference."


async def run_case(question: dict, model: str, context: Context, harness: bool) -> dict:
    """One question, answered by a fresh agent so capability state never leaks between cases."""
    log = HarnessLog()
    agent = build_agent(model, context, harness, log)
    start = time.perf_counter()
    try:
        result = await agent.run(question['text'])
    except Exception as e:
        return {'sql': None, 'metricflow_query': None, 'malloy_query': None, 'explanation': None, 'rows': None,
                'error': f'{type(e).__name__}: {e}', 'seconds': time.perf_counter() - start}
    seconds = time.perf_counter() - start
    answer = result.output
    metricflow_query = answer.query.model_dump(exclude_defaults=True) if isinstance(answer, MetricFlowAnswer) else None
    if metricflow_query is not None and getattr(answer, 'metric_definitions', None):
        metricflow_query['metric_definitions'] = [d.model_dump(exclude_none=True, exclude_defaults=True)
                                                  for d in answer.metric_definitions]
    try:
        sql = await answer_sql(answer)
        rows, error = await asyncio.to_thread(db.run_readonly, sql, max_rows=10), None  # {columns, rows (first 10), row_count}
    except Exception as e:
        sql, rows, error = getattr(answer, 'sql', None), None, str(e)
    u, r = result.usage, log.reviewer_usage  # reviewer calls are part of the case's cost
    return {
        'sql': sql,
        'metricflow_query': metricflow_query,
        'malloy_query': answer.malloy if isinstance(answer, MalloyAnswer) else None,
        'explanation': answer.explanation,
        'rows': rows,
        'error': error,
        'seconds': seconds,
        'input_tokens': u.input_tokens + r.get('input_tokens', 0),
        'output_tokens': u.output_tokens + r.get('output_tokens', 0),
        'cache_read_tokens': u.cache_read_tokens + r.get('cache_read_tokens', 0),
        'cache_write_tokens': u.cache_write_tokens + r.get('cache_write_tokens', 0),
        'requests': u.requests + r.get('requests', 0),
        'tool_calls': u.tool_calls,
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
) -> dict:
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
            passed, reason = await asyncio.to_thread(grade, output['sql'], output['error'], question)
            emit({'type': 'case', 'case': i, 'passed': passed, 'seconds': output['seconds']})
        return {'name': name, 'prompt': question['text'], 'expected_sql': question['expected_sql'], **output,
                'passed': passed, 'reason': reason, 'cost_usd': case_cost(output, model),
                'trace_url': tracking.trace_url(span.trace_id) if span is not None else None}

    start = time.perf_counter()
    cases = await asyncio.gather(*(case(i, q) for i, q in enumerate(questions)))
    total_seconds = time.perf_counter() - start

    n = max(len(cases), 1)
    passed = sum(c['passed'] for c in cases)
    total = lambda key: sum(c.get(key) or 0 for c in cases)  # noqa: E731
    return {
        'model': model,
        'harness_on': harness,
        'context': context,
        'total_seconds': total_seconds,
        'avg_case_seconds': total('seconds') / n,
        'passed': passed,
        'total': len(cases),
        'pass_rate': passed / n,
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


async def run_matrix(cells: list[dict], questions: Sequence[dict], repeats: int, params: dict) -> AsyncIterator[dict]:
    """Run every cell `repeats` times, one run after another, recording it all in MLflow. Yields a plan event; then,
    within each run, a case_start and a case event per question as it takes an agent slot and is graded, a cell (or
    cell_error) event as each cell finishes, and a run_end event. Close it (contextlib.aclosing) so an interrupted matrix
    is recorded as KILLED."""
    gold.check(questions)  # before any MLflow run is opened, so a stale reference never starts a matrix
    params = {'benchmark_version': BENCHMARK_VERSION, 'models': ','.join(dict.fromkeys(c['model'] for c in cells)),
              'harness_modes': ','.join(dict.fromkeys('on' if c['harness_on'] else 'off' for c in cells)),
              'contexts': ','.join(dict.fromkeys(c['context'] for c in cells)),
              'repeats': repeats, 'prompts': len(questions), **tracking.provenance(), **params}
    tracker = await asyncio.to_thread(tracking.MatrixTracker.start, cells=cells, questions=questions, params=params)
    limiter = asyncio.Semaphore(MAX_CONCURRENT_AGENT_RUNS)

    async def run_cell(cell: dict, run_no: int, events: asyncio.Queue) -> dict:
        try:
            report = await run_eval(
                label=cell['id'], model=cell['model'], questions=questions, harness=cell['harness_on'],
                context=cell['context'], limiter=limiter,
                mlflow_run_id=tracker.cell_runs[cell['id']] if tracker else None,
                trace_tags=tracker.trace_tags(cell['id'], run_no) if tracker else None,
                emit=lambda event: events.put_nowait({**event, 'id': cell['id'], 'run': run_no}),
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
           'mlflow': tracker.urls() if tracker else None}
    status = 'KILLED'  # stays KILLED if the matrix is interrupted, so History doesn't show it running forever
    tasks: list[asyncio.Task] = []
    try:
        for run_no in range(1, repeats + 1):
            started = time.perf_counter()
            events: asyncio.Queue = asyncio.Queue()
            tasks = [asyncio.create_task(run_cell(c, run_no, events)) for c in cells]
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
