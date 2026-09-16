"""Storage volumes: creation, attachment, resizing and reconciliation.

The service exists because a volume has a life of its own. It is created before
anything mounts it, it survives every instance that did, and it must be
reconciled even when nothing is using it — a claim that went ``Pending`` because
its StorageClass is ``WaitForFirstConsumer`` has no pod to notice it.

Two rules run through everything here:

**A volume is never deleted while it is attached.** Deleting the claim would
orphan the pods still mounting it, and Kubernetes would keep them running with a
dangling volume until they restarted. Detachment is therefore a visible,
separate step, and the refusal names the instances involved.

**Capacity only grows.** Kubernetes cannot shrink a claim, so accepting a shrink
would put the row and the cluster permanently out of step. The rejections here
mirror the API server rather than papering over it.
"""

from __future__ import annotations

import re

from sqlalchemy import ColumnElement, Select, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import policy
from app.core.config import Settings
from app.core.errors import BadRequestError, ConflictError, NotFoundError
from app.core.logging import get_logger
from app.k8s import K8sError, StorageObjects, build_storage_objects, run_gateway
from app.k8s.gateway import K8sGateway, PvcInfo
from app.models.enums import (
    InstanceStatus,
    StorageKind,
    StorageStatus,
    WorkloadEventType,
    WorkloadKind,
)
from app.models.instance import Instance
from app.models.storage import InstanceStorage, Storage
from app.models.user import User
from app.models.workspace import Workspace
from app.services import workload_events
from app.services.workload_common import Page, workspace_for_actor

logger = get_logger(__name__)

#: Kinds an ordinary user may create for themselves.
#:
#: A dataset is curated and a group share is agreed between colleagues, so both
#: are administrator actions. Letting anyone publish a read-only "dataset" would
#: make the label meaningless, and the whole value of the read-only guarantee is
#: that it means something.
SELF_SERVICE_KINDS: frozenset[str] = frozenset(
    {StorageKind.PERSONAL_HOME.value, StorageKind.PERSONAL_DATA.value}
)

#: Lower bound on a volume, so a typo of "0" is refused rather than accepted.
MIN_CAPACITY_GB = 1
MAX_CAPACITY_GB = 65536

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")

#: Statuses in which an instance holds a mount and must not be disturbed.
ATTACHED_INSTANCE_STATUSES: frozenset[InstanceStatus] = frozenset(
    {
        InstanceStatus.PENDING,
        InstanceStatus.CREATING,
        InstanceStatus.RUNNING,
        InstanceStatus.STOPPING,
        InstanceStatus.STOPPED,
    }
)


def _validate_name(name: str) -> str:
    cleaned = name.strip()
    if not _NAME_RE.match(cleaned) or len(cleaned) > 40:
        raise BadRequestError(
            "名称只能包含小写字母、数字和连字符，且不超过 40 个字符",
            code="invalid_storage_name",
        )
    return cleaned


def _validate_capacity(capacity_gb: int) -> int:
    if capacity_gb < MIN_CAPACITY_GB or capacity_gb > MAX_CAPACITY_GB:
        raise BadRequestError(
            f"容量必须在 {MIN_CAPACITY_GB}–{MAX_CAPACITY_GB} GB 之间",
            code="invalid_capacity",
        )
    return capacity_gb


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------
def visible_condition(actor: User) -> ColumnElement[bool] | None:
    """The rows the actor may see, as a bare SQL condition.

    ``None`` means "everything", which is what an administrator gets.

    This is a condition rather than a filtered ``Select`` so callers can attach
    it to a query they have already built. Returning a whole statement forced
    every caller to reach into ``.whereclause`` — which is typed as optional and
    would silently drop the filter if it were ever empty, turning a permission
    rule into a no-op.

    Visibility is expressed here rather than through ``policy.scope_filter``
    because it depends on the *kind*: a group share is readable by the whole
    group, while a colleague's personal volume is not, and a row-level rule
    keyed only on ``group_id`` cannot tell those two apart.
    """
    scope = policy.resolve_scope(actor)
    if scope.all_access:
        return None

    # A home or personal volume is visible only to its owner.
    own = Storage.owner_user_id == actor.id
    # Anything shared or curated is visible to the actor's groups, and to anyone
    # who is a member of a workspace holding it.
    #
    # `scope.group_ids` holds the groups this account *administers*, which is not
    # the same as the groups it belongs to. Sourcing "shared with my group" from
    # it made every group share invisible to the ordinary members it exists for.
    # Membership is the right source; the administrative grant is unioned in so a
    # group administrator still sees everything they govern.
    groups = frozenset(actor.group_ids) | scope.group_ids
    shared_kinds = [StorageKind.GROUP_SHARE.value, StorageKind.DATASET.value]
    shared = Storage.kind.in_(shared_kinds) & (
        Storage.group_id.in_(groups) | Storage.workspace_id.in_(scope.workspace_ids)
    )
    return own | shared


def _apply_filters(
    statement: Select[tuple[Storage]],
    *,
    workspace_id: int | None = None,
    kind: StorageKind | None = None,
    status: StorageStatus | None = None,
    search: str | None = None,
    owned_only: bool = False,
    actor: User | None = None,
    include_inactive: bool = False,
) -> Select[tuple[Storage]]:
    if workspace_id is not None:
        statement = statement.where(Storage.workspace_id == workspace_id)
    if kind is not None:
        statement = statement.where(Storage.kind == kind)
    if status is not None:
        statement = statement.where(Storage.status == status)
    if search:
        pattern = f"%{search.strip()}%"
        statement = statement.where(
            Storage.name.ilike(pattern)
            | Storage.display_name.ilike(pattern)
            | Storage.description.ilike(pattern)
        )
    if owned_only and actor is not None:
        statement = statement.where(Storage.owner_user_id == actor.id)
    if not include_inactive:
        statement = statement.where(Storage.is_active.is_(True))
    return statement


async def list_storages(
    session: AsyncSession,
    actor: User,
    *,
    workspace_id: int | None = None,
    kind: StorageKind | None = None,
    status: StorageStatus | None = None,
    search: str | None = None,
    owned_only: bool = False,
    include_inactive: bool = False,
    page: int = 1,
    page_size: int = 20,
) -> Page[Storage]:
    condition = visible_condition(actor)
    base = select(Storage)
    if condition is not None:
        base = base.where(condition)
    statement = _apply_filters(
        base,
        workspace_id=workspace_id,
        kind=kind,
        status=status,
        search=search,
        owned_only=owned_only,
        actor=actor,
        include_inactive=include_inactive,
    ).order_by(Storage.kind, Storage.name)

    total = await session.scalar(
        select(func.count()).select_from(statement.order_by(None).subquery())
    )
    rows = await session.scalars(statement.offset((page - 1) * page_size).limit(page_size))
    return Page(items=list(rows.unique()), total=int(total or 0))


async def get_storage(session: AsyncSession, actor: User, storage_id: int) -> Storage:
    """Fetch one volume, or 404.

    404 rather than 403 for anything out of reach: a 403 would confirm that a
    colleague's private volume exists.
    """
    storage = await session.get(Storage, storage_id)
    if storage is None:
        raise NotFoundError("存储不存在", code="storage_not_found")

    condition = visible_condition(actor)
    if condition is not None:
        allowed = await session.scalar(
            select(Storage.id).where(Storage.id == storage_id, condition)
        )
        if allowed is None:
            raise NotFoundError("存储不存在", code="storage_not_found")
    return storage


# ---------------------------------------------------------------------------
# Creation
# ---------------------------------------------------------------------------
async def create_storage(
    session: AsyncSession,
    actor: User,
    gateway: K8sGateway,
    settings: Settings,
    *,
    name: str,
    display_name: str | None = None,
    description: str | None = None,
    kind: StorageKind = StorageKind.PERSONAL_DATA,
    workspace_id: int | None = None,
    owner_user_id: int | None = None,
    capacity_gb: int = 10,
    access_mode: str | None = None,
    storage_class: str | None = None,
    is_read_only: bool | None = None,
    source: str | None = None,
    group_id: int | None = None,
) -> Storage:
    name = _validate_name(name)
    capacity_gb = _validate_capacity(capacity_gb)

    workspace = await workspace_for_actor(session, actor, workspace_id)

    if str(kind) in SELF_SERVICE_KINDS:
        # Only an administrator may publish something that claims to be shared.
        owner = await _resolve_owner(session, actor, owner_user_id)
    else:
        policy.require_permission(actor, "storage:manage")
        owner = await _resolve_owner(session, actor, owner_user_id)
        if str(kind) == StorageKind.DATASET.value and not source:
            # Provenance is the difference between a dataset and a directory
            # somebody made up. Without it the next person cannot judge whether
            # to trust it.
            raise BadRequestError(
                "数据集必须填写来源（source），以便使用者判断可用性",
                code="dataset_source_required",
            )

    if str(kind) == StorageKind.PERSONAL_HOME.value:
        await _ensure_single_home(session, workspace.id, owner.id)

    target_group_id = group_id if group_id is not None else workspace.group_id
    if group_id is not None:
        policy.ensure_group_access(actor, group_id)

    storage = Storage(
        name=name,
        display_name=display_name or name,
        description=description,
        kind=kind,
        status=StorageStatus.PENDING,
        workspace_id=workspace.id,
        group_id=target_group_id,
        owner_user_id=owner.id,
        created_by=actor.id,
        capacity_gb=capacity_gb,
        access_mode=access_mode or _default_access_mode(kind),
        storage_class=storage_class or _default_storage_class(settings, kind),
        # Read-only is a property of the kind; the caller may only add to it.
        is_read_only=bool(is_read_only) or str(kind) == StorageKind.DATASET.value,
        source=source,
    )
    session.add(storage)
    try:
        await session.flush()
    except IntegrityError as exc:
        # The unique constraint on (workspace_id, name) is the real arbiter, not
        # any pre-flight check: two concurrent requests for the same name both
        # pass a check and only one can win the insert. Translating it here means
        # the loser gets a 409 naming the problem instead of a 500.
        await session.rollback()
        raise ConflictError(f"该工作空间下已存在存储 {name}", code="storage_name_taken") from exc

    # Populate the `lazy="joined"` relationships from what we already fetched.
    #
    # A *newly constructed* row has no loaded relationships, and `joined` eager
    # loading only applies to rows that come back from a query. Touching
    # `storage.workspace` during serialisation would therefore trigger a lazy
    # load from sync context and raise `MissingGreenlet` — the same trap that
    # `Workspace.group` hit in Phase 3. Assigning here costs nothing (the objects
    # are in hand) and removes the whole class of failure for this path.
    storage.workspace = workspace
    storage.group = workspace.group if target_group_id == workspace.group_id else None
    storage.owner = owner
    await session.flush()

    objects = _objects_for(storage, workspace)
    storage.pvc_name = objects.pvc.name
    storage.k8s_namespace = objects.pvc.namespace

    try:
        await run_gateway(gateway.apply_pvc, objects.pvc)
    except K8sError as exc:
        # The row survives with a FAILED status and a message rather than being
        # rolled back. A user whose claim failed needs to see *what* failed and
        # be able to retry; a vanished row tells them nothing.
        storage.status = StorageStatus.FAILED
        storage.status_message = exc.message
        logger.warning("storage_create_failed", storage=storage.id, error=exc.message)
    else:
        await _observe(session, storage, gateway, commit=False)

    await workload_events.record(
        session,
        kind=WorkloadKind.STORAGE,
        workload_id=storage.id,
        event_type=WorkloadEventType.CREATED,
        message=f"创建存储 {storage.name}（{storage.capacity_gb} GB）",
        actor=actor,
    )
    await session.flush()
    return storage


async def _ensure_single_home(session: AsyncSession, workspace_id: int, owner_id: int) -> None:
    """Refuse a second personal home for the same person.

    Two homes would reintroduce exactly the ambiguity that makes a container
    disposable: nobody could say which one holds the code.
    """
    existing = await session.scalar(
        select(Storage.id).where(
            Storage.workspace_id == workspace_id,
            Storage.owner_user_id == owner_id,
            Storage.kind == StorageKind.PERSONAL_HOME.value,
            Storage.is_active.is_(True),
        )
    )
    if existing is not None:
        raise ConflictError(
            "你已经有一个个人主目录存储，不能重复创建",
            code="storage_home_exists",
        )


def _default_storage_class(settings: Settings, kind: StorageKind) -> str | None:
    """Pick the configured class for the volume's purpose.

    Separate settings because the requirements genuinely differ: a home volume
    wants a class that can be expanded in place, while a dataset shared by a
    whole lab usually needs different backing entirely.
    """
    mapping = {
        StorageKind.PERSONAL_HOME: settings.storage_class_home,
        StorageKind.GROUP_SHARE: settings.storage_class_share,
        StorageKind.DATASET: settings.storage_class_share,
    }
    return mapping.get(StorageKind(str(kind))) or settings.storage_class_data or None


def _default_access_mode(kind: StorageKind) -> str:
    """Pick the narrowest mode that still works.

    A personal volume is only ever mounted by one instance, so
    ``ReadWriteOnce`` is both sufficient and the mode every driver supports.
    Datasets and shares are meant for many readers at once, which requires
    ``ReadWriteMany`` — the driver may refuse it, and that refusal must surface
    rather than be silently downgraded.
    """
    if str(kind) in {StorageKind.DATASET.value, StorageKind.GROUP_SHARE.value}:
        return "ReadWriteMany"
    return "ReadWriteOnce"


async def _resolve_owner(session: AsyncSession, actor: User, owner_user_id: int | None) -> User:
    if owner_user_id is None or owner_user_id == actor.id:
        return actor
    # Naming somebody else as the owner is an administrative act.
    policy.require_permission(actor, "storage:manage")
    owner = await session.get(User, owner_user_id)
    if owner is None:
        raise NotFoundError("指定的用户不存在", code="user_not_found")
    return owner


def _objects_for(storage: Storage, workspace: Workspace) -> StorageObjects:
    return build_storage_objects(
        storage_id=storage.id,
        storage_name=storage.name,
        namespace=workspace.k8s_namespace,
        workspace_labels={"wl-workspace": workspace.k8s_namespace},
        size_gb=storage.capacity_gb,
        storage_class=storage.storage_class,
        access_mode=storage.access_mode,
        kind=str(storage.kind),
    )


# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------
async def _observe(
    session: AsyncSession, storage: Storage, gateway: K8sGateway, *, commit: bool = True
) -> Storage:
    """Read the claim back and record what the cluster actually says."""
    if not storage.k8s_namespace or not storage.pvc_name:
        return storage
    try:
        info = await run_gateway(gateway.read_pvc, storage.k8s_namespace, storage.pvc_name)
    except K8sError as exc:
        storage.status = StorageStatus.FAILED
        storage.status_message = exc.message
        if commit:
            await session.flush()
        return storage

    _absorb(storage, info)
    if commit:
        await session.flush()
    return storage


def _absorb(storage: Storage, info: PvcInfo | None) -> None:
    if info is None:
        # The row exists but the claim does not. Either it was deleted outside
        # the platform or the create never landed; either way the volume is not
        # usable and saying PENDING forever would be a lie.
        if storage.status is StorageStatus.BOUND:
            storage.status = StorageStatus.FAILED
            storage.status_message = "PersistentVolumeClaim 已不存在，可能被集群外部删除"
        return

    phase = (info.phase or "").lower()
    if phase == "bound":
        storage.status = StorageStatus.BOUND
        storage.status_message = None
    elif phase == "lost":
        storage.status = StorageStatus.FAILED
        storage.status_message = info.message or "PersistentVolumeClaim 处于 Lost 状态"
    else:
        storage.status = StorageStatus.PENDING
        storage.status_message = info.message

    # The cluster is the authority on how big the volume really is; the database
    # only records what was requested. Only ever upward: a report smaller than
    # the request means the claim is still being provisioned, not that it shrank.
    if info.capacity_gb is not None and info.capacity_gb > storage.capacity_gb:
        storage.capacity_gb = info.capacity_gb


async def reconcile_storage(
    session: AsyncSession, storage: Storage, gateway: K8sGateway
) -> Storage:
    return await _observe(session, storage, gateway)


async def reconcile_all(session: AsyncSession, gateway: K8sGateway, *, limit: int = 200) -> int:
    """Refresh every live volume. Returns how many were examined."""
    rows = await session.scalars(
        select(Storage)
        .where(Storage.is_active.is_(True))
        .where(
            Storage.status.in_(
                [
                    StorageStatus.PENDING.value,
                    StorageStatus.BOUND.value,
                    StorageStatus.RESIZING.value,
                ]
            )
        )
        .order_by(Storage.checked_at.asc().nulls_first())
        .limit(limit)
    )
    examined = 0
    for storage in rows.unique():
        await _observe(session, storage, gateway, commit=False)
        examined += 1
    if examined:
        await session.commit()
    return examined


# ---------------------------------------------------------------------------
# Mutation
# ---------------------------------------------------------------------------
async def _ensure_may_manage(actor: User, storage: Storage) -> None:
    """Owner, or somebody with the manage permission covering this row."""
    if storage.owner_user_id == actor.id and str(storage.kind) in SELF_SERVICE_KINDS:
        return
    if storage.owner_user_id == actor.id:
        return
    policy.require_permission(actor, "storage:manage")
    if storage.group_id is not None:
        policy.ensure_group_access(actor, storage.group_id)


async def update_storage(
    session: AsyncSession,
    actor: User,
    storage: Storage,
    *,
    display_name: str | None = None,
    description: str | None = None,
    is_active: bool | None = None,
    is_read_only: bool | None = None,
    source: str | None = None,
) -> Storage:
    await _ensure_may_manage(actor, storage)

    if display_name is not None:
        storage.display_name = display_name
    if description is not None:
        storage.description = description
    if source is not None:
        storage.source = source
    if is_read_only is not None:
        # A dataset cannot be made writable — that guarantee is its reason to
        # exist — so only the additive direction is accepted.
        if str(storage.kind) == StorageKind.DATASET.value and not is_read_only:
            raise BadRequestError("数据集始终只读，不能改为可写", code="dataset_always_read_only")
        storage.is_read_only = is_read_only
    if is_active is not None:
        if not is_active:
            await _ensure_detached(session, storage)
        storage.is_active = is_active
        if not is_active:
            storage.status = StorageStatus.RELEASED

    await workload_events.record(
        session,
        kind=WorkloadKind.STORAGE,
        workload_id=storage.id,
        event_type=WorkloadEventType.UPDATED,
        message="更新存储属性",
        actor=actor,
    )
    await session.flush()
    return storage


async def resize_storage(
    session: AsyncSession,
    actor: User,
    storage: Storage,
    gateway: K8sGateway,
    *,
    capacity_gb: int,
) -> Storage:
    """Grow a volume in place."""
    await _ensure_may_manage(actor, storage)
    _validate_capacity(capacity_gb)

    if capacity_gb < storage.capacity_gb:
        raise BadRequestError(
            f"存储只能扩容，不能缩小（当前 {storage.capacity_gb} GB）",
            code="storage_shrink_unsupported",
        )
    if capacity_gb == storage.capacity_gb:
        return storage
    if not storage.k8s_namespace or not storage.pvc_name:
        raise BadRequestError("存储尚未下发到集群，无法扩容", code="storage_not_provisioned")

    previous = storage.capacity_gb
    storage.status = StorageStatus.RESIZING
    try:
        await run_gateway(gateway.resize_pvc, storage.k8s_namespace, storage.pvc_name, capacity_gb)
    except K8sError as exc:
        # Leave the row at its old size: the cluster did not grow, so claiming
        # otherwise would over-report capacity that does not exist.
        storage.capacity_gb = previous
        storage.status = StorageStatus.FAILED
        storage.status_message = exc.message
        await session.flush()
        raise BadRequestError(
            "扩容失败：存储类可能不支持在线扩容", code="storage_resize_failed"
        ) from exc

    storage.capacity_gb = capacity_gb
    storage.status_message = None
    await workload_events.record(
        session,
        kind=WorkloadKind.STORAGE,
        workload_id=storage.id,
        event_type=WorkloadEventType.UPDATED,
        message=f"扩容 {previous} → {capacity_gb} GB",
        actor=actor,
    )
    await session.flush()
    return await _observe(session, storage, gateway)


async def _ensure_detached(session: AsyncSession, storage: Storage) -> None:
    """Refuse to retire a volume that an instance is still mounting."""
    holders = list(
        await session.scalars(
            select(Instance)
            .join(InstanceStorage, InstanceStorage.instance_id == Instance.id)
            .where(InstanceStorage.storage_id == storage.id)
            .where(Instance.is_active.is_(True))
            .where(Instance.status.in_(list(ATTACHED_INSTANCE_STATUSES)))
        )
    )
    if holders:
        names = "、".join(instance.name for instance in holders[:3])
        more = f" 等 {len(holders)} 个实例" if len(holders) > 3 else ""
        raise ConflictError(
            f"该存储仍被 {names}{more} 挂载，请先删除或解绑这些实例",
            code="storage_in_use",
            detail={"instanceIds": [instance.id for instance in holders]},
        )


async def delete_storage(
    session: AsyncSession,
    actor: User,
    storage: Storage,
    gateway: K8sGateway,
) -> Storage:
    """Retire a volume and delete its claim.

    Deliberately a *hard* delete of the claim and a *soft* delete of the row.
    The capacity must actually be released — a retired volume still holding a
    terabyte is the failure mode this exists to prevent — but the record is what
    lets an administrator answer "what happened to that dataset?" afterwards.
    """
    await _ensure_may_manage(actor, storage)
    await _ensure_detached(session, storage)

    if storage.k8s_namespace and storage.pvc_name:
        from app.k8s.gateway import WorkloadRefs

        try:
            gateway.delete_workload(storage.k8s_namespace, WorkloadRefs(pvc=storage.pvc_name))
        except K8sError as exc:
            raise BadRequestError(
                f"删除集群存储失败：{exc.message}", code="storage_delete_failed"
            ) from exc

    storage.is_active = False
    storage.status = StorageStatus.RELEASED
    await workload_events.record(
        session,
        kind=WorkloadKind.STORAGE,
        workload_id=storage.id,
        event_type=WorkloadEventType.DELETED,
        message=f"删除存储 {storage.name}，释放 {storage.capacity_gb} GB",
        actor=actor,
    )
    await session.flush()
    return storage


# ---------------------------------------------------------------------------
# Attachment
# ---------------------------------------------------------------------------
async def attachable_storages(
    session: AsyncSession, actor: User, workspace_id: int
) -> list[Storage]:
    """Volumes the actor could attach to an instance in this workspace."""
    statement = (
        select(Storage)
        .where(Storage.workspace_id == workspace_id)
        .where(Storage.is_active.is_(True))
        .where(Storage.kind != StorageKind.PERSONAL_HOME.value)
        .order_by(Storage.kind, Storage.name)
    )
    condition = visible_condition(actor)
    if condition is not None:
        statement = statement.where(condition)
    return list((await session.scalars(statement)).unique())


async def resolve_attachments(
    session: AsyncSession,
    actor: User,
    workspace: Workspace,
    storage_ids: list[int],
) -> list[Storage]:
    """Validate a requested set of mounts and return the volumes in order."""
    if not storage_ids:
        return []

    unique = list(dict.fromkeys(storage_ids))
    rows = list((await session.scalars(select(Storage).where(Storage.id.in_(unique)))).unique())
    by_id = {row.id: row for row in rows}

    resolved: list[Storage] = []
    for storage_id in unique:
        storage = by_id.get(storage_id)
        if storage is None:
            raise NotFoundError("存储不存在", code="storage_not_found")

        # Visibility is checked *first*, before any check that describes the
        # volume. Every later message ("it is in another workspace", "it is
        # disabled") confirms that the volume exists, and a caller who cannot
        # see it must not learn that.
        condition = visible_condition(actor)
        if condition is not None:
            allowed = await session.scalar(
                select(Storage.id).where(Storage.id == storage_id, condition)
            )
            if allowed is None:
                raise NotFoundError("存储不存在", code="storage_not_found")

        if not storage.is_active:
            raise BadRequestError(f"存储 {storage.name} 已停用，无法挂载", code="storage_inactive")
        if storage.workspace_id != workspace.id:
            # Cross-namespace mounts are the interesting failure: the claim is
            # in another namespace and the pod simply cannot see it. Caught here
            # rather than left to the scheduler, whose symptom would be a
            # container stuck in ContainerCreating with no useful message.
            raise BadRequestError(
                f"存储 {storage.name} 属于其他工作空间，无法挂载",
                code="storage_cross_workspace",
            )
        if storage.kind == StorageKind.PERSONAL_HOME.value:
            raise BadRequestError(
                "个人主目录不能手动挂载，实例会自动使用", code="storage_home_implicit"
            )
        resolved.append(storage)

    # One volume, one place. Two entries for the same volume would produce a
    # duplicate PVC volume name in the pod spec, which the API server rejects.
    paths = [s.default_mount_path for s in resolved]
    if len(set(paths)) != len(paths):
        raise BadRequestError(
            "多个存储映射到了同一个挂载路径，请分两次创建实例",
            code="storage_mount_conflict",
        )
    return resolved


def to_mounts(storages: list[Storage]) -> list[dict[str, object]]:
    """Turn volumes into the mount descriptors the workload builder expects."""
    return [
        {
            "storage_id": storage.id,
            "mount_path": storage.default_mount_path,
            "read_only": storage.effective_read_only,
            "pvc_name": storage.pvc_name,
            "name": storage.name,
            "kind": str(storage.kind),
        }
        for storage in storages
    ]


async def record_attachments(
    session: AsyncSession, instance_id: int, storages: list[Storage]
) -> None:
    """Persist the attachment rows after an instance is created."""
    for storage in storages:
        session.add(
            InstanceStorage(
                instance_id=instance_id,
                storage_id=storage.id,
                mount_path=storage.default_mount_path,
                is_read_only=storage.effective_read_only,
            )
        )
    await session.flush()


async def list_attachments(session: AsyncSession, instance_id: int) -> list[InstanceStorage]:
    return list(
        (
            await session.scalars(
                select(InstanceStorage)
                .where(InstanceStorage.instance_id == instance_id)
                .order_by(InstanceStorage.mount_path)
            )
        ).unique()
    )


async def usage_by_owner(session: AsyncSession, actor: User) -> dict[str, int]:
    """How much storage one user is actually charged for.

    Deliberately *not* :func:`visible_condition`. Seeing a volume and being
    charged for it are different things: a lab-wide dataset is visible to every
    member and owned by none of them, so counting it here would tell each of
    fifty members that they are personally consuming its hundred gigabytes.

    So this counts ownership. A global administrator, who administers the whole
    installation and has no meaningful personal total, gets the installation
    total instead — which is what they are actually looking for.
    """
    statement = select(
        func.count(Storage.id), func.coalesce(func.sum(Storage.capacity_gb), 0)
    ).where(Storage.is_active.is_(True))
    if not policy.resolve_scope(actor).all_access:
        statement = statement.where(Storage.owner_user_id == actor.id)
    count, capacity = (await session.execute(statement)).one()
    return {"count": int(count or 0), "capacityGb": int(capacity or 0)}
