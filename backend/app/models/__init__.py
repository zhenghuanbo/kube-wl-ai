"""ORM models.

Every model **must** be imported here: Alembic's autogenerate only sees tables
that have been registered on ``Base.metadata``.
"""

from __future__ import annotations

from app.models.audit import AuditLog
from app.models.enums import GroupKind, GroupRole, RoleCode, RoleScope, UserSource
from app.models.group import Group, UserGroupLink
from app.models.rbac import Permission, Role, RolePermission, UserRole
from app.models.ssh_key import SshKey
from app.models.token import ApiToken, RefreshToken
from app.models.user import User

__all__ = [
    "ApiToken",
    "AuditLog",
    "Group",
    "GroupKind",
    "GroupRole",
    "Permission",
    "RefreshToken",
    "Role",
    "RoleCode",
    "RolePermission",
    "RoleScope",
    "SshKey",
    "User",
    "UserGroupLink",
    "UserRole",
    "UserSource",
]
