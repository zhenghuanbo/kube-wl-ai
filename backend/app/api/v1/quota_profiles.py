"""Compute quota-profile endpoints.

A profile is a named set of compute ceilings that an administrator attaches to a
user or a workspace. This module is the *catalogue* — creating, editing and retiring
profiles. Attaching one is a change to a user or to a workspace, and lives with
those resources.

Writing here is global-administrator work: these rows decide what every tenant is
allowed, which makes them the one part of the resource catalogue a group
administrator may not edit. They may read it, so that a workspace's ceiling is
explainable to the person asking about it.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from app.auth.policy import PermissionCheck
from app.core.config import Settings, get_settings
from app.core.deps import CurrentUser, DbSession, require
from app.models.enums import QuotaProfileKind
from app.schemas.base import ApiMessage
from app.schemas.quota_profile import (
    QuotaProfileCreate,
    QuotaProfileRead,
    QuotaProfileUpdate,
)
from app.services import quota_profiles as quota_profile_service

router = APIRouter(prefix="/quota-profiles", tags=["quota"])

CanRead = Depends(require("quota_profile:read"))
CanCreate = Depends(require("quota_profile:create"))
CanUpdate = Depends(require("quota_profile:update"))
CanDelete = Depends(require("quota_profile:delete"))


def _read(profile: object) -> QuotaProfileRead:
    return QuotaProfileRead.model_validate(profile)


@router.get("", response_model=list[QuotaProfileRead])
async def list_quota_profiles(
    session: DbSession,
    actor: CurrentUser,
    _: Annotated[PermissionCheck, CanRead],
    kind: Annotated[QuotaProfileKind | None, Query()] = None,
    is_active: Annotated[bool | None, Query(alias="isActive")] = None,
) -> list[QuotaProfileRead]:
    profiles = await quota_profile_service.list_profiles(session, kind=kind, is_active=is_active)
    return [_read(profile) for profile in profiles]


@router.post("", response_model=QuotaProfileRead, status_code=status.HTTP_201_CREATED)
async def create_quota_profile(
    session: DbSession,
    actor: CurrentUser,
    _: Annotated[PermissionCheck, CanCreate],
    payload: QuotaProfileCreate,
    settings: Annotated[Settings, Depends(get_settings)],
) -> QuotaProfileRead:
    profile = await quota_profile_service.create_profile(session, actor, payload, settings)
    return _read(profile)


@router.get("/{profile_id}", response_model=QuotaProfileRead)
async def get_quota_profile(
    session: DbSession,
    actor: CurrentUser,
    _: Annotated[PermissionCheck, CanRead],
    profile_id: int,
) -> QuotaProfileRead:
    return _read(await quota_profile_service.get_profile(session, profile_id))


@router.patch("/{profile_id}", response_model=QuotaProfileRead)
async def update_quota_profile(
    session: DbSession,
    actor: CurrentUser,
    _: Annotated[PermissionCheck, CanUpdate],
    profile_id: int,
    payload: QuotaProfileUpdate,
) -> QuotaProfileRead:
    profile = await quota_profile_service.get_profile(session, profile_id)
    profile = await quota_profile_service.update_profile(session, actor, profile, payload)
    return _read(profile)


@router.delete("/{profile_id}", response_model=ApiMessage)
async def delete_quota_profile(
    session: DbSession,
    actor: CurrentUser,
    _: Annotated[PermissionCheck, CanDelete],
    profile_id: int,
) -> ApiMessage:
    profile = await quota_profile_service.get_profile(session, profile_id)
    name = profile.display_name or profile.name
    await quota_profile_service.delete_profile(session, actor, profile)
    return ApiMessage(message=f"配额模板 {name} 已删除")
