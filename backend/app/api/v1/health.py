"""Liveness / readiness probes and the Prometheus metrics endpoint."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Response
from sqlalchemy import text

from app.core.config import get_settings
from app.db.session import get_engine

router = APIRouter(tags=["health"])

VERSION = "0.1.0"


@router.get("/healthz", summary="Liveness probe")
async def healthz() -> dict[str, Any]:
    """Returns 200 as soon as the process is serving requests."""
    settings = get_settings()
    return {
        "status": "ok",
        "version": VERSION,
        "app_env": settings.app_env,
    }


@router.get("/readyz", summary="Readiness probe")
async def readyz(response: Response) -> dict[str, Any]:
    """Checks dependencies that must be reachable before serving traffic."""
    checks: dict[str, str] = {}
    healthy = True

    try:
        engine = get_engine()
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        checks["database"] = "ok"
    except Exception as exc:
        checks["database"] = f"error: {exc}"
        healthy = False

    if not healthy:
        response.status_code = 503

    return {"status": "ok" if healthy else "degraded", "checks": checks}


@router.get("/metrics", summary="Prometheus metrics", include_in_schema=False)
async def metrics() -> Response:
    """Expose Prometheus metrics when prometheus_client is installed."""
    try:
        from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
    except ImportError:  # pragma: no cover
        return Response(status_code=404)

    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)
