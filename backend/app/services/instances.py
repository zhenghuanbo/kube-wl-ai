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

from app.auth import policy
from app.core.config import Settings
from app.core.errors import BadRequestError, ConflictError, NotFoundError, QuotaExceededError
from app.core.logging import get_logger
from app.k8s import InstanceObjects, K8sError, K8sGateway, run_gateway
from app.k8s.gateway import VolumeMountSpec, WorkloadRefs
from app.k8s.naming import INSTANCE_LABEL, WORKSPACE_LABEL, is_managed_workload
from app.k8s.workloads import SSH_PORT, build_instance_objects, ingress_path
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
_PHASE_TO_STATUS: dict[str, InstanceStatus] = {
    "running": InstanceStatus.RUNNING,
    "pending": InstanceStatus.CREATING,
    "failed": InstanceStatus.FAILED,
    "succeeded": InstanceStatus.STOPPED,
    "unknown": InstanceStatus.ERROR,
}


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
    stmt = select(Instance).order_by(Instance.created_at.desc(), Instance.id.desc())
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
    instance = await session.get(Instance, instance_id)
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
    settings: Settings,
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

    storage_gb = payload.storage_gb or settings.instance_default_storage_gb
    pool_storage_cap = pool.limits.max_storage_gb_per_user if pool.limits else None
    if pool_storage_cap and storage_gb > pool_storage_cap:
        raise BadRequestError(
            f"存储超出资源组上限（{pool_storage_cap} GB）", code="storage_exceeds_pool_limit"
        )

    max_runtime = _resolve_runtime(payload.max_runtime_seconds, pool)

    quotas = await workspace_quotas(session, workspace.id)
    usage = await workspace_usage(session, workspace.id)
    check_quota(
        quotas,
        usage,
        cpu_milli=spec.cpu_milli,
        memory_mb=spec.memory_mb,
        storage_gb=storage_gb,
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
        storage_gb=storage_gb,
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
    await storage_service.record_attachments(session, instance.id, attached)

    await apply_cluster_state(session, instance, workspace, pool, settings, gateway)

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
    settings: Settings,
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

    # Read the attachments from the database rather than taking them as an
    # argument: this function is also the repair path, and a reconcile that
    # forgot the mounts would silently produce an instance missing its data.
    attachments = await storage_service.list_attachments(session, instance.id)
    extra_mounts = tuple(
        VolumeMountSpec(
            pvc_name=row.storage.pvc_name,
            mount_path=row.mount_path,
            read_only=row.is_read_only,
        )
        for row in attachments
        if row.storage is not None and row.storage.pvc_name
    )
    if len(extra_mounts) != len(attachments):
        # Not silent. An attachment whose claim has gone would otherwise just stop
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
        storage_gb=instance.storage_gb,
        ssh_enabled=instance.ssh_enabled,
        authorized_keys=keys,
        node_selector=pool_node_selector(pool),
        tolerations=tuple(pool.k8s_tolerations or ()),
        ingress_host=settings.instance_ingress_host,
        storage_class=pool.storage_class or settings.storage_class_home or None,
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
    """Push the objects, recording their names as they are accepted."""
    refs = objects.refs

    if objects.ssh_secret is not None:
        await run_gateway(gateway.apply_secret, objects.ssh_secret)
    if objects.pvc is not None:
        await run_gateway(gateway.apply_pvc, objects.pvc)

    await run_gateway(gateway.apply_deployment, replace(objects.deployment, replicas=replicas))

    node_port: int | None = instance.ssh_node_port
    if objects.ssh_service is not None:
        applied = await run_gateway(gateway.apply_service, objects.ssh_service)
        # The cluster picks the port, so read it back rather than assume the
        # one that was requested (which is usually none).
        for port in applied.ports:
            if port.node_port:
                node_port = port.node_port
    if objects.ide_service is not None:
        await run_gateway(gateway.apply_service, objects.ide_service)
    if objects.ingress is not None:
        await run_gateway(gateway.apply_ingress, objects.ingress)

    instance.k8s_deployment = refs.deployment
    instance.k8s_ssh_service = objects.ssh_service.name if objects.ssh_service else None
    instance.k8s_ide_service = objects.ide_service.name if objects.ide_service else None
    instance.k8s_ingress = refs.ingress
    instance.k8s_pvc = refs.pvc
    instance.k8s_secret = refs.secret
    instance.ssh_node_port = node_port if instance.ssh_enabled else None
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
async def reconcile_instance(
    session: AsyncSession, instance: Instance, gateway: K8sGateway
) -> Instance:
    """Pull the observed pod state into the row.

    Only instances that are supposed to have a pod are inspected. A stopped
    instance has no pod by design, and asking about it would flip it to ``error``
    on every sweep.
    """
    if not instance.is_active or instance.status in (
        InstanceStatus.STOPPED,
        InstanceStatus.DELETING,
    ):
        return instance

    namespace = instance_workspace_ns(instance)
    if not instance.k8s_deployment:
        return instance

    selector = f"{INSTANCE_LABEL}={instance.name}"
    try:
        pods = await run_gateway(gateway.list_pods, namespace, selector)
    except K8sError as exc:
        instance.status_message = f"状态查询失败：{exc}"
        await session.flush()
        return instance

    if not pods:
        if instance.status in (InstanceStatus.RUNNING, InstanceStatus.CREATING):
            instance.status = InstanceStatus.CREATING
            instance.status_message = "等待容器启动"
        await session.flush()
        return instance

    pod = pods[0]
    previous = instance.status
    instance.pod_name = pod.name
    instance.node_name = pod.node_name
    instance.last_seen_at = datetime.now(UTC)

    if pod.phase.lower() == "running" and pod.ready:
        instance.status = InstanceStatus.RUNNING
        instance.status_message = None
    else:
        instance.status = _PHASE_TO_STATUS.get(pod.phase.lower(), InstanceStatus.CREATING)
        instance.status_message = pod.message or f"容器状态：{pod.phase}"

    if previous != instance.status:
        logger.info(
            "instance_status_changed",
            instance=instance.name,
            previous=previous.value,
            current=instance.status.value,
        )
    await session.flush()
    await session.refresh(instance)
    return instance


async def reconcile_all(session: AsyncSession, gateway: K8sGateway, *, limit: int = 200) -> int:
    """Reconcile every live instance. Used by the periodic worker and by hand."""
    rows = list(
        (
            await session.scalars(
                select(Instance)
                .where(
                    Instance.is_active.is_(True),
                    Instance.status.in_(LIVE_STATUSES),
                )
                .order_by(Instance.id)
                .limit(limit)
            )
        ).all()
    )
    for instance in rows:
        await reconcile_instance(session, instance, gateway)
    return len(rows)


async def expire_overdue(session: AsyncSession, gateway: K8sGateway, *, limit: int = 200) -> int:
    """Stop instances whose deadline has passed.

    Enforced here rather than only in the UI: a runtime cap the user can ignore
    by leaving a tab open is not a cap.
    """
    now = datetime.now(UTC)
    rows = list(
        (
            await session.scalars(
                select(Instance)
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
    """The IDE path, or ``None`` when the instance serves no web IDE."""
    if instance.ide_type is IdeType.NONE:
        return None
    return ingress_path(instance.id)


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
    "stop_instance",
    "to_summary",
    "update_instance",
    "workspace_for_actor",
]
