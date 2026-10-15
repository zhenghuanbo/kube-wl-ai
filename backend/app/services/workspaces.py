"""Workspace lifecycle: the tenancy boundary and its cluster footprint.

Creating a workspace is not a database insert — it is a database insert *plus* a
namespace, a quota, a limit range and a network policy. This module owns both
halves and keeps them in step:

* the row is written first and marked ``PROVISIONING``;
* the cluster objects are applied;
* the row is promoted to ``ACTIVE``, or to ``ERROR`` with the reason attached.

Doing it in that order means a crash mid-way leaves a visible, retryable state
rather than a workspace whose namespace silently never existed.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import policy
from app.core.errors import BadRequestError, ConflictError, NotFoundError
from app.core.logging import get_logger
from app.k8s import (
    SERVICES_PER_INSTANCE,
    K8sError,
    K8sGateway,
    LimitSpec,
    NamespaceSpec,
    QuotaSpec,
    run_gateway,
)
from app.k8s.naming import namespace_for
from app.models.enums import QuotaResource, QuotaScope, WorkspaceRole, WorkspaceStatus
from app.models.group import Group
from app.models.resource import Quota, ResourceGroup, ResourceGroupSpec, ResourceSpec
from app.models.user import User
from app.models.workspace import Workspace, WorkspaceMember, WorkspaceResourceGrant
from app.schemas.workspace import (
    WorkspaceCreate,
    WorkspaceMemberAssign,
    WorkspaceQuotaLine,
    WorkspaceQuotaSync,
    WorkspaceResourceGrantRead,
    WorkspaceSummary,
    WorkspaceUpdate,
)
from app.services import resources as resource_service

logger = get_logger(__name__)

#: Kubernetes object names inside a workspace namespace.
QUOTA_NAME = "wl-quota"
LIMIT_RANGE_NAME = "wl-limits"
NETWORK_POLICY_NAME = "wl-default"

#: Units used purely for display; the API never does arithmetic on them.
_QUOTA_UNITS: dict[str, str] = {
    QuotaResource.CPU.value: "m",
    QuotaResource.MEMORY.value: "MB",
    QuotaResource.STORAGE.value: "GB",
    QuotaResource.INSTANCE.value: "个",
    QuotaResource.TASK.value: "个",
}


@dataclass(slots=True)
class Page[T]:
    items: Sequence[T]
    total: int


# ---------------------------------------------------------------------------
# Serialisation
# ---------------------------------------------------------------------------
def to_summary(
    workspace: Workspace, *, member_count: int | None = None, grant_count: int | None = None
) -> WorkspaceSummary:
    return WorkspaceSummary.from_workspace(
        workspace, member_count=member_count, resource_group_count=grant_count
    )


async def _counts(
    session: AsyncSession, workspace_ids: Sequence[int]
) -> tuple[dict[int, int], dict[int, int]]:
    """Member and grant counts for a page of workspaces, in two queries."""
    if not workspace_ids:
        return {}, {}

    member_rows = (
        await session.execute(
            select(WorkspaceMember.workspace_id, func.count())
            .where(WorkspaceMember.workspace_id.in_(workspace_ids))
            .group_by(WorkspaceMember.workspace_id)
        )
    ).all()
    grant_rows = (
        await session.execute(
            select(WorkspaceResourceGrant.workspace_id, func.count())
            .where(
                WorkspaceResourceGrant.workspace_id.in_(workspace_ids),
                WorkspaceResourceGrant.is_active.is_(True),
            )
            .group_by(WorkspaceResourceGrant.workspace_id)
        )
    ).all()
    members: dict[int, int] = {}
    for workspace_id, member_count in member_rows:
        members[workspace_id] = member_count
    grants: dict[int, int] = {}
    for workspace_id, grant_count in grant_rows:
        grants[workspace_id] = grant_count
    return members, grants


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------
async def list_workspaces(
    session: AsyncSession,
    actor: User,
    *,
    group_id: int | None = None,
    status: str | None = None,
    is_active: bool | None = None,
    search: str | None = None,
    offset: int = 0,
    limit: int = 20,
) -> Page[WorkspaceSummary]:
    stmt = select(Workspace).order_by(Workspace.name)
    stmt = policy.scope_filter(stmt, Workspace, actor)

    if group_id is not None:
        stmt = stmt.where(Workspace.group_id == group_id)
    if status is not None:
        stmt = stmt.where(Workspace.status == status)
    # Inactive means deleted: it stays in the database for audit and usage
    # history but must not appear unless explicitly asked for.
    stmt = stmt.where(Workspace.is_active.is_(is_active if is_active is not None else True))
    if search:
        pattern = f"%{search}%"
        stmt = stmt.where(
            or_(
                Workspace.name.ilike(pattern),
                Workspace.display_name.ilike(pattern),
                Workspace.k8s_namespace.ilike(pattern),
            )
        )

    total = await session.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = list((await session.scalars(stmt.offset(offset).limit(limit))).all())
    members, grants = await _counts(session, [row.id for row in rows])
    return Page(
        items=[
            to_summary(row, member_count=members.get(row.id, 0), grant_count=grants.get(row.id, 0))
            for row in rows
        ],
        total=total,
    )


async def get_workspace(session: AsyncSession, actor: User, workspace_id: int) -> Workspace:
    workspace = await session.get(Workspace, workspace_id)
    if workspace is None:
        raise NotFoundError("工作空间不存在", code="workspace_not_found")
    # Object-level check: the list may be scoped, but a direct id must be too.
    policy.ensure_object_access(actor, workspace, group_id=workspace.group_id)
    return workspace


async def member_of(session: AsyncSession, user_id: int) -> Workspace | None:
    """The one workspace a user belongs to, if any (plan D7)."""
    result: Workspace | None = await session.scalar(
        select(Workspace)
        .join(WorkspaceMember, WorkspaceMember.workspace_id == Workspace.id)
        .where(WorkspaceMember.user_id == user_id)
    )
    return result


async def list_members(session: AsyncSession, workspace: Workspace) -> list[WorkspaceMember]:
    return list(
        (
            await session.scalars(
                select(WorkspaceMember)
                .where(WorkspaceMember.workspace_id == workspace.id)
                .order_by(WorkspaceMember.id)
            )
        ).all()
    )


async def list_grants(session: AsyncSession, workspace: Workspace) -> list[WorkspaceResourceGrant]:
    return list(
        (
            await session.scalars(
                select(WorkspaceResourceGrant)
                .where(WorkspaceResourceGrant.workspace_id == workspace.id)
                .order_by(WorkspaceResourceGrant.id)
            )
        ).all()
    )


def grant_to_read(grant: WorkspaceResourceGrant) -> WorkspaceResourceGrantRead:
    return WorkspaceResourceGrantRead.from_grant(grant)


# ---------------------------------------------------------------------------
# Create / update / delete
# ---------------------------------------------------------------------------
async def create_workspace(
    session: AsyncSession, actor: User, payload: WorkspaceCreate, gateway: K8sGateway
) -> Workspace:
    group = await _resolve_target_group(session, actor, payload.group_id)

    if await session.scalar(
        select(Workspace).where(Workspace.group_id == group.id, Workspace.name == payload.name)
    ):
        raise ConflictError(
            f"用户组 {group.name} 下已存在工作空间 {payload.name}", code="workspace_name_taken"
        )

    namespace = namespace_for(group.name, payload.name)
    if await session.scalar(select(Workspace).where(Workspace.k8s_namespace == namespace)):
        raise ConflictError(f"命名空间 {namespace} 已被占用", code="namespace_taken")

    workspace = Workspace(
        name=payload.name,
        display_name=payload.display_name or payload.name,
        description=payload.description,
        group_id=group.id,
        k8s_namespace=namespace,
        status=WorkspaceStatus.PROVISIONING,
        created_by=actor.id,
    )
    session.add(workspace)
    await session.flush()

    if payload.owner_user_id is not None:
        await _add_member(
            session,
            workspace,
            payload.owner_user_id,
            WorkspaceRole.OWNER,
            move_from_existing=False,
        )

    if payload.resource_group_ids:
        for group_id in payload.resource_group_ids:
            await _grant(session, actor, workspace, group_id, check_group=True)

    await session.flush()
    await apply_cluster_state(session, workspace, gateway)

    logger.info(
        "workspace_created", workspace=workspace.name, namespace=namespace, actor=actor.username
    )
    return workspace


async def update_workspace(
    session: AsyncSession, actor: User, workspace: Workspace, payload: WorkspaceUpdate
) -> Workspace:
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(workspace, field, value)
    await session.flush()
    logger.info("workspace_updated", workspace=workspace.name, actor=actor.username)
    return workspace


async def delete_workspace(
    session: AsyncSession, actor: User, workspace: Workspace, gateway: K8sGateway
) -> None:
    """Mark for deletion and tear the namespace down.

    The row is kept, not deleted: audit entries and usage history reference it,
    and a namespace that failed to delete must remain visible for an operator to
    chase. ``is_active=false`` takes it out of every listing.
    """
    namespace = workspace.k8s_namespace
    workspace.status = WorkspaceStatus.DELETING
    workspace.is_active = False
    await session.flush()

    try:
        await run_gateway(gateway.delete_namespace, namespace)
    except K8sError as exc:
        await _persist_failure(session, workspace, f"删除命名空间失败：{exc}")
        logger.error("workspace_delete_failed", workspace=workspace.name, error=str(exc))
        raise

    workspace.status = WorkspaceStatus.SUSPENDED
    workspace.status_message = "命名空间已删除"
    await session.flush()
    logger.info("workspace_deleted", workspace=workspace.name, actor=actor.username)


async def _persist_failure(session: AsyncSession, workspace: Workspace, message: str) -> None:
    """Record a cluster failure durably, then let the caller raise.

    The request's session is rolled back when a handler raises, which would undo
    the very ``error`` state an operator needs to see — the workspace would
    vanish and the cluster would be left holding a half-built namespace with
    nothing pointing at it. Committing here is deliberate and is the one place
    this service commits mid-request: the failure *is* the outcome.
    """
    workspace.status = WorkspaceStatus.ERROR
    workspace.status_message = message
    await session.commit()


# ---------------------------------------------------------------------------
# Cluster provisioning
# ---------------------------------------------------------------------------
async def apply_cluster_state(
    session: AsyncSession, workspace: Workspace, gateway: K8sGateway
) -> WorkspaceQuotaSync:
    """Create/patch the namespace and everything that lives directly in it.

    Idempotent: safe to re-run as a repair action, which is what the reconcile
    loop and the "重新下发" button both do.
    """
    group = await session.get(Group, workspace.group_id)
    if group is None:
        raise BadRequestError("工作空间所属用户组不存在", code="unknown_group")

    # `Workspace.group` is `lazy="joined"`: it is populated only when the row is
    # read by a query, so a *newly constructed* workspace never has it. Assigning
    # the group we just fetched both populates the relationship and saves the
    # caller a second join. Without this, anything that renders the workspace
    # (the create response, for one) raises MissingGreenlet instead of loading.
    workspace.group = group

    namespace_spec = NamespaceSpec.for_workspace(
        workspace.k8s_namespace,
        group=group.name,
        workspace=workspace.name,
        workspace_id=workspace.id,
        group_id=group.id,
    )

    quotas, network_type, limit_spec = await _desired_quota(session, workspace)

    try:
        await run_gateway(gateway.ensure_namespace, namespace_spec)
        await run_gateway(gateway.apply_resource_quota, workspace.k8s_namespace, quotas)
        await run_gateway(gateway.apply_limit_range, workspace.k8s_namespace, limit_spec)
        await run_gateway(gateway.apply_network_policy, workspace.k8s_namespace, network_type)
    except K8sError as exc:
        await _persist_failure(session, workspace, f"下发失败：{exc}")
        logger.error("workspace_provision_failed", workspace=workspace.name, error=str(exc))
        raise

    from datetime import UTC, datetime

    workspace.status = WorkspaceStatus.ACTIVE
    workspace.status_message = None
    workspace.namespace_created_at = workspace.namespace_created_at or datetime.now(UTC)
    workspace.quota_applied_at = datetime.now(UTC)
    await session.flush()

    lines = [
        WorkspaceQuotaLine(
            resource=quota.resource,
            limit_value=quota.limit_value,
            used_value=quota.used_value,
            unit=quota.unit,
        )
        for quota in await resource_service.list_quotas(
            session, scope_type=QuotaScope.WORKSPACE, scope_id=workspace.id
        )
    ]
    return WorkspaceQuotaSync(quotas=lines, applied=True)


async def _desired_quota(
    session: AsyncSession, workspace: Workspace
) -> tuple[QuotaSpec, object, LimitSpec]:
    """Derive the Kubernetes quota from the workspace's granted pools."""
    grants = [grant for grant in await list_grants(session, workspace) if grant.is_active]
    group_ids = [grant.resource_group_id for grant in grants]

    limits = await resource_service.quota_limits_for_groups(session, group_ids)
    if not limits:
        # A workspace with no pool granted still needs a namespace, but it must
        # not be able to consume shared capacity. Zeroing cpu/memory is not
        # enough — Kubernetes reads an absent resource as *unlimited* — so the
        # pod count itself is pinned to zero, which blocks everything.
        limits = {
            QuotaResource.CPU.value: 0,
            QuotaResource.MEMORY.value: 0,
            QuotaResource.INSTANCE.value: 0,
            QuotaResource.TASK.value: 0,
            QuotaResource.STORAGE.value: 0,
        }

    await resource_service.upsert_quotas(
        session,
        scope_type=QuotaScope.WORKSPACE,
        scope_id=workspace.id,
        limits=limits,
        units=_QUOTA_UNITS,
    )

    network_type = await _effective_network_type(session, group_ids)
    limit_spec = await _effective_limit_spec(session, group_ids)

    hard: dict[str, str] = {}
    if not group_ids:
        # Fail closed: nothing may be scheduled until a pool is granted.
        hard["pods"] = "0"
    cpu_milli = limits.get(QuotaResource.CPU.value, 0)
    memory_mb = limits.get(QuotaResource.MEMORY.value, 0)
    storage_gb = limits.get(QuotaResource.STORAGE.value, 0)
    instances = limits.get(QuotaResource.INSTANCE.value, 0)
    tasks = limits.get(QuotaResource.TASK.value, 0)

    if cpu_milli:
        # Requests and limits are kept equal so a workspace cannot be scheduled
        # on a promise it is not allowed to keep; Kubernetes would otherwise
        # admit pods whose limits exceed the quota and fail them later.
        hard["requests.cpu"] = _cpu_quantity(cpu_milli)
        hard["limits.cpu"] = _cpu_quantity(cpu_milli)
    if memory_mb:
        hard["requests.memory"] = f"{memory_mb}Mi"
        hard["limits.memory"] = f"{memory_mb}Mi"
    # Set unconditionally, including as "0". An absent resource is *unlimited* to
    # Kubernetes — the same trap the no-pool branch above exists to avoid — and
    # leaving storage out when nothing was granted made the namespace an
    # unbounded place to put volumes. Note this is the only aggregate ceiling on
    # storage: the per-user cap (`max_storage_gb_per_user`) is checked when a
    # volume is registered and says nothing about the group's total.
    hard["requests.storage"] = f"{storage_gb}Gi"
    if instances:
        hard["count/deployments.apps"] = str(instances)
        # Two per instance, not one: an instance with both an IDE and SSH creates
        # a ClusterIP and a NodePort. Sizing this as one Service per instance let
        # a single instance consume a two-instance workspace's entire allowance,
        # so the second instance failed with `exceeded quota: count/services`
        # *after* its Deployment had been applied.
        hard["count/services"] = str(SERVICES_PER_INSTANCE * instances)
    if tasks:
        # Exactly the task allowance: a workspace may run its jobs and nothing
        # more, so the platform's counter and the cluster's cannot disagree.
        hard["count/jobs.batch"] = str(tasks)
        # One pod per instance or task, and no spare capacity for anything the
        # platform did not create.
        hard["pods"] = str(instances + tasks)

    gpu_total = sum(
        value for key, value in limits.items() if key.startswith(f"{QuotaResource.GPU.value}:")
    )
    if gpu_total:
        hard["requests.nvidia.com/gpu"] = str(gpu_total)
        hard["limits.nvidia.com/gpu"] = str(gpu_total)

    return QuotaSpec(name=QUOTA_NAME, hard=hard), network_type, limit_spec


async def _effective_network_type(session: AsyncSession, group_ids: Sequence[int]) -> object:
    """The most restrictive posture wins when several pools are granted."""
    from app.models.enums import NetworkType

    if not group_ids:
        return NetworkType.ISOLATED

    order = {NetworkType.ISOLATED: 0, NetworkType.INTERNAL: 1, NetworkType.INTERNET: 2}
    types = list(
        (
            await session.scalars(
                select(ResourceGroup.network_type).where(ResourceGroup.id.in_(group_ids))
            )
        ).all()
    )
    if not types:
        return NetworkType.ISOLATED
    return min(types, key=lambda item: order[item])


async def _effective_limit_spec(session: AsyncSession, group_ids: Sequence[int]) -> LimitSpec:
    """Pod resource defaults and ceilings, taken from the offered specs.

    These are two different numbers, and using one for both broke every pool whose
    catalogue was not uniform. The *defaults* come from the smallest offered spec,
    so a pod that declares nothing does not silently take a shape the user never
    asked for and cannot afford. The *ceilings* come from the largest, because a
    ``max`` set to the smallest spec makes every larger spec unschedulable — and
    unschedulable in the worst possible way: a ``LimitRange`` is admission control,
    so the pod is rejected before it ever exists. Nothing is left for the reconciler
    to look at, the instance never leaves ``creating``, and the pool quietly offers a
    spec it can never run. A pool listing both 2cpu and 4cpu could only ever run
    2cpu.
    """
    min_cpu: int | None = None
    max_cpu: int | None = None
    min_memory: int | None = None
    max_memory: int | None = None
    if group_ids:
        # Both ends in one round trip. The two dimensions are taken independently,
        # because the widest CPU and the largest memory need not come from the same
        # spec.
        min_cpu, max_cpu, min_memory, max_memory = (
            await session.execute(
                select(
                    func.min(ResourceSpec.cpu_milli),
                    func.max(ResourceSpec.cpu_milli),
                    func.min(ResourceSpec.memory_mb),
                    func.max(ResourceSpec.memory_mb),
                )
                .select_from(ResourceSpec)
                .join(ResourceGroupSpec, ResourceGroupSpec.resource_spec_id == ResourceSpec.id)
                .where(
                    ResourceGroupSpec.resource_group_id.in_(group_ids),
                    ResourceSpec.is_active.is_(True),
                )
            )
        ).one()

    if min_cpu is None or max_cpu is None or min_memory is None or max_memory is None:
        # These pools offer nothing. The namespace still needs a LimitRange, and the
        # values stay modest rather than absent: an absent ``max`` is *unlimited* to
        # Kubernetes, which is the trap this whole function exists to avoid.
        return LimitSpec(
            name=LIMIT_RANGE_NAME,
            default_cpu="500m",
            default_memory="1Gi",
            default_request_cpu="200m",
            default_request_memory="512Mi",
        )

    default_cpu = _cpu_quantity(min_cpu)
    default_memory = f"{min_memory}Mi"
    return LimitSpec(
        name=LIMIT_RANGE_NAME,
        default_cpu=default_cpu,
        default_memory=default_memory,
        default_request_cpu=default_cpu,
        default_request_memory=default_memory,
        max_cpu=_cpu_quantity(max_cpu),
        max_memory=f"{max_memory}Mi",
    )


def _cpu_quantity(milli: int) -> str:
    """Render millicores the way Kubernetes prefers (``8000m`` → ``8``)."""
    if milli % 1000 == 0:
        return str(milli // 1000)
    return f"{milli}m"


# ---------------------------------------------------------------------------
# Membership
# ---------------------------------------------------------------------------
async def assign_member(
    session: AsyncSession,
    actor: User,
    workspace: Workspace,
    payload: WorkspaceMemberAssign,
) -> WorkspaceMember:
    return await _add_member(
        session,
        workspace,
        payload.user_id,
        payload.role_in_workspace,
        move_from_existing=payload.move_from_existing,
    )


async def _add_member(
    session: AsyncSession,
    workspace: Workspace,
    user_id: int,
    role: WorkspaceRole,
    *,
    move_from_existing: bool,
) -> WorkspaceMember:
    user = await session.get(User, user_id)
    if user is None:
        raise BadRequestError("指定的用户不存在", code="unknown_user")

    existing = await session.scalar(
        select(WorkspaceMember).where(WorkspaceMember.user_id == user_id)
    )
    if existing is not None:
        if existing.workspace_id == workspace.id:
            raise ConflictError("该用户已在本次工作空间中", code="already_member")
        if not move_from_existing:
            # Relocating someone's workspace is destructive — their files and
            # running workloads live there — so it never happens implicitly.
            raise ConflictError(
                f"用户已属于工作空间 {existing.workspace_id}，如需迁移请显式确认",
                code="user_already_has_workspace",
            )
        existing.workspace_id = workspace.id
        existing.role_in_workspace = role
        await session.flush()
        await session.refresh(existing, ["workspace"])
        return existing

    link = WorkspaceMember(workspace_id=workspace.id, user_id=user_id, role_in_workspace=role)
    session.add(link)
    await session.flush()
    await session.refresh(link, ["user", "workspace"])
    logger.info("workspace_member_added", workspace=workspace.name, user=user.username)
    return link


async def remove_member(
    session: AsyncSession, actor: User, workspace: Workspace, user_id: int
) -> None:
    link = await session.scalar(
        select(WorkspaceMember).where(
            WorkspaceMember.workspace_id == workspace.id, WorkspaceMember.user_id == user_id
        )
    )
    if link is None:
        raise NotFoundError("该用户不属于此工作空间", code="not_a_member")

    remaining = (
        await session.scalar(
            select(func.count())
            .select_from(WorkspaceMember)
            .where(WorkspaceMember.workspace_id == workspace.id)
        )
        or 0
    )
    if remaining <= 1:
        # An ownerless workspace cannot be administered by anyone afterwards.
        raise BadRequestError("不能移除工作空间的最后一名成员", code="last_workspace_member")

    await session.delete(link)
    await session.flush()
    logger.info("workspace_member_removed", workspace=workspace.name, user_id=user_id)


# ---------------------------------------------------------------------------
# Resource grants
# ---------------------------------------------------------------------------
async def grant_resource_group(
    session: AsyncSession, actor: User, workspace: Workspace, resource_group_id: int
) -> WorkspaceResourceGrant:
    grant = await _grant(session, actor, workspace, resource_group_id, check_group=True)
    await session.flush()
    return grant


async def _grant(
    session: AsyncSession,
    actor: User,
    workspace: Workspace,
    resource_group_id: int,
    *,
    check_group: bool,
) -> WorkspaceResourceGrant:
    group = await session.get(ResourceGroup, resource_group_id)
    if group is None:
        raise NotFoundError("资源组不存在", code="resource_group_not_found")
    if not group.is_active:
        raise BadRequestError("该资源组已停用", code="resource_group_inactive")

    # A private pool belongs to one lab; another lab must not be able to
    # schedule onto it by guessing the id.
    if (
        check_group
        and group.kind.value == "private"
        and group.group_id is not None
        and group.group_id != workspace.group_id
    ):
        raise BadRequestError(
            "该专用资源组不属于此工作空间所在的用户组",
            code="resource_group_not_shared",
        )

    existing = await session.scalar(
        select(WorkspaceResourceGrant).where(
            WorkspaceResourceGrant.workspace_id == workspace.id,
            WorkspaceResourceGrant.resource_group_id == resource_group_id,
        )
    )
    if existing is not None:
        if existing.is_active:
            raise ConflictError("该资源组已授权给此工作空间", code="already_granted")
        existing.is_active = True
        existing.granted_by = actor.id
        await session.flush()
        return existing

    grant = WorkspaceResourceGrant(
        workspace_id=workspace.id, resource_group_id=resource_group_id, granted_by=actor.id
    )
    session.add(grant)
    await session.flush()
    await session.refresh(grant, ["resource_group"])
    logger.info(
        "resource_group_granted",
        workspace=workspace.name,
        resource_group=group.name,
        actor=actor.username,
    )
    return grant


async def revoke_resource_group(
    session: AsyncSession, actor: User, workspace: Workspace, resource_group_id: int
) -> None:
    grant = await session.scalar(
        select(WorkspaceResourceGrant).where(
            WorkspaceResourceGrant.workspace_id == workspace.id,
            WorkspaceResourceGrant.resource_group_id == resource_group_id,
            WorkspaceResourceGrant.is_active.is_(True),
        )
    )
    if grant is None:
        raise NotFoundError("该资源组未授权给此工作空间", code="grant_not_found")

    await session.delete(grant)
    await session.flush()
    logger.info(
        "resource_group_revoked",
        workspace=workspace.name,
        resource_group_id=resource_group_id,
        actor=actor.username,
    )


# ---------------------------------------------------------------------------
# Group resolution
# ---------------------------------------------------------------------------
async def _resolve_target_group(session: AsyncSession, actor: User, group_id: int | None) -> Group:
    """Pick the group a new workspace belongs to, and prove the caller may.

    Defaulting to the caller's own primary group is what makes the group-admin
    flow one click: they administer one lab, so asking them to choose is noise.
    """
    if group_id is None:
        group_id = actor.primary_group.id if actor.primary_group else None
        if group_id is None:
            raise BadRequestError("未指定用户组，且当前账户不属于任何用户组", code="group_required")

    group = await session.get(Group, group_id)
    if group is None:
        raise NotFoundError("用户组不存在", code="group_not_found")
    if not group.is_active:
        raise BadRequestError("该用户组已停用", code="group_inactive")

    policy.ensure_group_access(actor, group_id)
    return group


async def quota_lines(session: AsyncSession, workspace: Workspace) -> list[Quota]:
    return await resource_service.list_quotas(
        session, scope_type=QuotaScope.WORKSPACE, scope_id=workspace.id
    )
