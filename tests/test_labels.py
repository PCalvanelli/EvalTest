"""Rule labels for failed cases (app/labels.rule_label): what the case's own data shows, before any LLM reads it."""

import pytest

from app import labels
from app.labels import count_labels, rule_label

FAIL = {'name': 'b01', 'status': 'fail', 'error': None, 'failure': None, 'partial': False}


@pytest.mark.parametrize(('case', 'label'), [
    ({**FAIL, 'failure': 'budget', 'error': 'Over budget: tool_calls_limit'}, 'budget_or_timeout'),
    ({**FAIL, 'failure': 'timeout', 'error': 'Timed out after 180 s'}, 'budget_or_timeout'),
    ({**FAIL, 'partial': True}, 'extra_columns'),
    ({**FAIL, 'error': 'Catalog Error: Table with name fct_order does not exist!'}, 'hallucinated_name'),
    ({**FAIL, 'error': 'Binder Error: Referenced column "revenue" not found in FROM clause!'}, 'hallucinated_name'),
    ({**FAIL, 'error': "'net_rev' is not defined"}, 'hallucinated_name'),
    ({**FAIL, 'error': 'Parser Error: syntax error at or near "FROM"'}, 'compile_error'),
    (FAIL, None),  # wrong rows: needs the LLM
    ({**FAIL, 'status': 'pass'}, None),
    ({**FAIL, 'status': 'infra_error', 'error': 'ModelOverloadedError'}, None),
])
def test_rule_label(case, label):
    assert rule_label(case) == label


def test_ambiguous_reference_wins(monkeypatch):
    monkeypatch.setattr(labels, 'AMBIGUOUS', {'b01'})
    assert rule_label({**FAIL, 'error': 'does not exist'}) == 'gold_ambiguous'


def test_every_label_is_described():
    assert set(labels.Judged.__args__) <= set(labels.LABELS)


def test_count_labels():
    assert count_labels([{'failure_label': 'fan_out'}, {'failure_label': 'fan_out'}, {}, {'failure_label': 'other'}]) \
        == {'fan_out': 2, 'other': 1}
