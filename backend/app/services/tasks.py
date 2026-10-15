"""Custom task lifecycle — submit, watch, cancel, collect results.

A task is a Kubernetes ``Job`` with ``backoffLimit=0`` and
``restartPolicy=Never``: it runs once and reports. Everything below follows from
that.

* **Submission is two steps, as for instances** — validate against database
  facts first, then persist, then apply. A rejected request never touches the
  cluster, and a cluster failure leaves an ``error`` row rather than an orphan.
* **The wall-clock cap is Kubernetes' job, not ours.** ``activeDeadlineSeconds``
  is enforced by the control plane, so a task cannot outlive its limit because
  the backend happened to be restarting. The sweeper only *notices*.
* **Quota is released at a terminal status**, unlike an instance: a finished job
  will never consume anything again, so holding its reservation would be a lie.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import policy
from app.core.config import Settings
from app.core.errors import BadRequestError, ConflictError, NotFoundError
from app.core.logging import get_logger
from app.k8s import K8sError, K8sGateway, build_task_objects, run_gateway
from app.k8s.gateway import JobInfo, VolumeMountSpec
from app.k8s.naming import is_managed_workload
from app.models.enums import (
    TERMINAL_TASK_STATUSES,
    TaskStatus,
    WorkloadEventType,
    WorkloadKind,
    WorkspaceStatus,
)
from app.models.resource import ResourceGroup
from app.models.task import Task
from app.models.user import User
from app.models.workspace import Workspace
from app.schemas.task import TaskCreate, TaskSummary, TaskUpdate
from app.services import storages as storage_service
from app.services import workload_events as events
from app.services.quota_guard import Request, enforce_compute_quota
from app.services.workload_common import (
    Page,
    pool_node_selector,
    resolve_owner,
    resolve_pool_and_spec,
    workspace_for_actor,
)

logger = get_logger(__name__)

#: Kubernetes Job condition → platform status.
_STATE_TO_STATUS: dict[str, TaskStatus] = {
    "complete": TaskStatus.SUCCEEDED,
    "failed": TaskStatus.FAILED,
    "running": TaskStatus.RUNNING,
    "pending": TaskStatus.PENDING,
}

#: Cap on the script, so a pasted megabyte cannot become an API payload.
MAX_SCRIPT_BYTES = 64 * 1024


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------
async def list_tasks(
    session: AsyncSession,
    actor: User,
    *,
    workspace_id: int | None = None,
    status: TaskStatus | None = None,
    owner_user_id: int | None = None,
    search: str | None = None,
    include_inactive: bool = False,
    limit: int = 50,
    offset: int = 0,
) -> Page[Task]:
    """List tasks the caller is allowed to see.

    The row-level filter comes from the policy layer, not from this function:
    ``scope_filter`` is the single place that knows a normal user sees only
    their workspace, so a second hand-written filter here would be a second
    chance to get it wrong.
    """
    statement = select(Task)
    count_statement = select(func.count()).select_from(Task)

    statement = policy.scope_filter(statement, Task, actor)
    count_statement = policy.scope_filter(count_statement, Task, actor)

    if not include_inactive:
        statement = statement.where(Task.is_active.is_(True))
        count_statement = count_statement.where(Task.is_active.is_(True))
    if workspace_id is not None:
        statement = statement.where(Task.workspace_id == workspace_id)
        count_statement = count_statement.where(Task.workspace_id == workspace_id)
    if status is not None:
        statement = statement.where(Task.status == status)
        count_statement = count_statement.where(Task.status == status)
    if owner_user_id is not None:
        statement = statement.where(Task.owner_user_id == owner_user_id)
        count_statement = count_statement.where(Task.owner_user_id == owner_user_id)
    if search:
        pattern = f"%{search}%"
        matches = Task.name.ilike(pattern) | Task.display_name.ilike(pattern)
        statement = statement.where(matches)
        count_statement = count_statement.where(matches)

    total = await session.scalar(count_statement) or 0
    rows = (
        await session.execute(
            statement.order_by(Task.created_at.desc(), Task.id.desc()).limit(limit).offset(offset)
        )
    ).scalars()
    return Page(items=rows.all(), total=total)


async def get_task(session: AsyncSession, actor: User, task_id: int) -> Task:
    task = await session.get(Task, task_id)
    if task is None:
        raise NotFoundError("任务不存在", code="task_not_found")
    policy.ensure_object_access(actor, task)
    return task


async def task_namespace(task: Task) -> str:
    """The namespace a task's Job lives in — its workspace's namespace."""
    return task.workspace.k8s_namespace if task.workspace else ""


# ---------------------------------------------------------------------------
# Submit
# ---------------------------------------------------------------------------
async def create_task(
    session: AsyncSession,
    actor: User,
    payload: TaskCreate,
    settings: Settings,
    gateway: K8sGateway,
) -> Task:
    workspace = await workspace_for_actor(session, actor, payload.workspace_id)
    if workspace.status != WorkspaceStatus.ACTIVE:
        raise BadRequestError(
            f"工作空间当前状态为 {workspace.status.value}，无法提交任务",
            code="workspace_not_ready",
        )

    owner = await resolve_owner(session, actor, workspace, payload.owner_user_id)
    pool, spec = await resolve_pool_and_spec(
        session, workspace, payload.resource_group_id, payload.resource_spec_id
    )

    if await session.scalar(
        select(Task).where(Task.workspace_id == workspace.id, Task.name == payload.name)
    ):
        raise ConflictError(f"该工作空间下已存在任务 {payload.name}", code="task_name_taken")

    if len(payload.script.encode("utf-8")) > MAX_SCRIPT_BYTES:
        raise BadRequestError(
            f"脚本超过 {MAX_SCRIPT_BYTES // 1024} KB 上限", code="script_too_large"
        )

    max_runtime = _resolve_runtime(payload.max_runtime_seconds, pool)

    # A task owns no disk, exactly like an instance: its home is the owner's
    # managed storage volume. Refused before anything is created, so a missing
    # volume is an error the submitter sees rather than a job whose home is empty.
    await storage_service.require_user_volume(session, owner)

    # Every ceiling that applies to this submission, in one call: the owner's own
    # quota profile, the pool's per-user limits, and the workspace's ceiling.
    await enforce_compute_quota(
        session,
        owner=owner,
        workspace=workspace,
        pool=pool,
        request=Request(
            cpu_milli=spec.cpu_milli,
            memory_mb=spec.memory_mb,
            gpu_type=spec.gpu_type,
            gpu_count=spec.gpu_count,
            tasks=1,
        ),
    )

    task = Task(
        name=payload.name,
        display_name=payload.display_name or payload.name,
        description=payload.description,
        workspace_id=workspace.id,
        group_id=workspace.group_id,
        owner_user_id=owner.id,
        created_by=actor.id,
        resource_group_id=pool.id,
        resource_spec_id=spec.id,
        cpu_milli=spec.cpu_milli,
        memory_mb=spec.memory_mb,
        gpu_type=spec.gpu_type,
        gpu_count=spec.gpu_count,
        image=payload.image,
        script=payload.script,
        working_dir=payload.working_dir,
        env=payload.env,
        priority=payload.priority,
        max_runtime_seconds=max_runtime,
        ttl_seconds_after_finished=payload.ttl_seconds_after_finished,
        status=TaskStatus.PENDING,
    )
    session.add(task)
    await session.flush()

    # Opted-in shares. The home volume is *not* in here — the builder adds it — and
    # nothing is mounted unless it was asked for, because a task that silently saw
    # a colleague's shared directory would be a surprise, not a convenience.
    attached = await storage_service.resolve_attachments(
        session, actor, workspace, payload.storage_ids or []
    )
    # A global share has no claim in this namespace until something mounts it, and
    # the Job's pod template needs the claim to exist before the pod is scheduled.
    await storage_service.ensure_attachment_claims(session, workspace, attached, gateway)
    extra_mounts = tuple(
        VolumeMountSpec(
            pvc_name=storage_service.claim_name_for(storage, workspace),
            mount_path=mount_path,
            read_only=storage.is_read_only,
        )
        for storage, mount_path in storage_service.resolve_mount_paths(attached)
    )

    await events.record(
        session,
        kind=WorkloadKind.TASK,
        workload_id=task.id,
        event_type=WorkloadEventType.CREATED,
        to_status=TaskStatus.PENDING.value,
        message=f"任务 {task.name} 已提交",
        actor=actor,
    )
    await session.commit()

    try:
        await _apply(session, task, workspace, pool, extra_mounts, settings, gateway)
    except K8sError as exc:
        await _persist_failure(session, task, str(exc))
        raise

    await events.record(
        session,
        kind=WorkloadKind.TASK,
        workload_id=task.id,
        event_type=WorkloadEventType.STARTED,
        from_status=TaskStatus.PENDING.value,
        to_status=TaskStatus.RUNNING.value,
        message="已下发到集群",
        actor=actor,
    )
    await session.commit()
    await session.refresh(task)
    logger.info("task_created", task=task.name, workspace=workspace.name)
    return task


def _resolve_runtime(requested: int | None, pool: ResourceGroup) -> int | None:
    """Pick the runtime cap, never silently exceeding the pool's own.

    Rejected rather than clamped, matching instances: the UI offers the pool's
    options, so a larger value is an attempt to exceed the cap rather than a
    slip, and two different rules for the same field would be worse than either.
    """
    cap = pool.max_runtime_seconds
    if requested is None:
        return cap
    if cap is not None and requested > cap:
        raise BadRequestError(
            f"运行时长超出资源组上限（{cap} 秒）", code="runtime_exceeds_pool_limit"
        )
    return requested


async def _apply(
    session: AsyncSession,
    task: Task,
    workspace: Workspace,
    pool: ResourceGroup,
    extra_mounts: tuple[VolumeMountSpec, ...],
    settings: Settings,
    gateway: K8sGateway,
) -> None:
    # The home is the owner's managed storage volume. Looked up rather than stored
    # on the task, so the volume is whatever is registered now.
    owner = await session.get(User, task.owner_user_id)
    if owner is None:  # pragma: no cover - a foreign key makes this unreachable
        raise BadRequestError("任务的属主不存在", code="task_owner_missing")
    home = await storage_service.require_user_volume(session, owner)

    objects = build_task_objects(
        task_id=task.id,
        task_name=task.name,
        namespace=workspace.k8s_namespace,
        workspace_name=workspace.name,
        image=task.image,
        script=task.script,
        cpu_milli=task.cpu_milli,
        memory_mb=task.memory_mb,
        gpu_type=task.gpu_type,
        gpu_count=task.gpu_count,
        home_pvc_name=storage_service.claim_name_for(home, workspace),
        home_mount_path=home.mount_path_for(owner.username),
        working_dir=task.working_dir,
        env=dict(task.env or {}),
        node_selector=pool_node_selector(pool),
        tolerations=tuple(pool.k8s_tolerations or ()),
        max_runtime_seconds=task.max_runtime_seconds,
        ttl_seconds_after_finished=task.ttl_seconds_after_finished,
        extra_mounts=extra_mounts,
    )

    await run_gateway(gateway.apply_job, objects.job)

    task.k8s_job_name = objects.job.name
    task.status = TaskStatus.RUNNING
    task.status_message = None
    # Stamped at submission rather than when a sweep first observes the pod:
    # the number a user wants is "how long did this cost me", which includes the
    # time spent queued. Waiting for an observation would also leave it unset
    # entirely for any task that finishes between two sweeps.
    task.started_at = task.started_at or datetime.now(UTC)
    await session.flush()


async def _persist_failure(session: AsyncSession, task: Task, message: str) -> None:
    """Record a cluster failure durably, then let the caller raise.

    The request's session is rolled back when a handler raises, so writing the
    error first is the only way the row survives to point at whatever was
    half-created.
    """
    task.status = TaskStatus.ERROR
    task.status_message = message[:512]
    await events.record(
        session,
        kind=WorkloadKind.TASK,
        workload_id=task.id,
        event_type=WorkloadEventType.FAILED,
        to_status=TaskStatus.ERROR.value,
        message=message,
    )
    await session.commit()


# ---------------------------------------------------------------------------
# Cancel and delete
# ---------------------------------------------------------------------------
async def cancel_task(session: AsyncSession, task: Task, gateway: K8sGateway, actor: User) -> Task:
    """Stop a running task and mark it cancelled.

    ``Foreground`` propagation: the caller is told the job is cancelled only
    once its pods are actually gone, so the GPU is free when the UI says so.
    """
    if task.is_finished:
        raise BadRequestError("任务已结束，无法取消", code="task_already_finished")

    previous = task.status
    task.status = TaskStatus.CANCELLING
    await session.flush()

    if task.k8s_job_name:
        await run_gateway(gateway.delete_job, await task_namespace(task), task.k8s_job_name)

    task.status = TaskStatus.CANCELLED
    task.status_message = "用户取消"
    task.finished_at = task.finished_at or _now()
    task.is_active = False
    await session.flush()
    await events.record(
        session,
        kind=WorkloadKind.TASK,
        workload_id=task.id,
        event_type=WorkloadEventType.STOPPED,
        from_status=previous.value,
        to_status=TaskStatus.CANCELLED.value,
        message="已取消",
        actor=actor,
    )
    await session.refresh(task)
    logger.info("task_cancelled", task=task.name)
    return task


async def delete_task(session: AsyncSession, task: Task, gateway: K8sGateway, actor: User) -> None:
    """Delete a task and its Job.

    The row is kept and marked inactive: usage history and the audit trail both
    outlive the thing they describe, and a deleted task is exactly what someone
    asks about later.

    No volume is reclaimed. A task owns none — it mounts the owner's managed
    storage volume, which outlives it by design, so removing it here would destroy
    the user's home rather than clean up after the task.
    """
    namespace = await task_namespace(task)
    if task.k8s_job_name and is_managed_workload(task.k8s_job_name):
        await run_gateway(gateway.delete_job, namespace, task.k8s_job_name)

    task.is_active = False
    if not task.is_finished:
        task.status = TaskStatus.CANCELLED
        task.finished_at = task.finished_at or _now()
    await events.record(
        session,
        kind=WorkloadKind.TASK,
        workload_id=task.id,
        event_type=WorkloadEventType.DELETED,
        message=f"任务 {task.name} 已删除",
        actor=actor,
    )
    await session.flush()
    logger.info("task_deleted", task=task.name)


async def update_task(session: AsyncSession, task: Task, payload: TaskUpdate, actor: User) -> Task:
    """Edit a task's presentation only.

    The script, image and resource shape are deliberately immutable: Kubernetes
    Job pod templates cannot be changed in place, so "editing" a submitted task
    would either be a lie or a silent resubmit. Submitting a new task is honest.
    """
    fields = payload.model_dump(exclude_unset=True)
    for key in ("display_name", "description", "priority"):
        if key in fields and fields[key] is not None:
            setattr(task, key, fields[key])
    await session.flush()
    await session.refresh(task)
    return task


# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------
async def reconcile_task(session: AsyncSession, task: Task, gateway: K8sGateway) -> Task:
    """Pull one task's status back from the cluster."""
    if task.is_finished or not task.k8s_job_name:
        return task

    info = await run_gateway(gateway.read_job, await task_namespace(task), task.k8s_job_name)
    if info is None:
        # The Job is gone — expired by its own TTL, or deleted out of band. A
        # task that disappears without succeeding has not succeeded.
        if task.status != TaskStatus.CANCELLED:
            await _transition(
                session, task, TaskStatus.FAILED, "任务在集群中已不存在（可能超出保留时间）"
            )
        return task

    await _absorb(session, task, info)
    return task


async def _absorb(session: AsyncSession, task: Task, info: JobInfo) -> None:
    """Fold a cluster observation into the row."""
    task.pod_name = info.pod_name or task.pod_name
    task.node_name = info.node_name or task.node_name

    if info.state == "Complete" or info.succeeded:
        task.exit_code = info.exit_code if info.exit_code is not None else 0
        await _transition(session, task, TaskStatus.SUCCEEDED, info.message or "任务成功结束")
        return

    if info.state == "Failed" or info.failed:
        task.exit_code = info.exit_code
        if info.deadline_exceeded:
            await _transition(
                session,
                task,
                TaskStatus.TIMEOUT,
                f"超出最长运行时间（{task.max_runtime_seconds} 秒）",
            )
        else:
            code = f"，退出码 {info.exit_code}" if info.exit_code is not None else ""
            await _transition(
                session, task, TaskStatus.FAILED, f"{info.message or '任务失败'}{code}"
            )
        return

    if info.state == "Running":
        if task.started_at is None:
            task.started_at = _now()
        if task.status == TaskStatus.PENDING:
            await _transition(session, task, TaskStatus.RUNNING, None)


async def _transition(
    session: AsyncSession, task: Task, status: TaskStatus, message: str | None
) -> None:
    previous = task.status
    if previous == status and task.status_message == message:
        return
    task.status = status
    task.status_message = message[:512] if message else None
    if status == TaskStatus.RUNNING and task.started_at is None:
        task.started_at = _now()
    if status in TERMINAL_TASK_STATUSES:
        task.finished_at = task.finished_at or _now()
        if task.exit_code is None and status == TaskStatus.SUCCEEDED:
            task.exit_code = 0
    await session.flush()
    await events.record_status(
        session,
        kind=WorkloadKind.TASK,
        workload_id=task.id,
        from_status=previous.value,
        to_status=status.value,
        message=message,
    )
    logger.info("task_transition", task=task.name, status=status.value)


async def reconcile_all(session: AsyncSession, gateway: K8sGateway, *, limit: int = 200) -> int:
    """Advance every unfinished task. Returns how many changed."""
    rows = (
        await session.execute(
            select(Task)
            .where(Task.is_active.is_(True), Task.status.not_in(TERMINAL_TASK_STATUSES))
            .order_by(Task.id)
            .limit(limit)
        )
    ).scalars()

    changed = 0
    for task in rows:
        before = task.status
        await reconcile_task(session, task, gateway)
        if task.status != before:
            changed += 1
    if changed:
        await session.commit()
    return changed


async def read_logs(
    task: Task, gateway: K8sGateway, *, tail_lines: int = 500, previous: bool = False
) -> str:
    if not task.pod_name:
        raise BadRequestError("任务尚未产生容器，暂时没有日志", code="task_no_pod")
    return await run_gateway(
        gateway.read_pod_logs,
        await task_namespace(task),
        task.pod_name,
        tail_lines=tail_lines,
        previous=previous,
    )


def _now() -> datetime:
    return datetime.now(UTC)


def to_summary(task: Task) -> TaskSummary:
    return TaskSummary.from_task(task)


__all__ = [
    "MAX_SCRIPT_BYTES",
    "Page",
    "cancel_task",
    "create_task",
    "delete_task",
    "get_task",
    "list_tasks",
    "read_logs",
    "reconcile_all",
    "reconcile_task",
    "task_namespace",
    "to_summary",
    "update_task",
]
