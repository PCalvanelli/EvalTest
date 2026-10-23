"""Local settings (app/settings.py): defaults, saving, validation, and what reads them."""

import pytest
from pydantic import ValidationError

from app import pricing, settings
from app.settings import DEFAULTS, ModelEntry, Prices, Settings


@pytest.fixture(autouse=True)
def temp_settings(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, 'PATH', tmp_path / 'settings.json')
    monkeypatch.setattr(settings, '_cache', None)


def test_defaults_until_saved():
    assert settings.current() == DEFAULTS


def test_save_then_reset():
    custom = DEFAULTS.model_copy(update={'concurrency': 3, 'labeler_model': 'claude-sonnet-5-5'})
    assert settings.save(custom) == custom
    assert settings.current().concurrency == 3
    assert settings.reset() == DEFAULTS
    assert settings.current() == DEFAULTS


def test_an_unreadable_file_falls_back_to_defaults():
    settings.PATH.write_text('{not json')
    assert settings.current() == DEFAULTS


@pytest.mark.parametrize('change', [
    {'concurrency': 0},
    {'concurrency': 21},
    {'models': []},
    {'models': [DEFAULTS.models[0], DEFAULTS.models[0]]},
    {'labeler_model': 'has spaces'},
])
def test_invalid_settings_are_refused(change):
    with pytest.raises(ValidationError):
        Settings.model_validate({**DEFAULTS.model_dump(), **{k: v for k, v in change.items()}})


def test_negative_prices_are_refused():
    with pytest.raises(ValidationError):
        Prices(input=-1, output=0, cache_read=0, cache_write=0)


def test_cost_uses_saved_prices():
    case = {'input_tokens': 1_000_000, 'output_tokens': 0, 'cache_read_tokens': 0, 'cache_write_tokens': 0}
    assert pricing.case_cost(case, 'claude-haiku-5-5') == pytest.approx(0.10)
    cheap = ModelEntry(id='claude-haiku-5-5', name='Haiku', prices=Prices(input=1, output=1, cache_read=1, cache_write=1))
    settings.save(DEFAULTS.model_copy(update={'models': [cheap]}))
    assert pricing.case_cost(case, 'claude-haiku-5-5') == pytest.approx(1.0)
    assert pricing.case_cost(case, 'claude-opus-5-5') == 0.0  # no longer listed: cost not tracked
