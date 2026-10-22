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
    return statement.sql.strip()


def check_definition(definition: MetricDefinition, earlier: Sequence[MetricDefinition] = ()) -> str:
    """Validate a new query-time metric against the governed ones and earlier definitions, then compile it once
    on its own. Returns the SQL of that trial query; raises MetricFlowError with what to fix."""
    others = [d for d in earlier if d.name != definition.name]  # redefining a name replaces it
    definitions = [*others, definition]
    try:  # offsets and windows need a time axis; every model's metric_time rolls up to month
        return compile(MetricFlowQuery(metrics=[definition.name], group_by=['metric_time__month']), definitions)
    except MetricFlowError:
        return compile(MetricFlowQuery(metrics=[definition.name]), definitions)


@cache
def catalog_text() -> str:
    """Metrics grouped by the semantic model they come from, with descriptions and the group-bys they accept."""
    groups: dict[tuple[str, ...], list] = {}
    for metric in _engine().list_metrics():
        key = tuple(sorted(m.semantic_model_name for m in metric.semantic_models))
        groups.setdefault(key, []).append(metric)
    out = []
    for models, metrics in groups.items():
        out += [f'## {" + ".join(models)}', 'Metrics:']
        out += [f'- {m.name} ({m.type.name.lower()}): {(m.description or "").strip()}' for m in metrics]
        out.append('Group by / filter on:')
        for d in sorted(metrics[0].dimensions, key=lambda d: d.dunder_name):
            if d.dunder_name.startswith('metric_time'):
                out.append(f'- metric_time__<grain>: the metric\'s own time axis (smallest grain: {d.dunder_name.split("__")[1]})')
            else:
                out.append(f'- {d.dunder_name}: {(d.description or "").strip()}')
        out.append('')
    return '\n'.join(out)


def model_yaml() -> str:
    """The dbt semantic models and metrics definitions, as authored."""
    return '\n\n'.join(f'# dbt/models/{p.name}\n{p.read_text()}' for p in sorted((PROJECT / 'models').glob('*.yml')))


if __name__ == '__main__':
    # Parity check: MetricFlow must reproduce the gold answers, and Malloy and MetricFlow must agree on every metric.
    import asyncio

    from . import malloy_bridge

    Q = MetricFlowQuery
    day = lambda op, v: f"{{{{ TimeDimension('metric_time', 'day') }}}} {op} '{v}'"  # noqa: E731

    # (MetricFlow query, Malloy query, gold SQL). The gold SQL is what the sample prompts expect.
    GOLD = {
        'net revenue 2024': (
            Q(metrics=['net_revenue'], where=[day('>=', '2024-01-01'), day('<=', '2024-12-31')]),
            'run: orders -> { aggregate: net_revenue; where: order_date ? @2024 }',
            "SELECT SUM(o.subtotal_usd - least(coalesce(r.refund_usd, 0), o.subtotal_usd)) FROM marts.fct_orders o LEFT JOIN (SELECT order_id, SUM(refund_amount_usd) AS refund_usd FROM marts.fct_refunds GROUP BY 1) r USING (order_id) WHERE o.cancelled_at IS NULL AND o.order_date BETWEEN '2024-01-01' AND '2024-12-31'"),
        'MRR end of June 2025 by plan': (
            Q(metrics=['mrr'], group_by=['subscription_month__plan_name'], where=["{{ TimeDimension('metric_time', 'month') }} = '2025-06-01'"]),
            'run: subscription_months -> { group_by: plan_name; aggregate: mrr; where: as_of_month = @2025-06-01 }',
            "SELECT plan_name, SUM(CASE WHEN billing_interval = 'year' THEN plan_amount_usd / 12 ELSE plan_amount_usd END) FROM marts.dim_subscriptions WHERE started_at < DATE '2025-07-01' AND (canceled_at IS NULL OR CAST(canceled_at AS DATE) > DATE '2025-06-30') GROUP BY 1"),
        'active customers Q3 2025': (
            Q(metrics=['active_customers'], where=[day('>=', '2025-07-01'), day('<=', '2025-09-30')]),
            'run: orders -> { aggregate: active_customers; where: order_date ? @2025-Q3 }',
            "SELECT COUNT(DISTINCT customer_key) FROM marts.fct_orders WHERE cancelled_at IS NULL AND order_date BETWEEN '2025-07-01' AND '2025-09-30'"),
        'CPC by platform Q2 2025': (
            Q(metrics=['cost_per_click'], group_by=['ad_stat__platform'], where=[day('>=', '2025-04-01'), day('<=', '2025-06-30')]),
            'run: ad_stats -> { group_by: platform; aggregate: cost_per_click; where: activity_date ? @2025-Q2 }',
            "SELECT platform, SUM(cost_usd) / SUM(clicks) FROM marts.fct_ad_stats WHERE date BETWEEN '2025-04-01' AND '2025-06-30' GROUP BY 1"),
        'urgent tickets 2025': (
            Q(metrics=['ticket_count', 'avg_first_reply_hours'], where=["{{ Dimension('ticket__priority') }} = 'urgent'", day('>=', '2025-01-01')]),
            "run: tickets -> { aggregate: ticket_count, avg_first_reply_hours; where: priority = 'urgent' and created_date ? @2025 }",
            "SELECT COUNT(*), AVG(date_diff('minute', t.created_at, fr.m) / 60.0) FROM marts.fct_tickets t LEFT JOIN (SELECT ticket_id, MIN(created_at) m FROM marts.fct_ticket_comments WHERE is_public AND author_role = 'agent' GROUP BY 1) fr USING (ticket_id) WHERE priority = 'urgent' AND t.created_at >= '2025-01-01'"),
        'Footwear available 2025-09-30': (
            Q(metrics=['units_available'], where=[day('=', '2025-09-30'), "{{ Dimension('product__category') }} = 'Footwear'"]),
            "run: inventory -> { aggregate: units_available; where: snapshot_date = @2025-09-30 and products.category = 'Footwear' }",
            "SELECT SUM(i.qty_on_hand - i.qty_reserved) FROM marts.fct_inventory_snapshots i JOIN marts.dim_products p USING (product_id) WHERE i.snapshot_date = '2025-09-30' AND p.category = 'Footwear'"),
        'gross revenue by channel Q2 2025': (
            Q(metrics=['gross_revenue'], group_by=['order__sales_channel'], where=[day('>=', '2025-04-01'), day('<=', '2025-06-30')]),
            'run: orders -> { group_by: sales_channel; aggregate: gross_revenue; where: order_date ? @2025-Q2 }',
            "SELECT sales_channel, SUM(subtotal_usd) FROM marts.fct_orders WHERE cancelled_at IS NULL AND order_date BETWEEN '2025-04-01' AND '2025-06-30' GROUP BY 1"),
        'memberships churned in 2025': (
            Q(metrics=['churned_subscriptions'], where=["{{ TimeDimension('metric_time', 'month') }} >= '2025-01-01'"]),
            'run: subscription_months -> { aggregate: churned_subscriptions; where: as_of_month ? @2025 }',
            "SELECT COUNT(*) FROM marts.dim_subscriptions WHERE canceled_at >= '2025-01-01' AND canceled_at < '2025-10-01'"),
        'top 3 products by units sold 2025': (
            Q(metrics=['units_sold'], group_by=['product__product_name'], where=[day('>=', '2025-01-01')], order_by=['-units_sold'], limit=3),
            'run: order_lines -> { group_by: products.product_name; aggregate: units_sold; where: order_date ? @2025; order_by: units_sold desc; limit: 3 }',
            "SELECT p.product_name, SUM(l.quantity) FROM marts.fct_order_lines l JOIN marts.fct_orders o USING (order_id) JOIN marts.dim_products p USING (product_id) WHERE o.cancelled_at IS NULL AND l.order_date >= '2025-01-01' GROUP BY 1 ORDER BY 2 DESC LIMIT 3"),
        'AOV first vs repeat orders 2024': (
            Q(metrics=['avg_order_value'], group_by=['order__is_first_order'], where=[day('>=', '2024-01-01'), day('<=', '2024-12-31')]),
            'run: orders -> { group_by: is_first_order; aggregate: avg_order_value; where: order_date ? @2024 }',
            "WITH o AS (SELECT o.*, coalesce(r.refund_usd, 0) AS refund_usd, row_number() OVER (PARTITION BY o.shopify_customer_id ORDER BY o.created_at) = 1 AS is_first FROM marts.fct_orders o LEFT JOIN (SELECT order_id, SUM(refund_amount_usd) AS refund_usd FROM marts.fct_refunds GROUP BY 1) r USING (order_id)) SELECT is_first, SUM(subtotal_usd - least(refund_usd, subtotal_usd)) / COUNT(*) FROM o WHERE cancelled_at IS NULL AND year(order_date) = 2024 GROUP BY 1"),
        'units on hand per warehouse end of Q2 2025': (
            Q(metrics=['units_on_hand'], group_by=['inventory_snapshot__warehouse_code'], where=[day('=', '2025-06-30')]),
            'run: inventory -> { group_by: warehouse_code; aggregate: units_on_hand; where: snapshot_date = @2025-06-30 }',
            "SELECT warehouse_code, SUM(qty_on_hand) FROM marts.fct_inventory_snapshots WHERE snapshot_date = '2025-06-30' GROUP BY 1"),
    }

    # Every governed metric, grouped: Malloy and MetricFlow must agree row for row.
    PARITY = {
        'orders by month': (
            Q(metrics=['order_count', 'gross_revenue', 'refunds', 'net_revenue', 'avg_order_value', 'active_customers'], group_by=['metric_time__month']),
            'run: orders -> { group_by: m is order_date.month; aggregate: order_count, gross_revenue, refunds, net_revenue, avg_order_value, active_customers }'),
        'orders by segment/source/status': (
            Q(metrics=['net_revenue', 'order_count'], group_by=['customer__customer_segment', 'customer__acquisition_source', 'order__order_status']),
            'run: orders -> { group_by: customer.customer_segment, customer.acquisition_source, order_status; aggregate: net_revenue, order_count }'),
        'order lines by category': (
            Q(metrics=['units_sold', 'product_revenue'], group_by=['product__category']),
            'run: order_lines -> { group_by: products.category; aggregate: units_sold, product_revenue }'),
        'subscriptions by month': (
            Q(metrics=['mrr', 'active_subscriptions', 'new_subscriptions', 'churned_subscriptions'], group_by=['metric_time__month']),
            'run: subscription_months -> { group_by: as_of_month; aggregate: mrr, active_subscriptions, new_subscriptions, churned_subscriptions }'),
        'support by priority/segment': (
            Q(metrics=['ticket_count', 'solved_tickets', 'avg_first_reply_hours', 'avg_resolution_hours'], group_by=['ticket__priority', 'customer__customer_segment']),
            'run: tickets -> { group_by: priority, customer.customer_segment; aggregate: ticket_count, solved_tickets, avg_first_reply_hours, avg_resolution_hours }'),
        'marketing by platform': (
            Q(metrics=['ad_spend', 'clicks', 'impressions', 'cost_per_click', 'click_through_rate'], group_by=['ad_stat__platform']),
            'run: ad_stats -> { group_by: platform; aggregate: ad_spend, total_clicks, total_impressions, cost_per_click, click_through_rate }'),
        'inventory by warehouse on 2025-06-30': (
            Q(metrics=['units_on_hand', 'units_reserved', 'units_available'], group_by=['inventory_snapshot__warehouse_code'], where=[day('=', '2025-06-30')]),
            'run: inventory -> { group_by: warehouse_code; aggregate: units_on_hand, units_reserved, units_available; where: snapshot_date = @2025-06-30 }'),
    }

    def run_mf(q: MetricFlowQuery) -> list[tuple]:
        try:
            return db.result_set(compile(q))
        except Exception as e:
            return [('ERROR', str(e)[:300])]

    async def main() -> None:
        failures = 0

        async def run_malloy(malloy: str) -> list[tuple]:
            try:
                return db.result_set(await malloy_bridge.compile(malloy))
            except Exception as e:
                return [('ERROR', str(e)[:300])]

        # Group-by values sort differently across engines; compare rows as unordered value sets.
        num = lambda v: float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else v  # noqa: E731
        rows = lambda rs: sorted(tuple(sorted(repr(num(v)) for v in r)) for r in rs)  # noqa: E731
        # An all-filtered-out group sums to 0 in Malloy but NULL in plain SQL; same meaning here.
        null_as_0 = lambda rs: [tuple(0 if v is None else v for v in r) for r in rs]  # noqa: E731
        for name, (q, malloy, gold_sql) in GOLD.items():
            want = db.result_set(gold_sql)
            got_mf, got_m = run_mf(q), await run_malloy(malloy)
            ok = got_mf == want and got_m == want
            failures += not ok
            print(f'{"OK  " if ok else "FAIL"} gold    {name}: {want}' + ('' if ok else f'\n  metricflow {got_mf}\n  malloy     {got_m}'))
        for name, (q, malloy) in PARITY.items():
            got_mf, got_m = rows(null_as_0(run_mf(q))), rows(null_as_0(await run_malloy(malloy)))
            ok = got_mf == got_m
            failures += not ok
            print(f'{"OK  " if ok else "FAIL"} parity  {name} ({len(got_mf)} rows)' + ('' if ok else f'\n  metricflow {got_mf[:3]}\n  malloy     {got_m[:3]}'))
        try:
            compile(Q(metrics=['revenue']))
        except MetricFlowError as e:
            print('OK   compile error surfaces:', str(e).splitlines()[0][:200])
        raise SystemExit(1 if failures else 0)

    asyncio.run(main())
