"""RBAC catalogue payloads (read-only: the catalogue is seeded, not edited)."""

from __future__ import annotations

from app.models.rbac import Permission, Role
from app.schemas.base import ApiModel


class PermissionRead(ApiModel):
    code: str
    category: str
    description: str | None

    @classmethod
    def from_model(cls, permission: Permission) -> PermissionRead:
        return cls(
            code=permission.code,
            category=permission.category,
            description=permission.description,
        )


class RoleRead(ApiModel):
    code: str
    name: str
    description: str | None
    is_builtin: bool
    permissions: list[str]

    @classmethod
    def from_model(cls, role: Role) -> RoleRead:
        return cls(
            code=role.code,
            name=role.name,
            description=role.description,
            is_builtin=role.is_builtin,
            permissions=sorted(grant.permission.code for grant in role.permission_links),
        )


class PermissionCatalogue(ApiModel):
    """Every permission code, grouped by category — drives the UI matrix."""

    categories: dict[str, list[PermissionRead]]
    total: int
