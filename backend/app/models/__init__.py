"""ORM models.

Every model **must** be imported here: Alembic's autogenerate only sees tables
that have been registered on ``Base.metadata``.
"""

from __future__ import annotations

from app.models.audit import AuditLog
from app.models.enums import (
    GpuDeviceStatus,
    GroupKind,
    GroupRole,
    IdeType,
    InstanceStatus,
    NetworkType,
    NodeStatus,
    QuotaResource,
    QuotaScope,
    ResourceGroupKind,
    RoleCode,
    RoleScope,
    UserSource,
    WorkspaceRole,
    WorkspaceStatus,
)
from app.models.group import Group, UserGroupLink
from app.models.instance import Instance
from app.models.rbac import Permission, Role, RolePermission, UserRole
from app.models.resource import (
    GPU_MODELS,
    ComputeNode,
    GpuDevice,
    Quota,
    ResourceGroup,
    ResourceGroupLimit,
    ResourceGroupSpec,
    ResourceSpec,
    RuntimeLimit,
    gpu_resource_key,
)
from app.models.ssh_key import SshKey
from app.models.token import ApiToken, RefreshToken
from app.models.user import User
from app.models.workspace import Workspace, WorkspaceMember, WorkspaceResourceGrant

__all__ = [
    "GPU_MODELS",
    "ApiToken",
    "AuditLog",
    "ComputeNode",
    "GpuDevice",
    "GpuDeviceStatus",
    "Group",
    "GroupKind",
    "GroupRole",
    "IdeType",
    "Instance",
    "InstanceStatus",
    "NetworkType",
    "NodeStatus",
    "Permission",
    "Quota",
    "QuotaResource",
    "QuotaScope",
    "RefreshToken",
    "ResourceGroup",
    "ResourceGroupKind",
    "ResourceGroupLimit",
    "ResourceGroupSpec",
    "ResourceSpec",
    "Role",
    "RoleCode",
    "RolePermission",
    "RoleScope",
    "RuntimeLimit",
    "SshKey",
    "User",
    "UserGroupLink",
    "UserRole",
    "UserSource",
    "Workspace",
    "WorkspaceMember",
    "WorkspaceResourceGrant",
    "WorkspaceRole",
    "WorkspaceStatus",
    "gpu_resource_key",
]
