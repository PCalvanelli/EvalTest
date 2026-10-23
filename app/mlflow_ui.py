"""MLflow's own UI, served by the app at /mlflow from the local tracking store, so there is no second server.

MLflow's server reads its store, artifact root and URL prefix from environment variables when `mlflow.server`
is first imported, so they are set just before that import. Mount the returned app LAST: it answers every path
under /mlflow, and anything it doesn't know gets its 404.
"""

from __future__ import annotations

import logging
import os

from . import tracking

_log = logging.getLogger(__name__)

PREFIX = '/mlflow'


def asgi_app():
    """MLflow's ASGI app with all its routes under /mlflow, or None when the store isn't a local SQLite file."""
    if not tracking.EMBEDDED_UI:
        return None
    tracking.DATA.mkdir(exist_ok=True)
    os.environ['_MLFLOW_SERVER_FILE_STORE'] = tracking.TRACKING_URI
    os.environ['_MLFLOW_SERVER_ARTIFACT_ROOT'] = (tracking.DATA / 'mlartifacts').as_uri()
    os.environ['_MLFLOW_STATIC_PREFIX'] = PREFIX
    try:
        from mlflow.server.fastapi_app import app  # noqa: PLC0415 - must follow the environment setup above
    except Exception:
        _log.exception('Could not load the MLflow UI; /mlflow is unavailable')
        return None
    return app
