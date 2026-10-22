"""Run eval cells from the command line, without the UI, and print pass rate and tokens per run. Matrices are recorded
in MLflow like the UI's, so they show up in History. Needs ANTHROPIC_API_KEY.

    uv run python -m app.bench                                  # Sonnet, harness on, MetricFlow and Malloy, 1 run
    uv run python -m app.bench --contexts metricflow --runs 3
    uv run python -m app.bench --harness off --cases 13,14      # baseline, two questions
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import time

from . import tracking
from .agent import HARNESS_CAPABILITIES
from .evals import run_eval
from .expressibility import ROOT
from .main import MAX_CONCURRENT_AGENT_RUNS, cell_id


def sample_config() -> dict:
    """The sample prompt sets and rubric from static/js/config.js (the UI's source of truth)."""
    js = ("import('./static/js/config.js').then(m => console.log(JSON.stringify("
          "{benchmark: m.SAMPLE_PROMPTS, holdout: m.HOLDOUT_PROMPTS, rubric: m.DEFAULT_RUBRIC})))")
    return json.loads(subprocess.run(['node', '-e', js], cwd=ROOT, capture_output=True, text=True, check=True).stdout)


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--model', default='claude-sonnet-5-5')
    parser.add_argument('--judge-model', default='claude-sonnet-5-5')
    parser.add_argument('--contexts', nargs='+', default=['metricflow', 'malloy'], choices=['raw', 'metricflow', 'malloy'])
    parser.add_argument('--harness', choices=['on', 'off'], default='on')
    parser.add_argument('--capabilities', help='Comma-separated harness capabilities (default: the default set).')
    parser.add_argument('--set', default='benchmark', choices=['benchmark', 'holdout'])
    parser.add_argument('--cases', help='Comma-separated 1-based question numbers (default: all).')
    parser.add_argument('--runs', type=int, default=1)
    parser.add_argument('--out', help='Write every report to this JSON file.')
    args = parser.parse_args()

    config = sample_config()
    prompts = config[args.set]
    if args.cases:
        prompts = [prompts[int(i) - 1] for i in args.cases.split(',')]
    harness = (args.capabilities.split(',') if args.capabilities
               else [k for k, v in HARNESS_CAPABILITIES.items() if v['default']]) if args.harness == 'on' else []
    cells = [{'id': cell_id(args.model, bool(harness), c), 'model': args.model, 'harness_on': bool(harness), 'context': c}
             for c in args.contexts]
    tracker = tracking.MatrixTracker.start(
        cells=cells, prompts=prompts, rubric=config['rubric'], harness=harness, judge_model=args.judge_model,
        params={'models': args.model, 'harness_modes': args.harness, 'contexts': ','.join(args.contexts),
                'harness_capabilities': ','.join(harness) or 'none', 'judge_model': args.judge_model,
                'repeats': args.runs, 'prompts': len(prompts), 'source': 'bench'})
    limiter = asyncio.Semaphore(MAX_CONCURRENT_AGENT_RUNS)
    reports = []
    status = 'KILLED'  # stays KILLED if the bench is interrupted, so History doesn't show it running forever
    try:
        for run_no in range(1, args.runs + 1):
            started = time.perf_counter()

            async def cell_report(cell: dict) -> dict:
                report = await run_eval(
                    label=cell['id'], api_key=os.environ['ANTHROPIC_API_KEY'], workspace_id=None, model=args.model,
                    judge_model=args.judge_model, prompts=prompts, rubric=config['rubric'], harness=harness,
                    context=cell['context'], limiter=limiter,
                    mlflow_run_id=tracker.cell_runs[cell['id']] if tracker else None,
                    trace_tags=tracker.trace_tags(cell['id'], run_no) if tracker else None)
                if tracker:
                    await asyncio.to_thread(tracker.log_cell, cell['id'], run_no, report)
                return {**report, 'run': run_no}

            for report in await asyncio.gather(*(cell_report(c) for c in cells)):
                reports.append(report)
                failed = [f'{c["name"]}' for c in report['cases'] if not c['passed']]
                print(f'run {run_no}  {report["context"]:<10}  pass {report["passed"]}/{report["total"]} '
                      f'({report["pass_rate"]:.1%})  tokens {report["total_tokens"]:,}  requests {report["requests"]}'
                      f'  failed: {", ".join(failed) or "-"}', flush=True)
            seconds = time.perf_counter() - started
            print(f'run {run_no}  took {seconds:.0f}s', flush=True)
            if tracker:
                tracker.log_run_time(run_no, seconds)
        status = 'FINISHED'
    finally:
        if tracker:
            tracker.finish(status)
    if args.out:
        with open(args.out, 'w') as f:
            json.dump(reports, f, default=str, indent=1)


if __name__ == '__main__':
    asyncio.run(main())
