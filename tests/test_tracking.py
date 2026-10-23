"""MLflow bookkeeping (app/tracking.MatrixTracker): cross-run summaries when a matrix finishes."""

from app.tracking import MatrixTracker


class FakeClient:
    def __init__(self):
        self.metrics, self.terminated = {}, {}

    def log_batch(self, run_id, metrics=()):
        self.metrics.setdefault(run_id, {}).update({m.key: m.value for m in metrics})

    def log_dict(self, run_id, data, path):
        pass

    def set_terminated(self, run_id, status):
        self.terminated[run_id] = status


def test_finish_with_infra_errors_closes_every_run():
    client = FakeClient()
    tracker = MatrixTracker(client, 'parent', {'cell': 'child'})
    tracker.outcomes['cell'] = {1: [True, None, False], 2: [True, True, None]}  # None: an infra error, not scored
    tracker.costs['cell'] = {1: 0.1, 2: 0.1}
    tracker.finish()
    assert client.terminated == {'child': 'FINISHED', 'parent': 'FINISHED'}
    m = client.metrics['child']
    assert m['mean_pass_rate'] == (1 / 2 + 2 / 2) / 2
    assert m['pass_k'] == 2 / 3  # prompts 1 and 2 passed every scored run; prompt 3 failed its only one
    assert m['flaky_prompts'] == 0


def test_finish_without_prices_logs_no_cost():
    client = FakeClient()
    tracker = MatrixTracker(client, 'parent', {'cell': 'child'})
    tracker.outcomes['cell'] = {1: [True, False]}  # a model with no prices: no cost recorded for its runs
    tracker.finish()
    assert 'mean_cost_per_question_usd' not in client.metrics['child']
    assert client.terminated['child'] == 'FINISHED'
