"""RBAC catalogue endpoints.

The catalogue is seeded from `app.auth.permissions` and is read-only at runtime:
roles are code-defined so an operator cannot accidentally invent a capability the
backend does not enforce. These endpoints exist so the admin UI can render the
permission matrix from the same source of truth the server uses.
"""

from __future__ import annotations

from collections import defaultdict

from fastapi import APIRouter, Depends
from sqlalchemy import select

from app.core.deps import DbSession, require
from app.core.errors import NotFoundError
from app.models.rbac import Permission, Role
from app.schemas.rbac import PermissionCatalogue, PermissionRead, RoleRead

router = APIRouter(tags=["rbac"])


@router.get(
    "/roles",
    response_model=list[RoleRead],
    summary="List roles with their permissions",
    dependencies=[Depends(require("role:read"))],
)
async def list_roles(session: DbSession) -> list[RoleRead]:
    roles = (await session.scalars(select(Role).order_by(Role.id))).all()
    return [RoleRead.from_model(role) for role in roles]


@router.get(
    "/roles/{role_code}",
    response_model=RoleRead,
    summary="Read one role",
    dependencies=[Depends(require("role:read"))],
)
async def read_role(role_code: str, session: DbSession) -> RoleRead:
    role = await session.scalar(select(Role).where(Role.code == role_code))
    if role is None:
        raise NotFoundError("角色不存在")
    return RoleRead.from_model(role)


@router.get(
    "/permissions",
    response_model=PermissionCatalogue,
    summary="The full permission catalogue, grouped by category",
    dependencies=[Depends(require("permission:read"))],
)
async def list_permissions(session: DbSession) -> PermissionCatalogue:
    permissions = (
        await session.scalars(select(Permission).order_by(Permission.category, Permission.code))
    ).all()

    grouped: dict[str, list[PermissionRead]] = defaultdict(list)
    for permission in permissions:
        grouped[permission.category].append(PermissionRead.from_model(permission))

    return PermissionCatalogue(categories=dict(grouped), total=len(permissions))
