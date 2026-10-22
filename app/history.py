"""Read API over the MLflow store for the History, Traces and Labels views, plus human labels on traces.

A matrix is a parent MLflow run; its cells are child runs (see app/tracking.py). Traces are found through the
run they're linked to, so traces logged before trace tags existed still show up.
"""

from __future__ import annotations

import asyncio
import json
from collections import defaultdict
from typing import Any

import mlflow
from fastapi import APIRouter, HTTPException
from mlflow import MlflowClient
from mlflow.entities import AssessmentSource
from mlflow.tracing.constant import SpanAttributeKey
from pydantic import BaseModel

from . import tracking

router = APIRouter(prefix='/api')

HUMAN_LABEL = 'human_correct'
CHECKS = ('LLMJudge', 'ResultMatches', 'passed')  # what human labels are compared against
FINAL_METRICS = ('mean_pass_rate', 'pass_k', 'flaky_prompts', 'mean_cost_per_question_usd', 'runs_completed')
REPORT_METRICS = ('pass_rate', 'passed', 'total_seconds', 'avg_case_seconds', 'total_tokens', 'tool_calls', 'requests')
MAX_PAYLOAD_CHARS = 20_000


def _experiment() -> str:
    exp = tracking.experiment_id()
    if exp is None:
        raise HTTPException(status_code=503, detail='MLflow tracking is off (EVAL_MLFLOW=off) or unavailable.')
    return exp


def _all_runs(exp: str) -> list:
    return MlflowClient().search_runs([exp], max_results=50_000, order_by=['attributes.start_time DESC'])


def _cell(run) -> dict:
    p = run.data.params
    return {
        'id': run.data.tags.get('cell', run.info.run_name),
        'run_id': run.info.run_id,
        'model': p.get('model'),
        'harness_on': p.get('harness') == 'on',
        'context': p.get('context'),
        'status': run.info.status,
        'metrics': {k: run.data.metrics[k] for k in FINAL_METRICS if k in run.data.metrics},
        'mlflow_url': tracking.run_url(run.info.run_id),
    }


def _matrix(parent, children: list) -> dict:
    return {
        'id': parent.info.run_id,
        'name': parent.info.run_name,
        'start_time': parent.info.start_time,
        'end_time': parent.info.end_time,
        # Wall clock for the whole matrix, from MLflow's own run times (None while it's still running).
        'seconds': (parent.info.end_time - parent.info.start_time) / 1000 if parent.info.end_time else None,
        'status': parent.info.status,
        'params': dict(parent.data.params),
        'cells': sorted((_cell(c) for c in children), key=lambda c: c['id']),
        'mlflow_url': tracking.run_url(parent.info.run_id),
    }


def _group(runs: list) -> tuple[list, dict[str, list]]:
    parents, children = [], defaultdict(list)
    for r in runs:
        parent_id = r.data.tags.get('mlflow.parentRunId')
        if parent_id:
            children[parent_id].append(r)
        else:
            parents.append(r)
    return parents, children


# ─────────────────────────── Matrices ───────────────────────────

@router.get('/matrices')
async def list_matrices(limit: int = 100) -> list[dict]:
    def work():
        parents, children = _group(_all_runs(_experiment()))
        return [_matrix(p, children[p.info.run_id]) for p in parents[:limit]]

    return await asyncio.to_thread(work)


@router.get('/matrices/{matrix_id}')
async def get_matrix(matrix_id: str) -> dict:
    """Everything the Run tab's results view needs to re-render a past matrix."""
    return await asyncio.to_thread(_load_matrix, matrix_id)


def _load_matrix(matrix_id: str) -> dict:
    exp = _experiment()
    client = MlflowClient()
    try:
        parent = client.get_run(matrix_id)
    except Exception:
        raise HTTPException(status_code=404, detail='No such matrix.') from None
    children = [r for r in _all_runs(exp) if r.data.tags.get('mlflow.parentRunId') == matrix_id]
    matrix = _matrix(parent, children)
    matrix['prompts'] = _load(f'runs:/{matrix_id}/prompts.json', as_json=True) or []
    matrix['rubric'] = _load(f'runs:/{matrix_id}/rubric.md') or ''
    runs: dict[int, dict] = defaultdict(dict)
    errors: dict[str, str] = {}
    for child in children:
        cell = _cell(child)
        for art in client.list_artifacts(child.info.run_id):
            if not (art.is_dir and art.path.startswith('run-')):
                continue
            run_no = int(art.path.removeprefix('run-'))
            report = _load(f'runs:/{child.info.run_id}/{art.path}/report.json', as_json=True)
            if report is None:  # matrices logged before report.json existed: rebuild from cases + metrics
                report = _rebuild_report(client, child, cell, run_no)
            if report is not None:
                runs[run_no][cell['id']] = {**report, 'id': cell['id'], 'run': run_no}
        for key, value in child.data.tags.items():
            if key.startswith('error.run-'):
                errors[cell['id']] = f'Run {key.removeprefix("error.run-")}: {value}'
    matrix['runs'] = runs
    matrix['errors'] = errors
    matrix['run_seconds'] = {m.step: m.value for m in client.get_metric_history(matrix_id, 'run_seconds')}
    matrix['repeats'] = int(matrix['params'].get('repeats', max(runs, default=1)))
    return matrix


def _rebuild_report(client: MlflowClient, run, cell: dict, run_no: int) -> dict | None:
    cases = _load(f'runs:/{run.info.run_id}/run-{run_no}/cases.json', as_json=True)
    if cases is None:
        return None
    report = {'model': cell['model'], 'harness_on': cell['harness_on'], 'context': cell['context'], 'cases': cases,
              'harness': [] if not cell['harness_on'] else run.data.params.get('harness_capabilities', '').split(','),
              'total': len(cases), 'glossary_searches': sum(c.get('glossary_searches', 0) for c in cases)}
    for key in REPORT_METRICS:
        at_step = [m.value for m in client.get_metric_history(run.info.run_id, key) if m.step == run_no]
        report[key] = at_step[-1] if at_step else 0
    report['passed'] = int(report['passed'])
    return report


def _load(uri: str, as_json: bool = False) -> Any:
    try:
        return mlflow.artifacts.load_dict(uri) if as_json else mlflow.artifacts.load_text(uri)
    except Exception:
        return None


# ─────────────────────────── Traces ───────────────────────────

def _assessments(info) -> dict[str, dict]:
    """Latest assessment per name (a relabel supersedes the earlier label)."""
    latest: dict[str, dict] = {}
    for a in sorted(info.assessments or [], key=lambda a: a.create_time_ms or 0):
        if getattr(a, 'valid', True) is False or a.feedback is None:
            continue
        latest[a.name] = {'value': a.feedback.value, 'source': a.source.source_type, 'source_id': a.source.source_id,
                          'rationale': a.rationale, 'time': a.create_time_ms}
    return latest


def _prompt(info) -> str | None:
    try:
        inputs = json.loads(info.trace_metadata.get('mlflow.traceInputs') or info.request_preview or '{}')
    except (TypeError, ValueError):
        return info.request_preview
    return inputs.get('prompt') if isinstance(inputs, dict) else None


def _summary(info, cells_by_run: dict[str, str]) -> dict:
    tags = info.tags
    source_run = info.trace_metadata.get('mlflow.sourceRun')
    usage = info.token_usage or {}
    return {
        'trace_id': info.trace_id,
        'kind': tags.get('kind', 'agent'),
        'cell': tags.get('cell') or cells_by_run.get(source_run),
        'run_id': source_run,
        'case': tags.get('case'),
        'run': int(tags['run']) if tags.get('run', '').isdigit() else None,
        'state': str(info.state),
        'request_time': info.request_time,
        'duration_ms': info.execution_duration,
        'tokens': usage.get('total_tokens'),
        'prompt': _prompt(info),
        'assessments': _assessments(info),
    }


def _search(exp: str, run_ids: list[str] | None, filters: list[str], limit: int) -> list:
    filter_string = ' AND '.join(filters) or None
    kwargs = dict(locations=[exp], filter_string=filter_string, max_results=limit, include_spans=False,
                  return_type='list', order_by=['timestamp_ms DESC'])
    if run_ids is None:
        return list(mlflow.search_traces(**kwargs))
    return [t for rid in run_ids for t in mlflow.search_traces(run_id=rid, **kwargs)]


def _scope(exp: str, matrix: str | None, run_id: str | None) -> tuple[list[str] | None, dict[str, str]]:
    """Cell run ids to search (None = whole experiment) and a run id → cell id map."""
    runs = _all_runs(exp)
    cells_by_run = {r.info.run_id: r.data.tags.get('cell', r.info.run_name) for r in runs}
    if run_id:
        return [run_id], cells_by_run
    if matrix:
        return [r.info.run_id for r in runs if r.data.tags.get('mlflow.parentRunId') == matrix], cells_by_run
    return None, cells_by_run


def _quote(v: str) -> str:
    return "'" + v.replace("'", "") + "'"


@router.get('/traces')
async def list_traces(matrix: str | None = None, run_id: str | None = None, case: str | None = None,
                      kind: str | None = None, outcome: str | None = None, limit: int = 500) -> list[dict]:
    def work():
        exp = _experiment()
        run_ids, cells_by_run = _scope(exp, matrix, run_id)
        filters = [f'tags.`case` = {_quote(case)}'] if case else []
        if kind:
            filters.append(f'tags.`kind` = {_quote(kind)}')
        rows = [_summary(t.info, cells_by_run) for t in _search(exp, run_ids, filters, limit)]
        if outcome in ('pass', 'fail'):
            want = outcome == 'pass'
            rows = [r for r in rows if 'passed' in r['assessments'] and r['assessments']['passed']['value'] is want]
        return sorted(rows, key=lambda r: r['request_time'] or 0, reverse=True)[:limit]

    return await asyncio.to_thread(work)


def _payload(value: Any) -> dict:
    text = value if isinstance(value, str) else json.dumps(value, indent=2, default=str)
    return {'text': text[:MAX_PAYLOAD_CHARS], 'truncated': len(text) > MAX_PAYLOAD_CHARS}


@router.get('/traces/{trace_id}')
async def get_trace(trace_id: str) -> dict:
    def work():
        exp = _experiment()
        trace = mlflow.get_trace(trace_id)
        if trace is None:
            raise HTTPException(status_code=404, detail='No such trace.')
        _, cells_by_run = _scope(exp, None, None)
        spans = trace.data.spans
        t0 = min((s.start_time_ns for s in spans), default=0)
        out = []
        for s in spans:
            error = s.status.description or None
            for e in s.events or []:
                if e.name == 'exception':
                    error = e.attributes.get('exception.message') or error
            out.append({
                'id': s.span_id,
                'parent_id': s.parent_id,
                'name': s.name,
                'type': s.span_type,
                'start_ms': (s.start_time_ns - t0) / 1e6,
                'duration_ms': ((s.end_time_ns or s.start_time_ns) - s.start_time_ns) / 1e6,
                'status': str(s.status.status_code).rsplit('.', 1)[-1],
                'error': error,
                'tokens': s.get_attribute(SpanAttributeKey.CHAT_USAGE),
                'model': s.get_attribute(SpanAttributeKey.MODEL),
                'inputs': _payload(s.inputs),
                'outputs': _payload(s.outputs),
            })
        root = next((s for s in spans if s.parent_id is None), None)
        return {**_summary(trace.info, cells_by_run), 'spans': out,
                'output': root.outputs if root is not None and isinstance(root.outputs, dict) else None}

    return await asyncio.to_thread(work)


class LabelIn(BaseModel):
    correct: bool
    note: str | None = None


@router.post('/traces/{trace_id}/label')
async def label_trace(trace_id: str, body: LabelIn) -> dict:
    """Record your own verdict on an agent's answer; the latest label wins."""
    def work():
        _experiment()
        mlflow.log_feedback(trace_id=trace_id, name=HUMAN_LABEL, value=body.correct,
                            rationale=(body.note or '').strip() or None,
                            source=AssessmentSource('HUMAN', 'playground'))
        return _assessments(mlflow.get_trace(trace_id).info)

    return await asyncio.to_thread(work)


# ─────────────────────────── Labels: judge vs. human agreement ───────────────────────────

def agreement(pairs: list[tuple[bool, bool]]) -> dict:
    """Agreement and Cohen's kappa between human labels and a check, from (human, check) pairs."""
    n = len(pairs)
    tp = sum(h and c for h, c in pairs)
    tn = sum(not h and not c for h, c in pairs)
    fp = sum(not h and c for h, c in pairs)  # check passed an answer you marked wrong
    fn = sum(h and not c for h, c in pairs)  # check failed an answer you marked correct
    if n == 0:
        return {'n': 0, 'agreement': None, 'kappa': None, 'tp': 0, 'tn': 0, 'fp': 0, 'fn': 0}
    po = (tp + tn) / n
    pe = ((tp + fn) / n) * ((tp + fp) / n) + ((tn + fp) / n) * ((tn + fn) / n)
    kappa = (po - pe) / (1 - pe) if pe < 1 else None  # undefined when both sides are constant
    return {'n': n, 'agreement': po, 'kappa': kappa, 'tp': tp, 'tn': tn, 'fp': fp, 'fn': fn}


@router.get('/labels')
async def labels(matrix: str | None = None, limit: int = 2000) -> dict:
    def work():
        exp = _experiment()
        run_ids, cells_by_run = _scope(exp, matrix, None)
        rows = [_summary(t.info, cells_by_run) for t in _search(exp, run_ids, ["tags.`kind` = 'agent'"], limit)]
        labeled, queue = [], []
        for r in rows:
            a = r['assessments']
            (labeled if HUMAN_LABEL in a else queue).append(r)
        checks = {}
        for check in CHECKS:
            pairs = [(r['assessments'][HUMAN_LABEL]['value'] is True, r['assessments'][check]['value'] is True)
                     for r in labeled if check in r['assessments']]
            checks[check] = agreement(pairs)
        disagreements = [
            {**r, 'disagrees_with': [c for c in CHECKS if c in r['assessments']
                                     and (r['assessments'][c]['value'] is True) != (r['assessments'][HUMAN_LABEL]['value'] is True)]}
            for r in labeled
        ]

        def priority(r):  # most informative first: result check and judge disagree, then judge-only prompts
            a = r['assessments']
            if 'ResultMatches' in a and 'LLMJudge' in a and a['ResultMatches']['value'] != a['LLMJudge']['value']:
                return 0
            return 1 if 'ResultMatches' not in a else 2

        queue.sort(key=lambda r: (priority(r), -(r['request_time'] or 0)))
        return {'labeled': len(labeled), 'total': len(rows), 'checks': checks,
                'disagreements': [d for d in disagreements if d['disagrees_with']],
                'queue': [{**r, 'priority': priority(r)} for r in queue]}

    return await asyncio.to_thread(work)
