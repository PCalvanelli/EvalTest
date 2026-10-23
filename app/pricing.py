"""Per-model token prices (USD per million tokens), used to turn token usage into dollar cost.

Anthropic first-party list prices as of 2026-10-06. Cache reads for Opus/Sonnet are list prices; the Haiku
cache read and all cache writes use Anthropic's standard multipliers (read = 0.1× input, 5-minute write =
1.25× input). Haiku 5.5's price applies to prompts up to 100K tokens, which covers every request here.
"""

PRICING: dict[str, dict[str, float]] = {
    'claude-opus-5-5': {'input': 4.00, 'output': 20.00, 'cache_read': 0.20, 'cache_write': 5.00},
    'claude-sonnet-5-5': {'input': 2.00, 'output': 10.00, 'cache_read': 0.20, 'cache_write': 2.50},
    'claude-haiku-5-5': {'input': 0.10, 'output': 0.50, 'cache_read': 0.01, 'cache_write': 0.125},
}


def case_cost(case: dict, model: str) -> float:
    """List-price USD cost of one case's token usage."""
    p = PRICING.get(model)
    if p is None or case.get('input_tokens') is None:
        return 0.0
    read, write = case.get('cache_read_tokens') or 0, case.get('cache_write_tokens') or 0
    fresh = max(0, case['input_tokens'] - read - write)  # input_tokens includes cached tokens
    return (fresh * p['input'] + read * p['cache_read'] + write * p['cache_write']
            + (case.get('output_tokens') or 0) * p['output']) / 1e6
