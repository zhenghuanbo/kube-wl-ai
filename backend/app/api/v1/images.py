"""Image catalogue endpoints.

Reading needs ``image:read``. Registering a private image needs only
``image:create`` — cataloguing something for yourself is not a claim about what
anyone else should run — while publishing to a group or the whole installation
needs ``image:share``, because that *is* such a claim.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.core.deps import AppSettings, CurrentUser, DbSession, require
from app.k8s import run_gateway
from app.models.enums import ApplicationCategory, ImageKind, ImageVisibility, WorkloadKind
from app.registries import get_registry
from app.schemas.application import WorkloadEventRead
from app.schemas.common import Page, PageMeta
from app.schemas.image import (
    ImageDetail,
    ImageRegister,
    ImageSummary,
    ImageSyncRequest,
    ImageSyncResult,
    ImageUpdate,
    ImageUsage,
    RegistryStatusRead,
)
from app.services import images as image_service
from app.services import workload_events as event_service

router = APIRouter(tags=["images"])

CanRead = Depends(require("image:read"))
CanCreate = Depends(require("image:create"))
CanDelete = Depends(require("image:delete"))
#: The catalogue is a cache of the registry, so refreshing it is an
#: administrative act rather than something every user may trigger.
CanSync = Depends(require("image:share"))


def _pages(total: int, page_size: int) -> int:
    return (total + page_size - 1) // page_size if page_size else 0


async def _detail(session: DbSession, actor: CurrentUser, image: object) -> ImageDetail:
    in_use = await image_service.usage_count(session, image)  # type: ignore[arg-type]
    detail = ImageDetail(**ImageSummary.from_image(image).model_dump())  # type: ignore[arg-type]
    # Report the count we just computed rather than the denormalised column: the
    # column is refreshed by a sync, so on a detail view it can be arbitrarily
    # stale, and this is the number the caller is about to act on.
    detail.usage_count = in_use
    if image.is_pinned:  # type: ignore[attr-defined]
        detail.can_delete = False
        detail.blocked_reason = "管理员保留项"
    elif in_use:
        detail.can_delete = False
        detail.blocked_reason = f"仍被 {in_use} 个工作负载引用"
    return detail


@router.get("/images", response_model=Page[ImageSummary])
async def list_images(
    session: DbSession,
    actor: CurrentUser,
    _: Annotated[None, CanRead],
    category: Annotated[ApplicationCategory | None, Query()] = None,
    kind: Annotated[ImageKind | None, Query()] = None,
    visibility: Annotated[ImageVisibility | None, Query()] = None,
    framework: Annotated[str | None, Query(max_length=64)] = None,
    search: Annotated[str | None, Query(max_length=128)] = None,
    owned_only: Annotated[bool, Query(alias="ownedOnly")] = False,
    include_inactive: Annotated[bool, Query(alias="includeInactive")] = False,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=200, alias="pageSize")] = 20,
) -> Page[ImageSummary]:
    result = await image_service.list_images(
        session,
        actor,
        category=category,
        kind=kind,
        visibility=visibility,
        framework=framework,
        search=search,
        owned_only=owned_only,
        include_inactive=include_inactive,
        page=page,
        page_size=page_size,
    )
    return Page(
        items=[ImageSummary.from_image(row) for row in result.items],
        meta=PageMeta(
            page=page,
            page_size=page_size,
            total=result.total,
            pages=_pages(result.total, page_size),
        ),
    )


@router.get("/images/usage", response_model=ImageUsage)
async def image_usage(
    session: DbSession, actor: CurrentUser, _: Annotated[None, CanRead]
) -> ImageUsage:
    rows = await image_service.list_images(session, actor, page=1, page_size=200)
    return ImageUsage(
        count=rows.total,
        total_bytes=sum(row.size_bytes or 0 for row in rows.items),
        in_use=sum(1 for row in rows.items if row.usage_count > 0),
    )


@router.get("/images/registry", response_model=RegistryStatusRead)
async def registry_status(settings: AppSettings, _: Annotated[None, CanRead]) -> RegistryStatusRead:
    """Whether the registry is reachable, for the page's health banner."""
    client = get_registry(settings)
    payload = await run_gateway(image_service.registry_status, client)
    return RegistryStatusRead(**payload)


@router.get("/images/{image_id}", response_model=ImageDetail)
async def get_image(
    image_id: int,
    session: DbSession,
    actor: CurrentUser,
    _: Annotated[None, CanRead],
) -> ImageDetail:
    image = await image_service.get_image(session, actor, image_id)
    return await _detail(session, actor, image)


@router.post("/images", response_model=ImageDetail, status_code=201)
async def register_image(
    payload: ImageRegister,
    session: DbSession,
    actor: CurrentUser,
    settings: AppSettings,
    _: Annotated[None, CanCreate],
) -> ImageDetail:
    client = get_registry(settings)
    image = await image_service.register_image(
        session,
        actor,
        client,
        settings,
        reference=payload.reference,
        display_name=payload.display_name,
        description=payload.description,
        category=payload.category,
        kind=payload.kind,
        visibility=payload.visibility,
        workspace_id=payload.workspace_id,
        group_id=payload.group_id,
        framework=payload.framework,
        framework_version=payload.framework_version,
        python_version=payload.python_version,
        cuda_version=payload.cuda_version,
        os_version=payload.os_version,
        is_pinned=payload.is_pinned,
    )
    await session.commit()
    return await _detail(session, actor, image)


@router.patch("/images/{image_id}", response_model=ImageDetail)
async def update_image(
    image_id: int,
    payload: ImageUpdate,
    session: DbSession,
    actor: CurrentUser,
    _: Annotated[None, CanCreate],
) -> ImageDetail:
    image = await image_service.get_image(session, actor, image_id)
    await image_service.update_image(
        session,
        actor,
        image,
        display_name=payload.display_name,
        description=payload.description,
        category=payload.category,
        framework=payload.framework,
        framework_version=payload.framework_version,
        python_version=payload.python_version,
        cuda_version=payload.cuda_version,
        os_version=payload.os_version,
        is_active=payload.is_active,
        is_pinned=payload.is_pinned,
        visibility=payload.visibility,
        kind=payload.kind,
    )
    await session.commit()
    return await _detail(session, actor, image)


@router.post("/images/sync", response_model=ImageSyncResult)
async def sync_images(
    payload: ImageSyncRequest,
    session: DbSession,
    actor: CurrentUser,
    settings: AppSettings,
    _: Annotated[None, CanSync],
) -> ImageSyncResult:
    """Refresh the catalogue from the registry.

    Deliberately not a ``GET``: it writes, and a request that mutates state must
    not be reachable by an image tag or a prefetch.
    """
    client = get_registry(settings)
    result = await image_service.sync_images(
        session, actor, client, settings, projects=payload.projects or None
    )
    await session.commit()
    return ImageSyncResult(
        created=result.created,
        updated=result.updated,
        unchanged=result.unchanged,
        removed=result.removed,
        failed=result.failed,
        errors=result.errors,
        total=result.total,
    )


@router.delete("/images/{image_id}", response_model=ImageDetail)
async def delete_image(
    image_id: int,
    session: DbSession,
    actor: CurrentUser,
    settings: AppSettings,
    _: Annotated[None, CanDelete],
) -> ImageDetail:
    """Remove an entry from the catalogue.

    Returns the row rather than 204: it is retired rather than erased, and the
    caller needs its new state to redraw without a second request.
    """
    image = await image_service.get_image(session, actor, image_id)
    await image_service.delete_image(session, actor, image, get_registry(settings))
    await session.commit()
    return await _detail(session, actor, image)


@router.get("/images/{image_id}/events", response_model=list[WorkloadEventRead])
async def list_image_events(
    image_id: int,
    session: DbSession,
    actor: CurrentUser,
    _: Annotated[None, CanRead],
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[WorkloadEventRead]:
    # Resolve first: without this a caller could read the history of an image
    # they cannot see.
    await image_service.get_image(session, actor, image_id)
    events = await event_service.list_events(
        session, kind=WorkloadKind.IMAGE, workload_id=image_id, limit=limit
    )
    return [WorkloadEventRead.from_event(event) for event in events]
