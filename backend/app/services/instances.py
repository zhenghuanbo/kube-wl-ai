"""Container instance lifecycle.

An instance is created in four steps, in this order, because each one depends on
the previous:

1. **Validate** ownership, pool grant, spec membership and quota — all of which
   are database facts, so a rejected request never touches the cluster.
2. **Persist** the row as ``PENDING``, so the intent is durable before anything
   is applied.
3. **Apply** the Kubernetes objects through the gateway, recording the names and
   the allocated SSH NodePort.
4. **Report** ``CREATING`` and let :func:`reconcile_instance` promote it to
   ``RUNNING`` once the pod is actually up.

A failure at step 3 is persisted as ``ERROR`` rather than rolled back: the row is
the only record that a half-built workload exists, and losing it would leave an
orphan in the namespace with nothing pointing at it.
"""

from __future__ import annotations

import base64
import hashlib
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload, raiseload

from app.auth import policy
from app.core.config import Settings
from app.core.errors import BadRequestError, ConflictError, NotFoundError, QuotaExceededError
from app.core.logging import get_logger
from app.k8s import InstanceObjects, K8sError, K8sGateway, run_gateway
from app.k8s.gateway import PodInfo, VolumeMountSpec, WorkloadRefs
from app.k8s.naming import (
    INSTANCE_LABEL,
    MANAGED_LABEL_SELECTOR,
    WORKSPACE_LABEL,
    is_managed_workload,
)
from app.k8s.workloads import SSH_PORT, build_instance_objects, ide_base_path
from app.models.enums import (
    IdeType,
    InstanceStatus,
    QuotaResource,
    WorkspaceStatus,
)
from app.models.instance import Instance
from app.models.resource import (
    ResourceGroup,
    ResourceSpec,
    gpu_resource_key,
)
from app.models.ssh_key import SshKey
from app.models.user import User
from app.models.workspace import Workspace
from app.schemas.instance import InstanceCreate, InstanceSummary, InstanceUpdate
from app.services import storages as storage_service
from app.services.quota_guard import (
    LIVE_INSTANCE_STATUSES,
    Usage,
    check_quota,
    workspace_quotas,
    workspace_usage,
)
from app.services.workload_common import (
    Page,
    pool_node_selector,
    resolve_owner,
    resolve_pool_and_spec,
    workspace_for_actor,
)

logger = get_logger(__name__)

#: Statuses that still occupy quota. Owned by :mod:`app.services.quota_guard`,
#: which also counts tasks; re-exported here because instance callers think in
#: instance statuses.
LIVE_STATUSES = LIVE_INSTANCE_STATUSES

#: Statuses whose pod is running, for usage accounting.
RUNNING_STATUSES: tuple[InstanceStatus, ...] = (InstanceStatus.RUNNING,)

#: Kubernetes phase (lower-cased) → platform status.
#:
#: Only consulted for a pod that is **not** Ready. A Ready pod is `running`
#: regardless of its phase, and a Running-but-not-Ready pod is handled before this
#: table is reached — see :func:`status_from_pod`.
_PHASE_TO_STATUS: dict[str, InstanceStatus] = {
    "running": InstanceStatus.RUNNING,
    "pending": InstanceStatus.CREATING,
    "failed": InstanceStatus.FAILED,
    "succeeded": InstanceStatus.STOPPED,
    "unknown": InstanceStatus.ERROR,
}

#: Container reasons that mean "waiting longer will not help".
#:
#: Deliberately a *list of failures* rather than a list of acceptable states: a
#: reason nobody anticipated should leave the instance looking like it is still
#: starting (recoverable, and true for a slow pull) rather than declare a failure
#: the platform cannot substantiate.
FATAL_POD_REASONS: frozenset[str] = frozenset(
    {
        "ContainerCannotRun",
        "CrashLoopBackOff",
        "CreateContainerConfigError",
        "CreateContainerError",
        "ErrImagePull",
        "ImageInspectError",
        "ImagePullBackOff",
        "InvalidImageName",
        "RegistryUnavailable",
        "RunContainerError",
    }
)


def status_from_pod(pod: PodInfo) -> tuple[InstanceStatus, str | None]:
    """Translate an observed pod into ``(status, message)``.

    **A pod can be ``Running`` while its container never starts.** The phase
    describes the pod, and a pod whose container is in ``CrashLoopBackOff`` is
    still ``Running`` — only the container is not. Mapping the phase alone
    therefore reported a crash-looping instance as ``running``, which is worse
    than reporting nothing: the page showed 运行中 and offered an IDE link that
    could not possibly work.

    So readiness is decided first, and the container's own reason decides the
    rest. Anything Running-but-not-Ready *without* a known failure is still
    starting — a readiness probe that has not passed yet is not an error.
    """
    phase = pod.phase.lower()
    if phase == "running" and pod.ready:
        return InstanceStatus.RUNNING, None
    if pod.reason in FATAL_POD_REASONS:
        return InstanceStatus.ERROR, pod.message or f"容器异常：{pod.reason}"
    if phase == "running":
        # Running, not Ready, and no failing container to point at.
        return InstanceStatus.CREATING, pod.message or "容器已启动，尚未就绪"
    return (
        _PHASE_TO_STATUS.get(phase, InstanceStatus.CREATING),
        pod.message or f"容器状态：{pod.phase}",
    )


# ---------------------------------------------------------------------------
# SSH keys
# ---------------------------------------------------------------------------
def ssh_fingerprint(public_key: str) -> str:
    """OpenSSH-style SHA-256 fingerprint of a public key.

    Computed here rather than shelling out to ``ssh-keygen``: the platform must
    not depend on a binary being present, and hashing the base64 blob is exactly
    what OpenSSH does. Format matches ``ssh-keygen -lf`` so a user can compare
    it against their own key.
    """
    parts = public_key.split()
    if len(parts) < 2:
        raise BadRequestError("公钥格式不正确", code="invalid_ssh_key")

    # OpenSSH strips base64 padding from `authorized_keys`, and a key whose blob
    # is not a multiple of three bytes therefore arrives unpadded. b64decode
    # with validate=True rejects that, so the padding has to be restored before
    # decoding — otherwise a perfectly valid key is refused as malformed.
    encoded = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        blob = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError) as exc:
        raise BadRequestError("公钥不是合法的 base64", code="invalid_ssh_key") from exc

    digest = base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")
    return f"SHA256:{digest}"


async def list_ssh_keys(session: AsyncSession, user_id: int) -> list[SshKey]:
    return list(
        (
            await session.scalars(
                select(SshKey).where(SshKey.user_id == user_id).order_by(SshKey.id)
            )
        ).all()
    )


async def add_ssh_key(session: AsyncSession, actor: User, *, name: str, public_key: str) -> SshKey:
    normalized = " ".join(public_key.split())
    fingerprint = ssh_fingerprint(normalized)

    if await session.scalar(select(SshKey).where(SshKey.fingerprint == fingerprint)):
        raise ConflictError("该公钥已登记", code="ssh_key_duplicate")

    key = SshKey(user_id=actor.id, name=name, public_key=normalized, fingerprint=fingerprint)
    session.add(key)
    await session.flush()
    logger.info("ssh_key_added", user=actor.username, fingerprint=fingerprint)
    return key


async def delete_ssh_key(session: AsyncSession, actor: User, key_id: int) -> None:
    key = await session.get(SshKey, key_id)
    if key is None or key.user_id != actor.id:
        # 404 rather than 403: someone else's key id must not be probeable.
        raise NotFoundError("公钥不存在", code="ssh_key_not_found")
    await session.delete(key)
    await session.flush()
    logger.info("ssh_key_deleted", user=actor.username, fingerprint=key.fingerprint)


async def authorized_keys(session: AsyncSession, user_id: int) -> str | None:
    """The ``authorized_keys`` body for a user, or ``None`` when they have none."""
    keys = await list_ssh_keys(session, user_id)
    if not keys:
        return None
    body = "\n".join(key.public_key for key in keys)
    return f"{body}\n"


# ---------------------------------------------------------------------------
# Loader options
# ---------------------------------------------------------------------------
# Loading one ``Instance`` costs far more than the row it returns, and the cost is
# in the model's eager loaders rather than in the query.
#
# ``Instance`` joins its workspace, owner, resource group and resource spec
# (``lazy="joined"``) — rightly, since the API renders all four. But each of those
# entities carries eager *collections* of its own, and they cascade: a user's group
# links, role links, SSH keys, API tokens and workspace memberships; a workspace's
# members and resource grants; a resource group's specs and limits. Each one is a
# separate ``SELECT ... IN (...)``, and the rows those return have eager loaders
# too. Measured against the live database, fetching a single instance emits **72
# statements and takes 132 ms** — for one row. The same query on a raw connection
# takes 1.2 ms.
#
# That tax is paid by every sweep tick and by every request that lists or fetches an
# instance (``GET /instances`` measured 170 ms).
#
# So each query states what it reads and refuses the rest, rather than the model
# changing its defaults: those collections are used elsewhere on purpose (the
# SSH-key endpoints read ``user.ssh_keys``), and making them non-eager would turn
# every such use into an async lazy-load failure. ``raiseload`` doubles as a warning
# system — a caller that starts reading something it did not declare gets
# ``InvalidRequestError`` instead of a silent ``None``.
#
# Both shapes below come down to a single statement and about 1.2-1.5 ms.

#: What a reconcile sweep reads. Every function on the sweep path — ``_has_a_pod``,
#: ``instance_workspace_ns``, ``_absorb_observation``, ``_mark_query_failed``, and
#: the ``stop_instance`` that :func:`expire_overdue` calls — reads the instance's own
#: columns plus ``workspace.k8s_namespace``, and nothing else.
_SWEEP_LOAD = (
    raiseload("*"),
    joinedload(Instance.workspace),
)

#: What the instance serialisers read. ``InstanceSummary.from_instance`` — which
#: ``InstanceDetail`` inherits rather than overrides — reads ``workspace.k8s_namespace``,
#: ``owner.username``, ``owner.display_name`` and ``resource_group.display_name``.
#: Four scalar columns, so three relationships stay loaded and the resource spec is
#: refused along with every collection hanging off the other three.
_READ_LOAD = (
    raiseload("*"),
    joinedload(Instance.workspace),
    joinedload(Instance.owner),
    joinedload(Instance.resource_group),
)


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------
async def list_instances(
    session: AsyncSession,
    actor: User,
    *,
    workspace_id: int | None = None,
    owner_user_id: int | None = None,
    status: InstanceStatus | None = None,
    ide_type: IdeType | None = None,
    search: str | None = None,
    include_inactive: bool = False,
    offset: int = 0,
    limit: int = 20,
) -> Page[Instance]:
    stmt = (
        select(Instance)
        .options(*_READ_LOAD)
        .order_by(Instance.created_at.desc(), Instance.id.desc())
    )
    stmt = policy.scope_filter(stmt, Instance, actor)

    if not include_inactive:
        stmt = stmt.where(Instance.is_active.is_(True))
    if workspace_id is not None:
        stmt = stmt.where(Instance.workspace_id == workspace_id)
    if owner_user_id is not None:
        stmt = stmt.where(Instance.owner_user_id == owner_user_id)
    if status is not None:
        stmt = stmt.where(Instance.status == status)
    if ide_type is not None:
        stmt = stmt.where(Instance.ide_type == ide_type)
    if search:
        pattern = f"%{search}%"
        stmt = stmt.where(or_(Instance.name.ilike(pattern), Instance.display_name.ilike(pattern)))

    total = await session.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = list((await session.scalars(stmt.offset(offset).limit(limit))).all())
    return Page(items=rows, total=total)


async def get_instance(session: AsyncSession, actor: User, instance_id: int) -> Instance:
    instance = await session.get(Instance, instance_id, options=_READ_LOAD)
    if instance is None:
        raise NotFoundError("实例不存在", code="instance_not_found")
    policy.ensure_object_access(actor, instance)
    return instance


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------
async def create_instance(
    session: AsyncSession,
    actor: User,
    payload: InstanceCreate,
    gateway: K8sGateway,
) -> Instance:
    workspace = await workspace_for_actor(session, actor, payload.workspace_id)
    if workspace.status != WorkspaceStatus.ACTIVE:
        raise BadRequestError(
            f"工作空间当前状态为 {workspace.status.value}，无法创建实例",
            code="workspace_not_ready",
        )

    owner = await resolve_owner(session, actor, workspace, payload.owner_user_id)
    pool, spec = await resolve_pool_and_spec(
        session, workspace, payload.resource_group_id, payload.resource_spec_id
    )

    if await session.scalar(
        select(Instance).where(Instance.workspace_id == workspace.id, Instance.name == payload.name)
    ):
        raise ConflictError(f"该工作空间下已存在实例 {payload.name}", code="instance_name_taken")

    # An instance owns no disk: its home *is* the owner's managed storage volume, so
    # there is no useful instance without one. Refused here, before any row exists,
    # rather than at mount time — a container that starts with an empty home and
    # then writes into its ephemeral layer looks to the user like data loss.
    await storage_service.require_user_volume(session, owner)

    max_runtime = _resolve_runtime(payload.max_runtime_seconds, pool)

    quotas = await workspace_quotas(session, workspace.id)
    usage = await workspace_usage(session, workspace.id)
    check_quota(
        quotas,
        usage,
        cpu_milli=spec.cpu_milli,
        memory_mb=spec.memory_mb,
        gpu_type=spec.gpu_type,
        gpu_count=spec.gpu_count,
    )
    _check_pool_instance_caps(pool, usage, spec)

    instance = Instance(
        name=payload.name,
        display_name=payload.display_name or payload.name,
        description=payload.description,
        workspace_id=workspace.id,
        owner_user_id=owner.id,
        created_by=actor.id,
        resource_group_id=pool.id,
        resource_spec_id=spec.id,
        cpu_milli=spec.cpu_milli,
        memory_mb=spec.memory_mb,
        gpu_type=spec.gpu_type,
        gpu_count=spec.gpu_count,
        image=payload.image,
        ide_type=payload.ide_type,
        ssh_enabled=payload.ssh_enabled,
        max_runtime_seconds=max_runtime,
        status=InstanceStatus.PENDING,
    )
    session.add(instance)
    await session.flush()

    # Attachments are resolved *after* the instance row exists so the join rows
    # have a foreign key, but *before* the cluster state is built, because the
    # mounts are part of the pod spec.
    #
    # Attached volumes do not count against the instance's own storage quota:
    # each volume was already charged when it was created, and counting the same
    # gigabyte twice would make a workspace hit its ceiling for no reason.
    attached = await storage_service.resolve_attachments(
        session, actor, workspace, payload.storage_ids or []
    )
    # A global share has no claim in this namespace until something mounts it, and
    # the pod spec needs the claim to exist before the pod is scheduled.
    await storage_service.ensure_attachment_claims(session, workspace, attached, gateway)
    await storage_service.record_attachments(session, instance.id, attached)

    await apply_cluster_state(session, instance, workspace, pool, gateway)

    logger.info(
        "instance_created",
        instance=instance.name,
        namespace=workspace.k8s_namespace,
        owner=owner.username,
        actor=actor.username,
    )
    return instance


def _resolve_runtime(requested: int | None, pool: ResourceGroup) -> int | None:
    """Pick the runtime cap, never silently exceeding the pool's own."""
    cap = pool.max_runtime_seconds
    if requested is None:
        return cap
    if cap is not None and requested > cap:
        raise BadRequestError(
            f"运行时长超出资源组上限（{cap} 秒）", code="runtime_exceeds_pool_limit"
        )
    return requested


def _check_pool_instance_caps(pool: ResourceGroup, usage: Usage, spec: ResourceSpec) -> None:
    """Per-pool fairness limits, which apply on top of the workspace quota."""
    limits = pool.limits
    if limits is None:
        return
    if usage.instances + 1 > limits.max_concurrent_instances:
        raise QuotaExceededError(
            f"并发实例数超出资源组上限（{limits.max_concurrent_instances}）",
            detail={"resource": QuotaResource.INSTANCE.value},
        )
    if spec.gpu_count > limits.max_gpu_per_instance:
        raise QuotaExceededError(
            f"单实例加速卡数超出资源组上限（{limits.max_gpu_per_instance}）",
            detail={"resource": gpu_resource_key(spec.gpu_type)},
        )


# ---------------------------------------------------------------------------
# Cluster state
# ---------------------------------------------------------------------------
async def apply_cluster_state(
    session: AsyncSession,
    instance: Instance,
    workspace: Workspace,
    pool: ResourceGroup,
    gateway: K8sGateway,
    *,
    replicas: int = 1,
) -> None:
    """Create or repair every object that makes up the instance.

    Idempotent: this is both the creation path and the ``/reconcile`` repair
    action, so it must be safe to run against a half-built instance. ``replicas``
    exists so repairing a *stopped* instance rebuilds it without starting it.
    """
    keys = await authorized_keys(session, instance.owner_user_id)

    # The home volume is looked up rather than stored on the instance: the instance
    # does not own it, and the owner's volume can be re-registered underneath a
    # running instance. Resolving it here means a repair picks up the current one.
    owner = await session.get(User, instance.owner_user_id)
    if owner is None:  # pragma: no cover - a foreign key makes this unreachable
        raise BadRequestError("实例的属主不存在", code="instance_owner_missing")
    home = await storage_service.require_user_volume(session, owner)
    home_mount_path = home.mount_path_for(owner.username)

    # Read the attachments from the database rather than taking them as an
    # argument: this function is also the repair path, and a reconcile that
    # forgot the mounts would silently produce an instance missing its data.
    attachments = await storage_service.list_attachments(session, instance.id)
    extra_mounts = tuple(
        VolumeMountSpec(
            # Derived per namespace, not read from the volume: a global share is
            # reached through a claim named after the namespace doing the mounting.
            pvc_name=storage_service.claim_name_for(row.storage, workspace),
            mount_path=row.mount_path,
            read_only=row.is_read_only,
        )
        for row in attachments
        if row.storage is not None
    )
    if len(extra_mounts) != len(attachments):
        # Not silent. An attachment whose row has gone would otherwise just stop
        # being mounted, and the user would see an empty directory rather than a
        # failure — which is the worst possible way to lose access to data.
        logger.warning(
            "instance_attachment_skipped",
            instance=instance.id,
            attached=len(attachments),
            mounted=len(extra_mounts),
        )

    objects = build_instance_objects(
        instance_id=instance.id,
        instance_name=instance.name,
        owner_username=owner.username,
        namespace=workspace.k8s_namespace,
        workspace_labels={
            WORKSPACE_LABEL: workspace.name,
            "wl-managed": "true",
        },
        image=instance.image,
        ide_type=instance.ide_type,
        cpu_milli=instance.cpu_milli,
        memory_mb=instance.memory_mb,
        gpu_type=instance.gpu_type,
        gpu_count=instance.gpu_count,
        home_pvc_name=storage_service.claim_name_for(home, workspace),
        home_mount_path=home_mount_path,
        ssh_enabled=instance.ssh_enabled,
        authorized_keys=keys,
        node_selector=pool_node_selector(pool),
        tolerations=tuple(pool.k8s_tolerations or ()),
        extra_mounts=extra_mounts,
    )

    try:
        await _apply(session, instance, objects, gateway, replicas=replicas)
    except K8sError as exc:
        await _persist_failure(session, instance, f"下发失败：{exc}")
        raise

    # A repair must not talk a stopped instance into running.
    if replicas == 0:
        instance.status = InstanceStatus.STOPPED
        instance.status_message = None
    else:
        instance.status = InstanceStatus.CREATING
        instance.status_message = None
        instance.started_at = instance.started_at or datetime.now(UTC)
        _apply_expiry(instance)
    await session.flush()
    await session.refresh(instance)


async def _apply(
    session: AsyncSession,
    instance: Instance,
    objects: InstanceObjects,
    gateway: K8sGateway,
    *,
    replicas: int,
) -> None:
    """Push the objects, naming each one *before* it is applied.

    That order is the point, not an accident. Recording the names only once every
    apply had returned lost objects in two different ways:

    * a later object is rejected — a namespace ``ResourceQuota`` is the usual one
      — after an earlier object was already created. The Deployment exists, the
      Service does not, and the exception unwinds before anything was recorded.
    * an apply that took effect on the API server but raised on the way back: a
      timeout or a dropped connection, where "it failed" and "it exists" are both
      true.

    Either way the instance row ends up naming nothing. ``_persist_failure`` then
    commits an ``error`` row precisely so the half-built workload stays
    referenced — and its own docstring says as much — but a row with null columns
    references nothing: ``delete_instance`` builds the deletion list *from these
    columns*, so deleting the instance removed nothing from the cluster. The
    workload stayed behind, invisible to the platform and still consuming the
    quota that caused the failure, which left the workspace unable to create
    anything else.

    Naming the object first cannot lose anything, which is why it beats tracking
    successes: deleting an object that was never created is a no-op, and
    ``delete_workload`` already tolerates absent objects. The cost is that the row
    may name an object the cluster does not have, which is the harmless direction
    to be wrong in.
    """
    refs = objects.refs

    if objects.ssh_secret is not None:
        # Set only when a secret is actually created, so a repair that turns SSH
        # *off* keeps the old name and the delete path still collects it; the
        # secret's name is derived from the instance, so enabling it again reuses
        # the same object rather than leaking a second one.
        instance.k8s_secret = refs.secret
        await run_gateway(gateway.apply_secret, objects.ssh_secret)

    instance.k8s_deployment = refs.deployment
    await run_gateway(gateway.apply_deployment, replace(objects.deployment, replicas=replicas))

    node_port: int | None = instance.ssh_node_port
    if objects.ssh_service is not None:
        # The column records which Service is ours — named before the call, like
        # every other object here. The port itself is read back afterwards,
        # because the cluster picks it, so it stays unset if that never happens;
        # leaving it unset only hides the SSH command, while the recorded Service
        # name is what lets a delete clean it up.
        instance.k8s_ssh_service = objects.ssh_service.name
        applied = await run_gateway(gateway.apply_service, objects.ssh_service)
        # The cluster picks the port, so read it back rather than assume the
        # one that was requested (which is usually none).
        for port in applied.ports:
            if port.node_port:
                node_port = port.node_port
        instance.ssh_node_port = node_port if instance.ssh_enabled else None
    else:
        instance.ssh_node_port = None

    if objects.ide_service is not None:
        instance.k8s_ide_service = objects.ide_service.name
        await run_gateway(gateway.apply_service, objects.ide_service)

    # No Ingress is applied any more: the IDE is published by the platform's own
    # reverse proxy (``app/api/ide/proxy.py``) instead of by a cluster ingress
    # controller, so that the user never leaves the platform's origin. The
    # ``k8s_ingress`` column is left exactly as it was rather than cleared, so an
    # instance built before this change still names the object the delete path has
    # to collect.

    # Left null, always, and naming each object before its call cannot change
    # that: ``refs.pvc`` is None because an instance creates no claim of its own.
    # Its home belongs to the storage volume, and the delete path removes the
    # claims named here, so recording the owner's claim would make deleting an
    # instance destroy the user's storage. The column is kept for rows created
    # before that change, whose temporary home claims still have to be cleaned up.
    instance.k8s_pvc = refs.pvc
    await session.flush()


def _apply_expiry(instance: Instance) -> None:
    """Set the deadline from the runtime cap and the moment it started."""
    if instance.max_runtime_seconds is None or instance.started_at is None:
        instance.expires_at = None
        return
    started = instance.started_at
    if started.tzinfo is None:
        started = started.replace(tzinfo=UTC)
    instance.expires_at = started + timedelta(seconds=instance.max_runtime_seconds)


async def _persist_failure(session: AsyncSession, instance: Instance, message: str) -> None:
    """Record a cluster failure durably, then let the caller raise.

    Same reasoning as the workspace equivalent: the request's session is rolled
    back when a handler raises, and losing the ``error`` row would leave a
    half-built workload in the namespace with nothing referencing it.
    """
    instance.status = InstanceStatus.ERROR
    instance.status_message = message
    await session.commit()


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------
async def _require_deployment(instance: Instance) -> str:
    deployment = (instance.k8s_deployment or "").strip()
    if not deployment:
        raise BadRequestError(
            "实例尚未下发到集群，请先执行重新下发", code="instance_not_provisioned"
        )
    return deployment


async def start_instance(
    session: AsyncSession, instance: Instance, gateway: K8sGateway
) -> Instance:
    deployment = await _require_deployment(instance)
    if instance.status == InstanceStatus.RUNNING:
        return instance

    if instance.is_expired:
        # Restarting an expired instance would silently grant more time than the
        # pool allows; the deadline is recomputed from the new start instead.
        instance.started_at = None

    await run_gateway(gateway.scale_deployment, instance_workspace_ns(instance), deployment, 1)
    instance.status = InstanceStatus.CREATING
    instance.status_message = None
    instance.started_at = instance.started_at or datetime.now(UTC)
    instance.stopped_at = None
    _apply_expiry(instance)
    await session.flush()
    await session.refresh(instance)
    logger.info("instance_started", instance=instance.name)
    return instance


async def stop_instance(session: AsyncSession, instance: Instance, gateway: K8sGateway) -> Instance:
    deployment = await _require_deployment(instance)
    if instance.status == InstanceStatus.STOPPED:
        return instance

    await run_gateway(gateway.scale_deployment, instance_workspace_ns(instance), deployment, 0)
    instance.status = InstanceStatus.STOPPED
    instance.status_message = None
    instance.stopped_at = datetime.now(UTC)
    await session.flush()
    await session.refresh(instance)
    logger.info("instance_stopped", instance=instance.name)
    return instance


async def delete_instance(session: AsyncSession, instance: Instance, gateway: K8sGateway) -> None:
    """Delete the workload but keep the row.

    The row is retained because usage history references it and because an
    object that failed to delete must stay visible for an operator to chase.
    ``is_active=false`` takes it out of every listing, which is what the user
    perceives as deletion.
    """
    namespace = instance_workspace_ns(instance)
    refs = WorkloadRefs(
        deployment=_optional_name(instance.k8s_deployment),
        services=tuple(
            name
            for name in (
                _optional_name(instance.k8s_ssh_service),
                _optional_name(instance.k8s_ide_service),
            )
            if name
        ),
        ingress=_optional_name(instance.k8s_ingress),
        pvc=_optional_name(instance.k8s_pvc),
        secret=_optional_name(instance.k8s_secret),
    )

    instance.status = InstanceStatus.DELETING
    await session.flush()

    try:
        await run_gateway(gateway.delete_workload, namespace, refs)
    except K8sError as exc:
        await _persist_failure(session, instance, f"删除失败：{exc}")
        logger.error("instance_delete_failed", instance=instance.name, error=str(exc))
        raise

    instance.status = InstanceStatus.STOPPED
    instance.status_message = "已删除"
    instance.is_active = False
    instance.stopped_at = datetime.now(UTC)
    await session.flush()
    logger.info("instance_deleted", instance=instance.name)


def instance_workspace_ns(instance: Instance) -> str:
    """The namespace an instance lives in.

    Read from the loaded relationship; a missing workspace would mean the row is
    unusable, and failing loudly is better than guessing a namespace name.
    """
    workspace = instance.workspace
    if workspace is None:
        raise BadRequestError("实例所属工作空间不存在", code="workspace_not_found")
    return workspace.k8s_namespace


def _optional_name(value: str | None) -> str | None:
    stripped = (value or "").strip()
    return stripped or None


async def update_instance(
    session: AsyncSession, instance: Instance, payload: InstanceUpdate
) -> Instance:
    for name, value in payload.model_dump(exclude_unset=True).items():
        setattr(instance, name, value)
    await session.flush()
    return instance


# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------
def _has_a_pod(instance: Instance) -> bool:
    """Whether this instance is supposed to have a pod right now.

    Only these are worth asking the cluster about. A stopped instance has no pod by
    design, and asking would flip it to ``error`` on every sweep.
    """
    return bool(
        instance.is_active
        and instance.k8s_deployment
        and instance.status not in (InstanceStatus.STOPPED, InstanceStatus.DELETING)
    )


async def _mark_query_failed(session: AsyncSession, instance: Instance, exc: Exception) -> Instance:
    """Record that the cluster could not be read, without inventing a status.

    Deliberately does not touch ``status``: a failed *query* is not an observation,
    and a row that was ``running`` a moment ago is far more likely to still be
    running than to have become an error.
    """
    instance.status_message = f"状态查询失败：{exc}"
    await session.flush()
    return instance


async def _absorb_observation(
    session: AsyncSession,
    instance: Instance,
    pods: list[PodInfo],
    deployment_failure: str | None = None,
) -> Instance:
    """Fold the pods observed for one instance into its row.

    Takes the pods instead of fetching them, so that a caller which listed a whole
    namespace at once can serve many instances from that one listing. Performs no
    cluster access of its own, and ``deployment_failure`` is handed in for the same
    reason.
    """
    if not pods:
        if deployment_failure is not None:
            # The Deployment controller could not create a pod *at all*, and said
            # why. It will not do anything different next tick: the pod was refused
            # at admission, so retrying is the controller's job and failing is the
            # outcome. Reporting "等待容器启动" here is a promise the cluster has
            # already broken — which is how an instance whose container the cluster
            # would never accept sat on 创建中 forever, with the real reason
            # ("maximum cpu usage per Container is 2, but limit is 4") visible only
            # to whoever thought to run `kubectl describe rs`.
            #
            # Sticky until a pod appears, which is the only thing that proves the
            # cause was removed: `error` is in LIVE_INSTANCE_STATUSES, so the sweep
            # keeps observing this instance and flips it to `running` on its own.
            if instance.status is not InstanceStatus.ERROR:
                logger.warning(
                    "instance_pod_rejected",
                    instance=instance.name,
                    namespace=instance_workspace_ns(instance),
                    reason=deployment_failure,
                )
            instance.status = InstanceStatus.ERROR
            instance.status_message = f"集群拒绝创建容器：{deployment_failure}"
        elif instance.status in (InstanceStatus.RUNNING, InstanceStatus.CREATING):
            instance.status = InstanceStatus.CREATING
            instance.status_message = "等待容器启动"
        await session.flush()
        return instance

    # Newest first, as `list_pods` returns them: during a rollout the pod that is
    # coming up is the one worth reporting, and the one on its way out is not.
    pod = pods[0]
    previous = instance.status
    instance.pod_name = pod.name
    instance.node_name = pod.node_name
    instance.last_seen_at = datetime.now(UTC)

    instance.status, instance.status_message = status_from_pod(pod)

    if previous != instance.status:
        logger.info(
            "instance_status_changed",
            instance=instance.name,
            previous=previous.value,
            current=instance.status.value,
        )
    await session.flush()
    # Reloads server-side columns (`updated_at` has an `onupdate`). The HTTP
    # reconcile endpoint serialises this very object into its response, so the
    # refreshed value is not cosmetic there.
    #
    # The attributes are named because a bare ``refresh()`` reloads the row through
    # the *mapper's* loaders, re-running the eager cascade the query above was
    # narrowed to avoid — eighteen statements and 20 ms for this one row, and once
    # per instance in the batch path. A refresh is cheap (one statement) only when
    # the related rows are already in the identity map, which after a narrowed
    # select they are not. So narrowing the select without narrowing this would move
    # the cascade from once per sweep to once per instance rather than remove it.
    # Naming the attributes keeps the reload to the columns this function writes.
    await session.refresh(
        instance,
        attribute_names=[
            "status",
            "status_message",
            "pod_name",
            "node_name",
            "last_seen_at",
            "updated_at",
        ],
    )
    return instance


async def _deployment_failure(
    instance: Instance, gateway: K8sGateway, namespace: str
) -> str | None:
    """Why the Deployment has produced no pod, or ``None`` if it has not said.

    Only ever called for an instance that has *no* pod, so a healthy workload never
    pays for it — which is the whole reason this is one read per pod-less instance
    rather than a listing folded into the sweep. That does mean a namespace in which
    everything is pod-less (one broken ceiling breaks every instance in it) costs one
    read per such instance per sweep instead of one per namespace. Accepted: a single
    ``GET`` is a small fraction of the pod listing beside it, the state is
    self-correcting as soon as the cause is gone, and the alternative — inventing a
    ``wl-instance`` label on the Deployment and listing those too — buys a constant
    factor in a case that only exists while something is already wrong.

    A read that fails is *not* escalated: the instance keeps whatever its pods said.
    "Could not ask" is not evidence that the workload is broken.
    """
    deployment = instance.k8s_deployment
    if not deployment:
        return None
    try:
        status = await run_gateway(gateway.read_deployment_status, namespace, deployment)
    except K8sError as exc:
        logger.warning(
            "deployment_status_unreadable",
            instance=instance.name,
            deployment=deployment,
            error=str(exc),
        )
        return None
    return status.failure if status else None


async def reconcile_instance(
    session: AsyncSession, instance: Instance, gateway: K8sGateway
) -> Instance:
    """Pull one instance's observed pod state into its row.

    One instance, one listing — used by the HTTP reconcile endpoint, where there is
    a single instance to ask about. The periodic sweep uses :func:`reconcile_all`,
    which serves a whole namespace from one listing instead. Both end in
    :func:`_absorb_observation`, so the two cannot drift into disagreeing about what
    a given pod means.
    """
    if not _has_a_pod(instance):
        return instance

    namespace = instance_workspace_ns(instance)
    selector = f"{INSTANCE_LABEL}={instance.name}"
    try:
        pods = await run_gateway(gateway.list_pods, namespace, selector)
    except K8sError as exc:
        return await _mark_query_failed(session, instance, exc)

    failure = await _deployment_failure(instance, gateway, namespace) if not pods else None
    return await _absorb_observation(session, instance, pods, failure)


async def reconcile_all(session: AsyncSession, gateway: K8sGateway, *, limit: int = 200) -> int:
    """Reconcile every live instance, **commit** the result, and report how many moved.

    Returns the number of instances whose status actually changed, not the number
    examined. That is the meaning :func:`app.services.tasks.reconcile_all` already
    had, and it is the only one that lets a caller's log line — or the CLI's printed
    summary — be true. Counting "examined" is how ``app/cli.py`` came to print
    "实例 N 个状态变化" over a sweep that changed nothing.

    The commit is the point of this function, not a detail of it. Everything it
    calls only flushes, and every caller opened a session with
    ``async with factory() as session:`` — whose exit *closes* the session, and
    closing rolls back an uncommitted transaction. So every sweep read the cluster
    correctly, wrote the answer, and threw it away: instance statuses stayed
    ``creating`` forever, and the runtime cap in :func:`expire_overdue` was never
    enforced even once. The task reconciler looked healthy only because
    :func:`app.services.tasks.reconcile_all` happens to commit for itself.

    So a batch entry point owns its own commit. A caller that wants to compose
    this into a longer transaction should use :func:`reconcile_instance` directly,
    which stays flush-only on purpose.

    The sweep lists pods once per *namespace*, not once per instance. That turns the
    cost of a sweep from "one API server round trip per running instance, each one
    re-serialising a pod list that Python then rebuilds object by object" into "one
    per namespace in use" — a much smaller number, and one that grows far more
    slowly than the number of instances. Pods are handed back to their rows by the
    ``wl-instance`` label, which is an unambiguous key here: a workspace's
    ``k8s_namespace`` is unique, and so is an instance name within a workspace.

    The one thing the sweep still asks about per *instance* is why a pod-less
    instance has no pod — see :func:`_deployment_failure`. A healthy instance never
    reaches that call.
    """
    rows = list(
        (
            await session.scalars(
                select(Instance)
                .options(*_SWEEP_LOAD)
                .where(
                    Instance.is_active.is_(True),
                    Instance.status.in_(LIVE_STATUSES),
                )
                .order_by(Instance.id)
                .limit(limit)
            )
        ).all()
    )

    by_namespace: dict[str, list[Instance]] = {}
    for instance in rows:
        if _has_a_pod(instance):
            by_namespace.setdefault(instance_workspace_ns(instance), []).append(instance)

    changed = 0
    for namespace, group in by_namespace.items():
        try:
            pods = await run_gateway(gateway.list_pods, namespace, MANAGED_LABEL_SELECTOR)
        except K8sError as exc:
            # One namespace failing to list is not the sweep failing: every other
            # instance still gets observed. The message is written per instance
            # because the instance's own page is where it is read.
            for instance in group:
                await _mark_query_failed(session, instance, exc)
            continue

        by_instance: dict[str, list[PodInfo]] = {}
        for pod in pods:
            # Managed, but not an instance: a task Job's pod. It belongs to a row in
            # another table, and this sweep has nothing to say about it.
            if pod.instance_name is not None:
                by_instance.setdefault(pod.instance_name, []).append(pod)

        for instance in group:
            before = instance.status
            # Grouping preserved the newest-first order `list_pods` returns, so
            # `pods[0]` is still the pod a single-instance listing would have picked.
            pods = by_instance.get(instance.name, [])
            # A pod-less instance is the one case an empty listing cannot explain on
            # its own: "not started yet" and "never going to start" look identical.
            failure = await _deployment_failure(instance, gateway, namespace) if not pods else None
            await _absorb_observation(session, instance, pods, failure)
            if instance.status != before:
                changed += 1

    await session.commit()
    return changed


async def expire_overdue(session: AsyncSession, gateway: K8sGateway, *, limit: int = 200) -> int:
    """Stop instances whose deadline has passed, and **commit** the stop.

    Enforced here rather than only in the UI: a runtime cap the user can ignore
    by leaving a tab open is not a cap.

    The commit matters for the same reason as in :func:`reconcile_all`, and the
    consequence of its absence was worse: a stop that is rolled back leaves the
    deployment scaled to zero in the cluster while the row still says ``running``,
    so the next sweep sees no pods, concludes the instance is still coming up, and
    the instance never stops and never expires.
    """
    now = datetime.now(UTC)
    rows = list(
        (
            await session.scalars(
                select(Instance)
                .options(*_SWEEP_LOAD)
                .where(
                    Instance.is_active.is_(True),
                    Instance.expires_at.is_not(None),
                    Instance.expires_at <= now,
                    Instance.status.in_((InstanceStatus.RUNNING, InstanceStatus.CREATING)),
                )
                .order_by(Instance.id)
                .limit(limit)
            )
        ).all()
    )

    stopped = 0
    for instance in rows:
        try:
            await stop_instance(session, instance, gateway)
        except K8sError as exc:
            logger.error("instance_expiry_stop_failed", instance=instance.name, error=str(exc))
            continue
        instance.status_message = "已达最长运行时长，已自动停止"
        await session.flush()
        stopped += 1

    await session.commit()
    if stopped:
        logger.info("instances_expired", count=stopped)
    return stopped


# ---------------------------------------------------------------------------
# Connection details
# ---------------------------------------------------------------------------
def ssh_command(instance: Instance, settings: Settings) -> str | None:
    """The exact command a user should paste, or ``None`` when unusable."""
    if not instance.ssh_enabled or not instance.ssh_node_port:
        return None
    host = settings.instance_ssh_host
    if not host:
        # Better to state the port and let the user supply the host than to
        # invent a hostname that will not resolve.
        host = "<节点地址>"
    owner = instance.owner
    username = owner.username if owner else "dev"
    return f"ssh -p {instance.ssh_node_port} {username}@{host}"


def ide_path(instance: Instance) -> str | None:
    """Where a browser should open this instance's IDE, or ``None`` if it has none.

    A path on the *platform's own* origin — the frontend proxies ``/x`` to the
    backend and the backend forwards to the instance's ClusterIP — rather than an
    absolute URL on some other host. That is the whole point of the proxy route:
    the user stays on one origin, so nothing has to be reconfigured per cluster
    and no extra hostname has to resolve.

    The landing page is appended here rather than left to the IDE's own redirect,
    because "where the IDE happens to send ``/``" is a per-product detail. Jupyter
    serves its UI at ``lab``; the terminal and code-server are already at their
    root.
    """
    if instance.ide_type is IdeType.NONE:
        return None
    # `owner` is non-optional on the model (the foreign key guarantees it), and
    # the relationship is eager-loaded, so this is not a lazy access on a closed
    # session. `ssh_command` reads the same relationship.
    base = ide_base_path(instance.owner.username, instance.id)
    if instance.ide_type is IdeType.JUPYTER:
        return f"{base}lab"
    return base


def to_summary(instance: Instance) -> InstanceSummary:
    return InstanceSummary.from_instance(instance)


__all__ = [
    "LIVE_STATUSES",
    "RUNNING_STATUSES",
    "SSH_PORT",
    "Instance",
    "InstanceObjects",
    "Page",
    "Usage",
    "add_ssh_key",
    "apply_cluster_state",
    "authorized_keys",
    "create_instance",
    "delete_instance",
    "delete_ssh_key",
    "expire_overdue",
    "get_instance",
    "ide_path",
    "is_managed_workload",
    "list_instances",
    "list_ssh_keys",
    "reconcile_all",
    "reconcile_instance",
    "resolve_owner",
    "resolve_pool_and_spec",
    "ssh_command",
    "ssh_fingerprint",
    "start_instance",
    "status_from_pod",
    "stop_instance",
    "to_summary",
    "update_instance",
    "workspace_for_actor",
]
