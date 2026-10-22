"""FastAPI entrypoint: serves the UI and runs the eval matrix."""

from __future__ import annotations

import asyncio
import itertools
import json
import os
from collections.abc import AsyncIterator
from pathlib import Path

os.environ.setdefault('PYDANTIC_AI_NO_BANNER', '1')

from fastapi import FastAPI, HTTPException  # noqa: E402
from fastapi.responses import FileResponse, StreamingResponse  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402

from . import db, malloy_bridge  # noqa: E402
from .agent import CONTEXTS, GLOSSARY, HARNESS_CAPABILITIES, Context  # noqa: E402
from .evals import run_eval  # noqa: E402
from .semantic import layer  # noqa: E402

STATIC = Path(__file__).resolve().parent.parent / 'static'
MAX_CONCURRENT_AGENT_RUNS = 6

app = FastAPI(title='Text-to-SQL Eval Playground')


class PromptIn(BaseModel):
    text: str
    expected_sql: str | None = None


class RunRequest(BaseModel):
    api_key: str = Field(min_length=1)
    workspace_id: str | None = None
    models: list[str] = Field(min_length=1)
    judge_model: str = 'claude-haiku-5-5'
    harness_modes: list[bool] = Field(min_length=1)
    context_modes: list[Context] = Field(min_length=1)
    harness: list[str] = Field(default_factory=lambda: list(HARNESS_CAPABILITIES))
    prompts: list[PromptIn] = Field(min_length=1)
    rubric: str = Field(min_length=1)
    repeats: int = Field(1, ge=1, le=10, description='Run the whole matrix this many times, one after another.')


def cell_id(model: str, harness_on: bool, context: str) -> str:
    return f'{model}|{"harness" if harness_on else "no-harness"}|{context}'


@app.get('/')
def index() -> FileResponse:
    return FileResponse(STATIC / 'index.html')


@app.get('/api/config')
def config() -> dict:
    return {
        'schema': db.schema_overview(),
        'table_count': len(db.list_tables()),
        'semantic_catalog': layer.catalog_text(),
        'glossary': GLOSSARY,
        'malloy_model': malloy_bridge.MODEL_TEXT,
        'contexts': CONTEXTS,
        'harness_capabilities': HARNESS_CAPABILITIES,
    }


@app.post('/api/run')
async def run(req: RunRequest) -> StreamingResponse:
    """Run every (model, harness, context) cell `repeats` times, sequentially by run, and stream NDJSON:
    one plan line, then a run_start line per run and one line per finished cell."""
    if True in req.harness_modes and not req.harness:
        raise HTTPException(status_code=422, detail='Harness "On" is selected but no harness capabilities are enabled.')
    prompts = [p.model_dump() for p in req.prompts if p.text.strip()]
    if not prompts:
        raise HTTPException(status_code=422, detail='Add at least one prompt.')

    cells = [
        {'id': cell_id(m, h, ctx), 'model': m, 'harness_on': h, 'context': ctx}
        for m, h, ctx in itertools.product(dict.fromkeys(req.models), dict.fromkeys(req.harness_modes),
                                           dict.fromkeys(req.context_modes))
    ]
    limiter = asyncio.Semaphore(MAX_CONCURRENT_AGENT_RUNS)
    workspace_id = (req.workspace_id or '').strip() or None

    async def run_cell(cell: dict, run_no: int) -> dict:
        try:
            report = await run_eval(
                label=cell['id'],
                api_key=req.api_key,
                workspace_id=workspace_id,
                model=cell['model'],
                judge_model=req.judge_model,
                prompts=prompts,
                rubric=req.rubric,
                harness=req.harness if cell['harness_on'] else [],
                context=cell['context'],
                limiter=limiter,
            )
            return {'type': 'cell', 'run': run_no, 'report': {**report, 'id': cell['id'], 'run': run_no}}
        except Exception as e:
            return {'type': 'cell_error', 'run': run_no, 'id': cell['id'], 'error': f'{type(e).__name__}: {e}'}

    async def stream() -> AsyncIterator[str]:
        yield json.dumps({'type': 'plan', 'cells': cells, 'prompts': len(prompts), 'repeats': req.repeats}) + '\n'
        for run_no in range(1, req.repeats + 1):
            yield json.dumps({'type': 'run_start', 'run': run_no}) + '\n'
            for next_done in asyncio.as_completed([run_cell(c, run_no) for c in cells]):
                yield json.dumps(await next_done, default=str) + '\n'
        yield json.dumps({'type': 'done'}) + '\n'

    return StreamingResponse(stream(), media_type='application/x-ndjson')
