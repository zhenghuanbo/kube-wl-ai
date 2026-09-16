"""User management endpoints."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request

from app.auth import policy
from app.core.audit import record_audit
from app.core.deps import CurrentUser, DbSession, require
from app.core.errors import PermissionDeniedError
from app.models.enums import UserSource
from app.schemas.auth import GroupMembership
from app.schemas.base import ApiMessage
from app.schemas.common import Page, PageMeta
from app.schemas.user import (
    PasswordReset,
    UserCreate,
    UserDetail,
    UserGroupsUpdate,
    UserRoleGrant,
    UserSelfUpdate,
    UserStatusUpdate,
    UserSummary,
    UserUpdate,
)
from app.services import users as user_service

router = APIRouter(prefix="/users", tags=["users"])


@router.get(
    "",
    response_model=Page[UserSummary],
    summary="List users visible to the caller",
    dependencies=[Depends(require("user:read"))],
)
async def list_users(
    session: DbSession,
    actor: CurrentUser,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=200, alias="pageSize")] = 20,
    search: Annotated[str | None, Query(max_length=150)] = None,
    role: Annotated[str | None, Query(max_length=64)] = None,
    group_id: Annotated[int | None, Query(alias="groupId")] = None,
    is_active: Annotated[bool | None, Query(alias="isActive")] = None,
    source: UserSource | None = None,
) -> Page[UserSummary]:
    """Group admins see only their own group's members; global admins see all."""
    result = await user_service.list_users(
        session,
        actor,
        page=page,
        page_size=page_size,
        search=search,
        role_code=role,
        group_id=group_id,
        is_active=is_active,
        source=source,
    )
    pages = (result.total + page_size - 1) // page_size if page_size else 0
    return Page[UserSummary](
        items=[UserSummary.from_user(user) for user in result.items],
        meta=PageMeta(page=page, page_size=page_size, total=result.total, pages=pages),
    )


# --- self-service ------------------------------------------------------------
# Declared before /{user_id} so "me" is not parsed as an id.
@router.get("/me", response_model=UserDetail, summary="The caller's own profile")
async def read_me(session: DbSession, user: CurrentUser) -> UserDetail:
    return UserDetail.from_user(user)


@router.patch("/me", response_model=UserDetail, summary="Update the caller's own profile")
async def update_me(
    payload: UserSelfUpdate,
    request: Request,
    session: DbSession,
    user: CurrentUser,
) -> UserDetail:
    """Users may edit their display details, never their own roles or quota."""
    updated = await user_service.update_user(
        session, user, user, UserUpdate(**payload.model_dump(exclude_unset=True))
    )
    record_audit(
        session,
        action="user:update_self",
        actor=user,
        object_type="user",
        object_id=user.id,
        request=request,
    )
    return UserDetail.from_user(updated)


# --- collection --------------------------------------------------------------
@router.post(
    "",
    response_model=UserDetail,
    status_code=201,
    summary="Create a local (break-glass) account",
    dependencies=[Depends(require("user:create"))],
)
async def create_user(
    payload: UserCreate, request: Request, session: DbSession, actor: CurrentUser
) -> UserDetail:
    user = await user_service.create_user(session, actor, payload)
    record_audit(
        session,
        action="user:create",
        actor=actor,
        object_type="user",
        object_id=user.id,
        detail={"username": user.username, "roles": payload.role_codes},
        request=request,
    )
    return UserDetail.from_user(user)


# --- single user -------------------------------------------------------------
@router.get(
    "/{user_id}",
    response_model=UserDetail,
    summary="Read one user",
    dependencies=[Depends(require("user:read"))],
)
async def read_user(user_id: int, session: DbSession, actor: CurrentUser) -> UserDetail:
    return UserDetail.from_user(await user_service.get_user(session, actor, user_id))


@router.patch(
    "/{user_id}",
    response_model=UserDetail,
    summary="Update a user",
    dependencies=[Depends(require("user:update"))],
)
async def update_user(
    user_id: int,
    payload: UserUpdate,
    request: Request,
    session: DbSession,
    actor: CurrentUser,
) -> UserDetail:
    user = await user_service.get_user(session, actor, user_id)
    updated = await user_service.update_user(session, actor, user, payload)
    record_audit(
        session,
        action="user:update",
        actor=actor,
        object_type="user",
        object_id=user.id,
        detail={"fields": sorted(payload.model_dump(exclude_unset=True))},
        request=request,
    )
    return UserDetail.from_user(updated)


@router.delete(
    "/{user_id}",
    response_model=ApiMessage,
    summary="Delete a user",
    dependencies=[Depends(require("user:delete"))],
)
async def delete_user(
    user_id: int, request: Request, session: DbSession, actor: CurrentUser
) -> ApiMessage:
    user = await user_service.get_user(session, actor, user_id)
    username = user.username
    await user_service.delete_user(session, actor, user)
    record_audit(
        session,
        action="user:delete",
        actor=actor,
        object_type="user",
        object_id=user_id,
        detail={"username": username},
        request=request,
    )
    return ApiMessage(message=f"用户 {username} 已删除")


@router.post(
    "/{user_id}/activate",
    response_model=UserDetail,
    summary="Enable an account",
    dependencies=[Depends(require("user:update"))],
)
async def activate_user(
    user_id: int, request: Request, session: DbSession, actor: CurrentUser
) -> UserDetail:
    user = await user_service.get_user(session, actor, user_id)
    updated = await user_service.set_active(session, actor, user, is_active=True)
    record_audit(
        session,
        action="user:activate",
        actor=actor,
        object_type="user",
        object_id=user_id,
        request=request,
    )
    return UserDetail.from_user(updated)


@router.post(
    "/{user_id}/deactivate",
    response_model=UserDetail,
    summary="Disable an account",
    dependencies=[Depends(require("user:update"))],
)
async def deactivate_user(
    user_id: int, request: Request, session: DbSession, actor: CurrentUser
) -> UserDetail:
    user = await user_service.get_user(session, actor, user_id)
    updated = await user_service.set_active(session, actor, user, is_active=False)
    record_audit(
        session,
        action="user:deactivate",
        actor=actor,
        object_type="user",
        object_id=user_id,
        request=request,
    )
    return UserDetail.from_user(updated)


@router.patch(
    "/{user_id}/status",
    response_model=UserDetail,
    summary="Enable or disable an account in one call",
    dependencies=[Depends(require("user:update"))],
)
async def set_user_status(
    user_id: int,
    payload: UserStatusUpdate,
    request: Request,
    session: DbSession,
    actor: CurrentUser,
) -> UserDetail:
    user = await user_service.get_user(session, actor, user_id)
    updated = await user_service.set_active(session, actor, user, is_active=payload.is_active)
    record_audit(
        session,
        action="user:set_status",
        actor=actor,
        object_type="user",
        object_id=user_id,
        detail={"is_active": payload.is_active},
        request=request,
    )
    return UserDetail.from_user(updated)


@router.post(
    "/{user_id}/password",
    response_model=ApiMessage,
    summary="Reset a local account's password",
    dependencies=[Depends(require("user:update"))],
)
async def reset_password(
    user_id: int,
    payload: PasswordReset,
    request: Request,
    session: DbSession,
    actor: CurrentUser,
) -> ApiMessage:
    user = await user_service.get_user(session, actor, user_id)
    await user_service.reset_password(session, actor, user, payload.password)
    record_audit(
        session,
        action="user:reset_password",
        actor=actor,
        object_type="user",
        object_id=user_id,
        request=request,
    )
    return ApiMessage(message="密码已重置")


# --- memberships -------------------------------------------------------------
@router.get(
    "/{user_id}/groups",
    response_model=list[GroupMembership],
    summary="The user's group memberships",
    dependencies=[Depends(require("user:read"))],
)
async def read_user_groups(
    user_id: int, session: DbSession, actor: CurrentUser
) -> list[GroupMembership]:
    user = await user_service.get_user(session, actor, user_id)
    return [GroupMembership.from_link(link) for link in user.group_links]


@router.put(
    "/{user_id}/groups",
    response_model=UserDetail,
    summary="Replace the user's group memberships",
    dependencies=[Depends(require("user:update"))],
)
async def set_user_groups(
    user_id: int,
    payload: UserGroupsUpdate,
    request: Request,
    session: DbSession,
    actor: CurrentUser,
) -> UserDetail:
    user = await user_service.get_user(session, actor, user_id)
    updated = await user_service.set_groups(
        session,
        actor,
        user,
        group_ids=payload.group_ids,
        primary_group_id=payload.primary_group_id,
    )
    record_audit(
        session,
        action="user:set_groups",
        actor=actor,
        object_type="user",
        object_id=user_id,
        detail={"group_ids": payload.group_ids, "primary_group_id": payload.primary_group_id},
        request=request,
    )
    return UserDetail.from_user(updated)


# --- roles -------------------------------------------------------------------
@router.post(
    "/{user_id}/roles",
    response_model=UserDetail,
    summary="Grant a role (globally or within one group)",
    dependencies=[Depends(require("user:assign_role"))],
)
async def grant_role(
    user_id: int,
    payload: UserRoleGrant,
    request: Request,
    session: DbSession,
    actor: CurrentUser,
) -> UserDetail:
    user = await user_service.get_user(session, actor, user_id)
    updated = await user_service.grant_role(session, actor, user, payload)
    record_audit(
        session,
        action="user:grant_role",
        actor=actor,
        object_type="user",
        object_id=user_id,
        detail={
            "role": payload.role_code,
            "scope": payload.scope.value,
            "group_id": payload.group_id,
        },
        request=request,
    )
    return UserDetail.from_user(updated)


@router.delete(
    "/{user_id}/roles/{role_code}",
    response_model=UserDetail,
    summary="Revoke a role assignment",
    dependencies=[Depends(require("user:assign_role"))],
)
async def revoke_role(
    user_id: int,
    role_code: str,
    request: Request,
    session: DbSession,
    actor: CurrentUser,
    group_id: Annotated[int | None, Query(alias="groupId")] = None,
) -> UserDetail:
    user = await user_service.get_user(session, actor, user_id)
    updated = await user_service.revoke_role(
        session, actor, user, role_code=role_code, group_id=group_id
    )
    record_audit(
        session,
        action="user:revoke_role",
        actor=actor,
        object_type="user",
        object_id=user_id,
        detail={"role": role_code, "group_id": group_id},
        request=request,
    )
    return UserDetail.from_user(updated)


@router.get(
    "/{user_id}/permissions",
    response_model=list[str],
    summary="The permissions a user effectively holds",
    dependencies=[Depends(require("user:read"))],
)
async def read_user_permissions(user_id: int, session: DbSession, actor: CurrentUser) -> list[str]:
    """Advisory, for the admin UI. Never a substitute for the server's checks."""
    user = await user_service.get_user(session, actor, user_id)
    if not policy.is_global_admin(actor) and user.id != actor.id:
        raise PermissionDeniedError("仅管理员可查看他人权限清单")
    return sorted(policy.effective_permissions(user))
