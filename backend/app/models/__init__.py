"""ORM models.

Every model **must** be imported here: Alembic's autogenerate only sees tables
that have been registered on ``Base.metadata``.
"""

from __future__ import annotations

from app.models.application import Application
from app.models.audit import AuditLog
from app.models.enums import (
    TERMINAL_TASK_STATUSES,
    ApplicationCategory,
    ApplicationVisibility,
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
    TaskStatus,
    UserSource,
    WorkloadEventType,
    WorkloadKind,
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
from app.models.task import Task
from app.models.token import ApiToken, RefreshToken
from app.models.user import User
from app.models.workload_event import WorkloadEvent
from app.models.workspace import Workspace, WorkspaceMember, WorkspaceResourceGrant

__all__ = [
    "GPU_MODELS",
    "TERMINAL_TASK_STATUSES",
    "ApiToken",
    "Application",
    "ApplicationCategory",
    "ApplicationVisibility",
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
    "Task",
    "TaskStatus",
    "User",
    "UserGroupLink",
    "UserRole",
    "UserSource",
    "WorkloadEvent",
    "WorkloadEventType",
    "WorkloadKind",
    "Workspace",
    "WorkspaceMember",
    "WorkspaceResourceGrant",
    "WorkspaceRole",
    "WorkspaceStatus",
    "gpu_resource_key",
]
