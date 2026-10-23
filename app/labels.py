"""Why a case failed: one primary label per failed case. Rules label what the case's own data already shows, at grading
time. An LLM labels the rest (wrong rows) afterwards, from the prompt, the reference SQL, the agent's query and both
results. Labels explain failures; they never change pass/fail.

    uv run python -m app.labels <matrix id>              # label a past matrix's unlabeled failures
    uv run python -m app.labels <matrix id> --relabel    # redo its LLM labels too
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
from typing import Literal

import mlflow
from mlflow.entities import AssessmentSource
from pydantic import BaseModel, Field
from pydantic_ai import Agent

from . import db, gold, settings, tracking
from .agent import make_model
from .questions import AMBIGUOUS

LABELS = {
    'join_path': 'Wrong join path or missing join',
    'fan_out': 'Fan-out or double counting',
    'metric_definition': 'Wrong metric definition or aggregation',
    'filter_or_grain': 'Wrong filter, time range or grain',
    'extra_columns': 'Right numbers, extra columns',
    'hallucinated_name': 'Made-up table, column or metric',
    'compile_error': 'Final query never ran',
    'budget_or_timeout': 'Over budget or timed out',
    'gold_ambiguous': 'Reference answer is ambiguous',
    'other': 'Other',
}
Judged = Literal['join_path', 'fan_out', 'metric_definition', 'filter_or_grain', 'other']  # what only reading can tell
_UNKNOWN_NAME = re.compile(r'does not exist|not found|is not defined|unknown (?:metric|dimension|column|table)|'
                           r'no such|unable to find|unrecognized', re.IGNORECASE)


def rule_label(case: dict) -> str | None:
    """The label a failed case's own data shows, or None when it needs reading (wrong rows)."""
    if case.get('status') != 'fail':
        return None
    if case.get('name') in AMBIGUOUS:
        return 'gold_ambiguous'
    if case.get('failure') in ('budget', 'timeout'):
        return 'budget_or_timeout'
    if case.get('partial'):
        return 'extra_columns'
    if case.get('error'):
        return 'hallucinated_name' if _UNKNOWN_NAME.search(case['error']) else 'compile_error'
    return None


class Verdict(BaseModel):
    label: Judged = Field(description='The one primary reason the answer is wrong.')
    rationale: str = Field(description='One sentence: what the query did wrong, specifically.')


INSTRUCTIONS = """\
An analytics agent answered a business question with a query whose result does not match the reference answer. Name
the one primary reason, from the query and the two results:
- join_path: joined the wrong tables or keys, or left out a join the answer needs.
- fan_out: a join multiplied rows, so sums or counts are inflated (double counting).
- metric_definition: computed the metric differently from the reference (wrong formula, aggregation, or which rows
  the metric counts, such as including cancelled orders or not netting refunds).
- filter_or_grain: wrong time window, filter value, or grouping grain.
- other: none of these.
Judge against the reference SQL; it encodes the company's definitions. Answer in one sentence, naming the specific
mistake."""


def _brief(rows: list, limit: int = 10) -> str:
    return json.dumps(rows[:limit], default=str) + (f' … ({len(rows)} rows)' if len(rows) > limit else '')


async def judge(case: dict, question: dict, model: str | None = None) -> Verdict:
    """The LLM's label for a wrong-rows failure."""
    try:
        got = _brief(db.all_rows(case['sql'])) if case.get('sql') else 'no rows'
    except Exception as e:
        got = f'could not re-run: {e}'
    query = case.get('malloy_query') or (json.dumps(case['metricflow_query']) if case.get('metricflow_query') else None)
    result = await Agent(make_model(model or settings.current().labeler_model), output_type=Verdict, instructions=INSTRUCTIONS).run(
        f'Question:\n{question["text"]}\n\nReference SQL:\n{question["expected_sql"]}\n\n'
        f'Reference result:\n{_brief(gold.rows(question["id"]))}\n\n'
        + (f'Agent query:\n{query}\n\nIt compiled to:\n' if query else 'Agent SQL:\n')
        + f'{case.get("sql")}\n\nAgent explanation:\n{case.get("explanation")}\n\nAgent result:\n{got}'
    )
    return result.output


def log_label(case: dict, source: AssessmentSource) -> None:
    """Attach a case's label to its MLflow trace, beside its pass/fail."""
    if case.get('trace_id') and case.get('failure_label'):
        mlflow.log_feedback(trace_id=case['trace_id'], name='failure_label', value=case['failure_label'],
                            rationale=case.get('label_rationale'), source=source)


async def label_matrix(matrix_id: str, *, model: str | None = None, relabel: bool = False) -> dict[str, int]:
    """Label every failed case in a past matrix that rules couldn't (and, with relabel, redo earlier LLM labels).
    Saves the labels into each repeat's report, so History shows them, and onto the traces."""
    from mlflow import MlflowClient

    from .history import _load_matrix
    from .questions import QUESTION_SETS

    model = model or settings.current().labeler_model
    exp = tracking.experiment_id()  # points MLflow at the app's store before any client is made
    matrix = await asyncio.to_thread(_load_matrix, matrix_id)
    client = MlflowClient()
    by_id = {q['id']: q for qs in QUESTION_SETS.values() for q in qs}
    cell_runs = {r.data.tags.get('cell'): r.info.run_id for r in client.search_runs(
        [exp], filter_string=f"tags.mlflow.parentRunId = '{matrix_id}'")}
    limiter = asyncio.Semaphore(6)
    counts: dict[str, int] = {}

    async def one(case: dict) -> None:
        async with limiter:
            verdict = await judge(case, by_id[case['name']], model)
        case.update(failure_label=verdict.label, label_source='llm', label_rationale=verdict.rationale)
        log_label(case, AssessmentSource('LLM_JUDGE', model))
        counts[verdict.label] = counts.get(verdict.label, 0) + 1

    for run_no, cells in matrix['runs'].items():
        for cell_id, report in cells.items():
            todo = [c for c in report['cases'] if c.get('status') == 'fail' and c.get('name') in by_id
                    and (not c.get('failure_label') or (relabel and c.get('label_source') == 'llm'))]
            if not todo:
                continue
            await asyncio.gather(*(one(c) for c in todo))
            report['failure_labels'] = count_labels(report['cases'])
            await asyncio.to_thread(client.log_dict, cell_runs[cell_id], report, f'run-{run_no}/report.json')
    return counts


def count_labels(cases: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for c in cases:
        if c.get('failure_label'):
            counts[c['failure_label']] = counts.get(c['failure_label'], 0) + 1
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('matrix', help='Matrix (parent MLflow run) id, as in a History link: #/history?run=<id>')
    parser.add_argument('--model', help='Default: the labeler model in Settings.')
    parser.add_argument('--relabel', action='store_true', help='Also redo failures an LLM labeled before.')
    args = parser.parse_args()
    counts = asyncio.run(label_matrix(args.matrix, model=args.model, relabel=args.relabel))
    print('Labeled:', ', '.join(f'{LABELS[k]}: {n}' for k, n in sorted(counts.items(), key=lambda kv: -kv[1])) or 'nothing to label')


if __name__ == '__main__':
    main()
