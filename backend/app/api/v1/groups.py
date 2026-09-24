"""User-group management endpoints."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request

from app.auth import policy
from app.core.audit import record_audit
from app.core.config import Settings, get_settings
from app.core.deps import CurrentUser, DbSession, require
from app.models.enums import GroupKind
from app.schemas.base import ApiMessage
from app.schemas.common import Page, PageMeta
from app.schemas.group import (
    GroupCreate,
    GroupMemberAdd,
    GroupMemberRead,
    GroupMemberUpdate,
    GroupSummary,
    GroupSyncRequest,
    GroupSyncResult,
    GroupUpdate,
)
from app.services import groups as group_service

router = APIRouter(prefix="/groups", tags=["groups"])


@router.get(
    "",
    response_model=Page[GroupSummary],
    summary="List groups visible to the caller",
    dependencies=[Depends(require("group:read"))],
)
async def list_groups(
    session: DbSession,
    actor: CurrentUser,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=200, alias="pageSize")] = 20,
    search: Annotated[str | None, Query(max_length=128)] = None,
    kind: GroupKind | None = None,
    is_active: Annotated[bool | None, Query(alias="isActive")] = None,
) -> Page[GroupSummary]:
    """A plain user sees the groups they belong to; an admin sees more."""
    result = await group_service.list_groups(
        session,
        actor,
        page=page,
        page_size=page_size,
        search=search,
        kind=kind,
        is_active=is_active,
    )
    pages = (result.total + page_size - 1) // page_size if page_size else 0
    return Page[GroupSummary](
        items=[GroupSummary.from_group(group) for group in result.items],
        meta=PageMeta(page=page, page_size=page_size, total=result.total, pages=pages),
    )


@router.post(
    "",
    response_model=GroupSummary,
    status_code=201,
    summary="Create a user group",
    dependencies=[Depends(require("group:create"))],
)
async def create_group(
    payload: GroupCreate, request: Request, session: DbSession, actor: CurrentUser
) -> GroupSummary:
    group = await group_service.create_group(session, actor, payload)
    record_audit(
        session,
        action="group:create",
        actor=actor,
        object_type="group",
        object_id=group.id,
        detail={"name": group.name},
        request=request,
    )
    return GroupSummary.from_group(group)


# Declared before /{group_id} so "sync" is never parsed as an id.
@router.post(
    "/sync",
    response_model=GroupSyncResult,
    summary="Reconcile group memberships with the directory (LDAP)",
    dependencies=[Depends(require("group:update"))],
)
async def sync_groups(
    payload: GroupSyncRequest,
    request: Request,
    session: DbSession,
    actor: CurrentUser,
    settings: Annotated[Settings, Depends(get_settings)],
) -> GroupSyncResult:
    """Pull memberships from FreeIPA.

    Run with ``dryRun`` first: it reports exactly what would change without
    writing anything.
    """
    result = await group_service.sync_directory(
        session, actor, dry_run=payload.dry_run, settings=settings
    )
    if not payload.dry_run:
        record_audit(
            session,
            action="group:sync_directory",
            actor=actor,
            object_type="group",
            detail={
                "added": result.memberships_added,
                "removed": result.memberships_removed,
            },
            request=request,
        )
    return result


@router.get(
    "/{group_id}",
    response_model=GroupSummary,
    summary="Read one group",
    dependencies=[Depends(require("group:read"))],
)
async def read_group(group_id: int, session: DbSession, actor: CurrentUser) -> GroupSummary:
    policy.ensure_group_visible(actor, group_id)
    return GroupSummary.from_group(await group_service.get_group(session, actor, group_id))


@router.patch(
    "/{group_id}",
    response_model=GroupSummary,
    summary="Update a group",
    dependencies=[Depends(require("group:update"))],
)
async def update_group(
    group_id: int,
    payload: GroupUpdate,
    request: Request,
    session: DbSession,
    actor: CurrentUser,
) -> GroupSummary:
    group = await group_service.get_group(session, actor, group_id)
    updated = await group_service.update_group(session, actor, group, payload)
    record_audit(
        session,
        action="group:update",
        actor=actor,
        object_type="group",
        object_id=group_id,
        detail={"fields": sorted(payload.model_dump(exclude_unset=True))},
        request=request,
    )
    return GroupSummary.from_group(updated)


@router.delete(
    "/{group_id}",
    response_model=ApiMessage,
    summary="Delete a group",
    dependencies=[Depends(require("group:delete"))],
)
async def delete_group(
    group_id: int, request: Request, session: DbSession, actor: CurrentUser
) -> ApiMessage:
    group = await group_service.get_group(session, actor, group_id)
    name = group.name
    await group_service.delete_group(session, actor, group)
    record_audit(
        session,
        action="group:delete",
        actor=actor,
        object_type="group",
        object_id=group_id,
        detail={"name": name},
        request=request,
    )
    return ApiMessage(message=f"用户组 {name} 已删除")


# --- members -----------------------------------------------------------------
@router.get(
    "/{group_id}/members",
    response_model=Page[GroupMemberRead],
    summary="List group members",
    dependencies=[Depends(require("group:read"))],
)
async def list_members(
    group_id: int,
    session: DbSession,
    actor: CurrentUser,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=200, alias="pageSize")] = 50,
    search: Annotated[str | None, Query(max_length=150)] = None,
) -> Page[GroupMemberRead]:
    policy.ensure_group_visible(actor, group_id)
    group = await group_service.get_group(session, actor, group_id)
    result = await group_service.list_members(
        session, actor, group, page=page, page_size=page_size, search=search
    )
    pages = (result.total + page_size - 1) // page_size if page_size else 0
    return Page[GroupMemberRead](
        items=[GroupMemberRead.from_link(link) for link in result.items],
        meta=PageMeta(page=page, page_size=page_size, total=result.total, pages=pages),
    )


@router.post(
    "/{group_id}/members",
    response_model=GroupMemberRead,
    status_code=201,
    summary="Add a member",
    dependencies=[Depends(require("group:manage_member"))],
)
async def add_member(
    group_id: int,
    payload: GroupMemberAdd,
    request: Request,
    session: DbSession,
    actor: CurrentUser,
) -> GroupMemberRead:
    group = await group_service.get_group(session, actor, group_id)
    link = await group_service.add_member(session, actor, group, payload)
    record_audit(
        session,
        action="group:add_member",
        actor=actor,
        object_type="group",
        object_id=group_id,
        detail={"user_id": payload.user_id, "role_in_group": payload.role_in_group.value},
        request=request,
    )
    return GroupMemberRead.from_link(link)


@router.patch(
    "/{group_id}/members/{user_id}",
    response_model=GroupMemberRead,
    summary="Change a member's standing",
    dependencies=[Depends(require("group:manage_member"))],
)
async def update_member(
    group_id: int,
    user_id: int,
    payload: GroupMemberUpdate,
    request: Request,
    session: DbSession,
    actor: CurrentUser,
) -> GroupMemberRead:
    group = await group_service.get_group(session, actor, group_id)
    link = await group_service.update_member(session, actor, group, user_id, payload)
    record_audit(
        session,
        action="group:update_member",
        actor=actor,
        object_type="group",
        object_id=group_id,
        detail={"user_id": user_id, **payload.model_dump(exclude_unset=True)},
        request=request,
    )
    return GroupMemberRead.from_link(link)


@router.delete(
    "/{group_id}/members/{user_id}",
    response_model=ApiMessage,
    summary="Remove a member",
    dependencies=[Depends(require("group:manage_member"))],
)
async def remove_member(
    group_id: int,
    user_id: int,
    request: Request,
    session: DbSession,
    actor: CurrentUser,
) -> ApiMessage:
    group = await group_service.get_group(session, actor, group_id)
    await group_service.remove_member(session, actor, group, user_id)
    record_audit(
        session,
        action="group:remove_member",
        actor=actor,
        object_type="group",
        object_id=group_id,
        detail={"user_id": user_id},
        request=request,
    )
    return ApiMessage(message="成员已移除")
