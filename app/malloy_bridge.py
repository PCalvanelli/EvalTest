"""Compile Malloy queries to DuckDB SQL through the Node sidecar in malloy/compile.mjs."""

from __future__ import annotations

import asyncio
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


class SidecarError(RuntimeError):
    """The compiler itself is unavailable (not installed, or its process died): infrastructure, not the query."""


class _Sidecar:
    """One long-lived Node process per event loop; requests are serialized (compiles take milliseconds)."""

    def __init__(self) -> None:
        self.proc: asyncio.subprocess.Process | None = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self.lock: asyncio.Lock | None = None

    async def _start(self) -> None:
        if not (ROOT / 'malloy' / 'node_modules').exists():
            raise SidecarError('Malloy sidecar is not installed: run `npm ci` in the malloy/ directory.')
        node = shutil.which('node')
        if node is None:
            raise SidecarError('Node.js is required for the Malloy context but `node` was not found on PATH.')
        self.proc = await asyncio.create_subprocess_exec(
            node, str(SIDECAR),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            limit=16 * 1024 * 1024,
        )
        reply = await self._request({'init': True, 'ddl': db.schema_ddl('marts'), 'model': MODEL_TEXT})
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
            raise SidecarError('Malloy sidecar exited unexpectedly.')
        return json.loads(line)

    async def compile(self, query: str) -> str:
        loop = asyncio.get_running_loop()
        if self.loop is not loop or self.proc is None or self.proc.returncode is not None:
            self.loop, self.lock = loop, asyncio.Lock()
            async with self.lock:
                await self._start()
        assert self.lock is not None
        async with self.lock:
            reply = await self._request({'query': query})
        if 'error' in reply:
            raise MalloyError(reply['error'])
        return reply['sql']


_sidecar = _Sidecar()


async def compile(query: str) -> str:
    """Compile a Malloy query (run: ... statement) against the warehouse model; raises MalloyError."""
    return await _sidecar.compile(query)

