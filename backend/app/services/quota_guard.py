"""One place that decides whether a submission fits.

Three ceilings apply to every workload a user submits, and the **tightest one wins**:

1. the user's **quota profile** — what one person may hold anywhere, including how
   many instances, applications and tasks they may have at all;
2. the **resource group's per-user limits** — what one person may reserve inside the
   pool they picked, so a busy pool can be fair without capping every other pool;
3. the **workspace's ceilings** — what the namespace as a whole may hold, which is
   the same number Kubernetes enforces through its ``ResourceQuota``.

They are kept independent rather than collapsed into one effective limit, because
each answers a different question — "is this person over their allowance?", "is this
pool over its share?", "is this namespace full?" — and a refusal has to be able to
say which one ran out.

The rule for what occupies *compute* quota is "would Kubernetes count it":

* an instance reserves its shape only while it actually holds resources. A stopped
  instance has been scaled to zero replicas, so its pod is gone and Kubernetes'
  ``ResourceQuota`` stops counting it — and so does the platform, because the two
  disagreeing is what produces a refusal nobody can explain. Restarting therefore
  re-runs the gate (see ``app.services.instances.start_instance``): the allowance a
  stopped instance gave back may have been spent by something else in the meantime.
* a task stops counting towards *compute* the moment it reaches a terminal status,
  because it will never consume anything again.
* *counts* are the opposite of compute, and deliberately so. An instance keeps its
  slot in the user's "how many instances may I have" until it is deleted, running or
  not, because that ceiling answers "how much may I hold" — the user frees it by
  deleting, not by waiting.
* storage is counted from the workspace's **managed volumes**, never from the
  workloads that mount them — a volume is registered once, exists independently of
  any instance and outlives all of them. A compute submission therefore does not
  consult storage at all: the per-user cap is checked where gigabytes are actually
  committed (``app.services.storages``), and the namespace total by Kubernetes.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from sqlalchemy import ColumnElement, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import QuotaExceededError
from app.models.application import Application
from app.models.enums import TERMINAL_TASK_STATUSES, QuotaResource, QuotaScope
from app.models.instance import Instance
from app.models.quota_profile import QuotaProfile
from app.models.resource import Quota, ResourceGroup, gpu_resource_key
from app.models.storage import Storage
from app.models.task import Task
from app.models.user import User
from app.models.workspace import Workspace
from app.services import quota_profiles as profile_service
from app.services import resources as resource_service

#: Instance statuses that are still *live* in the platform's sense: the row exists and
#: the object is still observed and reconciled. ``stopped`` belongs here — a
#: deliberately stopped instance has to stay under observation so that a resurgent pod
#: is scaled back to zero. This is **not** the set that occupies compute quota; that is
#: :data:`RESERVING_INSTANCE_STATUSES`.
LIVE_INSTANCE_STATUSES: tuple[str, ...] = (
    "pending",
    "creating",
    "running",
    "stopping",
    "stopped",
    "error",
)

#: Instance statuses whose pods hold resources, and therefore the only ones charged
#: against a compute ceiling — everything except ``stopped``.
#:
#: A stopped instance's Deployment sits at zero replicas, so there is no pod, so
#: Kubernetes' ``ResourceQuota`` does not count it. Charging it anyway would make the
#: platform refuse submissions the cluster would have accepted, and (worse) make a
#: restart look safe when the allowance had already been spent. ``error`` stays in the
#: set on purpose: an instance that failed may still have a pod holding requests, and
#: charging it keeps the platform from approving something the namespace will reject.
RESERVING_INSTANCE_STATUSES: tuple[str, ...] = (
    "pending",
    "creating",
    "running",
    "stopping",
    "error",
)


@dataclass
class Usage:
    """What a person, a pool or a namespace currently reserves."""

    instances: int = 0
    tasks: int = 0
    cpu_milli: int = 0
    memory_mb: int = 0
    storage_gb: int = 0
    #: Card count per ``gpu:{model}`` key.
    gpu: dict[str, int] = field(default_factory=dict)


@dataclass
class ObjectCounts:
    """How many objects one person holds, counting stopped and finished ones."""

    instances: int = 0
    applications: int = 0
    tasks: int = 0


@dataclass(frozen=True)
class Request:
    """What one submission asks for, in the gate's units.

    A dimension with ``requested == 0`` is not consumed by this submission and is
    therefore not examined — otherwise a user who had already gone over one ceiling
    would be unable to do anything at all, and would be told about a limit that has
    nothing to do with what they asked for.
    """

    cpu_milli: int
    memory_mb: int
    gpu_type: str = "none"
    gpu_count: int = 0
    instances: int = 0
    applications: int = 0
    tasks: int = 0

    @property
    def wants_gpu(self) -> bool:
        return self.gpu_count > 0 and self.gpu_type not in ("", "none")


# ---------------------------------------------------------------------------
# Measuring
# ---------------------------------------------------------------------------
async def compute_usage(
    session: AsyncSession,
    *,
    workspace_id: int | None = None,
    user_id: int | None = None,
    resource_group_id: int | None = None,
) -> Usage:
    """Sum what is currently reserved, optionally narrowed to one person or pool.

    One function serves all three layers because the rule for *what counts* is
    identical; only the filter differs. Written once, the layers cannot disagree
    about whether a stopped instance still occupies quota.

    Only :data:`RESERVING_INSTANCE_STATUSES` instances are summed: what is measured
    here is what Kubernetes is counting right now, which is what makes the gate's
    answer and the namespace ``ResourceQuota``'s answer the same answer.
    """
    usage = Usage()

    instance_filters: list[ColumnElement[bool]] = [
        Instance.is_active.is_(True),
        Instance.status.in_(RESERVING_INSTANCE_STATUSES),
    ]
    if workspace_id is not None:
        instance_filters.append(Instance.workspace_id == workspace_id)
    if user_id is not None:
        instance_filters.append(Instance.owner_user_id == user_id)
    if resource_group_id is not None:
        instance_filters.append(Instance.resource_group_id == resource_group_id)

    for instance in (await session.scalars(select(Instance).where(*instance_filters))).all():
        usage.instances += 1
        usage.cpu_milli += instance.cpu_milli
        usage.memory_mb += instance.memory_mb
        if instance.has_gpu:
            key = gpu_resource_key(instance.gpu_type)
            usage.gpu[key] = usage.gpu.get(key, 0) + instance.gpu_count

    task_filters: list[ColumnElement[bool]] = [
        Task.is_active.is_(True),
        Task.status.not_in(TERMINAL_TASK_STATUSES),
    ]
    if workspace_id is not None:
        task_filters.append(Task.workspace_id == workspace_id)
    if user_id is not None:
        task_filters.append(Task.owner_user_id == user_id)
    if resource_group_id is not None:
        task_filters.append(Task.resource_group_id == resource_group_id)

    for task in (await session.scalars(select(Task).where(*task_filters))).all():
        usage.tasks += 1
        usage.cpu_milli += task.cpu_milli
        usage.memory_mb += task.memory_mb
        if task.has_gpu:
            key = gpu_resource_key(task.gpu_type)
            usage.gpu[key] = usage.gpu.get(key, 0) + task.gpu_count

    # Storage is charged to the workspace, never to a person or a pool: a volume
    # belongs to the namespace it lives in (see the module docstring). Only asked
    # for when the caller narrowed to a workspace.
    if workspace_id is not None and user_id is None and resource_group_id is None:
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


async def object_counts(session: AsyncSession, user_id: int) -> ObjectCounts:
    """How many objects a person holds, counting the stopped and the finished.

    A count ceiling answers "how much may I have", not "how much is running", so a
    stopped instance and a finished task each still occupy a slot until they are
    deleted. That is what makes the number predictable: the user frees it by
    deleting, rather than by waiting. It is also literally the list the user is
    looking at on the workload pages — running and stopped rows alike — so the number
    the platform refuses on is the number the page shows.
    """
    instances = await session.scalar(
        select(func.count())
        .select_from(Instance)
        .where(Instance.owner_user_id == user_id, Instance.is_active.is_(True))
    )
    tasks = await session.scalar(
        select(func.count())
        .select_from(Task)
        .where(Task.owner_user_id == user_id, Task.is_active.is_(True))
    )
    applications = await session.scalar(
        select(func.count())
        .select_from(Application)
        .where(Application.owner_user_id == user_id, Application.is_active.is_(True))
    )
    return ObjectCounts(
        instances=int(instances or 0),
        applications=int(applications or 0),
        tasks=int(tasks or 0),
    )


async def workspace_quotas(session: AsyncSession, workspace_id: int) -> dict[str, Quota]:
    """The workspace's own ceiling rows, keyed by resource."""
    quotas = await resource_service.list_quotas(
        session, scope_type=QuotaScope.WORKSPACE, scope_id=workspace_id
    )
    return {quota.resource: quota for quota in quotas}


async def workspace_usage(session: AsyncSession, workspace_id: int) -> dict[str, int]:
    """Live usage inside one namespace, keyed the way the ``quotas`` table is.

    This is what the quota API reports as "used", and it is measured rather than
    remembered: the same function the gate uses to decide whether a submission fits is
    the one that answers "how much is in use". The two cannot disagree, which is the
    property the deleted ``used_value`` column only pretended to have.

    All accelerator models are summed into one ``gpu`` figure because that is the
    dimension the workspace profile — and therefore the namespace quota — uses;
    per-model tracking survives only in the per-user and per-pool ceilings.
    """
    usage = await compute_usage(session, workspace_id=workspace_id)
    return {
        QuotaResource.CPU.value: usage.cpu_milli,
        QuotaResource.MEMORY.value: usage.memory_mb,
        QuotaResource.STORAGE.value: usage.storage_gb,
        QuotaResource.GPU.value: sum(usage.gpu.values()),
    }


async def scope_usage(
    session: AsyncSession, scope_type: QuotaScope, scope_id: int
) -> dict[str, int]:
    """Measured usage for whatever a ``quotas`` row is scoped to.

    Only the workspace scope has any: a person's ceilings live on their quota profile
    and a pool's on its limits row, and neither is a ``quotas`` row, so there are no
    other scopes to measure for in practice. Anything else reports nothing rather than
    zero-with-an-asterisk, and the caller renders the empty answer as zero.
    """
    if scope_type is QuotaScope.WORKSPACE:
        return await workspace_usage(session, scope_id)
    return {}


# ---------------------------------------------------------------------------
# Comparing
# ---------------------------------------------------------------------------
def _cores(milli: int) -> str:
    return f"{milli / 1000:g} 核"


def _gigabytes(megabytes: int) -> str:
    return f"{megabytes / 1024:g} GB"


def _cards(count: int) -> str:
    return f"{count} 张"


def _items(count: int) -> str:
    return f"{count} 个"


def _limit_cores(cores: int | None) -> int | None:
    """A cores ceiling in milli-cores, or ``None`` when it is not checked."""
    return None if cores is None or cores < 0 else cores * 1000


def _limit_gb(gigabytes: int | None) -> int | None:
    return None if gigabytes is None or gigabytes < 0 else gigabytes * 1024


def _limit_count(count: int | None) -> int | None:
    return None if count is None or count < 0 else count


@dataclass(frozen=True, slots=True)
class _Ceiling:
    """One dimension of one layer, ready to compare and to report."""

    layer: str
    layer_key: str
    dimension: str
    resource: str
    #: ``None`` or negative means this layer does not check this dimension.
    limit: int | None
    used: int
    requested: int
    render: Callable[[int], str] = _items


def _enforce(ceilings: Sequence[_Ceiling]) -> None:
    """Raise for the first ceiling this submission would break, or return."""
    for ceiling in ceilings:
        if ceiling.requested <= 0:
            continue
        limit = ceiling.limit
        if limit is None or limit < 0:
            continue
        if ceiling.used + ceiling.requested <= limit:
            continue
        raise QuotaExceededError(
            f"{ceiling.layer}配额不足：{ceiling.dimension} 需要 "
            f"{ceiling.render(ceiling.requested)}，上限 {ceiling.render(limit)}"
            f"（已用 {ceiling.render(ceiling.used)}）",
            detail={
                # Which of the three layers refused, so the UI can point at the
                # profile, the pool or the workspace rather than guessing.
                "layer": ceiling.layer_key,
                "resource": ceiling.resource,
                "requested": ceiling.requested,
                "limit": limit,
                "used": ceiling.used,
            },
        )


async def enforce_compute_quota(
    session: AsyncSession,
    *,
    owner: User,
    workspace: Workspace,
    pool: ResourceGroup,
    request: Request,
) -> None:
    """Refuse a submission that would break any of the three ceilings.

    Raises 409 rather than 403: this is a capacity decision, not a permission one,
    and the caller has to name the resource that ran out.
    """
    profile: QuotaProfile = await profile_service.resolve_user_profile(session, owner)
    counts = await object_counts(session, owner.id)

    owner_usage = await compute_usage(session, user_id=owner.id)
    space_usage = await compute_usage(session, workspace_id=workspace.id)
    limits = pool.limits
    pool_usage = (
        await compute_usage(session, user_id=owner.id, resource_group_id=pool.id)
        if limits is not None
        else Usage()
    )
    space_quotas = await workspace_quotas(session, workspace.id)

    user_layer = f"用户「{owner.username}」"
    pool_layer = f"资源组「{pool.name}」每用户"
    space_layer = f"工作空间「{workspace.name}」"

    ceilings: list[_Ceiling] = [
        # ① The person, across everything they hold. The counts live here and
        # nowhere else, because "how many may I have" is a property of a person.
        _Ceiling(
            user_layer,
            "user",
            "CPU",
            QuotaResource.CPU.value,
            _limit_cores(profile.cpu_cores),
            owner_usage.cpu_milli,
            request.cpu_milli,
            _cores,
        ),
        _Ceiling(
            user_layer,
            "user",
            "内存",
            QuotaResource.MEMORY.value,
            _limit_gb(profile.memory_gb),
            owner_usage.memory_mb,
            request.memory_mb,
            _gigabytes,
        ),
        _Ceiling(
            user_layer,
            "user",
            "实例数",
            QuotaResource.INSTANCE.value,
            _limit_count(profile.max_instances),
            counts.instances,
            request.instances,
        ),
        _Ceiling(
            user_layer,
            "user",
            "应用数",
            QuotaResource.APPLICATION.value,
            _limit_count(profile.max_applications),
            counts.applications,
            request.applications,
        ),
        _Ceiling(
            user_layer,
            "user",
            "自定义业务数",
            QuotaResource.TASK.value,
            _limit_count(profile.max_tasks),
            counts.tasks,
            request.tasks,
        ),
        # ② The pool, narrowed to this person: what they hold *here*, measured
        # against what this pool lends one person.
        _Ceiling(
            pool_layer,
            "pool",
            "CPU",
            QuotaResource.CPU.value,
            None if limits is None else _limit_cores(limits.max_cpu_cores_per_user),
            pool_usage.cpu_milli,
            request.cpu_milli,
            _cores,
        ),
        _Ceiling(
            pool_layer,
            "pool",
            "内存",
            QuotaResource.MEMORY.value,
            None if limits is None else _limit_gb(limits.max_memory_gb_per_user),
            pool_usage.memory_mb,
            request.memory_mb,
            _gigabytes,
        ),
    ]

    if request.wants_gpu:
        gpu_key = gpu_resource_key(request.gpu_type)
        gpu_label = f"{request.gpu_type.upper()} 加速卡"
        ceilings.extend(
            [
                _Ceiling(
                    user_layer,
                    "user",
                    gpu_label,
                    gpu_key,
                    _limit_count(profile.gpu),
                    owner_usage.gpu.get(gpu_key, 0),
                    request.gpu_count,
                    _cards,
                ),
                _Ceiling(
                    pool_layer,
                    "pool",
                    gpu_label,
                    gpu_key,
                    None if limits is None else _limit_count(limits.max_gpu_per_user),
                    pool_usage.gpu.get(gpu_key, 0),
                    request.gpu_count,
                    _cards,
                ),
            ]
        )

    # ③ The namespace. Kubernetes will enforce these same numbers, so a workspace
    # whose row is missing must fail closed rather than look unlimited: an absent
    # resource means *no limit at all* to Kubernetes, which is the trap this mirrors.
    for resource, amount, label in (
        (QuotaResource.CPU.value, request.cpu_milli, "CPU"),
        (QuotaResource.MEMORY.value, request.memory_mb, "内存"),
    ):
        if amount > 0 and space_quotas.get(resource) is None:
            raise QuotaExceededError(
                f"工作空间未获得{label}配额，无法创建", detail={"resource": resource}
            )

    ceilings.append(
        _Ceiling(
            space_layer,
            "workspace",
            "CPU",
            QuotaResource.CPU.value,
            _ceiling_of(space_quotas, QuotaResource.CPU.value),
            space_usage.cpu_milli,
            request.cpu_milli,
            _cores,
        )
    )
    ceilings.append(
        _Ceiling(
            space_layer,
            "workspace",
            "内存",
            QuotaResource.MEMORY.value,
            _ceiling_of(space_quotas, QuotaResource.MEMORY.value),
            space_usage.memory_mb,
            request.memory_mb,
            _gigabytes,
        )
    )
    if request.wants_gpu:
        # A single total here, unlike the two layers above. The workspace's `gpu` row
        # is one number because the quota template has one GPU field, so the ceiling
        # says "eight cards", not "eight of each model" — and the usage compared
        # against it is therefore every model added up. A per-model key here would
        # also be looking for a row that is never written, and the missing-row branch
        # below would then refuse *every* GPU request.
        gpu_total = sum(space_usage.gpu.values())
        if space_quotas.get(QuotaResource.GPU.value) is None:
            raise QuotaExceededError(
                f"工作空间未获得{request.gpu_type.upper()} 加速卡配额，无法创建",
                detail={"resource": QuotaResource.GPU.value},
            )
        ceilings.append(
            _Ceiling(
                space_layer,
                "workspace",
                f"{request.gpu_type.upper()} 加速卡",
                QuotaResource.GPU.value,
                _ceiling_of(space_quotas, QuotaResource.GPU.value),
                gpu_total,
                request.gpu_count,
                _cards,
            )
        )

    _enforce(ceilings)


def _ceiling_of(quotas: dict[str, Quota], resource: str) -> int | None:
    """The workspace's number for a resource, or ``None`` when it has no row."""
    quota = quotas.get(resource)
    return None if quota is None else quota.limit_value


async def enforce_application_quota(session: AsyncSession, *, owner: User) -> None:
    """Refuse a new application once its owner has reached their allowance.

    Registering an application reserves no compute: nothing is scheduled until it is
    launched, and that launch goes through the full three-layer check as an instance.
    So the only ceiling that applies here is the owner's own count — and it is
    checked so that "how many applications may I register" cannot be exceeded simply
    because there was no workload to gate.
    """
    profile: QuotaProfile = await profile_service.resolve_user_profile(session, owner)
    counts = await object_counts(session, owner.id)
    _enforce(
        [
            _Ceiling(
                f"用户「{owner.username}」",
                "user",
                "应用数",
                QuotaResource.APPLICATION.value,
                _limit_count(profile.max_applications),
                counts.applications,
                1,
            )
        ]
    )
