"""Aggregate router for ``/api/v1``.

Phase 1+ routers are registered here as they are implemented, e.g.::

    from app.api.v1 import auth
    api_router.include_router(auth.router, prefix="/auth", tags=["auth"])
"""

from __future__ import annotations

from fastapi import APIRouter

api_router = APIRouter()
