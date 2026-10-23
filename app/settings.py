"""Local settings, changed from the Settings page: the models on offer with their prices, how many agents run at once,
and the model that labels failures. Saved per install in data/settings.json (git-ignored); anything never saved uses
DEFAULTS. API keys are not settings (they stay in environment variables), and neither are the per-prompt limits
(evals.LIMITS): those define what a pass means, so they change in code, with BENCHMARK_VERSION.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from pydantic import BaseModel, Field, ValidationError, model_validator

_log = logging.getLogger(__name__)

PATH = Path(__file__).resolve().parent.parent / 'data' / 'settings.json'


class Prices(BaseModel):
    """USD per million tokens."""

    input: float = Field(ge=0)
    output: float = Field(ge=0)
    cache_read: float = Field(ge=0)
    cache_write: float = Field(ge=0)


class ModelEntry(BaseModel):
    id: str = Field(pattern=r'^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,99}$', description='The provider model id')
    name: str = Field(min_length=1, max_length=60, description='Shown in the UI')
    checked: bool = Field(False, description='Picked by default on the Run tab')
    prices: Prices | None = Field(None, description='None: cost is not tracked for this model')


class Settings(BaseModel):
    models: list[ModelEntry] = Field(min_length=1, max_length=20)
    concurrency: int = Field(ge=1, le=20, description='Agents running at once across the whole matrix')
    labeler_model: str = Field(pattern=r'^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,99}$', description='Labels wrong-rows failures')

    @model_validator(mode='after')
    def _unique_ids(self) -> Settings:
        ids = [m.id for m in self.models]
        if len(ids) != len(set(ids)):
            raise ValueError('Each model id can be listed once.')
        return self


# Anthropic first-party list prices as of 2026-10-06. Cache reads for Opus/Sonnet are list prices; the Haiku cache read
# and all cache writes use Anthropic's standard multipliers (read = 0.1× input, 5-minute write = 1.25× input). Haiku
# 5.5's price applies to prompts up to 100K tokens, which covers every request here.
DEFAULTS = Settings(
    models=[
        ModelEntry(id='claude-opus-5-5', name='Opus 5.5',
                   prices=Prices(input=4.00, output=20.00, cache_read=0.20, cache_write=5.00)),
        ModelEntry(id='claude-sonnet-5-5', name='Sonnet 5.5', checked=True,
                   prices=Prices(input=2.00, output=10.00, cache_read=0.20, cache_write=2.50)),
        ModelEntry(id='claude-haiku-5-5', name='Haiku 5.5',
                   prices=Prices(input=0.10, output=0.50, cache_read=0.01, cache_write=0.125)),
    ],
    concurrency=6,
    labeler_model='claude-haiku-5-5',
)

_cache: tuple[float, Settings] | None = None


def current() -> Settings:
    """The saved settings, or DEFAULTS. Re-read only when the file changes, so it's cheap to call per case."""
    global _cache
    try:
        mtime = PATH.stat().st_mtime
    except FileNotFoundError:
        return DEFAULTS
    if _cache is None or _cache[0] != mtime:
        try:
            _cache = (mtime, Settings.model_validate_json(PATH.read_text()))
        except (OSError, ValueError, ValidationError):
            _log.exception('Ignoring unreadable %s; using the defaults', PATH)
            return DEFAULTS
    return _cache[1]


def save(settings: Settings) -> Settings:
    PATH.parent.mkdir(exist_ok=True)
    tmp = PATH.with_suffix('.tmp')
    tmp.write_text(json.dumps(settings.model_dump(), indent=1) + '\n')
    tmp.replace(PATH)  # atomic, so a run reading it mid-save never sees half a file
    return current()


def reset() -> Settings:
    PATH.unlink(missing_ok=True)
    return DEFAULTS


def prices(model: str) -> Prices | None:
    return next((m.prices for m in current().models if m.id == model), None)
