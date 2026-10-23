"""MLflow tracking for the eval matrix: results that outlive the browser tab.

One parent MLflow run per matrix submission and one child run per cell (model × harness × context). Each
repeat logs the cell's metrics at step = run number; pass^k and the means are logged when the matrix
finishes. Every case is one trace (agent, model and tool spans from MLflow's Pydantic AI autologging)
linked to its cell's run, with its pass/fail grade attached as feedback.

Logs to a local SQLite store in data/ by default, and the app serves MLflow's own UI at /mlflow from
that store (app/mlflow_ui.py). Set MLFLOW_TRACKING_URI to use a tracking server instead, or EVAL_MLFLOW=off to
disable. A tracking failure is logged and never fails an eval.
"""

from __future__ import annotations

import importlib.metadata
import json
import logging
import os
import subprocess
import threading
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path

import mlflow
from mlflow import MlflowClient
from mlflow.entities import AssessmentSource, Metric, Param, SpanType
from mlflow.entities.span import LiveSpan

_log = logging.getLogger(__name__)

DATA = Path(__file__).resolve().parent.parent / 'data'
ENABLED = os.environ.get('EVAL_MLFLOW', 'on').lower() not in ('off', '0', 'false')
TRACKING_URI = os.environ.get('MLFLOW_TRACKING_URI') or f'sqlite:///{DATA / "mlflow.db"}'
_REMOTE = TRACKING_URI.startswith(('http://', 'https://'))
# A local store's UI is mounted on our own server at /mlflow (same origin); a tracking server has its own.
EMBEDDED_UI = ENABLED and TRACKING_URI.startswith('sqlite:')
UI_URL = (TRACKING_URI if _REMOTE else '/mlflow').rstrip('/')
EXPERIMENT = 'text-to-sql-evals'

_lock = threading.Lock()  # cells finish concurrently; one writer at a time keeps the SQLite store happy
_experiment_id: str | None = None


def experiment_id() -> str | None:
    """Configure MLflow on first use. None when tracking is off or unavailable."""
    global ENABLED, _experiment_id
    if not ENABLED or _experiment_id:
        return _experiment_id
    try:
        DATA.mkdir(exist_ok=True)
        mlflow.set_tracking_uri(TRACKING_URI)
        client = MlflowClient()
        exp = client.get_experiment_by_name(EXPERIMENT)
        if exp is None:
            # Pin local artifacts next to the SQLite store, so `mlflow ui` finds them whatever its working directory.
            db_file = TRACKING_URI.removeprefix('sqlite:///')
            location = (Path(db_file).resolve().parent / 'mlartifacts').as_uri() if db_file != TRACKING_URI else None
            _experiment_id = client.create_experiment(EXPERIMENT, artifact_location=location)
        else:
            _experiment_id = exp.experiment_id
        mlflow.set_experiment(experiment_id=_experiment_id)
        mlflow.pydantic_ai.autolog(silent=True)
    except Exception:
        _log.exception('MLflow tracking disabled: could not reach %s', TRACKING_URI)
        ENABLED = False
    return _experiment_id


ROOT = Path(__file__).resolve().parent.parent
PACKAGES = ('duckdb', 'pydantic-ai-slim', 'metricflow', 'dbt-core', 'dbt-duckdb')


def provenance() -> dict[str, str]:
    """What produced a matrix's results, logged as params: code version, warehouse build, frozen references, tools."""
    from . import db, gold  # gold imports the questions; keep tracking importable on its own

    def git(*args: str) -> str:
        try:
            return subprocess.run(['git', *args], cwd=ROOT, capture_output=True, text=True, timeout=5).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return ''

    out = {'git_sha': git('rev-parse', 'HEAD') or 'unknown', 'git_dirty': str(bool(git('status', '--porcelain'))),
           'warehouse_hash': db._BUILD_HASH, 'gold_checksum': gold.checksum() or 'none'}
    for name in PACKAGES:
        try:
            out[f'version.{name}'] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            pass
    try:
        lock = json.loads((ROOT / 'malloy' / 'package-lock.json').read_text())
        out['version.malloy'] = lock['packages']['node_modules/@malloydata/malloy']['version']
    except (OSError, KeyError, ValueError):
        pass
    return out


@contextmanager
def span(run_id: str | None, name: str, inputs: object) -> Iterator[LiveSpan | None]:
    """An agent root span (one trace) linked to an MLflow run; yields None when the run isn't tracked."""
    if run_id is None:
        yield None
        return
    with mlflow.start_span(name=name, span_type=SpanType.AGENT, run_id=run_id) as s:
        s.set_inputs(inputs)
        yield s


def run_url(run_id: str) -> str:
    return f'{UI_URL}/#/experiments/{_experiment_id}/runs/{run_id}'


def trace_url(trace_id: str) -> str:
    return f'{UI_URL}/#/experiments/{_experiment_id}/traces?selectedEvaluationId={trace_id}'


class MatrixTracker:
    """MLflow runs for one matrix submission. Methods block on I/O: call them via asyncio.to_thread."""

    def __init__(self, client: MlflowClient, parent_id: str, cell_runs: dict[str, str]):
        self.client = client
        self.parent_id = parent_id
        self.cell_runs = cell_runs
        self.outcomes: dict[str, dict[int, list[bool | None]]] = {cid: {} for cid in cell_runs}  # None: infra error
        self.costs: dict[str, dict[int, float]] = {cid: {} for cid in cell_runs}

    @classmethod
    def start(cls, *, cells: Sequence[dict], questions: Sequence[dict], params: dict) -> MatrixTracker | None:
        exp = experiment_id()
        if exp is None:
            return None
        try:
            with _lock:
                client = MlflowClient()
                parent = client.create_run(exp, run_name=f'matrix {time.strftime("%Y-%m-%d %H:%M:%S")}').info.run_id
                client.log_batch(parent, params=[Param(k, str(v)) for k, v in params.items()])
                client.log_dict(parent, list(questions), 'prompts.json')
                cell_runs = {}
                for c in cells:
                    cell_params = {
                        'model': c['model'],
                        'harness': 'on' if c['harness_on'] else 'off',
                        'context': c['context'],
                        'prompts': len(questions),
                    }
                    run = client.create_run(exp, run_name=c['id'], tags={'mlflow.parentRunId': parent, 'cell': c['id']})
                    client.log_batch(run.info.run_id, params=[Param(k, str(v)) for k, v in cell_params.items()])
                    cell_runs[c['id']] = run.info.run_id
            return cls(client, parent, cell_runs)
        except Exception:
            _log.exception('MLflow: could not start runs; continuing without tracking')
            return None

    def trace_tags(self, cell_id: str, run_no: int) -> dict[str, str]:
        return {'matrix': self.parent_id, 'cell': cell_id, 'run': str(run_no)}

    def urls(self) -> dict:
        return {'matrix': run_url(self.parent_id), 'cells': {cid: run_url(rid) for cid, rid in self.cell_runs.items()}}

    def log_cell(self, cell_id: str, run_no: int, report: dict) -> None:
        try:
            mlflow.flush_trace_async_logging()  # traces must be stored before feedback can attach to them
            with _lock:
                self._log_cell(cell_id, run_no, report)
        except Exception:
            _log.exception('MLflow: could not log %s run %s', cell_id, run_no)

    def _log_cell(self, cell_id: str, run_no: int, report: dict) -> None:
        run_id = self.cell_runs[cell_id]
        cases = report['cases']
        cost = report['cost_usd']
        n = max(report['total'], 1)
        values = {
            'pass_rate': report['pass_rate'],
            'passed': report['passed'],
            'total_seconds': report['total_seconds'],
            'avg_case_seconds': report['avg_case_seconds'],
            'total_tokens': report['total_tokens'],
            'tokens_per_question': report['total_tokens'] / n,
            'tool_calls': report['tool_calls'],
            'requests': report['requests'],
            'cost_usd': cost,
            'cost_per_question_usd': cost / n,
            'infra_errors': report.get('infra_errors', 0),
            'over_budget': report.get('over_budget', 0),
            'timeouts': report.get('timeouts', 0),
            'partial': report.get('partial', 0),
        }
        ts = int(time.time() * 1000)
        self.client.log_batch(run_id, metrics=[Metric(k, float(v), ts, run_no) for k, v in values.items()])
        self.client.log_dict(run_id, report, f'run-{run_no}/report.json')  # lets History re-render this run
        self.outcomes[cell_id][run_no] = [None if c.get('status') == 'infra_error' else c['passed'] for c in cases]
        self.costs[cell_id][run_no] = cost / n

        for c in cases:
            if c.get('trace_id') and c.get('status') != 'infra_error':
                mlflow.log_feedback(trace_id=c['trace_id'], name='passed', value=c['passed'], rationale=c['reason'],
                                    source=AssessmentSource('CODE', 'results_match'))

    def log_run_time(self, run_no: int, seconds: float) -> None:
        """Wall-clock seconds for one run of the whole matrix, on the parent run at step = run number."""
        try:
            with _lock:
                self.client.log_metric(self.parent_id, 'run_seconds', seconds, int(time.time() * 1000), run_no)
        except Exception:
            _log.exception('MLflow: could not log the time of run %s', run_no)

    def log_cell_error(self, cell_id: str, run_no: int, error: str) -> None:
        try:
            with _lock:
                self.client.set_tag(self.cell_runs[cell_id], f'error.run-{run_no}', error[:5000])
        except Exception:
            _log.exception('MLflow: could not log error for %s', cell_id)

    def finish(self, status: str = 'FINISHED') -> None:
        """Log cross-run consistency per cell and close every run. status is FINISHED or KILLED."""
        try:
            with _lock:
                summary = {}
                for cell_id, run_id in self.cell_runs.items():
                    runs = self.outcomes[cell_id]
                    if runs:
                        # A question's infra-error runs are left out; one with no scored run at all drops out entirely.
                        per_prompt = [[x for x in p if x is not None] for p in zip(*runs.values())]
                        per_prompt = [p for p in per_prompt if p]
                        values = {
                            'mean_pass_rate': sum(sum(r) / max(len(r), 1) for r in runs.values()) / len(runs),
                            'pass_k': sum(all(p) for p in per_prompt) / max(len(per_prompt), 1),
                            'flaky_prompts': sum(any(p) and not all(p) for p in per_prompt),
                            'runs_completed': len(runs),
                            'mean_cost_per_question_usd': sum(self.costs[cell_id].values()) / len(runs),
                        }
                        self.client.log_batch(run_id, metrics=[Metric(k, float(v), int(time.time() * 1000), 0)
                                                               for k, v in values.items()])
                        summary[cell_id] = values
                    self.client.set_terminated(run_id, status if runs or status == 'KILLED' else 'FAILED')
                self.client.log_dict(self.parent_id, summary, 'summary.json')
                self.client.set_terminated(self.parent_id, status)
        except Exception:
            _log.exception('MLflow: could not finish runs')

