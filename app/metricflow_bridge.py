"""Compile dbt MetricFlow queries to DuckDB SQL, in-process, from the dbt project in dbt/."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import threading
from collections.abc import Sequence
from functools import cache, lru_cache
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from . import db

ROOT = Path(__file__).resolve().parent.parent
PROJECT = ROOT / 'dbt'
MANIFEST = PROJECT / 'target' / 'semantic_manifest.json'
_STAMP = PROJECT / 'target' / '.evaltest-build'
_PROJECT_FILES = sorted([PROJECT / 'dbt_project.yml', *(PROJECT / 'models').rglob('*'), *(PROJECT / 'macros').rglob('*')])
# The manifest names relations by database, which is the warehouse file's name: rebuild when either changes.
_BUILD_KEY = hashlib.sha256(
    b''.join(f.read_bytes() for f in _PROJECT_FILES if f.is_file()) + db.DB_PATH.stem.encode()
).hexdigest()[:12]
MAX_ERROR_CHARS = 2000


class MetricFlowQuery(BaseModel):
    metrics: list[str] = Field(description='Metric names, e.g. ["net_revenue"]. All must share the requested group-bys.')
    group_by: list[str] = Field(
        default_factory=list,
        description='Dimensions to group by, as entity__dimension (e.g. customer__customer_segment) or '
                    'metric_time__<grain> (day, week, month, quarter, year).',
    )
    where: list[str] = Field(
        default_factory=list,
        description="SQL filter expressions using MetricFlow Jinja, ANDed together, e.g. "
                    "\"{{ Dimension('order__sales_channel') }} = 'web'\" or "
                    "\"{{ TimeDimension('metric_time', 'day') }} >= '2025-01-01'\".",
    )
    order_by: list[str] = Field(default_factory=list, description='Metric or group-by names; prefix with - for descending.')
    limit: int | None = None


class PostStep(BaseModel):
    """A presentation step over MetricFlow's result rows (like a BI tool on the dbt Semantic Layer). Never reads
    warehouse tables: it only ranks or rescales the governed result."""

    kind: Literal['rank_filter', 'share_of_total', 'select', 'drop_nulls'] = Field(
        description='rank_filter: keep the top `keep` rows per partition by order_by (ties kept). '
                    'share_of_total: add a column metric / total of that metric over the partition (or all rows). '
                    'select: keep only the listed columns, in that order. '
                    'drop_nulls: drop rows where any listed column is NULL (e.g. an unknown customer), after earlier steps.'
    )
    partition_by: list[str] = Field(default_factory=list, description='Group-by names of the query to rank or total within; empty = all rows.')
    order_by: str | None = Field(None, description="rank_filter: a metric or group-by of the query; prefix '-' for highest first.")
    keep: int = Field(1, ge=1, description='rank_filter: rows to keep per partition (rank <= keep, so ties are kept).')
    metric: str | None = Field(None, description='share_of_total: the metric to divide by its total.')
    name: str | None = Field(None, pattern=r'^[a-z][a-z0-9_]*$', description='share_of_total: the new column name (default <metric>_share).')
    columns: list[str] = Field(default_factory=list, description='select: the columns to return, in order; drop_nulls: the columns to check.')


class MetricFlowQueryWithSteps(MetricFlowQuery):
    post_process: list[PostStep] = Field(
        default_factory=list,
        description='Optional steps applied in order to the result rows: per-group top-N (rank_filter) or share of a '
                    'total (share_of_total).',
    )


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _apply_steps(sql: str, query: MetricFlowQuery) -> str:
    """Wrap MetricFlow's SQL in one outer query per post-processing step."""
    columns = set(query.group_by) | set(query.metrics)
    for step in getattr(query, 'post_process', []) or []:
        unknown = [c for c in [*step.partition_by, (step.order_by or '').lstrip('-'), step.metric or '', *step.columns]
                   if c and c not in columns]
        if unknown:
            raise MetricFlowError(f'{", ".join(unknown)} is not a group-by or metric of this query; '
                                  f'steps can only use: {", ".join(sorted(columns))}.')
        partition = f'PARTITION BY {", ".join(map(_quote, step.partition_by))} ' if step.partition_by else ''
        if step.kind == 'drop_nulls':
            if not step.columns:
                raise MetricFlowError('drop_nulls needs columns.')
            sql = f'SELECT * FROM ({sql}) mf WHERE ' + ' AND '.join(f'{_quote(c)} IS NOT NULL' for c in step.columns)
        elif step.kind == 'select':
            if not step.columns:
                raise MetricFlowError('select needs columns.')
            sql = f'SELECT {", ".join(map(_quote, step.columns))} FROM ({sql}) mf'
            columns = set(step.columns)
        elif step.kind == 'rank_filter':
            if not step.order_by:
                raise MetricFlowError('rank_filter needs order_by.')
            direction = 'DESC' if step.order_by.startswith('-') else 'ASC'
            sql = (f'SELECT * FROM ({sql}) mf QUALIFY rank() OVER ({partition}ORDER BY '
                   f'{_quote(step.order_by.lstrip("-"))} {direction}) <= {int(step.keep)}')
        else:
            if not step.metric:
                raise MetricFlowError('share_of_total needs metric.')
            name = step.name or f'{step.metric}_share'
            columns.add(name)
            sql = (f'SELECT *, {_quote(step.metric)} / NULLIF(SUM({_quote(step.metric)}) OVER ({partition.strip()}), 0) '
                   f'AS {_quote(name)} FROM ({sql}) mf')
    return sql


class MetricInput(BaseModel):
    name: str = Field(description='An existing metric (governed, or one you defined earlier).')
    alias: str | None = Field(None, description='Name to use for this input inside a derived expr.')
    offset_window: str | None = Field(
        None, description="Read the metric from an earlier period, e.g. '1 month' or '1 year' (derived metrics only)."
    )
    filter: str | None = Field(None, description="MetricFlow Jinja filter, e.g. \"{{ Dimension('ticket__priority') }} = 'urgent'\".")


class MetricDefinition(BaseModel):
    """A metric the agent defines at query time, built only from existing metrics (the harness's MetricFlow tool)."""

    name: str = Field(pattern=r'^[a-z][a-z0-9_]*$', description='New, unused snake_case name.')
    description: str = Field(description='What it measures, in one sentence.')
    type: Literal['derived', 'cumulative', 'ratio'] = Field(
        description='derived: an expression over input metrics (offset_window for prior periods); '
                    'cumulative: a running, rolling or period-to-date total of one metric; '
                    'ratio: numerator / denominator metrics, each optionally filtered.'
    )
    expr: str | None = Field(None, description="derived: SQL expression over the input names/aliases, e.g. 'revenue / prev - 1'.")
    metrics: list[MetricInput] = Field(default_factory=list, description='derived: the input metrics.')
    metric: MetricInput | None = Field(None, description='cumulative: the metric to accumulate.')
    window: str | None = Field(None, description="cumulative: rolling window, e.g. '3 months'. Omit for all-time.")
    grain_to_date: Literal['week', 'month', 'quarter', 'year'] | None = Field(
        None, description='cumulative: reset at the start of each period (e.g. year for year-to-date).'
    )
    numerator: MetricInput | None = Field(None, description='ratio: numerator metric.')
    denominator: MetricInput | None = Field(None, description='ratio: denominator metric.')

    def to_manifest(self) -> dict:
        """The metric in the semantic manifest's own shape."""
        inp = lambda m: m.model_dump(exclude_none=True) if m else None  # noqa: E731
        if self.type == 'derived':
            if not self.expr or not self.metrics:
                raise MetricFlowError('A derived metric needs expr and metrics.')
            params = {'expr': self.expr, 'metrics': [inp(m) for m in self.metrics]}
        elif self.type == 'cumulative':
            if self.metric is None:
                raise MetricFlowError('A cumulative metric needs metric.')
            if self.window and self.grain_to_date:
                raise MetricFlowError('Use either window or grain_to_date, not both.')
            cumulative = {'metric': inp(self.metric), 'period_agg': 'last'}
            cumulative |= {'window': self.window} if self.window else {}
            cumulative |= {'grain_to_date': self.grain_to_date} if self.grain_to_date else {}
            params = {'cumulative_type_params': cumulative}
        else:
            if self.numerator is None or self.denominator is None:
                raise MetricFlowError('A ratio metric needs numerator and denominator.')
            num, den = inp(self.numerator), inp(self.denominator)
            if num['name'] == den['name']:  # same metric on both sides: MetricFlow needs distinct column names
                num.setdefault('alias', f'{self.name}_numerator')
                den.setdefault('alias', f'{self.name}_denominator')
            params = {'numerator': num, 'denominator': den}
        return {'name': self.name, 'description': self.description, 'type': self.type, 'type_params': params}


class MetricFlowError(Exception):
    """MetricFlow rejected a query (unknown metric, dimension not reachable, bad filter, ...)."""


def _build_manifest() -> None:
    """`dbt parse` writes target/semantic_manifest.json. Only parses: dbt never touches the warehouse."""
    if MANIFEST.exists() and _STAMP.exists() and _STAMP.read_text() == _BUILD_KEY:
        return
    db.connection()  # the warehouse file must exist for the profile's path
    dbt = Path(sys.executable).with_name('dbt')
    proc = subprocess.run(
        [str(dbt), 'parse', '--profiles-dir', str(PROJECT), '--project-dir', str(PROJECT), '--no-send-anonymous-usage-stats',
         '--quiet'],
        cwd=PROJECT, capture_output=True, text=True, env={**os.environ, 'EVALTEST_WAREHOUSE': str(db.DB_PATH)},
    )
    if proc.returncode != 0 or not MANIFEST.exists():
        raise RuntimeError(f'dbt parse failed:\n{proc.stdout[-2000:]}{proc.stderr[-2000:]}')
    _STAMP.write_text(_BUILD_KEY)


class _CompileOnlyClient:
    """MetricFlow's engine wants a SqlClient, but only uses it for the dialect: we explain (render SQL) and run the
    SQL ourselves through db.run_readonly, like every other context."""

    def __init__(self) -> None:
        from metricflow.protocols.sql_client import SqlEngine
        from metricflow.sql.render.duckdb_renderer import DuckDbSqlPlanRenderer

        self.sql_engine_type = SqlEngine.DUCKDB
        self.sql_plan_renderer = DuckDbSqlPlanRenderer()

    def render_bind_parameter_key(self, key: str) -> str:
        return f'${key}'

    def query(self, *args, **kwargs):  # noqa: ANN002, ANN003
        raise NotImplementedError('compile-only client')

    execute = dry_run = query

    def close(self) -> None:
        pass


_lock = threading.Lock()  # the engine renumbers its generated ids per query; keep queries one at a time


@cache
def _base_manifest() -> dict:
    _build_manifest()
    return json.loads(MANIFEST.read_text())


@lru_cache(maxsize=64)
def _engine(extra_metrics: str = '[]'):  # noqa: ANN202
    """The engine over the governed manifest, plus any query-time metric definitions (as a JSON list, so the cache
    can key on it). Definitions are validated like dbt validates the project; problems raise MetricFlowError."""
    from metricflow.engine.metricflow_engine import MetricFlowEngine
    from metricflow_semantic_interfaces.validations.semantic_manifest_validator import SemanticManifestValidator
    from metricflow_semantics.model.dbt_manifest_parser import parse_manifest_from_dbt_generated_manifest
    from metricflow_semantics.model.semantic_manifest_lookup import SemanticManifestLookup

    base, extra = _base_manifest(), json.loads(extra_metrics)
    try:
        manifest = parse_manifest_from_dbt_generated_manifest(json.dumps({**base, 'metrics': base['metrics'] + extra}))
        if extra:
            errors = SemanticManifestValidator().validate_semantic_manifest(manifest).errors
            if errors:
                raise MetricFlowError('Metric definition is invalid:\n' + '\n'.join(f'- {e.message}' for e in errors[:5]))
        return MetricFlowEngine(semantic_manifest_lookup=SemanticManifestLookup(manifest), sql_client=_CompileOnlyClient())
    except MetricFlowError:
        raise
    except Exception as e:
        raise MetricFlowError(f'Metric definition is invalid: {(str(e).strip() or type(e).__name__)[:MAX_ERROR_CHARS]}') from e


def _definitions_key(definitions: Sequence[MetricDefinition]) -> str:
    governed = {m['name'] for m in _base_manifest()['metrics']}
    names = [d.name for d in definitions]
    if clash := sorted(set(names) & governed):
        raise MetricFlowError(f'{", ".join(clash)} already exists as a governed metric; pick a new name.')
    if len(set(names)) != len(names):
        raise MetricFlowError('Each defined metric needs a unique name.')
    return json.dumps([d.to_manifest() for d in definitions], sort_keys=True)


def compile(query: MetricFlowQuery, definitions: Sequence[MetricDefinition] = ()) -> str:
    """Compile a MetricFlow query to DuckDB SQL; raises MetricFlowError with MetricFlow's message on bad input.
    `definitions` are query-time metrics the query may use alongside the governed ones."""
    from metricflow.engine.metricflow_engine import MetricFlowQueryRequest

    if not query.metrics:
        raise MetricFlowError('A query needs at least one metric.')
    request = MetricFlowQueryRequest.create(
        metric_names=query.metrics,
        group_by_names=query.group_by or None,
        where_constraints=query.where or None,
        order_by_names=query.order_by or None,
        limit=query.limit,
    )
    try:
        with _lock:
            statement = _engine(_definitions_key(definitions)).explain(request).sql_statement.without_descriptions
    except Exception as e:
        message = str(e).strip() or type(e).__name__
        raise MetricFlowError(message[:MAX_ERROR_CHARS]) from e
    if statement.bind_parameter_set.param_items:
        raise MetricFlowError('Query needs bind parameters, which are not supported here.')
    return _apply_steps(statement.sql.strip(), query)


def check_definition(definition: MetricDefinition, earlier: Sequence[MetricDefinition] = ()) -> str:
    """Validate a new query-time metric against the governed ones and earlier definitions, then compile it once
    on its own. Returns the SQL of that trial query; raises MetricFlowError with what to fix."""
    others = [d for d in earlier if d.name != definition.name]  # redefining a name replaces it
    definitions = [*others, definition]
    try:  # offsets and windows need a time axis; every model's metric_time rolls up to month
        return compile(MetricFlowQuery(metrics=[definition.name], group_by=['metric_time__month']), definitions)
    except MetricFlowError:
        return compile(MetricFlowQuery(metrics=[definition.name]), definitions)


def _metric_groups() -> dict[tuple[str, ...], list]:
    """Governed metrics grouped by the semantic models they come from (metrics in a group share their dimensions)."""
    groups: dict[tuple[str, ...], list] = {}
    for metric in _engine().list_metrics():
        groups.setdefault(tuple(sorted(m.semantic_model_name for m in metric.semantic_models)), []).append(metric)
    return groups


def _group_heading(models: tuple[str, ...], metrics: list) -> list[str]:
    return [f'## {" + ".join(models)}', 'Metrics:',
            *(f'- {m.name} ({m.type.name.lower()}): {(m.description or "").strip()}' for m in metrics)]


@cache
def catalog_text() -> str:
    """Metrics grouped by the semantic model they come from, with descriptions and the group-bys they accept."""
    out = []
    for models, metrics in _metric_groups().items():
        out += _group_heading(models, metrics)
        out.append('Group by / filter on:')
        for d in sorted(metrics[0].dimensions, key=lambda d: d.dunder_name):
            if d.dunder_name.startswith('metric_time'):
                out.append(f'- metric_time__<grain>: the metric\'s own time axis (smallest grain: {d.dunder_name.split("__")[1]})')
            else:
                out.append(f'- {d.dunder_name}: {(d.description or "").strip()}')
        out.append('')
    return '\n'.join(out)


@cache
def compact_catalog_text() -> str:
    """The same catalog with each entity's dimensions described once: a metric group lists `prefix__*` for an entity
    whose dimensions it can all reach (e.g. order__customer__* from order_lines), and the descriptions follow at the end.
    Same content as catalog_text(), roughly half the tokens, since the customer, order and product dimensions
    otherwise repeat under every metric group that reaches them."""
    groups = _metric_groups()
    entity_dims: dict[str, dict[str, str]] = {}  # entity -> dimension name -> description
    for metrics in groups.values():
        for d in metrics[0].dimensions:
            if not d.dunder_name.startswith('metric_time'):
                *path, name = d.dunder_name.split('__')
                entity_dims.setdefault(path[-1], {})[name] = (d.description or '').strip()
    out = []
    for models, metrics in groups.items():
        out += _group_heading(models, metrics)
        by_prefix: dict[str, set[str]] = {}
        dims = []
        for d in sorted(metrics[0].dimensions, key=lambda d: d.dunder_name):
            if d.dunder_name.startswith('metric_time'):
                dims.append(f'metric_time__<grain> (smallest grain: {d.dunder_name.split("__")[1]})')
            else:
                prefix, name = d.dunder_name.rsplit('__', 1)
                by_prefix.setdefault(prefix, set()).add(name)
        for prefix, names in sorted(by_prefix.items()):
            if names == set(entity_dims[prefix.split('__')[-1]]):
                dims.append(f'{prefix}__*')
            else:
                dims += [f'{prefix}__{n}' for n in sorted(names)]
        out += [f'Group by / filter on: {", ".join(dims)}', '']
    out += ['## Dimensions', '`prefix__*` above means every dimension of that entity, e.g. order__customer__* includes '
            'order__customer__customer_segment.']
    for entity, dims in sorted(entity_dims.items()):
        out.append(f'{entity}__:')
        out += [f'- {name}: {desc}' for name, desc in sorted(dims.items())]
    return '\n'.join(out)

