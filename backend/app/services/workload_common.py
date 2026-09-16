"""Helpers shared by every workload that lives in a workspace namespace.

Instances and tasks differ in what they run and how long they last, but the
questions asked *before* anything is created are identical: may this person act
in this workspace, is the pool actually granted to it, is the spec part of that
pool, and whose name goes on the row. Answering them in two places would let the
two paths drift, and the drift would be a security bug rather than a cosmetic
one.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import policy
from app.core.errors import BadRequestError, NotFoundError
from app.models.resource import ResourceGroup, ResourceGroupSpec, ResourceSpec
from app.models.user import User
from app.models.workspace import Workspace, WorkspaceMember, WorkspaceResourceGrant


@dataclass(slots=True)
class Page[T]:
    items: Sequence[T]
    total: int


async def workspace_for_actor(
    session: AsyncSession, actor: User, workspace_id: int | None
) -> Workspace:
    """Resolve the target workspace and prove the caller may act in it.

    Omitting ``workspace_id`` means "my own workspace", which is what a normal
    user always wants; an administrator acting on behalf of somebody else passes
    it explicitly.
    """
    if workspace_id is None:
        workspace = await session.scalar(
            select(Workspace)
            .join(WorkspaceMember, WorkspaceMember.workspace_id == Workspace.id)
            .where(WorkspaceMember.user_id == actor.id, Workspace.is_active.is_(True))
        )
        if workspace is None:
            raise BadRequestError("你尚未被分配工作空间，请先联系管理员", code="no_workspace")
        return workspace

    workspace = await session.get(Workspace, workspace_id)
    if workspace is None:
        raise NotFoundError("工作空间不存在", code="workspace_not_found")
    policy.ensure_object_access(actor, workspace, group_id=workspace.group_id)
    return workspace


async def resolve_pool_and_spec(
    session: AsyncSession, workspace: Workspace, pool_id: int, spec_id: int
) -> tuple[ResourceGroup, ResourceSpec]:
    """Check that the workspace holds this pool, and that the pool offers this spec."""
    pool = await session.get(ResourceGroup, pool_id)
    if pool is None:
        raise NotFoundError("资源组不存在", code="resource_group_not_found")
    if not pool.is_active:
        raise BadRequestError("该资源组已停用", code="resource_group_inactive")

    granted = await session.scalar(
        select(WorkspaceResourceGrant).where(
            WorkspaceResourceGrant.workspace_id == workspace.id,
            WorkspaceResourceGrant.resource_group_id == pool_id,
            WorkspaceResourceGrant.is_active.is_(True),
        )
    )
    if granted is None:
        # Not a permission failure: the workspace simply has no claim on this
        # hardware, and saying so plainly is more useful than a generic denial.
        raise BadRequestError("该资源组未授权给此工作空间", code="resource_group_not_granted")

    spec = await session.get(ResourceSpec, spec_id)
    if spec is None:
        raise BadRequestError("资源规格不存在", code="unknown_resource_spec")
    if not spec.is_active:
        raise BadRequestError("该资源规格已停用", code="resource_spec_inactive")

    offered = await session.scalar(
        select(func.count())
        .select_from(ResourceGroupSpec)
        .where(
            ResourceGroupSpec.resource_group_id == pool_id,
            ResourceGroupSpec.resource_spec_id == spec_id,
        )
    )
    if not offered:
        raise BadRequestError("该规格不属于所选资源组", code="resource_spec_not_in_group")
    return pool, spec


async def resolve_owner(
    session: AsyncSession, actor: User, workspace: Workspace, owner_user_id: int | None
) -> User:
    """Decide whose name goes on the row, and prove they belong to the workspace."""
    if owner_user_id is None or owner_user_id == actor.id:
        membership = await session.scalar(
            select(WorkspaceMember).where(
                WorkspaceMember.workspace_id == workspace.id,
                WorkspaceMember.user_id == actor.id,
            )
        )
        if membership is None:
            # An administrator may create work in a workspace they do not belong
            # to, but then they must say who it is for.
            raise BadRequestError("你不属于此工作空间，请指定归属人", code="owner_required")
        return actor

    owner = await session.get(User, owner_user_id)
    if owner is None:
        raise BadRequestError("指定的归属用户不存在", code="unknown_user")
    membership = await session.scalar(
        select(WorkspaceMember).where(
            WorkspaceMember.workspace_id == workspace.id,
            WorkspaceMember.user_id == owner.id,
        )
    )
    if membership is None:
        raise BadRequestError("该用户不属于此工作空间", code="owner_not_in_workspace")
    return owner
