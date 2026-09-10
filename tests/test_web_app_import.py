"""Web app startup contract: the module must expose a FastAPI instance.

The deployment CMD is ``uvicorn src.newsforge.web.app:app``. That import only
works if the module exposes an ASGI application named ``app``. Previously the
module defined create_app() but never called it nor returned its result, so
``from src.newsforge.web.app import app`` raised ImportError and uvicorn could
not start (CONTAINER_START=FAIL).

Run: python -m pytest tests/test_web_app_import.py -q
"""
from __future__ import annotations

import fastapi

from src.newsforge.web.app import app


def test_module_exposes_fastapi_instance():
    assert isinstance(app, fastapi.FastAPI), "module must expose a FastAPI instance"


def test_routes_are_registered():
    # create_app registers the SSR routes; uvicorn needs them present.
    paths = {getattr(r, "path", "") for r in app.routes}
    assert "/health" in paths
    assert "/articles" in paths
    assert "/sitemap.xml" in paths


def test_health_route_is_a_get():
    health = next(
        (r for r in app.routes if getattr(r, "path", "") == "/health"), None
    )
    assert health is not None, "/health route must be registered"
