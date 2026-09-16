"""Versioned API router.

Every resource router is registered here; `app.main` mounts this once at
``settings.api_v1_prefix``.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.v1 import (
    applications,
    auth,
    groups,
    images,
    instances,
    rbac,
    resources,
    storages,
    tasks,
    users,
    workspaces,
)

api_router = APIRouter()

api_router.include_router(auth.router)
api_router.include_router(users.router)
api_router.include_router(groups.router)
api_router.include_router(rbac.router)
api_router.include_router(resources.router)
api_router.include_router(workspaces.router)
api_router.include_router(instances.router)
api_router.include_router(applications.router)
api_router.include_router(tasks.router)
api_router.include_router(images.router)
api_router.include_router(storages.router)
