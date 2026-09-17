"""FastAPI application factory."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.v1 import health
from app.api.v1.router import api_router
from app.api.wellknown import router as wellknown_router
from app.api.ws import terminal_router
from app.core.config import Settings, get_settings
from app.core.errors import register_exception_handlers
from app.core.logging import configure_logging, get_logger
from app.core.version import APP_NAME, VERSION
from app.db.session import dispose_engine, init_engine
from app.middleware.audit import AuditMiddleware
from app.middleware.request_context import REQUEST_ID_HEADER, RequestContextMiddleware

logger = get_logger(__name__)

_DEV_CORS_ORIGINS = [
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "http://localhost:8080",
    "http://127.0.0.1:8080",
]


def _resolve_cors_origins(settings: Settings) -> list[str]:
    if settings.cors_origins:
        return settings.cors_origins
    # Never fall back to "*": credentials are allowed, and the browser rejects
    # wildcard origins together with credentials.
    return [] if settings.is_production else list(_DEV_CORS_ORIGINS)


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build and configure the application."""
    settings = settings or get_settings()
    configure_logging(settings)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncGenerator[None]:
        logger.info("startup", app_env=settings.app_env, version=VERSION)
        init_engine(settings)
        yield
        await dispose_engine()
        logger.info("shutdown")

    app = FastAPI(
        title=f"{APP_NAME} API",
        description="Kubernetes GPU resource scheduling and management system",
        version=VERSION,
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url=f"{settings.api_v1_prefix}/openapi.json",
        lifespan=lifespan,
    )

    register_exception_handlers(app)

    # Middleware order matters: `add_middleware` prepends, so the last one added
    # is the outermost. Desired nesting, outermost first:
    #   CORS -> request context (id/logging) -> audit -> router
    # CORS must wrap everything so error responses still carry its headers, and
    # the audit layer must run inside the request context to see the request id.
    app.add_middleware(AuditMiddleware, api_prefix=settings.api_v1_prefix)
    app.add_middleware(RequestContextMiddleware)

    cors_origins = _resolve_cors_origins(settings)
    if cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=cors_origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
            expose_headers=[REQUEST_ID_HEADER],
        )

    # Infra probes and well-known documents live at the root, outside the
    # versioned API surface.
    app.include_router(health.router)
    # Websockets live outside the versioned prefix: they speak a terminal
    # protocol, not the REST envelope.
    app.include_router(terminal_router)
    app.include_router(wellknown_router)
    app.include_router(api_router, prefix=settings.api_v1_prefix)

    return app


app = create_app()
