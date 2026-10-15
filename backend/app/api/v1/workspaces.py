"""Workspace endpoints.

A workspace is the tenancy boundary: it owns a Kubernetes namespace, may hold
several users, and is the thing resource groups are granted to.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from app.auth.policy import PermissionCheck
from app.core.deps import CurrentUser, DbSession, require
from app.k8s import get_gateway
from app.models.enums import QuotaProfileKind, WorkspaceStatus
from app.models.workspace import Workspace
from app.schemas.base import ApiMessage
from app.schemas.common import Page, PageMeta
from app.schemas.quota_profile import QuotaProfileAssign
from app.schemas.workspace import (
    WorkspaceCreate,
    WorkspaceDetail,
    WorkspaceMemberAssign,
    WorkspaceMemberRead,
    WorkspaceQuotaSync,
    WorkspaceResourceGrantCreate,
    WorkspaceResourceGrantRead,
    WorkspaceSummary,
    WorkspaceUpdate,
)
from app.services import quota_profiles as quota_profile_service
from app.services import workspaces as workspace_service

router = APIRouter(prefix="/workspaces", tags=["workspaces"])

#: Permission dependencies, named so the route signatures stay readable.
CanRead = Depends(require("workspace:read"))
CanCreate = Depends(require("workspace:create"))
CanUpdate = Depends(require("workspace:update"))
CanDelete = Depends(require("workspace:delete"))
CanAssign = Depends(require("workspace:assign_member"))
CanGrant = Depends(require("workspace:grant_resource"))
#: Not folded into `CanUpdate`: this is the permission that moves a ceiling, and a
#: group administrator may rename a workspace without being able to re-quota it.
CanAssignQuota = Depends(require("quota_profile:assign"))


def _pages(total: int, page_size: int) -> int:
    return (total + page_size - 1) // page_size if page_size else 0


@router.get("", response_model=Page[WorkspaceSummary])
async def list_workspaces(
    session: DbSession,
    actor: CurrentUser,
    _: Annotated[PermissionCheck, CanRead],
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=200, alias="pageSize")] = 20,
    search: Annotated[str | None, Query(max_length=150)] = None,
    group_id: Annotated[int | None, Query(alias="groupId")] = None,
    status_filter: Annotated[WorkspaceStatus | None, Query(alias="status")] = None,
    is_active: Annotated[bool | None, Query(alias="isActive")] = None,
) -> Page[WorkspaceSummary]:
    """A user sees the workspace they belong to; a group admin sees their lab's."""
    result = await workspace_service.list_workspaces(
        session,
        actor,
        group_id=group_id,
        status=status_filter,
        is_active=is_active,
        search=search,
        offset=(page - 1) * page_size,
        limit=page_size,
    )
    return Page[WorkspaceSummary](
        items=list(result.items),
        meta=PageMeta(
            page=page,
            page_size=page_size,
            total=result.total,
            pages=_pages(result.total, page_size),
        ),
    )


@router.get("/mine", response_model=WorkspaceSummary | None)
async def my_workspace(
    session: DbSession,
    actor: CurrentUser,
    _: Annotated[PermissionCheck, CanRead],
) -> WorkspaceSummary | None:
    """The caller's own workspace.

    A dedicated endpoint because a user belongs to exactly one workspace and the
    UI needs it on every page load; listing and filtering would be wasteful and
    would depend on the scope filter getting the single-row case right.
    """
    workspace = await workspace_service.member_of(session, actor.id)
    if workspace is None:
        return None
    return workspace_service.to_summary(workspace)


@router.post("", response_model=WorkspaceDetail, status_code=status.HTTP_201_CREATED)
async def create_workspace(
    session: DbSession,
    actor: CurrentUser,
    _: Annotated[PermissionCheck, CanCreate],
    payload: WorkspaceCreate,
) -> WorkspaceDetail:
    """Create the row and provision its namespace, quota and network policy."""
    workspace = await workspace_service.create_workspace(session, actor, payload, get_gateway())
    return await _detail(session, workspace)


@router.get("/{workspace_id}", response_model=WorkspaceDetail)
async def get_workspace(
    session: DbSession,
    actor: CurrentUser,
    _: Annotated[PermissionCheck, CanRead],
    workspace_id: int,
) -> WorkspaceDetail:
    workspace = await workspace_service.get_workspace(session, actor, workspace_id)
    return await _detail(session, workspace)


@router.patch("/{workspace_id}", response_model=WorkspaceDetail)
async def update_workspace(
    session: DbSession,
    actor: CurrentUser,
    _: Annotated[PermissionCheck, CanUpdate],
    workspace_id: int,
    payload: WorkspaceUpdate,
) -> WorkspaceDetail:
    workspace = await workspace_service.get_workspace(session, actor, workspace_id)
    workspace = await workspace_service.update_workspace(session, actor, workspace, payload)
    return await _detail(session, workspace)


@router.delete("/{workspace_id}", response_model=ApiMessage)
async def delete_workspace(
    session: DbSession,
    actor: CurrentUser,
    _: Annotated[PermissionCheck, CanDelete],
    workspace_id: int,
) -> ApiMessage:
    """Deletes the Kubernetes namespace. The row is retained for audit."""
    workspace = await workspace_service.get_workspace(session, actor, workspace_id)
    await workspace_service.delete_workspace(session, actor, workspace, get_gateway())
    return ApiMessage(message=f"工作空间 {workspace.name} 已删除")


# --- cluster state -----------------------------------------------------------
@router.post("/{workspace_id}/reconcile", response_model=WorkspaceQuotaSync)
async def reconcile_workspace(
    session: DbSession,
    actor: CurrentUser,
    _: Annotated[PermissionCheck, CanUpdate],
    workspace_id: int,
) -> WorkspaceQuotaSync:
    """Re-apply the namespace, quota, limit range and network policy.

    Idempotent by design: this is the repair action for a workspace stuck in
    ``error``, and the same call the periodic reconcile loop makes.
    """
    workspace = await workspace_service.get_workspace(session, actor, workspace_id)
    return await workspace_service.apply_cluster_state(session, workspace, get_gateway())


@router.put("/{workspace_id}/quota-profile", response_model=WorkspaceDetail)
async def set_workspace_quota_profile(
    session: DbSession,
    actor: CurrentUser,
    _: Annotated[PermissionCheck, CanAssignQuota],
    workspace_id: int,
    payload: QuotaProfileAssign,
) -> WorkspaceDetail:
    """Attach a quota template, or clear it to follow the default.

    The namespace's ceilings are re-applied straight away. They are *derived* from
    the template, so a change that only took effect at the next reconcile would look
    like it had been ignored — and the user would have no way to tell the difference
    from a failure.
    """
    workspace = await workspace_service.get_workspace(session, actor, workspace_id)
    profile = await quota_profile_service.profile_for_assignment(
        session, payload.profile_id, QuotaProfileKind.WORKSPACE
    )
    workspace.quota_profile_id = profile.id if profile else None
    await session.flush()
    await workspace_service.apply_cluster_state(session, workspace, get_gateway())
    return await _detail(session, workspace)


# --- members -----------------------------------------------------------------
@router.get("/{workspace_id}/members", response_model=list[WorkspaceMemberRead])
async def list_members(
    session: DbSession,
    actor: CurrentUser,
    _: Annotated[PermissionCheck, CanRead],
    workspace_id: int,
) -> list[WorkspaceMemberRead]:
    workspace = await workspace_service.get_workspace(session, actor, workspace_id)
    members = await workspace_service.list_members(session, workspace)
    return [WorkspaceMemberRead.from_link(link) for link in members]


@router.post(
    "/{workspace_id}/members",
    response_model=WorkspaceMemberRead,
    status_code=status.HTTP_201_CREATED,
)
async def assign_member(
    session: DbSession,
    actor: CurrentUser,
    _: Annotated[PermissionCheck, CanAssign],
    workspace_id: int,
    payload: WorkspaceMemberAssign,
) -> WorkspaceMemberRead:
    """Assign a user.

    A user may belong to exactly one workspace, so assigning someone who already
    has one requires ``moveFromExisting: true`` — silently relocating their
    files and running workloads would be a data-loss bug.
    """
    workspace = await workspace_service.get_workspace(session, actor, workspace_id)
    link = await workspace_service.assign_member(session, actor, workspace, payload)
    return WorkspaceMemberRead.from_link(link)


@router.delete("/{workspace_id}/members/{user_id}", response_model=ApiMessage)
async def remove_member(
    session: DbSession,
    actor: CurrentUser,
    _: Annotated[PermissionCheck, CanAssign],
    workspace_id: int,
    user_id: int,
) -> ApiMessage:
    workspace = await workspace_service.get_workspace(session, actor, workspace_id)
    await workspace_service.remove_member(session, actor, workspace, user_id)
    return ApiMessage(message="成员已移除")


# --- resource grants ---------------------------------------------------------
@router.get("/{workspace_id}/resources", response_model=list[WorkspaceResourceGrantRead])
async def list_resource_grants(
    session: DbSession,
    actor: CurrentUser,
    _: Annotated[PermissionCheck, CanRead],
    workspace_id: int,
) -> list[WorkspaceResourceGrantRead]:
    workspace = await workspace_service.get_workspace(session, actor, workspace_id)
    grants = await workspace_service.list_grants(session, workspace)
    return [WorkspaceResourceGrantRead.from_grant(grant) for grant in grants]


@router.post(
    "/{workspace_id}/resources",
    response_model=WorkspaceResourceGrantRead,
    status_code=status.HTTP_201_CREATED,
)
async def grant_resource_group(
    session: DbSession,
    actor: CurrentUser,
    _: Annotated[PermissionCheck, CanGrant],
    workspace_id: int,
    payload: WorkspaceResourceGrantCreate,
) -> WorkspaceResourceGrantRead:
    """Grant a resource group, then push the recalculated quota to the cluster."""
    workspace = await workspace_service.get_workspace(session, actor, workspace_id)
    grant = await workspace_service.grant_resource_group(
        session, actor, workspace, payload.resource_group_id
    )
    await workspace_service.apply_cluster_state(session, workspace, get_gateway())
    return WorkspaceResourceGrantRead.from_grant(grant)


@router.delete("/{workspace_id}/resources/{resource_group_id}", response_model=ApiMessage)
async def revoke_resource_group(
    session: DbSession,
    actor: CurrentUser,
    _: Annotated[PermissionCheck, CanGrant],
    workspace_id: int,
    resource_group_id: int,
) -> ApiMessage:
    """Revoke a grant.

    The quota shrinks immediately. Running workloads are deliberately left
    alone — Kubernetes will refuse new pods but will not kill what is running,
    which is the behaviour an administrator wants when draining a pool.
    """
    workspace = await workspace_service.get_workspace(session, actor, workspace_id)
    await workspace_service.revoke_resource_group(session, actor, workspace, resource_group_id)
    await workspace_service.apply_cluster_state(session, workspace, get_gateway())
    return ApiMessage(message="资源组授权已撤销")


async def _detail(session: DbSession, workspace: Workspace) -> WorkspaceDetail:
    """Assemble the detail payload with its members and grants."""
    summary = workspace_service.to_summary(workspace)
    members = await workspace_service.list_members(session, workspace)
    grants = await workspace_service.list_grants(session, workspace)
    return WorkspaceDetail(
        **summary.model_dump(),
        members=[WorkspaceMemberRead.from_link(link) for link in members],
        resource_grants=[WorkspaceResourceGrantRead.from_grant(grant) for grant in grants],
    )
