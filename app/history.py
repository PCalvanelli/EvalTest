"""Read API over the MLflow store for the History view. A matrix is a parent MLflow run; its cells are child runs
(see app/tracking.py). Traces are inspected in MLflow's own UI."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from typing import Any

import mlflow
from fastapi import APIRouter, HTTPException, Query
from mlflow import MlflowClient

from . import tracking

router = APIRouter(prefix='/api')

FINAL_METRICS = ('mean_pass_rate', 'pass_k', 'flaky_prompts', 'mean_cost_per_question_usd', 'runs_completed')


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


@router.get('/matrices')
async def list_matrices(limit: int = Query(25, ge=1, le=100), offset: int = Query(0, ge=0)) -> dict:
    """One page of matrices, newest first, plus the total count for paging."""
    def work():
        parents, children = _group(_all_runs(_experiment()))
        page = parents[offset:offset + limit]
        return {'matrices': [_matrix(p, children[p.info.run_id]) for p in page], 'total': len(parents)}

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
    matrix['prompts'] = _load(f'runs:/{matrix_id}/prompts.json') or []
    runs: dict[int, dict] = defaultdict(dict)
    errors: dict[str, str] = {}
    for child in children:
        cell = _cell(child)
        # Reports saved before cost_usd joined report.json still have it as a per-run metric.
        costs = {m.step: m.value for m in client.get_metric_history(child.info.run_id, 'cost_usd')}
        for art in client.list_artifacts(child.info.run_id):
            if not (art.is_dir and art.path.startswith('run-')):
                continue
            run_no = int(art.path.removeprefix('run-'))
            report = _load(f'runs:/{child.info.run_id}/{art.path}/report.json')
            if report is not None:
                if report.get('cost_usd') is None and run_no in costs:
                    report['cost_usd'] = costs[run_no]
                runs[run_no][cell['id']] = {**report, 'id': cell['id'], 'run': run_no}
        for key, value in child.data.tags.items():
            if key.startswith('error.run-'):
                errors[cell['id']] = f'Run {key.removeprefix("error.run-")}: {value}'
    matrix['runs'] = runs
    matrix['errors'] = errors
    matrix['run_seconds'] = {m.step: m.value for m in client.get_metric_history(matrix_id, 'run_seconds')}
    matrix['repeats'] = int(matrix['params'].get('repeats', max(runs, default=1)))
    return matrix


def _load(uri: str) -> Any:
    try:
        return mlflow.artifacts.load_dict(uri)
    except Exception:
        return None
