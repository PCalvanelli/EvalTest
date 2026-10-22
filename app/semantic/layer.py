"""A tiny semantic layer: per-domain models defined in YAML; metric queries compile to DuckDB SQL."""

from __future__ import annotations

from functools import cache
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field

MODELS_PATH = Path(__file__).with_name('warehouse.yaml')
TIME_GRAINS = ('day', 'week', 'month', 'quarter', 'year')

Scalar = str | int | float | bool


class Filter(BaseModel):
    field: str = Field(description='A dimension (time grains allowed, e.g. order_date__year) or a metric name.')
    op: Literal['=', '!=', '>', '>=', '<', '<=', 'in', 'not in']
    value: Scalar | list[Scalar] = Field(description="Dates as 'YYYY-MM-DD' strings; a list for in / not in.")


class OrderBy(BaseModel):
    field: str = Field(description='A metric or dimension that is selected in this query.')
    desc: bool = False


class SemanticQuery(BaseModel):
    model: str = Field(description='The semantic model to query, e.g. orders. Metrics and dimensions must come from it.')
    metrics: list[str] = Field(default_factory=list, description='Measure names to compute.')
    dimensions: list[str] = Field(
        default_factory=list, description='Dimensions to group by; time dimensions accept __<grain>, e.g. order_date__month.'
    )
    filters: list[Filter] = Field(default_factory=list)
    segments: list[str] = Field(default_factory=list, description='Named, predefined filters.')
    order_by: list[OrderBy] = Field(default_factory=list)
    limit: int | None = None


@cache
def models() -> dict[str, dict]:
    return yaml.safe_load(MODELS_PATH.read_text())['models']


def _model(name: str) -> dict:
    if name not in models():
        raise ValueError(f'Unknown model {name!r}. Valid models: {", ".join(models())}.')
    return models()[name]


def _dimension_sql(m: dict, name: str) -> str:
    dims = m['dimensions']
    base, _, grain = name.partition('__')
    if base not in dims:
        raise ValueError(f'Unknown dimension {name!r}. Valid dimensions: {", ".join(dims)}.')
    spec = dims[base]
    if not grain:
        return spec['sql']
    if spec.get('type') != 'time':
        raise ValueError(f'Dimension {base!r} is not a time dimension, so it has no grains.')
    if grain not in TIME_GRAINS:
        raise ValueError(f'Unknown time grain {grain!r}. Valid grains: {", ".join(TIME_GRAINS)}.')
    return spec['sql'] if grain == 'day' else f"CAST(date_trunc('{grain}', {spec['sql']}) AS DATE)"


def _measure_sql(m: dict, name: str) -> str:
    measures = m['measures']
    if name not in measures:
        raise ValueError(f'Unknown metric {name!r}. Valid metrics: {", ".join(measures)}.')
    return measures[name]['sql']


def _literal(v: Scalar) -> str:
    if isinstance(v, bool):
        return 'TRUE' if v else 'FALSE'
    if isinstance(v, (int, float)):
        return repr(v)
    return "'" + str(v).replace("'", "''") + "'"


def _predicate(expr: str, f: Filter) -> str:
    if f.op in ('in', 'not in'):
        values = f.value if isinstance(f.value, list) else [f.value]
        return f'{expr} {f.op.upper()} ({", ".join(map(_literal, values))})'
    if isinstance(f.value, list):
        raise ValueError(f'Operator {f.op!r} on {f.field!r} takes a single value, not a list.')
    return f'{expr} {f.op} {_literal(f.value)}'


def compile(query: SemanticQuery) -> str:
    """Compile a semantic query to DuckDB SQL; raises ValueError naming valid options on bad input."""
    m = _model(query.model)
    if not query.metrics and not query.dimensions:
        raise ValueError('A query needs at least one metric or dimension.')

    select = [f'{_dimension_sql(m, d)} AS {d}' for d in query.dimensions]
    select += [f'{_measure_sql(m, name)} AS {name}' for name in query.metrics]

    where = []
    for seg in query.segments:
        if seg not in m.get('segments', {}):
            raise ValueError(f'Unknown segment {seg!r}. Valid segments: {", ".join(m.get("segments", {})) or "none"}.')
        where.append(f'({m["segments"][seg]["sql"]})')
    having = []
    for f in query.filters:
        if f.field in m['measures']:
            having.append(_predicate(_measure_sql(m, f.field), f))
        else:
            where.append(_predicate(_dimension_sql(m, f.field), f))

    selected = set(query.dimensions) | set(query.metrics)
    for o in query.order_by:
        if o.field not in selected:
            raise ValueError(f'Cannot order by {o.field!r}: it is not among the selected metrics/dimensions.')

    distinct = 'DISTINCT ' if not query.metrics else ''
    lines = [f'SELECT {distinct}' + ',\n       '.join(select), f'FROM {m["from"].strip()}', *m['joins']]
    if where:
        lines.append('WHERE ' + '\n  AND '.join(where))
    if query.dimensions and query.metrics:
        lines.append('GROUP BY ' + ', '.join(str(i + 1) for i in range(len(query.dimensions))))
    if having:
        lines.append('HAVING ' + '\n   AND '.join(having))
    if query.order_by:
        lines.append('ORDER BY ' + ', '.join(f'{o.field}{" DESC" if o.desc else ""}' for o in query.order_by))
    if query.limit is not None:
        lines.append(f'LIMIT {int(query.limit)}')
    return '\n'.join(lines)


def catalog_text() -> str:
    """Human/LLM-readable description of every semantic model."""
    out = []
    for name, m in models().items():
        out += [f'## Model `{name}`', m['description'].strip(), '', 'Metrics:']
        out += [f'- {k}: {v["description"]}' for k, v in m['measures'].items()]
        out.append('Dimensions:')
        for k, v in m['dimensions'].items():
            grains = f' [time grains: {k}__{"|".join(TIME_GRAINS)}]' if v.get('type') == 'time' else ''
            out.append(f'- {k}: {v["description"]}{grains}')
        if m.get('segments'):
            out.append('Segments:')
            out += [f'- {k}: {v["description"]}' for k, v in m['segments'].items()]
        out.append('')
    return '\n'.join(out)


if __name__ == '__main__':
    from app import db

    samples = [
        SemanticQuery(model='orders', metrics=['net_revenue'], dimensions=['order_date__month'], segments=['valid_orders'],
                      filters=[Filter(field='order_date', op='>=', value='2025-01-01')], order_by=[OrderBy(field='order_date__month')]),
        SemanticQuery(model='subscriptions', metrics=['mrr'], dimensions=['plan_name'], filters=[Filter(field='month', op='=', value='2025-06-01')]),
        SemanticQuery(model='marketing', metrics=['cost_per_click'], dimensions=['platform'],
                      filters=[Filter(field='date', op='>=', value='2025-04-01'), Filter(field='date', op='<', value='2025-07-01')]),
        SemanticQuery(model='inventory', metrics=['units_available'],
                      filters=[Filter(field='snapshot_date', op='=', value='2025-09-30'), Filter(field='product_category', op='=', value='Footwear')]),
        SemanticQuery(model='support', metrics=['ticket_count', 'avg_first_reply_hours'],
                      filters=[Filter(field='priority', op='=', value='urgent'), Filter(field='created_date', op='>=', value='2025-01-01')]),
        SemanticQuery(model='orders', metrics=['units_sold']),
        SemanticQuery(model='warehouse', metrics=['revenue']),
    ]
    for q in samples:
        try:
            sql = compile(q)
            print(sql, '\n->', db.run_readonly(sql, max_rows=4), '\n')
        except ValueError as e:
            print('error:', e, '\n')
