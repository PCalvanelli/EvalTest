"""FastAPI entrypoint: serves the UI and runs the eval matrix."""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator
from contextlib import aclosing
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import gold, harness_view, history, mlflow_ui, tracking
from .agent import CONTEXTS, Context
from .evals import BENCHMARK_VERSION, matrix_cells, run_matrix
from .questions import QUESTION_SETS

STATIC = Path(__file__).resolve().parent.parent / 'static'

app = FastAPI(title='Semantic Layer Bench')
app.include_router(history.router)


@app.middleware('http')
async def revalidate_static(request: Request, call_next):
    # The page is ES modules; without this the browser can keep running an old module after an edit.
    response = await call_next(request)
    if request.url.path == '/' or request.url.path.startswith('/static/'):
        response.headers['Cache-Control'] = 'no-cache'
    return response


class RunRequest(BaseModel):
    models: list[str] = Field(min_length=1)
    harness_modes: list[bool] = Field(min_length=1)
    context_modes: list[Context] = Field(min_length=1)
    question_set: Literal['benchmark', 'holdout'] = 'benchmark'
    repeats: int = Field(1, ge=1, le=10, description='Run everything this many times, one after another.')


@app.get('/')
def index() -> FileResponse:
    return FileResponse(STATIC / 'index.html')


@app.get('/api/config')
def config() -> dict:
    return {
        'contexts': CONTEXTS,
        'benchmark_version': BENCHMARK_VERSION,
        'question_sets': {name: len(qs) for name, qs in QUESTION_SETS.items()},
        'api_key_set': bool(os.environ.get('ANTHROPIC_API_KEY')),
        'mlflow_url': tracking.UI_URL if tracking.ENABLED else None,
    }


@app.get('/api/harness/{context}')
async def harness(context: Context, mode: Literal['on', 'off'] = 'on') -> dict:
    """Read-only: what one target's agent sees with the harness, or without it."""
    return await harness_view.describe(context, mode == 'on')


@app.post('/api/run')
async def run(req: RunRequest) -> StreamingResponse:
    """Run the matrix and stream its events (see evals.run_matrix) as NDJSON, ending with a done line."""
    if not os.environ.get('ANTHROPIC_API_KEY'):
        raise HTTPException(status_code=422, detail='Set ANTHROPIC_API_KEY and restart the server.')
    try:
        gold.check(QUESTION_SETS[req.question_set])
    except gold.GoldError as e:
        raise HTTPException(status_code=409, detail=str(e)) from None

    async def stream() -> AsyncIterator[str]:
        cells = matrix_cells(req.models, req.harness_modes, req.context_modes)
        matrix = run_matrix(cells, QUESTION_SETS[req.question_set], req.repeats, {'question_set': req.question_set})
        async with aclosing(matrix) as events:
            async for event in events:
                yield json.dumps(event, default=str) + '\n'
        yield json.dumps({'type': 'done'}) + '\n'

    return StreamingResponse(stream(), media_type='application/x-ndjson')


app.mount('/static', StaticFiles(directory=STATIC), name='static')
# MLflow's own UI at /mlflow, from the same store. Mounted last: it answers every path our routes don't.
if (mlflow_app := mlflow_ui.asgi_app()) is not None:
    app.mount('/', mlflow_app)
