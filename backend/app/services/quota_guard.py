"""One place that answers "how much is this workspace using?".

Both instances and tasks consume the same workspace quota, because they consume
the same namespace and are bounded by the same ``ResourceQuota``. Computing the
two totals in separate modules would let each be individually correct while the
sum exceeded what the cluster will actually admit — the bug would surface as a
pod stuck ``Pending`` with no error anywhere.

The rule for what counts is deliberately *not* "running pods":

* An instance reserves its shape from the moment the row exists and keeps
  reserving it while stopped. Releasing on stop would let a workspace hold more
  than its quota during a restart.
* A task stops counting the moment it reaches a terminal status, because it will
  never consume anything again.
* Storage is counted from the workspace's **managed volumes**, never from the
  workloads that mount them. A volume is registered once, exists independently of
  any instance, and outlives all of them — so charging an instance for it would
  both double-count a shared volume and forget the charge entirely whenever that
  instance was deleted. It would also be meaningless now that an instance has no
  disk of its own and merely mounts the owner's volume.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import QuotaExceededError
from app.models.enums import TERMINAL_TASK_STATUSES, QuotaResource, QuotaScope
from app.models.instance import Instance
from app.models.resource import Quota, gpu_resource_key
from app.models.storage import Storage
from app.models.task import Task
from app.services import resources as resource_service

#: Instance statuses that still occupy quota — everything that has not been
#: deleted, including stopped ones (see the module docstring).
LIVE_INSTANCE_STATUSES: tuple[str, ...] = (
    "pending",
    "creating",
    "running",
    "stopping",
    "stopped",
    "error",
)


@dataclass
class Usage:
    """What a workspace is currently consuming."""

    instances: int = 0
    tasks: int = 0
    cpu_milli: int = 0
    memory_mb: int = 0
    storage_gb: int = 0
    #: Card count per ``gpu:{model}`` key.
    gpu: dict[str, int] = field(default_factory=dict)


async def workspace_usage(session: AsyncSession, workspace_id: int) -> Usage:
    """Sum everything the workspace currently reserves."""
    usage = Usage()

    instances = (
        await session.execute(
            select(Instance).where(
                Instance.workspace_id == workspace_id,
                Instance.is_active.is_(True),
                Instance.status.in_(LIVE_INSTANCE_STATUSES),
            )
        )
    ).scalars()

    for instance in instances:
        usage.instances += 1
        usage.cpu_milli += instance.cpu_milli
        usage.memory_mb += instance.memory_mb
        if instance.has_gpu:
            key = gpu_resource_key(instance.gpu_type)
            usage.gpu[key] = usage.gpu.get(key, 0) + instance.gpu_count

    tasks = (
        await session.execute(
            select(Task).where(
                Task.workspace_id == workspace_id,
                Task.is_active.is_(True),
                Task.status.not_in(TERMINAL_TASK_STATUSES),
            )
        )
    ).scalars()

    for task in tasks:
        usage.tasks += 1
        usage.cpu_milli += task.cpu_milli
        usage.memory_mb += task.memory_mb
        if task.has_gpu:
            key = gpu_resource_key(task.gpu_type)
            usage.gpu[key] = usage.gpu.get(key, 0) + task.gpu_count

    # Storage is the workspace's registered volumes, not what is running on top of
    # them (see the module docstring). A global share has no workspace and so is
    # charged to nobody, which is the intent: it belongs to the cluster, not to a
    # lab, and billing one lab for everyone's shared data would be arbitrary.
    usage.storage_gb = int(
        await session.scalar(
            select(func.coalesce(func.sum(Storage.capacity_gb), 0)).where(
                Storage.workspace_id == workspace_id,
                Storage.is_active.is_(True),
            )
        )
        or 0
    )

    return usage


async def workspace_quotas(session: AsyncSession, workspace_id: int) -> dict[str, Quota]:
    quotas = await resource_service.list_quotas(
        session, scope_type=QuotaScope.WORKSPACE, scope_id=workspace_id
    )
    return {quota.resource: quota for quota in quotas}


def check_quota(
    quotas: dict[str, Quota],
    usage: Usage,
    *,
    cpu_milli: int,
    memory_mb: int,
    gpu_type: str,
    gpu_count: int,
    storage_gb: int = 0,
    instances: int = 0,
    tasks: int = 0,
) -> None:
    """Refuse a request that would exceed a workspace ceiling.

    Raises 409 rather than 403: this is a capacity decision, not a permission
    one, and the UI has to name the resource that ran out.

    Only the resources the request itself *consumes* are checked. Storage is
    therefore checked when a volume is registered (which is when gigabytes are
    committed) and not when a workload is started, because starting a workload
    consumes none — an instance only mounts a volume that was already counted, and
    refusing to start it because the workspace is at its storage ceiling would
    block work over data that is already on disk.
    """
    checks = [
        (QuotaResource.CPU.value, usage.cpu_milli + cpu_milli, "CPU"),
        (QuotaResource.MEMORY.value, usage.memory_mb + memory_mb, "内存"),
    ]
    if storage_gb:
        checks.append((QuotaResource.STORAGE.value, usage.storage_gb + storage_gb, "存储"))
    if instances:
        checks.insert(0, (QuotaResource.INSTANCE.value, usage.instances + instances, "实例数"))
    if tasks:
        checks.insert(0, (QuotaResource.TASK.value, usage.tasks + tasks, "任务数"))
    if gpu_count > 0 and gpu_type not in ("", "none"):
        key = gpu_resource_key(gpu_type)
        checks.append((key, usage.gpu.get(key, 0) + gpu_count, f"{gpu_type.upper()} 加速卡"))

    for resource, requested, label in checks:
        quota = quotas.get(resource)
        if quota is None:
            raise QuotaExceededError(
                f"工作空间未获得{label}配额，无法创建", detail={"resource": resource}
            )
        if requested > quota.limit_value:
            raise QuotaExceededError(
                f"{label}配额不足：需要 {requested}，上限 {quota.limit_value}"
                f"（已用 {quota.used_value}）",
                detail={
                    "resource": resource,
                    "requested": requested,
                    "limit": quota.limit_value,
                    "used": quota.used_value,
                },
            )
