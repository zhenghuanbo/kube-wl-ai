"""Compute-resource catalogue: pools, specs, runtime limits, nodes and quotas."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import policy
from app.core.errors import BadRequestError, ConflictError, NotFoundError
from app.core.logging import get_logger
from app.k8s import K8sGateway, NodeInfo, run_gateway
from app.models.enums import GpuDeviceStatus, NodeStatus, QuotaResource, QuotaScope
from app.models.group import Group
from app.models.resource import (
    POOL_LABEL,
    ComputeNode,
    GpuDevice,
    Quota,
    ResourceGroup,
    ResourceGroupLimit,
    ResourceGroupSpec,
    ResourceSpec,
    RuntimeLimit,
    gpu_resource_key,
)
from app.models.user import User
from app.schemas.resource import (
    ComputeNodeRead,
    ResourceGroupCreate,
    ResourceGroupDetail,
    ResourceGroupLimitsRead,
    ResourceGroupLimitsUpdate,
    ResourceGroupSummary,
    ResourceGroupUpdate,
    ResourceSpecCreate,
    ResourceSpecRead,
    ResourceSpecUpdate,
    RuntimeLimitCreate,
    RuntimeLimitRead,
    RuntimeLimitUpdate,
)

logger = get_logger(__name__)


@dataclass(slots=True)
class Page[T]:
    items: Sequence[T]
    total: int


# ---------------------------------------------------------------------------
# Serialisation helpers (kept here so the API layer stays thin)
# ---------------------------------------------------------------------------
def _gpu_models(specs: Sequence[ResourceSpec]) -> list[str]:
    return sorted({spec.gpu_type for spec in specs if spec.gpu_type and spec.gpu_type != "none"})


def group_to_summary(
    group: ResourceGroup,
    *,
    specs: Sequence[ResourceSpec] | None = None,
    spec_count: int | None = None,
    node_count: int = 0,
) -> ResourceGroupSummary:
    resolved = list(specs) if specs is not None else []
    return ResourceGroupSummary(
        id=group.id,
        name=group.name,
        display_name=group.display_name or group.name,
        description=group.description,
        kind=group.kind,
        group_id=group.group_id,
        group_name=group.group.name if group.group else None,
        network_type=group.network_type,
        max_runtime_seconds=group.max_runtime_seconds,
        storage_class=group.storage_class,
        is_active=group.is_active,
        is_default=group.is_default,
        # Not read from ``group.nodes``: that collection is ``lazy="raise"``,
        # because counting it means loading every node row — labels blob included —
        # to produce one integer. Callers pass a count from a grouped query.
        node_count=node_count,
        gpu_models=_gpu_models(resolved),
        spec_count=spec_count if spec_count is not None else len(resolved),
        created_at=group.created_at,
        updated_at=group.updated_at,
    )


def group_to_detail(
    group: ResourceGroup, specs: Sequence[ResourceSpec], *, node_count: int = 0
) -> ResourceGroupDetail:
    summary = group_to_summary(group, specs=specs, node_count=node_count)
    return ResourceGroupDetail(
        **summary.model_dump(),
        k8s_label_selector=dict(group.k8s_label_selector or {}),
        k8s_tolerations=list(group.k8s_tolerations or []),
        limits=ResourceGroupLimitsRead.model_validate(group.limits, from_attributes=True)
        if group.limits
        else None,
        specs=[ResourceSpecRead.model_validate(spec, from_attributes=True) for spec in specs],
    )


def spec_to_read(spec: ResourceSpec) -> ResourceSpecRead:
    return ResourceSpecRead.model_validate(spec, from_attributes=True)


def limit_to_read(limit: RuntimeLimit) -> RuntimeLimitRead:
    return RuntimeLimitRead.model_validate(limit, from_attributes=True)


def node_to_read(node: object) -> ComputeNodeRead:
    devices = node.gpu_devices  # type: ignore[attr-defined]
    pool = node.resource_group  # type: ignore[attr-defined]
    return ComputeNodeRead(
        id=node.id,  # type: ignore[attr-defined]
        name=node.name,  # type: ignore[attr-defined]
        display_name=node.display_name or node.name,  # type: ignore[attr-defined]
        status=node.status.value,  # type: ignore[attr-defined]
        schedulable=node.schedulable,  # type: ignore[attr-defined]
        cpu_total_milli=node.cpu_total_milli,  # type: ignore[attr-defined]
        memory_total_mb=node.memory_total_mb,  # type: ignore[attr-defined]
        cpu_allocatable_milli=node.cpu_allocatable_milli,  # type: ignore[attr-defined]
        memory_allocatable_mb=node.memory_allocatable_mb,  # type: ignore[attr-defined]
        labels=dict(node.labels or {}),  # type: ignore[attr-defined]
        is_managed=node.is_managed,  # type: ignore[attr-defined]
        resource_group_id=node.resource_group_id,  # type: ignore[attr-defined]
        resource_group_name=(pool.display_name or pool.name) if pool is not None else None,
        gpu_count=len(devices),
        gpu_models=sorted({device.model for device in devices}),
        last_seen_at=node.last_seen_at,  # type: ignore[attr-defined]
    )


def quota_to_dict(quota: Quota) -> dict[str, object]:
    return {
        "id": quota.id,
        "scope_type": quota.scope_type.value,
        "scope_id": quota.scope_id,
        "resource": quota.resource,
        "limit_value": quota.limit_value,
        "used_value": quota.used_value,
        "available": quota.available,
        "unit": quota.unit,
        "is_active": quota.is_active,
    }


# ---------------------------------------------------------------------------
# Resource groups
# ---------------------------------------------------------------------------
async def list_resource_groups(
    session: AsyncSession,
    actor: User,
    *,
    kind: str | None = None,
    is_active: bool | None = None,
    search: str | None = None,
    offset: int = 0,
    limit: int = 20,
) -> Page[ResourceGroupSummary]:
    stmt = select(ResourceGroup).order_by(ResourceGroup.name)
    stmt = policy.scope_filter(stmt, ResourceGroup, actor)

    if kind is not None:
        stmt = stmt.where(ResourceGroup.kind == kind)
    if is_active is not None:
        stmt = stmt.where(ResourceGroup.is_active == is_active)
    if search:
        pattern = f"%{search}%"
        stmt = stmt.where(
            or_(ResourceGroup.name.ilike(pattern), ResourceGroup.display_name.ilike(pattern))
        )

    total = await session.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = list((await session.scalars(stmt.offset(offset).limit(limit))).all())

    specs = await specs_for_groups(session, [group.id for group in rows])
    counts = await node_counts_for_groups(session, [group.id for group in rows])
    return Page(
        items=[
            group_to_summary(
                group,
                specs=specs.get(group.id, []),
                spec_count=len(specs.get(group.id, [])),
                node_count=counts.get(group.id, 0),
            )
            for group in rows
        ],
        total=total,
    )


async def specs_for_groups(
    session: AsyncSession, group_ids: Sequence[int]
) -> dict[int, list[ResourceSpec]]:
    """Specs per resource group, in one query rather than one per group."""
    if not group_ids:
        return {}
    stmt = (
        select(ResourceGroupSpec.resource_group_id, ResourceSpec)
        .join(ResourceSpec, ResourceSpec.id == ResourceGroupSpec.resource_spec_id)
        .where(ResourceGroupSpec.resource_group_id.in_(group_ids))
        .order_by(ResourceSpec.cpu_milli, ResourceSpec.memory_mb, ResourceSpec.gpu_count)
    )
    grouped: dict[int, list[ResourceSpec]] = {group_id: [] for group_id in group_ids}
    for group_id, spec in (await session.execute(stmt)).all():
        grouped.setdefault(group_id, []).append(spec)
    return grouped


async def node_counts_for_groups(session: AsyncSession, group_ids: Sequence[int]) -> dict[int, int]:
    """Node count per resource group, in one query rather than one per group."""
    if not group_ids:
        return {}
    stmt = (
        select(ComputeNode.resource_group_id, func.count())
        .where(ComputeNode.resource_group_id.in_(group_ids))
        .group_by(ComputeNode.resource_group_id)
    )
    return {
        group_id: count
        for group_id, count in (await session.execute(stmt)).all()
        if group_id is not None
    }


async def get_resource_group(session: AsyncSession, actor: User, group_id: int) -> ResourceGroup:
    group = await session.get(ResourceGroup, group_id)
    if group is None:
        raise NotFoundError("资源组不存在", code="resource_group_not_found")
    # Catalogue rows are world-readable, so there is no row scope to enforce;
    # the permission check happened at the route.
    return group


async def create_resource_group(
    session: AsyncSession, actor: User, payload: ResourceGroupCreate
) -> ResourceGroup:
    if await session.scalar(select(ResourceGroup).where(ResourceGroup.name == payload.name)):
        raise ConflictError(f"资源组 {payload.name} 已存在", code="resource_group_name_taken")

    # Resolve the parent up front and hold the object: see _resolve_owning_group
    # for why the relationship, not the foreign key, is what gets assigned.
    owning_group = await _resolve_owning_group(session, payload.group_id)

    has_default = bool(
        await session.scalar(
            select(func.count())
            .select_from(ResourceGroup)
            .where(ResourceGroup.is_default.is_(True))
        )
    )

    group = ResourceGroup(
        name=payload.name,
        display_name=payload.display_name or payload.name,
        description=payload.description,
        kind=payload.kind,
        group=owning_group,
        k8s_label_selector=payload.k8s_label_selector,
        k8s_tolerations=payload.k8s_tolerations,
        network_type=payload.network_type,
        max_runtime_seconds=payload.max_runtime_seconds,
        storage_class=payload.storage_class,
        # The first pool becomes the default, because a pool set with no catch-all
        # cannot hold the invariant that every node has a home. Restoring a lost
        # default falls out of the same rule. An explicit request still wins.
        is_default=payload.is_default or not has_default,
    )
    session.add(group)
    await session.flush()

    # Every pool gets a limits row immediately: an absent one would mean
    # "unlimited" at the enforcement site, which is the wrong default.
    session.add(ResourceGroupLimit(resource_group_id=group.id))
    await session.flush()
    await session.refresh(group, ["limits"])
    logger.info("resource_group_created", resource_group=group.name, actor=actor.username)
    return group


async def update_resource_group(
    session: AsyncSession, actor: User, group: ResourceGroup, payload: ResourceGroupUpdate
) -> ResourceGroup:
    data = payload.model_dump(exclude_unset=True)
    # The owning group is assigned as a relationship, not as a foreign key. This
    # object was loaded by a query, so ``group.group`` already holds the *previous*
    # parent: writing only ``group_id`` would leave that stale value in place and
    # the response would name the old group. Re-pointing the relationship also lets
    # ``groupId: null`` mean "make this a cluster-wide pool".
    if "group_id" in data:
        group.group = await _resolve_owning_group(session, data.pop("group_id"))
    for field, value in data.items():
        setattr(group, field, value)
    await session.flush()
    if group.is_default:
        await _demote_other_group_defaults(session, group)
    logger.info("resource_group_updated", resource_group=group.name, actor=actor.username)
    return group


async def delete_resource_group(session: AsyncSession, actor: User, group: ResourceGroup) -> None:
    from app.models.workspace import WorkspaceResourceGrant

    grants = (
        await session.scalar(
            select(func.count())
            .select_from(WorkspaceResourceGrant)
            .where(
                WorkspaceResourceGrant.resource_group_id == group.id,
                WorkspaceResourceGrant.is_active.is_(True),
            )
        )
        or 0
    )
    if grants:
        # Deleting would silently strip running workspaces of their hardware.
        raise ConflictError(
            f"仍有 {grants} 个工作空间在使用该资源组，请先撤销授权",
            code="resource_group_in_use",
        )

    nodes = (
        await session.scalar(
            select(func.count())
            .select_from(ComputeNode)
            .where(ComputeNode.resource_group_id == group.id)
        )
        or 0
    )
    if nodes:
        # The foreign key would null these out, which would leave hardware that no
        # workload can reach. Better to say so and let the operator move them, the
        # same way grants are handled above.
        raise ConflictError(
            f"仍有 {nodes} 个节点属于该资源组，请先把节点移到其他资源组",
            code="resource_group_has_nodes",
        )

    was_default = group.is_default
    await session.delete(group)
    await session.flush()
    if was_default:
        # Nothing is silently promoted: an operator picks the next catch-all, and
        # until they do the node sync reports every new node as unpooled.
        logger.warning("default_resource_group_deleted", resource_group=group.name)
    logger.info("resource_group_deleted", resource_group=group.name, actor=actor.username)


async def _demote_other_group_defaults(session: AsyncSession, keep: ResourceGroup) -> None:
    """Exactly one resource group may be the default."""
    others = (
        await session.scalars(
            select(ResourceGroup).where(
                ResourceGroup.id != keep.id, ResourceGroup.is_default.is_(True)
            )
        )
    ).all()
    for other in others:
        other.is_default = False
    await session.flush()


async def default_resource_group(session: AsyncSession) -> ResourceGroup | None:
    group: ResourceGroup | None = await session.scalar(
        select(ResourceGroup).where(ResourceGroup.is_default.is_(True))
    )
    return group


async def set_group_nodes(
    session: AsyncSession,
    actor: User,
    group: ResourceGroup,
    node_ids: Sequence[int],
    gateway: K8sGateway,
) -> NodeSyncOutcome:
    """Replace which nodes belong to a resource group.

    Nodes dropped from the list go to the default pool rather than becoming
    unassigned: the partition has to stay total, and "I took it out of this pool"
    is not the same statement as "nothing may ever schedule here again".

    The default pool itself is not editable this way. Its membership is defined as
    the complement — every node no named pool claims — so a list for it would be a
    second source of truth for the same fact.
    """
    if group.is_default:
        raise BadRequestError(
            "默认资源组的节点范围由其他资源组决定，请改为把节点分配到其他资源组",
            code="default_group_nodes_are_derived",
        )

    wanted = set(node_ids)
    found = (
        set((await session.scalars(select(ComputeNode.id).where(ComputeNode.id.in_(wanted)))).all())
        if wanted
        else set()
    )
    if found != wanted:
        missing = sorted(wanted - found)
        raise BadRequestError(f"节点不存在：{missing}", code="unknown_node")

    fallback = await default_resource_group(session)
    in_group = list(
        (
            await session.scalars(
                select(ComputeNode).where(ComputeNode.resource_group_id == group.id)
            )
        ).all()
    )
    leaving = [node for node in in_group if node.id not in wanted]
    if leaving and fallback is None:
        raise BadRequestError(
            "尚未设置默认资源组，无法安置被移出的节点",
            code="no_default_resource_group",
        )

    outcome = NodeSyncOutcome()
    moving = (
        list((await session.scalars(select(ComputeNode).where(ComputeNode.id.in_(wanted)))).all())
        if wanted
        else []
    )

    for node in moving:
        if node.resource_group_id != group.id:
            await _file_node(session, node, group, gateway, outcome)
    for node in leaving:
        await _file_node(session, node, fallback, gateway, outcome)

    await session.flush()
    logger.info(
        "resource_group_nodes_set",
        resource_group=group.name,
        held=len(moving),
        moved_out=len(leaving),
        labels_patched=outcome.labels_patched,
        actor=actor.username,
    )
    return outcome


async def _file_node(
    session: AsyncSession,
    node: ComputeNode,
    pool: ResourceGroup | None,
    gateway: K8sGateway,
    outcome: NodeSyncOutcome,
) -> None:
    """Point a node's row and its cluster label at the same pool."""
    node.resource_group_id = pool.id if pool is not None else None
    if pool is None:
        outcome.nodes_unpooled += 1
        return
    if (node.labels or {}).get(POOL_LABEL) != pool.name:
        await run_gateway(gateway.patch_node_labels, node.name, {POOL_LABEL: pool.name})
        outcome.labels_patched += 1
        node.labels = {**(node.labels or {}), POOL_LABEL: pool.name}
    await session.flush()


async def update_group_limits(
    session: AsyncSession, actor: User, group: ResourceGroup, payload: ResourceGroupLimitsUpdate
) -> ResourceGroupLimit:
    limit = group.limits
    if limit is None:
        limit = ResourceGroupLimit(resource_group_id=group.id)
        session.add(limit)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(limit, field, value)
    await session.flush()
    logger.info("resource_group_limits_updated", resource_group=group.name, actor=actor.username)
    return limit


async def set_group_specs(
    session: AsyncSession, actor: User, group: ResourceGroup, spec_ids: Sequence[int]
) -> list[ResourceSpec]:
    """Replace the set of specs a resource group offers."""
    if spec_ids:
        found = set(
            (
                await session.scalars(select(ResourceSpec.id).where(ResourceSpec.id.in_(spec_ids)))
            ).all()
        )
        unknown = set(spec_ids) - found
        if unknown:
            raise BadRequestError(
                f"资源规格不存在: {sorted(unknown)}", code="unknown_resource_spec"
            )

    await session.execute(
        delete(ResourceGroupSpec).where(ResourceGroupSpec.resource_group_id == group.id)
    )
    for spec_id in dict.fromkeys(spec_ids):
        session.add(ResourceGroupSpec(resource_group_id=group.id, resource_spec_id=spec_id))
    await session.flush()
    logger.info(
        "resource_group_specs_set",
        resource_group=group.name,
        count=len(spec_ids),
        actor=actor.username,
    )
    return list(
        (
            await session.scalars(
                select(ResourceSpec)
                .join(ResourceGroupSpec, ResourceGroupSpec.resource_spec_id == ResourceSpec.id)
                .where(ResourceGroupSpec.resource_group_id == group.id)
            )
        ).all()
    )


# ---------------------------------------------------------------------------
# Resource specs
# ---------------------------------------------------------------------------
async def list_resource_specs(
    session: AsyncSession,
    actor: User,
    *,
    gpu_only: bool = False,
    is_active: bool | None = None,
    search: str | None = None,
) -> list[ResourceSpec]:
    stmt = select(ResourceSpec).order_by(ResourceSpec.cpu_milli, ResourceSpec.gpu_count)
    stmt = policy.scope_filter(stmt, ResourceSpec, actor)
    if gpu_only:
        stmt = stmt.where(ResourceSpec.gpu_count > 0)
    if is_active is not None:
        stmt = stmt.where(ResourceSpec.is_active == is_active)
    if search:
        # Matching `list_resource_groups` deliberately: this endpoint used to be
        # the one list in the API that ignored `search`, and an unrecognised
        # query parameter is dropped rather than rejected. The failure mode is a
        # caller confidently reading the first row of an unfiltered list — which
        # is exactly what two smoke scripts did, silently testing a CPU-only spec
        # while believing they had selected the single-A100 one.
        pattern = f"%{search}%"
        stmt = stmt.where(
            or_(ResourceSpec.name.ilike(pattern), ResourceSpec.display_name.ilike(pattern))
        )
    return list((await session.scalars(stmt)).all())


async def create_resource_spec(
    session: AsyncSession, actor: User, payload: ResourceSpecCreate
) -> ResourceSpec:
    if await session.scalar(select(ResourceSpec).where(ResourceSpec.name == payload.name)):
        raise ConflictError(f"资源规格 {payload.name} 已存在", code="resource_spec_name_taken")
    _validate_gpu(payload.gpu_type, payload.gpu_count)

    spec = ResourceSpec(**payload.model_dump())
    session.add(spec)
    await session.flush()
    logger.info("resource_spec_created", resource_spec=spec.name, actor=actor.username)
    return spec


async def update_resource_spec(
    session: AsyncSession, actor: User, spec: ResourceSpec, payload: ResourceSpecUpdate
) -> ResourceSpec:
    data = payload.model_dump(exclude_unset=True)
    _validate_gpu(data.get("gpu_type", spec.gpu_type), data.get("gpu_count", spec.gpu_count))
    for field, value in data.items():
        setattr(spec, field, value)
    await session.flush()
    logger.info("resource_spec_updated", resource_spec=spec.name, actor=actor.username)
    return spec


async def delete_resource_spec(session: AsyncSession, actor: User, spec: ResourceSpec) -> None:
    in_use = (
        await session.scalar(
            select(func.count())
            .select_from(ResourceGroupSpec)
            .where(ResourceGroupSpec.resource_spec_id == spec.id)
        )
        or 0
    )
    if in_use:
        raise ConflictError(
            f"该规格仍被 {in_use} 个资源组引用，请先移除", code="resource_spec_in_use"
        )
    await session.delete(spec)
    await session.flush()
    logger.info("resource_spec_deleted", resource_spec=spec.name, actor=actor.username)


def _validate_gpu(gpu_type: str, gpu_count: int) -> None:
    if gpu_count > 0 and (not gpu_type or gpu_type == "none"):
        raise BadRequestError("申请 GPU 时必须指定型号", code="gpu_type_required")
    if gpu_count == 0 and gpu_type not in ("", "none"):
        # Silently ignoring the model would make "0×A100" specs that look like
        # they reserve an A100.
        raise BadRequestError(
            f"gpu_count 为 0 时 gpu_type 应为 none，当前为 {gpu_type!r}",
            code="gpu_type_without_count",
        )


# ---------------------------------------------------------------------------
# Runtime limits
# ---------------------------------------------------------------------------
async def list_runtime_limits(
    session: AsyncSession, actor: User, *, is_active: bool | None = None
) -> list[RuntimeLimit]:
    stmt = select(RuntimeLimit).order_by(RuntimeLimit.max_runtime_seconds.nulls_last())
    stmt = policy.scope_filter(stmt, RuntimeLimit, actor)
    if is_active is not None:
        stmt = stmt.where(RuntimeLimit.is_active == is_active)
    return list((await session.scalars(stmt)).all())


async def create_runtime_limit(
    session: AsyncSession, actor: User, payload: RuntimeLimitCreate
) -> RuntimeLimit:
    if await session.scalar(select(RuntimeLimit).where(RuntimeLimit.name == payload.name)):
        raise ConflictError(f"时长限制 {payload.name} 已存在", code="runtime_limit_name_taken")

    limit = RuntimeLimit(
        name=payload.name,
        display_name=payload.display_name or payload.name,
        max_runtime_seconds=payload.max_runtime_seconds,
        is_default=payload.is_default,
    )
    session.add(limit)
    await session.flush()
    if limit.is_default:
        await _demote_other_defaults(session, limit)
    logger.info("runtime_limit_created", runtime_limit=limit.name, actor=actor.username)
    return limit


async def update_runtime_limit(
    session: AsyncSession, actor: User, limit: RuntimeLimit, payload: RuntimeLimitUpdate
) -> RuntimeLimit:
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(limit, field, value)
    await session.flush()
    if limit.is_default:
        await _demote_other_defaults(session, limit)
    return limit


async def delete_runtime_limit(session: AsyncSession, actor: User, limit: RuntimeLimit) -> None:
    await session.delete(limit)
    await session.flush()
    logger.info("runtime_limit_deleted", runtime_limit=limit.name, actor=actor.username)


async def _demote_other_defaults(session: AsyncSession, keep: RuntimeLimit) -> None:
    """Exactly one runtime limit may be the default."""
    others = (
        await session.scalars(
            select(RuntimeLimit).where(
                RuntimeLimit.id != keep.id, RuntimeLimit.is_default.is_(True)
            )
        )
    ).all()
    for other in others:
        other.is_default = False
    await session.flush()


# ---------------------------------------------------------------------------
# Nodes
# ---------------------------------------------------------------------------
#: Node columns the sync owns. Compared before and after so ``nodes_updated``
#: counts nodes that actually changed rather than nodes that were merely visited.
_TRACKED_NODE_FIELDS = (
    "status",
    "schedulable",
    "cpu_total_milli",
    "memory_total_mb",
    "cpu_allocatable_milli",
    "memory_allocatable_mb",
    "labels",
    "hostname",
    "resource_group_id",
)


@dataclass(slots=True)
class NodeSyncOutcome:
    """What one sync saw and changed.

    Counters rather than a bare tuple: ``labels_patched`` and ``nodes_unpooled``
    are the two numbers that tell an operator whether the partition still holds,
    and a tuple of four anonymous integers is exactly how those get lost.
    """

    nodes_seen: int = 0
    nodes_created: int = 0
    nodes_updated: int = 0
    devices_seen: int = 0
    #: Nodes whose ``wl-pool`` label the sync had to correct.
    labels_patched: int = 0
    #: Nodes with no pool, because no default pool is configured. Should be zero
    #: on any working install: a node in this state is hardware nothing can use.
    nodes_unpooled: int = 0


def _node_fingerprint(node: ComputeNode) -> tuple[object, ...]:
    return tuple(getattr(node, field) for field in _TRACKED_NODE_FIELDS)


def resolve_node_pool_id(
    node: ComputeNode,
    labels: dict[str, str],
    pools_by_name: dict[str, ResourceGroup],
    default_pool: ResourceGroup | None,
) -> int | None:
    """Decide which pool a node belongs to.

    The database is the authority. The label is consulted **only** when the row
    has nothing yet, which is how an operator pre-labels a batch of nodes before
    the platform has ever seen them — and how a node whose pool was deleted gets
    re-homed without anyone touching it.

    Returns ``None`` only when the node has no pool and there is no default to
    put it in, which is a configuration error rather than a state to settle into.
    """
    if node.resource_group_id is not None:
        return node.resource_group_id
    labelled = labels.get(POOL_LABEL)
    if labelled is not None and labelled in pools_by_name:
        return pools_by_name[labelled].id
    return default_pool.id if default_pool is not None else None


async def sync_nodes(
    session: AsyncSession, actor: User | None, gateway: K8sGateway
) -> NodeSyncOutcome:
    """Reconcile ``compute_nodes`` / ``gpu_devices`` with the cluster.

    Also the reconciliation point for pool membership: a node the database
    already files somewhere keeps that pool, a node it has never seen is adopted
    from its ``wl-pool`` label if that names a real pool, and anything left over
    goes to the default pool. The label is then written back, so the cluster and
    the database converge on the same answer.

    Convergence is what makes this safe to run repeatedly: nothing is written
    when the label already agrees, so a steady state produces no API traffic.
    """
    nodes: list[NodeInfo] = await run_gateway(gateway.list_nodes)

    existing = {node.name: node for node in (await session.scalars(select(ComputeNode))).all()}
    pools = list((await session.scalars(select(ResourceGroup))).all())
    pools_by_name = {pool.name: pool for pool in pools}
    pools_by_id = {pool.id: pool for pool in pools}
    default_pool = next((pool for pool in pools if pool.is_default), None)

    outcome = NodeSyncOutcome(nodes_seen=len(nodes))
    seen: set[str] = set()

    for info in nodes:
        seen.add(info.name)
        node = existing.get(info.name)
        is_new = node is None
        if node is None:
            node = ComputeNode(name=info.name)
            session.add(node)
            outcome.nodes_created += 1

        was = _node_fingerprint(node)
        labels = dict(info.labels)

        pool_id = resolve_node_pool_id(node, labels, pools_by_name, default_pool)
        if pool_id is None:
            outcome.nodes_unpooled += 1
        else:
            pool = pools_by_id[pool_id]
            if labels.get(POOL_LABEL) != pool.name:
                # Single-key patch, so the node is never briefly in two pools or
                # in none — which is the whole reason the label is a single key.
                await run_gateway(gateway.patch_node_labels, info.name, {POOL_LABEL: pool.name})
                outcome.labels_patched += 1
                labels[POOL_LABEL] = pool.name

        node.resource_group_id = pool_id
        node.status = NodeStatus(info.status)
        node.schedulable = info.schedulable
        node.cpu_total_milli = info.cpu_total_milli
        node.memory_total_mb = info.memory_total_mb
        node.cpu_allocatable_milli = info.cpu_allocatable_milli
        node.memory_allocatable_mb = info.memory_allocatable_mb
        node.labels = labels
        node.hostname = labels.get("kubernetes.io/hostname")
        node.last_seen_at = datetime.now(UTC)

        await session.flush()
        await _sync_devices(session, node, info, is_new=is_new)
        outcome.devices_seen += len(info.gpu_devices)

        if not is_new and _node_fingerprint(node) != was:
            outcome.nodes_updated += 1

    # A node that disappeared from the cluster must not keep looking schedulable.
    # Its pool is left alone: the machine is expected back, and re-homing it would
    # silently empty a pool every time a node reboots.
    for name, node in existing.items():
        if name in seen:
            continue
        was = _node_fingerprint(node)
        node.status = NodeStatus.UNKNOWN
        node.schedulable = False
        node.last_seen_at = datetime.now(UTC)
        if _node_fingerprint(node) != was:
            outcome.nodes_updated += 1

    await session.flush()
    if outcome.nodes_unpooled:
        logger.warning(
            "nodes_without_a_pool",
            count=outcome.nodes_unpooled,
            hint="no resource group is marked as the default",
        )
    logger.info(
        "nodes_synced",
        **{
            "seen": outcome.nodes_seen,
            "created": outcome.nodes_created,
            "updated": outcome.nodes_updated,
            "labels_patched": outcome.labels_patched,
            "unpooled": outcome.nodes_unpooled,
            "actor": actor.username if actor is not None else "scheduler",
        },
    )
    return outcome


async def _sync_devices(
    session: AsyncSession, node: ComputeNode, info: NodeInfo, *, is_new: bool
) -> None:
    if is_new:
        # ``gpu_devices`` is ``lazy="selectin"``, which loads the cards of nodes
        # that came out of a *query*. A node created in this session never was one,
        # so its collection is unloaded and touching it would attempt IO. One
        # refresh for a node we have just discovered — never again — is cheaper
        # than a query per node on every subsequent sweep.
        await session.refresh(node, ["gpu_devices"])
    by_index = {device.index: device for device in node.gpu_devices}

    present: set[int] = set()
    for index, model, memory_mb, uuid in info.gpu_devices:
        present.add(index)
        device = by_index.get(index)
        if device is None:
            device = GpuDevice(node_id=node.id, index=index)
            session.add(device)
        device.model = model
        if memory_mb:
            device.memory_mb = memory_mb
        device.uuid = uuid
        device.status = GpuDeviceStatus.AVAILABLE
        device.last_seen_at = datetime.now(UTC)

    # Cards the device plugin no longer reports are gone, not idle.
    for index, device in by_index.items():
        if index not in present:
            await session.delete(device)
    await session.flush()


async def list_nodes(session: AsyncSession, actor: User) -> list[ComputeNode]:
    stmt = select(ComputeNode).order_by(ComputeNode.name)
    stmt = policy.scope_filter(stmt, ComputeNode, actor)
    return list((await session.scalars(stmt)).all())


# ---------------------------------------------------------------------------
# Quotas
# ---------------------------------------------------------------------------
async def list_quotas(
    session: AsyncSession,
    *,
    scope_type: QuotaScope,
    scope_id: int,
) -> list[Quota]:
    return list(
        (
            await session.scalars(
                select(Quota)
                .where(Quota.scope_type == scope_type, Quota.scope_id == scope_id)
                .order_by(Quota.resource)
            )
        ).all()
    )


async def quota_limits_for_groups(
    session: AsyncSession, group_ids: Sequence[int]
) -> dict[str, int]:
    """Sum the per-user allowances of the given resource groups.

    Summing is deliberate: a workspace granted two pools may use its allowance
    in each, and because one user has exactly one workspace (plan D7) the
    per-user ceiling *is* the per-workspace ceiling.
    """
    if not group_ids:
        return {}

    limits = list(
        (
            await session.scalars(
                select(ResourceGroupLimit).where(
                    ResourceGroupLimit.resource_group_id.in_(group_ids)
                )
            )
        ).all()
    )
    if not limits:
        return {}

    totals = {
        QuotaResource.CPU.value: 0,
        QuotaResource.MEMORY.value: 0,
        QuotaResource.STORAGE.value: 0,
        QuotaResource.INSTANCE.value: 0,
        QuotaResource.TASK.value: 0,
    }
    for limit in limits:
        totals[QuotaResource.CPU.value] += limit.max_cpu_milli_per_user
        totals[QuotaResource.MEMORY.value] += limit.max_memory_mb_per_user
        totals[QuotaResource.STORAGE.value] += limit.max_storage_gb_per_user
        totals[QuotaResource.INSTANCE.value] += limit.max_concurrent_instances
        totals[QuotaResource.TASK.value] += limit.max_concurrent_tasks

    # GPUs are tracked per model, so they cannot be summed into one number.
    specs = await gpu_counts_for_groups(session, group_ids)
    for model, count in specs.items():
        totals[gpu_resource_key(model)] = count

    return totals


async def gpu_counts_for_groups(session: AsyncSession, group_ids: Sequence[int]) -> dict[str, int]:
    """Total GPU allowance per accelerator model across the given pools."""
    if not group_ids:
        return {}

    rows = (
        await session.execute(
            select(ResourceSpec.gpu_type, func.sum(ResourceSpec.gpu_count))
            .join(ResourceGroupSpec, ResourceGroupSpec.resource_spec_id == ResourceSpec.id)
            .where(
                ResourceGroupSpec.resource_group_id.in_(group_ids),
                ResourceSpec.gpu_count > 0,
            )
            .group_by(ResourceSpec.gpu_type)
        )
    ).all()

    # The allowance per model is the *largest* spec the pools offer, not the sum
    # of every spec: specs are alternatives a user picks between, so summing
    # them would multiply the allowance by the size of the catalogue.
    per_model: dict[str, int] = {}
    for model, _total in rows:
        if model and model != "none":
            per_model[model] = 0

    if per_model:
        specs = list(
            (
                await session.scalars(
                    select(ResourceSpec)
                    .join(ResourceGroupSpec, ResourceGroupSpec.resource_spec_id == ResourceSpec.id)
                    .where(
                        ResourceGroupSpec.resource_group_id.in_(group_ids),
                        ResourceSpec.gpu_count > 0,
                    )
                )
            ).all()
        )
        for spec in specs:
            per_model[spec.gpu_type] = max(per_model.get(spec.gpu_type, 0), spec.gpu_count)
    return per_model


async def upsert_quotas(
    session: AsyncSession,
    *,
    scope_type: QuotaScope,
    scope_id: int,
    limits: dict[str, int],
    units: dict[str, str] | None = None,
) -> list[Quota]:
    """Set the limit for each resource key, creating rows as needed.

    ``used_value`` is preserved: recomputing a limit must never silently reset
    the consumption counter, or a user could exceed their quota by asking an
    administrator to edit it.
    """
    existing = {
        quota.resource: quota
        for quota in await list_quotas(session, scope_type=scope_type, scope_id=scope_id)
    }
    resolved_units = units or {}

    for resource, limit_value in limits.items():
        quota = existing.get(resource)
        if quota is None:
            quota = Quota(
                scope_type=scope_type,
                scope_id=scope_id,
                resource=resource,
                limit_value=limit_value,
                used_value=0,
                unit=resolved_units.get(resource, ""),
            )
            session.add(quota)
        else:
            quota.limit_value = limit_value
            quota.is_active = True
            if resource in resolved_units:
                quota.unit = resolved_units[resource]

    await session.flush()
    return await list_quotas(session, scope_type=scope_type, scope_id=scope_id)


async def get_quota(
    session: AsyncSession, *, scope_type: QuotaScope, scope_id: int, resource: str
) -> Quota | None:
    result: Quota | None = await session.scalar(
        select(Quota).where(
            Quota.scope_type == scope_type,
            Quota.scope_id == scope_id,
            Quota.resource == resource,
        )
    )
    return result


async def _resolve_owning_group(session: AsyncSession, group_id: int | None) -> Group | None:
    """Load the pool's owning group, or ``None`` for a cluster-wide pool.

    This returns the entity rather than only validating an id, and callers assign
    the **relationship** instead of the raw foreign key. The difference is not
    cosmetic: assigning ``group_id`` alone leaves ``ResourceGroup.group`` unloaded,
    so rendering the group's name afterwards becomes a lazy load. Under async
    SQLAlchemy that raises ``MissingGreenlet`` — unless the parent happens to
    already sit in the session's identity map, in which case the lazy load is
    answered from memory and nothing goes wrong. A correctness property that holds
    only when some unrelated earlier query warmed a cache is a bug that surfaces
    under load and reproduces nowhere.
    """
    if group_id is None:
        return None
    parent = await session.get(Group, group_id)
    if parent is None:
        raise BadRequestError("指定的用户组不存在", code="unknown_group")
    return parent
