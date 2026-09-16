"""Custom task endpoints.

Submitting runs a shell command in the caller's workspace; cancelling stops it;
logs are read from the pod. The permission codes are separate from instances
because cancelling somebody else's job is a different decision from stopping
their interactive session.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from app.core.deps import AppSettings, CurrentUser, DbSession, require
from app.k8s import K8sError, get_gateway
from app.models.enums import TaskStatus, WorkloadKind
from app.schemas.application import WorkloadEventRead
from app.schemas.common import Page, PageMeta
from app.schemas.task import (
    TaskCreate,
    TaskDetail,
    TaskLogs,
    TaskSummary,
    TaskUpdate,
)
from app.services import tasks as task_service
from app.services import workload_events as event_service

router = APIRouter(tags=["tasks"])

CanRead = Depends(require("task:read"))
CanCreate = Depends(require("task:create"))
CanCancel = Depends(require("task:cancel"))
CanLogs = Depends(require("task:logs"))


def _pages(total: int, page_size: int) -> int:
    return (total + page_size - 1) // page_size if page_size else 0


@router.get("/tasks", response_model=Page[TaskSummary])
async def list_tasks(
    session: DbSession,
    actor: CurrentUser,
    _: Annotated[None, CanRead],
    workspace_id: Annotated[int | None, Query(alias="workspaceId")] = None,
    status_filter: Annotated[TaskStatus | None, Query(alias="status")] = None,
    owner_user_id: Annotated[int | None, Query(alias="ownerUserId")] = None,
    search: Annotated[str | None, Query(max_length=128)] = None,
    include_inactive: Annotated[bool, Query(alias="includeInactive")] = False,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=200, alias="pageSize")] = 20,
) -> Page[TaskSummary]:
    result = await task_service.list_tasks(
        session,
        actor,
        workspace_id=workspace_id,
        status=status_filter,
        owner_user_id=owner_user_id,
        search=search,
        include_inactive=include_inactive,
        limit=page_size,
        offset=(page - 1) * page_size,
    )
    return Page(
        items=[task_service.to_summary(task) for task in result.items],
        meta=PageMeta(
            total=result.total,
            page=page,
            page_size=page_size,
            pages=_pages(result.total, page_size),
        ),
    )


@router.get("/tasks/{task_id}", response_model=TaskDetail)
async def get_task(
    session: DbSession,
    actor: CurrentUser,
    task_id: int,
    _: Annotated[None, CanRead],
) -> TaskDetail:
    task = await task_service.get_task(session, actor, task_id)
    return TaskDetail.from_task(task)


@router.post("/tasks", response_model=TaskDetail, status_code=status.HTTP_201_CREATED)
async def create_task(
    session: DbSession,
    actor: CurrentUser,
    payload: TaskCreate,
    settings: AppSettings,
    _: Annotated[None, CanCreate],
) -> TaskDetail:
    gateway = get_gateway(settings)
    task = await task_service.create_task(session, actor, payload, settings, gateway)
    return TaskDetail.from_task(task)


@router.patch("/tasks/{task_id}", response_model=TaskDetail)
async def update_task(
    session: DbSession,
    actor: CurrentUser,
    task_id: int,
    payload: TaskUpdate,
    _: Annotated[None, CanCreate],
) -> TaskDetail:
    """Change presentation only. The script and resources are immutable."""
    task = await task_service.get_task(session, actor, task_id)
    updated = await task_service.update_task(session, task, payload, actor)
    await session.commit()
    await session.refresh(updated)
    return TaskDetail.from_task(updated)


@router.post("/tasks/{task_id}/cancel", response_model=TaskDetail)
async def cancel_task(
    session: DbSession,
    actor: CurrentUser,
    task_id: int,
    settings: AppSettings,
    _: Annotated[None, CanCancel],
) -> TaskDetail:
    task = await task_service.get_task(session, actor, task_id)
    gateway = get_gateway(settings)
    cancelled = await task_service.cancel_task(session, task, gateway, actor)
    await session.commit()
    await session.refresh(cancelled)
    return TaskDetail.from_task(cancelled)


@router.post("/tasks/{task_id}/reconcile", response_model=TaskDetail)
async def reconcile_task(
    session: DbSession,
    actor: CurrentUser,
    task_id: int,
    settings: AppSettings,
    _: Annotated[None, CanRead],
) -> TaskDetail:
    """Pull this task's state back from the cluster on demand.

    Exposed because the periodic sweep runs once a minute, and "I finished three
    seconds ago" is exactly when a user refreshes the page.
    """
    task = await task_service.get_task(session, actor, task_id)
    gateway = get_gateway(settings)
    refreshed = await task_service.reconcile_task(session, task, gateway)
    await session.commit()
    await session.refresh(refreshed)
    return TaskDetail.from_task(refreshed)


@router.get("/tasks/{task_id}/logs", response_model=TaskLogs)
async def read_task_logs(
    session: DbSession,
    actor: CurrentUser,
    task_id: int,
    settings: AppSettings,
    _: Annotated[None, CanLogs],
    tail_lines: Annotated[int, Query(ge=1, le=5000, alias="tailLines")] = 500,
    previous: Annotated[bool, Query()] = False,
) -> TaskLogs:
    task = await task_service.get_task(session, actor, task_id)
    gateway = get_gateway(settings)
    # A finished task's pod may already have been collected by its TTL, and the
    # user asking for the log of a task that just ended is the normal case. That
    # is a fact about the task, not a platform fault, so it is reported as an
    # empty log with an explanation rather than as a 502.
    try:
        logs = await task_service.read_logs(task, gateway, tail_lines=tail_lines, previous=previous)
    except K8sError as exc:
        logs = f"（日志不可用：{exc}）"
    return TaskLogs(task_id=task.id, pod_name=task.pod_name, logs=logs, previous=previous)


@router.delete("/tasks/{task_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_task(
    session: DbSession,
    actor: CurrentUser,
    task_id: int,
    settings: AppSettings,
    _: Annotated[None, CanCancel],
) -> None:
    task = await task_service.get_task(session, actor, task_id)
    gateway = get_gateway(settings)
    await task_service.delete_task(session, task, gateway, actor)
    await session.commit()


@router.get("/tasks/{task_id}/events", response_model=list[WorkloadEventRead])
async def list_task_events(
    session: DbSession,
    actor: CurrentUser,
    task_id: int,
    _: Annotated[None, CanRead],
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[WorkloadEventRead]:
    await task_service.get_task(session, actor, task_id)
    events = await event_service.list_events(
        session, kind=WorkloadKind.TASK, workload_id=task_id, limit=limit
    )
    return [WorkloadEventRead.from_event(event) for event in events]
