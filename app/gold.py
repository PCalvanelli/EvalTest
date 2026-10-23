"""Frozen reference results. Each question's expected SQL is run once and its rows saved to gold/gold.json, with the
warehouse build they came from and a checksum, so grading compares against fixed answers rather than recomputing them.
A matrix refuses to run when the file is missing, edited by hand, from another warehouse build, or older than a
question's expected SQL.

    uv run python -m app.gold freeze   # after changing a question's expected SQL or the warehouse
    uv run python -m app.gold verify   # re-run every expected SQL and compare with the frozen rows
"""

from __future__ import annotations

import argparse
import functools
import hashlib
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from . import db
from .questions import QUESTION_SETS

PATH = Path(__file__).resolve().parent.parent / 'gold' / 'gold.json'
VERIFY_REL_TOL = 1e-9  # re-runs only differ by float summation order


class GoldError(RuntimeError):
    pass


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _jsonable(v: object) -> object:
    return v if v is None or isinstance(v, (bool, int, float, str)) else str(v)  # dates and the like compare as text


def _checksum(questions: dict) -> str:
    return _sha(json.dumps(questions, sort_keys=True))


def freeze() -> None:
    questions = {q['id']: {'sql_sha': _sha(q['expected_sql']),
                           'rows': [[_jsonable(v) for v in r] for r in db.all_rows(q['expected_sql'])]}
                 for qs in QUESTION_SETS.values() for q in qs}
    PATH.parent.mkdir(exist_ok=True)
    PATH.write_text(json.dumps({'warehouse_hash': db._BUILD_HASH, 'checksum': _checksum(questions),
                                'questions': questions}, indent=1) + '\n')
    _load.cache_clear()


@functools.cache
def _load() -> dict:
    if not PATH.exists():
        raise GoldError(f'No frozen reference results at {PATH}. Run: uv run python -m app.gold freeze')
    gold = json.loads(PATH.read_text())
    if gold['checksum'] != _checksum(gold['questions']):
        raise GoldError(f'{PATH} was edited by hand (checksum mismatch). Re-freeze it: uv run python -m app.gold freeze')
    if gold['warehouse_hash'] != db._BUILD_HASH:
        raise GoldError(f'{PATH} was frozen on warehouse build {gold["warehouse_hash"]}, not the current {db._BUILD_HASH}. '
                        'Re-freeze it: uv run python -m app.gold freeze')
    return gold


def check(questions: Sequence[dict]) -> None:
    """Raise GoldError unless every question has frozen rows from its current expected SQL. Runs no queries."""
    frozen = _load()['questions']
    stale = [q['id'] for q in questions if frozen.get(q['id'], {}).get('sql_sha') != _sha(q['expected_sql'])]
    if stale:
        raise GoldError(f'No frozen rows for the current expected SQL of {", ".join(stale)}. '
                        'Run: uv run python -m app.gold freeze')


def rows(question_id: str) -> list[list]:
    return _load()['questions'][question_id]['rows']


def checksum() -> str | None:
    """The frozen file's checksum, for run provenance; None when it is missing or invalid."""
    try:
        return _load()['checksum']
    except GoldError:
        return None


def verify() -> list[str]:
    """Problems with the frozen file: anything check() rejects, or a question whose expected SQL now returns other rows."""
    try:
        questions = [q for qs in QUESTION_SETS.values() for q in qs]
        check(questions)
    except GoldError as e:
        return [str(e)]
    return [f'{q["id"]}: expected SQL no longer returns the frozen rows' for q in questions
            if not db.rows_match(db.all_rows(q['expected_sql']), rows(q['id']), rel_tol=VERIFY_REL_TOL)]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('command', choices=['freeze', 'verify'])
    args = parser.parse_args()
    if args.command == 'freeze':
        freeze()
        print(f'Froze {len(_load()["questions"])} questions to {PATH}')
        return
    problems = verify()
    for p in problems:
        print(p)
    if problems:
        sys.exit(1)
    print(f'All {len(_load()["questions"])} frozen results match.')


if __name__ == '__main__':
    main()
