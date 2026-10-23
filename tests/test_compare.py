"""The answer comparator (app/db.compare_rows) and grading against the frozen reference rows."""

import pytest

from app import db, evals, gold
from app.questions import QUESTION_SETS

EXPECTED = [('2025-01-01', 10.0), ('2025-02-01', 20.0)]


@pytest.mark.parametrize(('actual', 'outcome'), [
    (EXPECTED, 'match'),
    (EXPECTED[::-1], 'match'),  # row order ignored
    ([(10.0, '2025-01-01'), (20.0, '2025-02-01')], 'match'),  # column order ignored
    ([('2025-01-01', 10.04), ('2025-02-01', 20.0)], 'match'),  # 0.4% off: within REL_TOL
    ([('2025-01-01', 10.06), ('2025-02-01', 20.0)], 'mismatch'),  # 0.6% off
    ([('2025-01-01 00:00:00', 10), ('2025-02', 20)], 'match'),  # a midnight timestamp or YYYY-MM is that day
    ([('2025-01-01', 10.0, 'x'), ('2025-02-01', 20.0, 'y')], 'partial'),  # extra column
    ([('2025-01-01',), ('2025-02-01',)], 'mismatch'),  # missing column
    (EXPECTED[:1], 'mismatch'),  # missing row
    ([('2025-01-01', None), ('2025-02-01', 20.0)], 'mismatch'),  # NULL is not a number
])
def test_compare_rows(actual, outcome):
    assert db.compare_rows(actual, EXPECTED) == outcome


def test_order_matters():
    assert db.compare_rows(EXPECTED[::-1], EXPECTED, order_matters=True) == 'mismatch'
    assert db.compare_rows(EXPECTED, EXPECTED, order_matters=True) == 'match'
    assert db.compare_rows([(10.0, '2025-01-01'), (20.0, '2025-02-01')], EXPECTED, order_matters=True) == 'match'


def test_numbers_mode_ignores_labels():
    assert db.compare_rows([('first', 1.0), ('repeat', 2.0)], [(True, 1.0), (False, 2.0)], 'numbers') == 'match'


def test_empty_results():
    assert db.compare_rows([], []) == 'match'
    assert db.compare_rows([], EXPECTED) == 'mismatch'


def test_rows_match_is_an_exact_match_only():
    assert db.rows_match(EXPECTED, EXPECTED)
    assert not db.rows_match([('2025-01-01', 10.0, 'x'), ('2025-02-01', 20.0, 'y')], EXPECTED)


QUESTIONS = [q for qs in QUESTION_SETS.values() for q in qs]


def test_gold_is_current():
    gold.check(QUESTIONS)


@pytest.mark.parametrize('question', QUESTIONS, ids=[q['id'] for q in QUESTIONS])
def test_reference_sql_passes(question):
    assert evals.grade(question['expected_sql'], None, question) == ('pass', None)


def test_wrong_and_partial_answers_fail():
    q = QUESTION_SETS['benchmark'][0]  # b01: one number
    assert evals.grade('SELECT 1', None, q)[0] == 'fail'
    sql = q['expected_sql'].replace('SELECT SUM(', "SELECT 'extra', SUM(", 1)
    assert evals.grade(sql, None, q)[0] == 'partial'
    assert evals.grade(None, 'boom', q) == ('fail', 'Query failed: boom')
