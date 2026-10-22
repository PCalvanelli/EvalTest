"""Can each benchmark question be answered in dbt MetricFlow and in Malloy at all? A hand-written reference answer per
question and target, graded with the eval's own result matcher against the expected SQL. This is the ceiling an agent
can reach with each semantic layer (and its harness tools); it costs no API calls.

    uv run python -m app.expressibility
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

from . import db, malloy_bridge, metricflow_bridge
from .metricflow_bridge import MetricDefinition as D, MetricFlowQueryWithSteps as Q, MetricInput as I, PostStep as P

ROOT = Path(__file__).resolve().parent.parent
day = lambda op, v: f"{{{{ TimeDimension('metric_time', 'day') }}}} {op} '{v}'"  # noqa: E731
month = lambda op, v: f"{{{{ TimeDimension('metric_time', 'month') }}}} {op} '{v}'"  # noqa: E731
dim = lambda name, cond: f"{{{{ Dimension('{name}') }}}} {cond}"  # noqa: E731


def _prior(metric: str, grain: str) -> D:
    return D(name=f'{metric}_growth', description='Growth vs the prior period', type='derived', expr=f'{metric} / prev - 1',
             metrics=[I(name=metric), I(name=metric, alias='prev', offset_window=f'1 {grain}')])


def _change(metric: str, grain: str) -> D:
    return D(name=f'{metric}_change', description='Change vs the prior period', type='derived', expr=f'{metric} - prev',
             metrics=[I(name=metric), I(name=metric, alias='prev', offset_window=f'1 {grain}')])


# (MetricFlow query, query-time metric definitions) per question; None = not expressible.
METRICFLOW: dict[str, tuple[Q, list[D]] | None] = {
    'benchmark:1': (Q(metrics=['net_revenue'], where=[day('>=', '2024-01-01'), day('<=', '2024-12-31')]), []),
    'benchmark:2': (Q(metrics=['mrr'], group_by=['subscription_month__plan_name'], where=[month('=', '2025-06-01')]), []),
    'benchmark:3': (Q(metrics=['active_customers'], where=[day('>=', '2025-07-01'), day('<=', '2025-09-30')]), []),
    'benchmark:4': (Q(metrics=['cost_per_click'], group_by=['ad_stat__platform'], where=[day('>=', '2025-04-01'), day('<=', '2025-06-30')],
                      order_by=['cost_per_click'], limit=1), []),
    'benchmark:5': (Q(metrics=['ticket_count', 'avg_first_reply_hours'], where=[dim('ticket__priority', "= 'urgent'"), day('>=', '2025-01-01')]), []),
    'benchmark:6': (Q(metrics=['units_available'], where=[day('=', '2025-09-30'), dim('product__category', "= 'Footwear'")]), []),
    'benchmark:7': (Q(metrics=['customer_count'], where=[dim('customer__has_ordered', '= TRUE'), dim('customer__has_opened_ticket', '= TRUE')]), []),
    'benchmark:8': (Q(metrics=['gross_revenue'], group_by=['order__sales_channel'], where=[day('>=', '2025-04-01'), day('<=', '2025-06-30')]), []),
    'benchmark:9': (Q(metrics=['churned_subscriptions'], where=[month('>=', '2025-01-01')]), []),
    'benchmark:10': (Q(metrics=['units_sold'], group_by=['product__product_name'], where=[day('>=', '2025-01-01')], order_by=['-units_sold'], limit=3), []),
    'benchmark:11': (Q(metrics=['avg_order_value'], group_by=['order__is_first_order'], where=[day('>=', '2024-01-01'), day('<=', '2024-12-31')]), []),
    'benchmark:12': (Q(metrics=['units_on_hand'], group_by=['inventory_snapshot__warehouse_code'], where=[day('=', '2025-06-30')]), []),
    'benchmark:13': (Q(metrics=['net_revenue_growth'], group_by=['metric_time__month'], where=[month('>=', '2025-01-01')]), [_prior('net_revenue', 'month')]),
    'benchmark:14': (Q(metrics=['net_revenue_ytd'], group_by=['metric_time__month'], where=[month('>=', '2025-01-01')]),
                     [D(name='net_revenue_ytd', description='YTD', type='cumulative', metric=I(name='net_revenue'), grain_to_date='year')]),
    'benchmark:15': (Q(metrics=['units_sold'], group_by=['product__category', 'product__product_name'], where=[day('>=', '2025-01-01')],
                       post_process=[P(kind='rank_filter', partition_by=['product__category'], order_by='-units_sold')]), []),
    'benchmark:16': (Q(metrics=['second_order_30d_share']), [D(name='second_order_30d_share', description='x', type='ratio',
                     numerator=I(name='order_count', filter=dim('order__order_sequence', '= 2') + ' AND ' + dim('order__days_since_prior_order', '<= 30')),
                     denominator=I(name='order_count', filter=dim('order__order_sequence', '= 2')))]),
    'benchmark:17': (Q(metrics=['median_days_between_orders'], where=[dim('order__order_sequence', '= 2')]), []),
    'benchmark:18': (Q(metrics=['mrr', 'mrr_change'], group_by=['metric_time__month'], where=[month('>=', '2025-01-01')]), [_change('mrr', 'month')]),
    'benchmark:19': (Q(metrics=['ad_spend', 'ad_spend_3m_avg'], group_by=['metric_time__month'], where=[month('>=', '2025-01-01')]),
                     [D(name='ad_spend_3m_avg', description='x', type='derived', expr='(cur + m1 + m2) / 3',
                        metrics=[I(name='ad_spend', alias='cur'), I(name='ad_spend', alias='m1', offset_window='1 month'),
                                 I(name='ad_spend', alias='m2', offset_window='2 months')])]),
    'benchmark:20': (Q(metrics=['net_revenue'], group_by=['customer__email'], where=["{{ Entity('customer') }} IS NOT NULL"],
                       post_process=[P(kind='share_of_total', metric='net_revenue', name='share'), P(kind='rank_filter', order_by='-net_revenue', keep=5)]), []),
    'benchmark:21': (Q(metrics=['reply_4h_share'], group_by=['ticket__priority'], where=[day('>=', '2025-01-01')]),
                     [D(name='reply_4h_share', description='x', type='ratio', numerator=I(name='ticket_count', filter=dim('ticket__first_reply_hours', '<= 4')),
                        denominator=I(name='ticket_count'))]),
    'benchmark:22': (Q(metrics=['units_available'], group_by=['inventory_snapshot__warehouse_code', 'metric_time__day'],
                       where=[day('>=', '2025-07-01'), day('<=', '2025-09-30')],
                       post_process=[P(kind='rank_filter', partition_by=['inventory_snapshot__warehouse_code'], order_by='units_available')]), []),
    'holdout:1': (Q(metrics=['net_revenue_growth'], group_by=['metric_time__quarter'], where=["{{ TimeDimension('metric_time', 'quarter') }} = '2024-10-01'"],
                    post_process=[P(kind='select', columns=['net_revenue_growth'])]), [_prior('net_revenue', 'quarter')]),
    'holdout:2': (Q(metrics=['product_revenue'], group_by=['order__sales_channel', 'product__category'], where=[day('>=', '2025-01-01')],
                    post_process=[P(kind='rank_filter', partition_by=['order__sales_channel'], order_by='-product_revenue')]), []),
    'holdout:3': (Q(metrics=['solved_24h_share'], group_by=['ticket__channel'], where=[day('>=', '2025-01-01')]),
                  [D(name='solved_24h_share', description='x', type='ratio', numerator=I(name='ticket_count', filter=dim('ticket__resolution_hours', '<= 24')),
                     denominator=I(name='ticket_count'))]),
    'holdout:4': (Q(metrics=['median_days_between_orders'], where=[dim('order__order_sequence', '= 3')]), []),
    'holdout:5': (Q(metrics=['customer_count'], where=[dim('customer__has_membership', '= TRUE'), dim('customer__has_opened_ticket', '= TRUE')]), []),
    'holdout:6': (Q(metrics=['new_subscriptions_ytd'], group_by=['metric_time__month'], where=[month('>=', '2025-01-01')]),
                  [D(name='new_subscriptions_ytd', description='x', type='cumulative', metric=I(name='new_subscriptions'), grain_to_date='year')]),
    'holdout:7': (Q(metrics=['cpc_3m'], group_by=['metric_time__month'], where=[month('>=', '2025-01-01')]),
                  [D(name='cpc_3m', description='x', type='derived', expr='(s0 + s1 + s2) / (c0 + c1 + c2)',
                     metrics=[I(name='ad_spend', alias='s0'), I(name='ad_spend', alias='s1', offset_window='1 month'),
                              I(name='ad_spend', alias='s2', offset_window='2 months'), I(name='clicks', alias='c0'),
                              I(name='clicks', alias='c1', offset_window='1 month'), I(name='clicks', alias='c2', offset_window='2 months')])]),
    'holdout:8': (Q(metrics=['order_count'], group_by=['customer__email'], where=[day('>=', '2025-01-01')],
                    post_process=[P(kind='share_of_total', metric='order_count', name='share'), P(kind='drop_nulls', columns=['customer__email']),
                                  P(kind='rank_filter', order_by='-order_count', keep=2)]), []),
    'holdout:9': (Q(metrics=['units_on_hand'], group_by=['inventory_snapshot__warehouse_code', 'product__product_name'], where=[day('=', '2025-09-30')],
                    post_process=[P(kind='rank_filter', partition_by=['inventory_snapshot__warehouse_code'], order_by='-units_on_hand')]), []),
    'holdout:10': (Q(metrics=['active_subscriptions', 'active_subscriptions_change'], group_by=['metric_time__quarter'],
                     where=["{{ TimeDimension('metric_time', 'quarter') }} >= '2025-01-01'"]), [_change('active_subscriptions', 'quarter')]),
}

MALLOY: dict[str, str | None] = {
    'benchmark:1': 'run: orders -> { aggregate: net_revenue; where: order_date ? @2024 }',
    'benchmark:2': 'run: subscription_months -> { group_by: plan_name; aggregate: mrr; where: as_of_month = @2025-06-01 }',
    'benchmark:3': 'run: orders -> { aggregate: active_customers; where: order_date ? @2025-Q3 }',
    'benchmark:4': 'run: ad_stats -> { group_by: platform; aggregate: cost_per_click; where: activity_date ? @2025-Q2; order_by: cost_per_click asc; limit: 1 }',
    'benchmark:5': "run: tickets -> { aggregate: ticket_count, avg_first_reply_hours; where: priority = 'urgent' and created_date ? @2025 }",
    'benchmark:6': "run: inventory -> { aggregate: units_available; where: snapshot_date = @2025-09-30 and products.category = 'Footwear' }",
    'benchmark:7': 'run: customers -> { aggregate: customer_count; where: orders.order_id is not null and tickets.ticket_id is not null }',
    'benchmark:8': 'run: orders -> { group_by: sales_channel; aggregate: gross_revenue; where: order_date ? @2025-Q2 }',
    'benchmark:9': 'run: subscription_months -> { aggregate: churned_subscriptions; where: as_of_month ? @2025 }',
    'benchmark:10': 'run: order_lines -> { group_by: products.product_name; aggregate: units_sold; where: order_date ? @2025; order_by: units_sold desc; limit: 3 }',
    'benchmark:11': 'run: orders -> { group_by: is_first_order; aggregate: avg_order_value; where: order_date ? @2024 }',
    'benchmark:12': 'run: inventory -> { group_by: warehouse_code; aggregate: units_on_hand; where: snapshot_date = @2025-06-30 }',
    'benchmark:13': ('run: orders -> { group_by: m is order_date.month; aggregate: net_revenue }'
                     ' -> { select: m, net_revenue; calculate: growth is net_revenue / lag(net_revenue) - 1; order_by: m }'
                     ' -> { select: m, growth; where: m >= @2025-01 }'),
    'benchmark:14': ('run: orders -> { group_by: m is order_date.month; aggregate: net_revenue; where: order_date ? @2025 }'
                     ' -> { select: m, net_revenue; calculate: ytd is sum_moving(net_revenue, 11); order_by: m }'
                     ' -> { select: m, ytd }'),
    'benchmark:15': ('run: order_lines -> { group_by: products.category, products.product_name; aggregate: units_sold; where: order_date ? @2025 }'
                     ' -> { select: category, product_name, units_sold; calculate: r is rank() { partition_by: category; order_by: units_sold desc } }'
                     ' -> { select: category, product_name, units_sold; where: r = 1 }'),
    'benchmark:16': ('run: orders -> { aggregate: share is count() { where: order_sequence = 2 and days_since_prior_order <= 30 }'
                     ' / count() { where: order_sequence = 2 } }'),
    'benchmark:17': 'run: order_gap_stats -> { select: median_days; where: gap_after_order = 1 }',
    'benchmark:18': ('run: subscription_months -> { group_by: as_of_month; aggregate: mrr }'
                     ' -> { select: as_of_month, mrr; calculate: change is mrr - lag(mrr); order_by: as_of_month }'
                     ' -> { select: as_of_month, mrr, change; where: as_of_month >= @2025-01-01 }'),
    'benchmark:19': ('run: ad_stats -> { group_by: m is activity_date.month; aggregate: ad_spend }'
                     ' -> { select: m, ad_spend; calculate: avg_3m is avg_moving(ad_spend, 2); order_by: m }'
                     ' -> { select: m, ad_spend, avg_3m; where: m >= @2025-01 }'),
    'benchmark:20': ('run: orders -> { group_by: customer.email; aggregate: net_revenue, share is net_revenue / all(net_revenue);'
                     ' where: customer_key is not null; order_by: net_revenue desc; limit: 5 }'),
    'benchmark:21': ('run: tickets -> { group_by: priority; aggregate: share is count() { where: first_reply_hours <= 4 } / count();'
                     ' where: created_date ? @2025 }'),
    'benchmark:22': ('run: inventory -> { group_by: warehouse_code, snapshot_date; aggregate: units_available;'
                     ' where: snapshot_date >= @2025-07-01 and snapshot_date <= @2025-09-30 }'
                     ' -> { select: warehouse_code, snapshot_date, units_available;'
                     ' calculate: r is rank() { partition_by: warehouse_code; order_by: units_available asc } }'
                     ' -> { select: warehouse_code, snapshot_date, units_available; where: r = 1 }'),
    'holdout:1': ('run: orders -> { group_by: q is order_date.quarter; aggregate: net_revenue }'
                  ' -> { select: q, net_revenue; calculate: growth is net_revenue / lag(net_revenue) - 1; order_by: q }'
                  ' -> { select: growth; where: q = @2024-10-01 }'),
    'holdout:2': ('run: order_lines -> { group_by: orders.sales_channel, products.category; aggregate: product_revenue; where: order_date ? @2025 }'
                  ' -> { select: sales_channel, category, product_revenue;'
                  ' calculate: r is rank() { partition_by: sales_channel; order_by: product_revenue desc } }'
                  ' -> { select: sales_channel, category, product_revenue; where: r = 1 }'),
    'holdout:3': ('run: tickets -> { group_by: channel; aggregate: share is count() { where: resolution_hours <= 24 } / count();'
                  ' where: created_date ? @2025 }'),
    'holdout:4': 'run: order_gap_stats -> { select: median_days; where: gap_after_order = 2 }',
    'holdout:5': 'run: customers -> { aggregate: customer_count; where: memberships.subscription_id is not null and tickets.ticket_id is not null }',
    'holdout:6': ('run: subscription_months -> { group_by: as_of_month; aggregate: new_subscriptions; where: as_of_month ? @2025 }'
                  ' -> { select: as_of_month, new_subscriptions; calculate: running is sum_moving(new_subscriptions, 11); order_by: as_of_month }'
                  ' -> { select: as_of_month, running }'),
    'holdout:7': ('run: ad_stats -> { group_by: m is activity_date.month; aggregate: ad_spend, total_clicks }'
                  ' -> { select: m, ad_spend, total_clicks; calculate: cpc_3m is sum_moving(ad_spend, 2) / sum_moving(total_clicks, 2); order_by: m }'
                  ' -> { select: m, cpc_3m; where: m >= @2025-01 }'),
    'holdout:8': ('run: orders -> { group_by: customer.email; aggregate: order_count, share is order_count / all(order_count);'
                  ' where: order_date ? @2025 }'
                  ' -> { select: email, order_count, share; where: email is not null; order_by: order_count desc; limit: 2 }'),
    'holdout:9': ('run: inventory -> { group_by: warehouse_code, products.product_name; aggregate: units_on_hand; where: snapshot_date = @2025-09-30 }'
                  ' -> { select: warehouse_code, product_name, units_on_hand;'
                  ' calculate: r is rank() { partition_by: warehouse_code; order_by: units_on_hand desc } }'
                  ' -> { select: warehouse_code, product_name, units_on_hand; where: r = 1 }'),
    'holdout:10': ('run: subscription_months -> { group_by: q is as_of_month.quarter; aggregate: active_subscriptions;'
                   ' where: month(as_of_month) = 3 | 6 | 9 | 12 }'
                   ' -> { select: q, active_subscriptions; calculate: change is active_subscriptions - lag(active_subscriptions); order_by: q }'
                   ' -> { select: q, active_subscriptions, change; where: q >= @2025-01-01 }'),
}


def prompt_sets() -> dict[str, list[dict]]:
    """The sample prompt sets from static/js/config.js (the UI's source of truth)."""
    js = ("import('./static/js/config.js').then(m => console.log(JSON.stringify("
          "{benchmark: m.SAMPLE_PROMPTS, holdout: m.HOLDOUT_PROMPTS})))")
    return json.loads(subprocess.run(['node', '-e', js], cwd=ROOT, capture_output=True, text=True, check=True).stdout)


def grade(sql: str, expected: str | None) -> str:
    if expected is None:
        db.run_readonly(sql)
        return 'runs (judge-only)'
    return 'MATCH' if db.results_match(sql, expected) else 'differs'


async def main() -> None:
    sets = prompt_sets()
    totals: dict[str, list[int]] = {}
    for set_name, prompts in sets.items():
        print(f'\n{set_name} ({len(prompts)} questions)')
        for i, prompt in enumerate(prompts, 1):
            key, expected, cells = f'{set_name}:{i}', prompt.get('expected_sql'), []
            for target in ('metricflow', 'malloy'):
                try:
                    if target == 'metricflow':
                        entry = METRICFLOW.get(key)
                        sql = metricflow_bridge.compile(entry[0], entry[1]) if entry else None
                    else:
                        malloy = MALLOY.get(key)
                        sql = await malloy_bridge.compile(malloy) if malloy else None
                    verdict = grade(sql, expected) if sql else 'not expressible'
                except Exception as e:
                    verdict = f'ERROR {str(e).splitlines()[0][:80]}'
                ok = verdict in ('MATCH', 'runs (judge-only)')
                totals.setdefault(f'{set_name}/{target}', [0, 0])
                totals[f'{set_name}/{target}'][0] += ok
                totals[f'{set_name}/{target}'][1] += 1
                cells.append(f'{target}: {verdict}')
            print(f'  {i:>2}. {prompt["text"][:58]:<58}  ' + '   '.join(cells))
    print()
    for k, (ok, n) in totals.items():
        print(f'{k:<22} {ok}/{n} expressible ({ok / n:.0%})')


if __name__ == '__main__':
    asyncio.run(main())
