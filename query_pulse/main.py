"""FastAPI application factory.

Run locally with ``uvicorn query_pulse.main:create_app --factory --reload``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text
from starlette.middleware.sessions import SessionMiddleware

from query_pulse import __doc__ as description
from query_pulse.accounts.router import router as auth_router
from query_pulse.config import Settings, get_settings
from query_pulse.db import create_engine, create_sessionmaker
from query_pulse.sources.registry import SourceRegistry
from query_pulse.sources.router import router as sources_router

VERSION = "0.1.0"


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    engine = create_engine(settings.database_url)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        await engine.dispose()

    app = FastAPI(
        title="Query Pulse API", version=VERSION, description=description, lifespan=lifespan
    )
    app.state.settings = settings
    app.state.engine = engine
    app.state.sessionmaker = create_sessionmaker(engine)
    app.state.source_registry = SourceRegistry()

    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.session_secret.get_secret_value(),
        session_cookie="qp_session",
        max_age=settings.session_max_age_seconds,
        same_site="lax",
        https_only=settings.session_https_only,
    )

    @app.get("/health", tags=["ops"], operation_id="health")
    async def health() -> dict[str, str]:
        """Liveness: the process is up."""
        return {"status": "ok"}

    @app.get("/ready", tags=["ops"], operation_id="ready", response_model=None)
    async def ready(request: Request) -> dict[str, str] | JSONResponse:
        """Readiness: the app database answers."""
        try:
            async with request.app.state.engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
        except Exception:  # noqa: BLE001 - any failure means not ready
            return JSONResponse({"status": "not_ready", "database": "unreachable"}, status_code=503)
        return {"status": "ready", "database": "ok"}

    app.include_router(auth_router)
    app.include_router(sources_router)
    return app
