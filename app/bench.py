"""Run eval cells from the command line, without the UI, and print pass rate and tokens per run. Matrices are recorded
in MLflow like the UI's, so they show up in History. Needs ANTHROPIC_API_KEY.

    uv run python -m app.bench                                  # Sonnet, harness on, MetricFlow and Malloy, 1 run
    uv run python -m app.bench --contexts metricflow --runs 3
    uv run python -m app.bench --harness off --cases b13,b14    # baseline, two questions
    uv run python -m app.bench --harness on off                 # both, side by side
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from contextlib import aclosing

from .evals import matrix_cells, run_matrix
from .questions import QUESTION_SETS


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--model', default='claude-sonnet-5-5')
    parser.add_argument('--contexts', nargs='+', default=['metricflow', 'malloy'], choices=['raw', 'glossary', 'metricflow', 'malloy'])
    parser.add_argument('--harness', nargs='+', choices=['on', 'off'], default=['on'])
    parser.add_argument('--set', default='benchmark', choices=list(QUESTION_SETS))
    parser.add_argument('--cases', help='Comma-separated question ids (b13) or 1-based numbers (default: all).')
    parser.add_argument('--runs', type=int, default=1)
    parser.add_argument('--seed', type=int, help='Seed for the per-run question order (default: random, recorded).')
    parser.add_argument('--out', help='Write every report to this JSON file.')
    args = parser.parse_args()
    if not os.environ.get('ANTHROPIC_API_KEY'):
        parser.error('set ANTHROPIC_API_KEY')

    questions = QUESTION_SETS[args.set]
    if args.cases:
        by_id = {q['id']: q for q in questions}
        questions = [by_id[c] if c in by_id else questions[int(c) - 1] for c in args.cases.split(',')]
    cells = matrix_cells([args.model], [h == 'on' for h in args.harness], args.contexts)
    reports = []
    params = {'question_set': args.set, 'source': 'bench'}
    async with aclosing(run_matrix(cells, questions, args.runs, params, seed=args.seed)) as events:
        async for event in events:
            if event['type'] == 'cell':
                report = event['report']
                reports.append(report)
                failed = [c['name'] for c in report['cases'] if c['status'] == 'fail']
                harness = 'harness' if report['harness_on'] else 'baseline'
                print(f'run {event["run"]}  {report["context"]:<10} {harness:<8}  pass {report["passed"]}/{report["total"]} '
                      f'({report["pass_rate"]:.1%})  tokens {report["total_tokens"]:,}  cost ${report["cost_usd"]:.2f}'
                      f'  failed: {", ".join(failed) or "-"}'
                      + (f'  infra errors (not scored): {report["infra_errors"]}' if report['infra_errors'] else ''), flush=True)
            elif event['type'] == 'cell_error':
                print(f'run {event["run"]}  {event["id"]}  ERROR {event["error"]}', flush=True)
            elif event['type'] == 'run_end':
                print(f'run {event["run"]}  took {event["seconds"]:.0f}s', flush=True)
    if args.out:
        with open(args.out, 'w') as f:
            json.dump(reports, f, default=str, indent=1)


if __name__ == '__main__':
    asyncio.run(main())
