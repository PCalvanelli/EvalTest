"""The Malloy compiler sidecar (app/malloy_bridge): every reply must be the reply to its own request."""

import asyncio
import shutil

import pytest

from app import malloy_bridge

pytestmark = pytest.mark.skipif(not (malloy_bridge.ROOT / 'malloy' / 'node_modules').exists() or not shutil.which('node'),
                                reason='Malloy sidecar not installed')

QUERIES = [f'run: orders -> {{ aggregate: n is count(); limit: {i + 1} }}' for i in range(6)]


def test_concurrent_first_calls_share_one_process_and_get_their_own_sql():
    async def main():
        bridge = malloy_bridge._Sidecar()
        sqls = await asyncio.gather(*(bridge.compile(q) for q in QUERIES))
        bridge._stop()
        return sqls

    assert [f'LIMIT {i + 1}' in sql for i, sql in enumerate(asyncio.run(main()))] == [True] * len(QUERIES)


def test_a_cancelled_request_does_not_shift_later_replies():
    async def main():
        bridge = malloy_bridge._Sidecar()
        await bridge.compile(QUERIES[0])  # started
        task = asyncio.create_task(bridge.compile(QUERIES[1]))
        await asyncio.sleep(0)
        await asyncio.sleep(0)  # the request is written; its reply is on its way
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        sql = await bridge.compile(QUERIES[4])
        bridge._stop()
        return sql

    assert 'LIMIT 5' in asyncio.run(main())
