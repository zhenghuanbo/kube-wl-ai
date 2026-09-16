"""Storage endpoints.

Reading needs ``storage:read``; what each caller sees is decided by the
visibility rule in the service. Creating and deleting a personal volume needs
only ``storage:create`` — a user's own scratch space is theirs to manage — while
publishing a group share or a dataset needs ``storage:manage``, because those
are claims about what the whole lab may rely on.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.core.deps import AppSettings, CurrentUser, DbSession, require
from app.k8s import get_gateway
from app.models.enums import StorageKind, StorageStatus, WorkloadKind
from app.schemas.application import WorkloadEventRead
from app.schemas.common import Page, PageMeta
from app.schemas.storage import (
    StorageCreate,
    StorageDetail,
    StorageResize,
    StorageSummary,
    StorageUpdate,
    StorageUsage,
)
from app.services import storages as storage_service
from app.services import workload_events as event_service

router = APIRouter(tags=["storage"])

CanRead = Depends(require("storage:read"))
CanCreate = Depends(require("storage:create"))
CanDelete = Depends(require("storage:delete"))


def _pages(total: int, page_size: int) -> int:
    return (total + page_size - 1) // page_size if page_size else 0


async def _detail(session: DbSession, actor: CurrentUser, storage: object) -> StorageDetail:
    attachments = await storage_service.list_attachments(session, storage.id)  # type: ignore[attr-defined]
    detail = StorageDetail(**StorageSummary.from_storage(storage).model_dump())
    detail.pvc_name = storage.pvc_name  # type: ignore[attr-defined]
    detail.k8s_namespace = storage.k8s_namespace  # type: ignore[attr-defined]
    detail.size_bytes = storage.size_bytes  # type: ignore[attr-defined]
    detail.checked_at = storage.checked_at  # type: ignore[attr-defined]
    detail.attached_instance_ids = [row.instance_id for row in attachments]
    return detail


@router.get("/storages", response_model=Page[StorageSummary])
async def list_storages(
    session: DbSession,
    actor: CurrentUser,
    _: Annotated[None, CanRead],
    workspace_id: Annotated[int | None, Query(alias="workspaceId")] = None,
    kind: Annotated[StorageKind | None, Query()] = None,
    status: Annotated[StorageStatus | None, Query()] = None,
    search: Annotated[str | None, Query(max_length=128)] = None,
    owned_only: Annotated[bool, Query(alias="ownedOnly")] = False,
    include_inactive: Annotated[bool, Query(alias="includeInactive")] = False,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=200, alias="pageSize")] = 20,
) -> Page[StorageSummary]:
    result = await storage_service.list_storages(
        session,
        actor,
        workspace_id=workspace_id,
        kind=kind,
        status=status,
        search=search,
        owned_only=owned_only,
        include_inactive=include_inactive,
        page=page,
        page_size=page_size,
    )
    return Page(
        items=[StorageSummary.from_storage(row) for row in result.items],
        meta=PageMeta(
            page=page,
            page_size=page_size,
            total=result.total,
            pages=_pages(result.total, page_size),
        ),
    )


@router.get("/storages/usage", response_model=StorageUsage)
async def storage_usage(
    session: DbSession, actor: CurrentUser, _: Annotated[None, CanRead]
) -> StorageUsage:
    totals = await storage_service.usage_by_owner(session, actor)
    return StorageUsage(count=totals["count"], capacity_gb=totals["capacityGb"])


@router.get("/storages/attachable", response_model=list[StorageSummary])
async def list_attachable(
    session: DbSession,
    actor: CurrentUser,
    _: Annotated[None, CanRead],
    workspace_id: Annotated[int, Query(alias="workspaceId")],
) -> list[StorageSummary]:
    """Volumes this caller could mount, for the instance create dialog."""
    rows = await storage_service.attachable_storages(session, actor, workspace_id)
    return [StorageSummary.from_storage(row) for row in rows]


@router.get("/storages/{storage_id}", response_model=StorageDetail)
async def get_storage(
    storage_id: int,
    session: DbSession,
    actor: CurrentUser,
    _: Annotated[None, CanRead],
) -> StorageDetail:
    storage = await storage_service.get_storage(session, actor, storage_id)
    return await _detail(session, actor, storage)


@router.post("/storages", response_model=StorageDetail, status_code=201)
async def create_storage(
    payload: StorageCreate,
    session: DbSession,
    actor: CurrentUser,
    settings: AppSettings,
    _: Annotated[None, CanCreate],
) -> StorageDetail:
    storage = await storage_service.create_storage(
        session,
        actor,
        get_gateway(settings),
        settings,
        name=payload.name,
        display_name=payload.display_name,
        description=payload.description,
        kind=payload.kind,
        workspace_id=payload.workspace_id,
        owner_user_id=payload.owner_user_id,
        capacity_gb=payload.capacity_gb,
        access_mode=payload.access_mode,
        storage_class=payload.storage_class,
        is_read_only=payload.is_read_only,
        source=payload.source,
        group_id=payload.group_id,
    )
    await session.commit()
    return await _detail(session, actor, storage)


@router.patch("/storages/{storage_id}", response_model=StorageDetail)
async def update_storage(
    storage_id: int,
    payload: StorageUpdate,
    session: DbSession,
    actor: CurrentUser,
    _: Annotated[None, CanCreate],
) -> StorageDetail:
    storage = await storage_service.get_storage(session, actor, storage_id)
    await storage_service.update_storage(
        session,
        actor,
        storage,
        display_name=payload.display_name,
        description=payload.description,
        is_active=payload.is_active,
        is_read_only=payload.is_read_only,
        source=payload.source,
    )
    await session.commit()
    return await _detail(session, actor, storage)


@router.post("/storages/{storage_id}/resize", response_model=StorageDetail)
async def resize_storage(
    storage_id: int,
    payload: StorageResize,
    session: DbSession,
    actor: CurrentUser,
    settings: AppSettings,
    _: Annotated[None, CanCreate],
) -> StorageDetail:
    storage = await storage_service.get_storage(session, actor, storage_id)
    # The service is async and dispatches its own blocking cluster calls through
    # `run_gateway`; wrapping the service itself would hand back a coroutine.
    await storage_service.resize_storage(
        session, actor, storage, get_gateway(settings), capacity_gb=payload.capacity_gb
    )
    await session.commit()
    return await _detail(session, actor, storage)


@router.post("/storages/{storage_id}/reconcile", response_model=StorageDetail)
async def reconcile_storage(
    storage_id: int,
    session: DbSession,
    actor: CurrentUser,
    settings: AppSettings,
    _: Annotated[None, CanRead],
) -> StorageDetail:
    storage = await storage_service.get_storage(session, actor, storage_id)
    await storage_service.reconcile_storage(session, storage, get_gateway(settings))
    await session.commit()
    return await _detail(session, actor, storage)


@router.delete("/storages/{storage_id}", response_model=StorageDetail)
async def delete_storage(
    storage_id: int,
    session: DbSession,
    actor: CurrentUser,
    settings: AppSettings,
    _: Annotated[None, CanDelete],
) -> StorageDetail:
    """Retire a volume and delete its claim.

    Returns the row rather than 204: the record is kept as history, and the
    caller needs its new status to redraw without a second request.
    """
    storage = await storage_service.get_storage(session, actor, storage_id)
    await storage_service.delete_storage(session, actor, storage, get_gateway(settings))
    await session.commit()
    return await _detail(session, actor, storage)


@router.get("/storages/{storage_id}/events", response_model=list[WorkloadEventRead])
async def list_storage_events(
    storage_id: int,
    session: DbSession,
    actor: CurrentUser,
    _: Annotated[None, CanRead],
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[WorkloadEventRead]:
    # Resolve first: without this a caller could read the timeline of a volume
    # they cannot see.
    await storage_service.get_storage(session, actor, storage_id)
    events = await event_service.list_events(
        session, kind=WorkloadKind.STORAGE, workload_id=storage_id, limit=limit
    )
    return [WorkloadEventRead.from_event(event) for event in events]
