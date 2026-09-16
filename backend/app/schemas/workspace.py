"""Schemas for workspaces, membership and resource grants."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from pydantic import Field, field_validator

from app.models.enums import WorkspaceRole, WorkspaceStatus
from app.schemas.base import ApiModel

if TYPE_CHECKING:
    from app.models.group import Group
    from app.models.workspace import Workspace, WorkspaceMember, WorkspaceResourceGrant


class WorkspaceGroupRef(ApiModel):
    id: int
    name: str
    display_name: str


class WorkspaceSummary(ApiModel):
    @classmethod
    def from_workspace(
        cls,
        workspace: Workspace,
        *,
        member_count: int | None = None,
        resource_group_count: int | None = None,
    ) -> WorkspaceSummary:
        """Build the summary from a loaded workspace.

        Counts are passed in rather than read off the relationships: computing
        them per row would be an N+1 query on a list page, and the collections
        are not always loaded.
        """
        group: Group | None = workspace.group
        return cls(
            id=workspace.id,
            name=workspace.name,
            display_name=workspace.display_name or workspace.name,
            description=workspace.description,
            group_id=workspace.group_id,
            group_name=group.name if group else None,
            group_display_name=(group.display_name or group.name) if group else None,
            k8s_namespace=workspace.k8s_namespace,
            status=workspace.status,
            status_message=workspace.status_message,
            is_active=workspace.is_active,
            member_count=(
                member_count
                if member_count is not None
                else len(workspace.members)
                if "members" not in _unloaded(workspace)
                else 0
            ),
            resource_group_count=(
                resource_group_count
                if resource_group_count is not None
                else len(workspace.resource_grants)
                if "resource_grants" not in _unloaded(workspace)
                else 0
            ),
            created_at=workspace.created_at,
            updated_at=workspace.updated_at,
        )

    id: int
    name: str
    display_name: str
    description: str | None = None
    group_id: int
    group_name: str | None = None
    group_display_name: str | None = None
    k8s_namespace: str
    status: WorkspaceStatus
    status_message: str | None = None
    is_active: bool
    member_count: int = 0
    resource_group_count: int = 0
    created_at: datetime
    updated_at: datetime


class WorkspaceDetail(WorkspaceSummary):
    members: list[WorkspaceMemberRead] = Field(default_factory=list)
    resource_grants: list[WorkspaceResourceGrantRead] = Field(default_factory=list)


class WorkspaceCreate(ApiModel):
    name: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9-]*$")
    display_name: str = Field(default="", max_length=128)
    description: str | None = None
    #: Omitted means "the caller's own primary group", which is what a group
    #: admin creating a workspace for their own lab always wants.
    group_id: int | None = None
    #: Ids of pool owners to move in at creation time. The pool itself must be
    #: public or owned by the same group.
    resource_group_ids: list[int] = Field(default_factory=list)
    owner_user_id: int | None = None

    @field_validator("name")
    @classmethod
    def _reserved(cls, value: str) -> str:
        # The namespace is `wl-{group}-{name}`; a name like "admin" or "system"
        # reads like platform infrastructure and invites confusion in kubectl.
        if value in {"admin", "system", "kube-system", "default", "public"}:
            raise ValueError(f"{value!r} is reserved")
        return value


class WorkspaceUpdate(ApiModel):
    display_name: str | None = Field(default=None, max_length=128)
    description: str | None = None
    is_active: bool | None = None


class WorkspaceMemberRead(ApiModel):
    @classmethod
    def from_link(cls, link: WorkspaceMember) -> WorkspaceMemberRead:
        user = link.user
        return cls(
            user_id=link.user_id,
            username=user.username,
            full_name=user.full_name,
            display_name=user.display_name,
            email=user.email,
            is_active=user.is_active,
            role_in_workspace=link.role_in_workspace,
            joined_at=link.joined_at,
        )

    user_id: int
    username: str
    full_name: str
    display_name: str
    email: str | None = None
    is_active: bool
    role_in_workspace: WorkspaceRole
    joined_at: datetime


class WorkspaceMemberAssign(ApiModel):
    """Assign a user to a workspace.

    Because a user may belong to exactly one workspace, this *moves* them if
    they already have one — which is why the payload requires an explicit
    acknowledgement rather than silently relocating someone's work.
    """

    user_id: int
    role_in_workspace: WorkspaceRole = WorkspaceRole.MEMBER
    #: Must be true when the target user already belongs to another workspace.
    move_from_existing: bool = False


class WorkspaceResourceGrantRead(ApiModel):
    @classmethod
    def from_grant(cls, grant: WorkspaceResourceGrant) -> WorkspaceResourceGrantRead:
        group = grant.resource_group
        return cls(
            resource_group_id=grant.resource_group_id,
            name=group.name,
            display_name=group.display_name or group.name,
            kind=group.kind.value,
            network_type=group.network_type.value,
            granted_at=grant.granted_at,
            is_active=grant.is_active,
        )

    resource_group_id: int
    name: str
    display_name: str
    kind: str
    network_type: str
    granted_at: datetime
    is_active: bool


class WorkspaceResourceGrantCreate(ApiModel):
    resource_group_id: int


class WorkspaceQuotaSync(ApiModel):
    """Result of recomputing a workspace's quota and pushing it to the cluster."""

    quotas: list[WorkspaceQuotaLine] = Field(default_factory=list)
    applied: bool
    message: str | None = None


class WorkspaceQuotaLine(ApiModel):
    resource: str
    limit_value: int
    used_value: int
    unit: str


def _unloaded(instance: object) -> set[str]:
    """Names of relationships not yet loaded on an ORM instance.

    Touching an unloaded relationship from async code raises ``MissingGreenlet``
    rather than quietly issuing a query, so callers must check first.
    """
    from sqlalchemy import inspect

    state = inspect(instance, raiseerr=False)
    return set(state.unloaded) if state is not None else set()
