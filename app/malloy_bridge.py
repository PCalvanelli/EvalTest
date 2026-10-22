"""Compile Malloy queries to DuckDB SQL through the Node sidecar in malloy/compile.mjs."""

from __future__ import annotations

import asyncio
import itertools
import json
import shutil
from pathlib import Path

from . import db

ROOT = Path(__file__).resolve().parent.parent
SIDECAR = ROOT / 'malloy' / 'compile.mjs'
MODEL_PATH = Path(__file__).with_name('malloy') / 'warehouse.malloy'
MODEL_TEXT = MODEL_PATH.read_text()


class MalloyError(Exception):
    """The Malloy compiler rejected a query (or the model)."""


class _Sidecar:
    """One long-lived Node process per event loop; requests are serialized (compiles take milliseconds)."""

    def __init__(self) -> None:
        self.proc: asyncio.subprocess.Process | None = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self.lock: asyncio.Lock | None = None
        self.ids = itertools.count()

    async def _start(self) -> None:
        if not (ROOT / 'malloy' / 'node_modules').exists():
            raise RuntimeError('Malloy sidecar is not installed: run `npm ci` in the malloy/ directory.')
        node = shutil.which('node')
        if node is None:
            raise RuntimeError('Node.js is required for the Malloy context but `node` was not found on PATH.')
        self.proc = await asyncio.create_subprocess_exec(
            node, str(SIDECAR),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            limit=16 * 1024 * 1024,
        )
        reply = await self._request({'type': 'init', 'ddl': db.schema_ddl('marts'), 'model': MODEL_TEXT})
        if 'error' in reply:
            self.proc.kill()
            self.proc = None
            raise MalloyError(f'Malloy model failed to compile: {reply["error"]}')

    async def _request(self, msg: dict) -> dict:
        assert self.proc and self.proc.stdin and self.proc.stdout
        self.proc.stdin.write((json.dumps(msg) + '\n').encode())
        await self.proc.stdin.drain()
        line = await self.proc.stdout.readline()
        if not line:
            raise RuntimeError('Malloy sidecar exited unexpectedly.')
        return json.loads(line)

    async def compile(self, query: str) -> str:
        loop = asyncio.get_running_loop()
        if self.loop is not loop or self.proc is None or self.proc.returncode is not None:
            self.loop, self.lock = loop, asyncio.Lock()
            async with self.lock:
                await self._start()
        assert self.lock is not None
        async with self.lock:
            reply = await self._request({'id': next(self.ids), 'query': query})
        if 'error' in reply:
            raise MalloyError(reply['error'])
        return reply['sql']


_sidecar = _Sidecar()


async def compile(query: str) -> str:
    """Compile a Malloy query (run: ... statement) against the warehouse model; raises MalloyError."""
    return await _sidecar.compile(query)


if __name__ == '__main__':
    # Parity check: the Malloy model must reproduce the gold answers and the YAML semantic layer exactly.
    from .semantic.layer import Filter as F, SemanticQuery as Q, compile as compile_semantic

    GOLD = {
        'net revenue 2024': (
            'run: orders -> { aggregate: net_revenue; where: order_date ? @2024 }',
            "SELECT SUM(o.subtotal_usd - least(coalesce(r.refund_usd, 0), o.subtotal_usd)) FROM marts.fct_orders o LEFT JOIN (SELECT order_id, SUM(refund_amount_usd) AS refund_usd FROM marts.fct_refunds GROUP BY 1) r USING (order_id) WHERE o.cancelled_at IS NULL AND o.order_date BETWEEN '2024-01-01' AND '2024-12-31'"),
        'MRR end of June 2025 by plan': (
            'run: subscription_months -> { group_by: plan_name; aggregate: mrr; where: as_of_month = @2025-06-01 }',
            "SELECT plan_name, SUM(CASE WHEN billing_interval = 'year' THEN plan_amount_usd / 12 ELSE plan_amount_usd END) FROM marts.dim_subscriptions WHERE started_at < DATE '2025-07-01' AND (canceled_at IS NULL OR CAST(canceled_at AS DATE) > DATE '2025-06-30') GROUP BY 1"),
        'active customers Q3 2025': (
            'run: orders -> { aggregate: active_customers; where: order_date ? @2025-Q3 }',
            "SELECT COUNT(DISTINCT customer_key) FROM marts.fct_orders WHERE cancelled_at IS NULL AND order_date BETWEEN '2025-07-01' AND '2025-09-30'"),
        'CPC by platform Q2 2025': (
            'run: ad_stats -> { group_by: platform; aggregate: cost_per_click; where: activity_date ? @2025-Q2 }',
            "SELECT platform, SUM(cost_usd) / SUM(clicks) FROM marts.fct_ad_stats WHERE date BETWEEN '2025-04-01' AND '2025-06-30' GROUP BY 1"),
        'urgent tickets 2025': (
            "run: tickets -> { aggregate: ticket_count, avg_first_reply_hours; where: priority = 'urgent' and created_date ? @2025 }",
            "SELECT COUNT(*), AVG(date_diff('minute', t.created_at, fr.m) / 60.0) FROM marts.fct_tickets t LEFT JOIN (SELECT ticket_id, MIN(created_at) m FROM marts.fct_ticket_comments WHERE is_public AND author_role = 'agent' GROUP BY 1) fr USING (ticket_id) WHERE priority = 'urgent' AND t.created_at >= '2025-01-01'"),
        'Footwear available 2025-09-30': (
            "run: inventory -> { aggregate: units_available; where: snapshot_date = @2025-09-30 and products.category = 'Footwear' }",
            "SELECT SUM(i.qty_on_hand - i.qty_reserved) FROM marts.fct_inventory_snapshots i JOIN marts.dim_products p USING (product_id) WHERE i.snapshot_date = '2025-09-30' AND p.category = 'Footwear'"),
        'customers with orders and tickets': (
            'run: customers -> { aggregate: customer_count; where: orders.order_id is not null and tickets.ticket_id is not null }',
            'SELECT COUNT(DISTINCT o.customer_key) FROM marts.fct_orders o JOIN marts.fct_tickets t USING (customer_key)'),
    }

    # Every governed metric, grouped, against the YAML semantic layer.
    YAML_PARITY = {
        'orders by month': (
            'run: orders -> { group_by: m is order_date.month; aggregate: order_count, gross_revenue, refunds, net_revenue, avg_order_value, active_customers }',
            Q(model='orders', dimensions=['order_date__month'], metrics=['order_count', 'gross_revenue', 'refunds', 'net_revenue', 'avg_order_value', 'active_customers'])),
        'orders by segment/source/status': (
            'run: orders -> { group_by: customer.customer_segment, customer.acquisition_source, order_status; aggregate: net_revenue, order_count }',
            Q(model='orders', dimensions=['customer_segment', 'acquisition_source', 'order_status'], metrics=['net_revenue', 'order_count'])),
        'order lines by category': (
            'run: order_lines -> { group_by: products.category; aggregate: units_sold, product_revenue }',
            Q(model='order_lines', dimensions=['product_category'], metrics=['units_sold', 'product_revenue'])),
        'subscriptions by month': (
            'run: subscription_months -> { group_by: as_of_month; aggregate: mrr, active_subscriptions, new_subscriptions, churned_subscriptions }',
            Q(model='subscriptions', dimensions=['month'], metrics=['mrr', 'active_subscriptions', 'new_subscriptions', 'churned_subscriptions'])),
        'support by priority/segment': (
            'run: tickets -> { group_by: priority, customer.customer_segment; aggregate: ticket_count, solved_tickets, avg_first_reply_hours, avg_resolution_hours }',
            Q(model='support', dimensions=['priority', 'customer_segment'], metrics=['ticket_count', 'solved_tickets', 'avg_first_reply_hours', 'avg_resolution_hours'])),
        'marketing by platform': (
            'run: ad_stats -> { group_by: platform; aggregate: ad_spend, total_clicks, total_impressions, cost_per_click, click_through_rate }',
            Q(model='marketing', dimensions=['platform'], metrics=['ad_spend', 'clicks', 'impressions', 'cost_per_click', 'click_through_rate'])),
        'inventory by warehouse on 2025-06-30': (
            'run: inventory -> { group_by: warehouse_code; aggregate: units_on_hand, units_reserved, units_available; where: snapshot_date = @2025-06-30 }',
            Q(model='inventory', dimensions=['warehouse_code'], metrics=['units_on_hand', 'units_reserved', 'units_available'],
              filters=[F(field='snapshot_date', op='=', value='2025-06-30')])),
    }

    async def main() -> None:
        failures = 0
        async def run(malloy: str) -> list[tuple]:
            try:
                return db.result_set(await compile(malloy))
            except Exception as e:
                return [('ERROR', str(e))]

        for name, (malloy, gold_sql) in GOLD.items():
            got, want = await run(malloy), db.result_set(gold_sql)
            failures += got != want
            print(f'{"OK  " if got == want else "FAIL"} gold   {name}: {got if got == want else (got, want)}')
        for name, (malloy, q) in YAML_PARITY.items():
            # An all-filtered-out group sums to 0 in Malloy but NULL in plain SQL; same meaning here.
            null_as_0 = lambda rows: [tuple(0 if v is None else v for v in r) for r in rows]  # noqa: E731
            got, want = null_as_0(await run(malloy)), null_as_0(db.result_set(compile_semantic(q)))
            failures += got != want
            print(f'{"OK  " if got == want else "FAIL"} yaml   {name} ({len(got)} rows)' + ('' if got == want else f'\n  malloy {got[:3]}\n  yaml   {want[:3]}'))
        try:
            await compile('run: orders -> { aggregate: revenue }')
        except MalloyError as e:
            print('OK   compile error surfaces:', e)
        raise SystemExit(1 if failures else 0)

    asyncio.run(main())
