"""Measured pass rates per target and question tag (app/history.measured), pooled from History."""

import asyncio
from types import SimpleNamespace

from app import history
from app.evals import BENCHMARK_VERSION


def report(model, context, harness, outcomes):
    return {'model': model, 'context': context, 'harness_on': harness,
            'cases': [{'name': name, 'passed': passed, 'status': 'pass' if passed else 'fail'} for name, passed in outcomes]}


def test_pools_by_target_and_tag(monkeypatch):
    parent = SimpleNamespace(info=SimpleNamespace(run_id='m1'),
                             data=SimpleNamespace(params={'benchmark_version': str(BENCHMARK_VERSION)}))
    runs = {1: {'a': report('haiku', 'raw', True, [('b01', True), ('b03', False)]),
                'b': report('sonnet', 'raw', True, [('b01', True)]),
                'c': report('haiku', 'raw', False, [('b01', False)])},
            2: {'a': report('haiku', 'raw', True, [('b01', True), ('b03', True)])}}
    monkeypatch.setattr(history, '_experiment', lambda: 'exp')
    monkeypatch.setattr(history, '_all_runs', lambda exp: [])
    monkeypatch.setattr(history, '_group', lambda runs_: ([parent], {}))
    monkeypatch.setattr(history, '_cached_matrix', lambda mid: {'runs': runs})
    out = asyncio.run(history.measured())
    assert out['model'] == 'haiku' and out['models'] == {'haiku': 5, 'sonnet': 1}
    raw = out['cells']['raw']
    assert raw['all'] == {'passed': 3, 'n': 4}  # harness only: the baseline case is left out
    assert raw['fan-out-risk'] == {'passed': 2, 'n': 2}  # b01's tags
    assert raw['consistent'] == {'passed': 1, 'n': 2}  # b01 passed both repeats; b03 didn't
