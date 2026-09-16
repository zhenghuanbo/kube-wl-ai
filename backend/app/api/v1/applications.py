"""Application catalogue endpoints.

Reading the catalogue needs ``application:read``; what each caller actually
*sees* is decided by the visibility rule in the service, not by the permission
code. The two are different questions: the permission says you may use the
catalogue at all, visibility says which entries are yours to know about.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from app.core.deps import AppSettings, CurrentUser, DbSession, require
from app.k8s import get_gateway
from app.models.enums import ApplicationCategory, ApplicationVisibility, WorkloadKind
from app.schemas.application import (
    ApplicationCreate,
    ApplicationDetail,
    ApplicationLaunch,
    ApplicationSummary,
    ApplicationUpdate,
    WorkloadEventRead,
)
from app.schemas.common import Page, PageMeta
from app.schemas.instance import InstanceDetail
from app.services import applications as application_service
from app.services import instances as instance_service
from app.services import workload_events as event_service

router = APIRouter(tags=["applications"])

CanRead = Depends(require("application:read"))
CanCreate = Depends(require("application:create"))
CanUpdate = Depends(require("application:update"))
CanDelete = Depends(require("application:delete"))
CanLaunch = Depends(require("instance:create"))


def _pages(total: int, page_size: int) -> int:
    return (total + page_size - 1) // page_size if page_size else 0


@router.get("/applications", response_model=Page[ApplicationSummary])
async def list_applications(
    session: DbSession,
    actor: CurrentUser,
    _: Annotated[None, CanRead],
    category: Annotated[ApplicationCategory | None, Query()] = None,
    visibility: Annotated[ApplicationVisibility | None, Query()] = None,
    owned_only: Annotated[bool, Query(alias="ownedOnly")] = False,
    search: Annotated[str | None, Query(max_length=128)] = None,
    include_inactive: Annotated[bool, Query(alias="includeInactive")] = False,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=200, alias="pageSize")] = 20,
) -> Page[ApplicationSummary]:
    result = await application_service.list_applications(
        session,
        actor,
        category=category.value if category else None,
        visibility=visibility,
        owned_only=owned_only,
        search=search,
        include_inactive=include_inactive,
        limit=page_size,
        offset=(page - 1) * page_size,
    )
    return Page(
        items=[application_service.to_summary(app) for app in result.items],
        meta=PageMeta(
            total=result.total,
            page=page,
            page_size=page_size,
            pages=_pages(result.total, page_size),
        ),
    )


@router.get("/applications/{application_id}", response_model=ApplicationDetail)
async def get_application(
    session: DbSession,
    actor: CurrentUser,
    application_id: int,
    _: Annotated[None, CanRead],
) -> ApplicationDetail:
    app = await application_service.get_application(session, actor, application_id)
    return application_service.to_detail(app)


@router.post("/applications", response_model=ApplicationDetail, status_code=status.HTTP_201_CREATED)
async def create_application(
    session: DbSession,
    actor: CurrentUser,
    payload: ApplicationCreate,
    settings: AppSettings,
    _: Annotated[None, CanCreate],
) -> ApplicationDetail:
    app = await application_service.create_application(session, actor, payload, settings)
    return application_service.to_detail(app)


@router.patch("/applications/{application_id}", response_model=ApplicationDetail)
async def update_application(
    session: DbSession,
    actor: CurrentUser,
    application_id: int,
    payload: ApplicationUpdate,
    _: Annotated[None, CanUpdate],
) -> ApplicationDetail:
    app = await application_service.get_application(session, actor, application_id)
    updated = await application_service.update_application(session, actor, app, payload)
    await session.commit()
    await session.refresh(updated)
    return application_service.to_detail(updated)


@router.delete("/applications/{application_id}", response_model=ApplicationDetail)
async def delete_application(
    session: DbSession,
    actor: CurrentUser,
    application_id: int,
    _: Annotated[None, CanDelete],
) -> ApplicationDetail:
    """Retire an application from the catalogue.

    A PATCH-shaped operation behind DELETE because the row is kept: instances
    created from it reference it, and those references must stay resolvable.
    """
    app = await application_service.get_application(session, actor, application_id)
    await application_service.delete_application(session, actor, app)
    await session.commit()
    await session.refresh(app)
    return application_service.to_detail(app)


@router.post(
    "/applications/{application_id}/launch",
    response_model=InstanceDetail,
    status_code=status.HTTP_201_CREATED,
)
async def launch_application(
    session: DbSession,
    actor: CurrentUser,
    application_id: int,
    payload: ApplicationLaunch,
    settings: AppSettings,
    _: Annotated[None, CanLaunch],
) -> InstanceDetail:
    """Create an instance from this application.

    Requires ``instance:create`` rather than an application permission: the
    result is an instance, and it is bounded by exactly the same rules.
    """
    app = await application_service.get_application(session, actor, application_id)
    gateway = get_gateway(settings)
    instance = await application_service.launch_application(
        session, actor, app, payload, settings, gateway
    )
    summary = instance_service.to_summary(instance)  # type: ignore[arg-type]
    return InstanceDetail(
        **summary.model_dump(),
        k8s_deployment=instance.k8s_deployment,  # type: ignore[attr-defined]
        k8s_ssh_service=instance.k8s_ssh_service,  # type: ignore[attr-defined]
        k8s_ide_service=instance.k8s_ide_service,  # type: ignore[attr-defined]
        k8s_ingress=instance.k8s_ingress,  # type: ignore[attr-defined]
        k8s_pvc=instance.k8s_pvc,  # type: ignore[attr-defined]
        pod_name=instance.pod_name,  # type: ignore[attr-defined]
        ssh_command=instance_service.ssh_command(instance, settings),  # type: ignore[arg-type]
        ide_path=instance_service.ide_path(instance),  # type: ignore[arg-type]
    )


@router.get("/applications/{application_id}/events", response_model=list[WorkloadEventRead])
async def list_application_events(
    session: DbSession,
    actor: CurrentUser,
    application_id: int,
    _: Annotated[None, CanRead],
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[WorkloadEventRead]:
    await application_service.get_application(session, actor, application_id)
    events = await event_service.list_events(
        session, kind=WorkloadKind.APPLICATION, workload_id=application_id, limit=limit
    )
    return [WorkloadEventRead.from_event(event) for event in events]
