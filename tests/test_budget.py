"""Per-case budgets (app/evals._attempt): a case stopped by a limit still reports what it spent, so its cost counts."""

import asyncio

from pydantic_ai.models.test import TestModel

from app import evals


def test_over_budget_case_keeps_its_usage(monkeypatch):
    agent = evals.build_agent('claude-haiku-5-5', 'raw', False, evals.HarnessLog())
    monkeypatch.setattr(evals, 'build_agent', lambda *args: agent)
    monkeypatch.setattr(evals, 'TOOL_CALL_LIMIT', 1)  # the test model calls every tool at once: over budget
    with agent.override(model=TestModel(call_tools='all')):
        case = asyncio.run(evals._attempt({'id': 'b01', 'text': 'Total revenue?'}, 'claude-haiku-5-5', 'raw', False))
    assert case['failure'] == 'budget'
    assert case['input_tokens'] > 0 and case['requests'] == 1
