"""Turn a case's token usage into dollar cost, at the model's prices from the settings (app/settings.py)."""

from . import settings


def case_cost(case: dict, model: str) -> float:
    """List-price USD cost of one case's token usage."""
    p = settings.prices(model)
    if p is None or case.get('input_tokens') is None:
        return 0.0
    read, write = case.get('cache_read_tokens') or 0, case.get('cache_write_tokens') or 0
    fresh = max(0, case['input_tokens'] - read - write)  # input_tokens includes cached tokens
    return (fresh * p.input + read * p.cache_read + write * p.cache_write + (case.get('output_tokens') or 0) * p.output) / 1e6
