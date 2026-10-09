"""Storage volumes: creation, attachment, resizing and reconciliation.

The service exists because a volume has a life of its own. It is created before
anything mounts it, it survives every instance that did, and it must be
reconciled even when nothing is using it.

Three rules run through everything here:

**A volume is a directory.** The layout under ``settings.storage_root`` is decided
outside the platform — by whoever provisions the filesystem — so creation *names*
an existing directory rather than allocating one. Every path is derived by
:mod:`app.services.storage_paths`, and ``host_path`` is unique: two rows claiming
one directory would be two volumes sharing an identity, and which one a pod got
would depend on which row happened to win.

**A volume is never deleted while it is attached.** Deleting the claim would
orphan the pods still mounting it, and Kubernetes would keep them running with a
dangling volume until they restarted. Detachment is therefore a visible, separate
step, and the refusal names the instances involved.

**Creation is an administrator's act.** Placing a directory is a decision with
consequences past one user — a group's own directory exposes every member's home —
so it is not something a user may do to themselves, and it is gated on
``storage:manage``. Ordinary users read; they do not place.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

from sqlalchemy import ColumnElement, Select, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import policy
from app.core.config import Settings
from app.core.errors import BadRequestError, ConflictError, NotFoundError
from app.core.logging import get_logger
from app.k8s import K8sError, StorageObjects, build_storage_objects, run_gateway
from app.k8s.gateway import K8sGateway, PvcInfo, PvInfo, WorkloadRefs
from app.k8s.naming import storage_pv_name
from app.models.enums import (
    InstanceStatus,
    StorageKind,
    StorageStatus,
    WorkloadEventType,
    WorkloadKind,
)
from app.models.group import Group
from app.models.instance import Instance
from app.models.storage import InstanceStorage, Storage
from app.models.user import User
from app.models.workspace import Workspace
from app.services import storage_paths, workload_events
from app.services.workload_common import Page

logger = get_logger(__name__)

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

    Visibility follows the kind, because the kind is what the volume *is*:

    * a user's volume belongs to its owner and to nobody else — sharing a group
      with somebody does not make their home directory readable;
    * a **named** group share reaches the whole group, which is the point of it;
    * a group's **own** directory reaches only the people who administer that
      group, because it contains every member's home and "the group can see it"
      is exactly the wrong answer for a directory of private homes;
    * a global share reaches everyone, which is what makes it global.
    """
    scope = policy.resolve_scope(actor)
    if scope.all_access:
        return None

    # `scope.group_ids` holds the groups this account *administers*, which is not
    # the same as the groups it belongs to. Sourcing "shared with my group" from
    # it made every group share invisible to the ordinary members it exists for.
    # Membership is the right source; the administrative grant is unioned in so an
    # administrator still sees everything they govern.
    groups = frozenset(actor.group_ids) | scope.group_ids
    # Groups the actor administers — a strictly narrower set, used for the group's
    # own directory, where membership is deliberately not enough.
    administered = frozenset(scope.group_ids)

    own = (Storage.kind == StorageKind.USER_VOLUME.value) & (Storage.owner_user_id == actor.id)
    named_share = (
        (Storage.kind == StorageKind.GROUP_SHARE.value)
        & Storage.dir_name.is_not(None)
        & Storage.group_id.in_(groups)
    )
    group_directory = (
        (Storage.kind == StorageKind.GROUP_SHARE.value)
        & Storage.dir_name.is_(None)
        & Storage.group_id.in_(administered)
    )
    global_share = Storage.kind == StorageKind.GLOBAL_SHARE.value
    return own | named_share | group_directory | global_share


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
    kind: StorageKind = StorageKind.USER_VOLUME,
    workspace_id: int | None = None,
    owner_user_id: int | None = None,
    capacity_gb: int = 10,
    access_mode: str | None = None,
    is_read_only: bool | None = None,
    source: str | None = None,
    group_id: int | None = None,
    dir_name: str | None = None,
) -> Storage:
    """Register a directory as a managed volume, and create the cluster objects.

    ``workspace_id`` is accepted so a caller that already knows the workspace does
    not have to be rewritten, but it is not *trusted*: the workspace comes from the
    group. The namespace that holds the claim must be the one the group's workloads
    run in, and honouring a caller-supplied workspace would put the claim somewhere
    no pod of theirs could ever mount it.
    """
    name = _validate_name(name)
    capacity_gb = _validate_capacity(capacity_gb)

    # Placing a directory is an administrator's act. A group's own volume exposes
    # every member's home, and a user's volume decides where their code lives. The
    # path comes from an installation-wide layout rather than from the volume's
    # owner, and a wrong one is not self-correcting: the PV is `DirectoryOrCreate`,
    # so it would quietly materialise the wrong directory instead of failing.
    policy.require_permission(actor, "storage:manage")

    kind = StorageKind(str(kind))
    group, owner, workspace = await _resolve_binding(
        session, actor, kind=kind, group_id=group_id, owner_user_id=owner_user_id
    )
    if workspace_id is not None and workspace is not None and workspace.id != workspace_id:
        raise BadRequestError(
            f"存储卷必须建立在用户组 {group.name} 的工作空间上",  # type: ignore[union-attr]
            code="storage_workspace_mismatch",
        )

    host_path = storage_paths.volume_path(
        settings,
        kind,
        group_name=group.name if group is not None else None,
        username=owner.username if owner is not None else None,
        dir_name=dir_name,
        name=name,
    )

    storage = Storage(
        name=name,
        display_name=display_name or name,
        description=description,
        kind=kind,
        status=StorageStatus.PENDING,
        host_path=host_path,
        dir_name=dir_name if kind is StorageKind.GROUP_SHARE else None,
        # Null for a global share: it belongs to no workspace, and its claims are
        # created per namespace by whatever mounts it.
        workspace_id=workspace.id if workspace is not None else None,
        group_id=group.id if group is not None else None,
        owner_user_id=owner.id if owner is not None else None,
        created_by=actor.id,
        capacity_gb=capacity_gb,
        access_mode=access_mode or _default_access_mode(kind),
        is_read_only=bool(is_read_only),
        source=source,
    )
    session.add(storage)
    try:
        await session.flush()
    except IntegrityError as exc:
        # The unique indexes are the arbiter, not a pre-flight check: two
        # concurrent creates both pass a check and only one can win the insert.
        # Translating afterwards means the loser gets a message naming which rule
        # it broke, instead of a 500.
        await session.rollback()
        raise await _explain_conflict(session, host_path=host_path, name=name) from exc

    # Populate the `lazy="joined"` relationships from what we already fetched.
    #
    # A *newly constructed* row has no loaded relationships, and eager loading only
    # applies to rows that come back from a query. Touching `storage.group` during
    # serialisation would therefore trigger a lazy load from sync context and raise
    # `MissingGreenlet`. Assigning here costs nothing — the objects are in hand —
    # and removes that whole class of failure for this path.
    storage.workspace = workspace
    storage.group = group
    storage.owner = owner
    await session.flush()

    objects = _objects_for(storage, workspace)
    storage.pvc_name = objects.pvc.name if objects.pvc is not None else None
    storage.k8s_namespace = objects.pvc.namespace if objects.pvc is not None else None

    try:
        # The volume first, then the claim: a claim pinned by name to a volume
        # that does not exist yet is a claim that reports `Pending` with no reason.
        await run_gateway(gateway.apply_pv, objects.pv)
        if objects.pvc is not None:
            await run_gateway(gateway.apply_pvc, objects.pvc)
    except K8sError as exc:
        # The row survives with a FAILED status and a message rather than being
        # rolled back. An administrator whose volume failed needs to see *what*
        # failed and be able to retry; a vanished row tells them nothing.
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
        message=f"创建存储卷 {storage.name}（{host_path}，{storage.capacity_gb} GB）",
        actor=actor,
    )
    await session.flush()
    return storage


async def _explain_conflict(session: AsyncSession, *, host_path: str, name: str) -> ConflictError:
    """Turn a unique-index violation into the sentence that explains it.

    Built from the row that won rather than from the request, so the message can
    name the volume already holding the directory — which is the piece of
    information an administrator actually needs in order to decide what to do.
    """
    taken = await session.scalar(
        select(Storage.name).where(Storage.host_path == host_path, Storage.is_active.is_(True))
    )
    if taken is not None:
        return ConflictError(
            f"目录 {host_path} 已经是存储卷「{taken}」的目录，一个目录只能对应一个存储卷",
            code="storage_path_taken",
        )
    return ConflictError(f"该工作空间下已存在存储卷 {name}", code="storage_name_taken")


async def _resolve_binding(
    session: AsyncSession,
    actor: User,
    *,
    kind: StorageKind,
    group_id: int | None,
    owner_user_id: int | None,
) -> tuple[Group | None, User | None, Workspace | None]:
    """Which group, user and workspace a volume of this kind binds to.

    Required rather than defaulted, because the directory is *derived* from the
    binding: a user volume with no user has no directory at all, and one naming a
    user from another group would derive a path that group does not own.
    """
    if kind is StorageKind.GLOBAL_SHARE:
        # Belongs to nobody, so there is nothing to check and no namespace to
        # hold a claim.
        return None, None, None

    if kind is StorageKind.USER_VOLUME:
        if owner_user_id is None:
            raise BadRequestError(
                "用户存储卷必须指定所属用户（ownerUserId）", code="storage_requires_owner"
            )
        owner = await session.get(User, owner_user_id)
        if owner is None:
            raise NotFoundError("指定的用户不存在", code="user_not_found")
        # The group comes from the user, not from the request: the directory is
        # `{root}/{group}/{username}`, so letting the caller name a different group
        # would put one group's user inside another group's directory.
        group = owner.primary_group
        if group is None:
            raise BadRequestError(
                f"用户 {owner.username} 不属于任何用户组，无法确定存储目录",
                code="storage_owner_has_no_group",
            )
    else:
        if group_id is None:
            raise BadRequestError(
                "组共享存储卷必须指定用户组（groupId）", code="storage_requires_group"
            )
        group = await session.get(Group, group_id)
        if group is None:
            raise NotFoundError("指定的用户组不存在", code="group_not_found")
        owner = None

    policy.ensure_group_access(actor, group.id)
    return group, owner, await _workspace_for_group(session, group)


async def _workspace_for_group(session: AsyncSession, group: Group) -> Workspace:
    """The one workspace a group's storage lives in.

    One group has one workspace, and its namespace is what holds the volume's
    claim. A group with none cannot own storage — the claim would have nowhere to
    live and no pod could mount it — and a group with several is refused rather
    than guessed at, because silently picking one would decide which namespace
    gets the data.
    """
    rows = list(
        (await session.scalars(select(Workspace).where(Workspace.group_id == group.id))).unique()
    )
    if not rows:
        raise BadRequestError(
            f"用户组 {group.name} 还没有工作空间，存储卷的声明无处存放",
            code="group_has_no_workspace",
        )
    if len(rows) > 1:
        raise ConflictError(
            f"用户组 {group.name} 有 {len(rows)} 个工作空间，存储卷无法确定归属",
            code="group_has_multiple_workspaces",
        )
    return rows[0]


def _default_access_mode(kind: StorageKind) -> str:
    """Pick the narrowest mode that still works.

    A user's volume is theirs alone, and ``ReadWriteOnce`` is the narrowest mode
    that still lets several of their own pods share it — every workload they start
    mounts the home directory, so it does need sharing, just not widely. The share
    kinds are meant for many readers at once, which requires ``ReadWriteMany``; a
    driver that refuses it must say so rather than be silently downgraded.
    """
    if StorageKind(str(kind)) is StorageKind.USER_VOLUME:
        return "ReadWriteOnce"
    return "ReadWriteMany"


def _objects_for(storage: Storage, workspace: Workspace | None) -> StorageObjects:
    return build_storage_objects(
        storage_id=storage.id,
        storage_name=storage.name,
        host_path=storage.host_path,
        # A global share has no workspace and therefore no namespace to hold a
        # claim: its claims are created per namespace by whatever mounts it.
        namespace=workspace.k8s_namespace if workspace is not None else None,
        workspace_labels=(
            {"wl-workspace": workspace.k8s_namespace} if workspace is not None else {}
        ),
        size_gb=storage.capacity_gb,
        access_mode=storage.access_mode,
        kind=str(storage.kind),
    )


# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------
async def _observe(
    session: AsyncSession, storage: Storage, gateway: K8sGateway, *, commit: bool = True
) -> Storage:
    """Read the volume back and record what the cluster actually says.

    Both objects are read even though only one of them decides the status. The
    PersistentVolume is what knows which *directory* this is, and the whole promise
    of the feature is that the row and the directory agree — a disagreement is
    exactly what reconciliation exists to notice, and it is invisible from the
    claim alone.
    """
    try:
        volume = await run_gateway(gateway.read_pv, storage_pv_name(storage.id, storage.name))
        claim = None
        if storage.pvc_name and storage.k8s_namespace:
            claim = await run_gateway(gateway.read_pvc, storage.k8s_namespace, storage.pvc_name)
    except K8sError as exc:
        storage.status = StorageStatus.FAILED
        storage.status_message = exc.message
        if commit:
            await session.flush()
        return storage

    _absorb(storage, claim=claim, volume=volume)
    # Written by every reconcile because it is the rotation key: leaving it null
    # pins the same first page of rows in the queue forever and the rest are never
    # examined at all.
    storage.checked_at = datetime.now(UTC)
    if commit:
        await session.flush()
    return storage


def _absorb(storage: Storage, *, claim: PvcInfo | None, volume: PvInfo | None) -> None:
    """Fold what the cluster says into the row.

    Capacity is deliberately *not* taken from the cluster. A hostPath volume's size
    is bookkeeping — nothing enforces it — so "the cluster says it is bigger than we
    recorded" means somebody edited the cluster, not that the user gained capacity.
    Copying it upward would silently inflate what the platform believes it has
    handed out, which is the opposite of what an accounting field is for.
    """
    # The directory is the one fact that must not drift: everything else is derived
    # from it, so if this is wrong nothing downstream can be right.
    if volume is not None and volume.host_path and volume.host_path != storage.host_path:
        storage.status = StorageStatus.FAILED
        storage.status_message = (
            f"集群中该卷指向 {volume.host_path}，与记录的目录 {storage.host_path} 不一致"
        )
        return

    if storage.pvc_name and storage.k8s_namespace:
        _absorb_claim(storage, claim)
    else:
        _absorb_volume(storage, volume)


def _absorb_claim(storage: Storage, info: PvcInfo | None) -> None:
    if info is None:
        # The row exists but the claim does not. Either it was deleted outside the
        # platform or the create never landed; either way the volume is not usable
        # and claiming PENDING forever would be a lie.
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


def _absorb_volume(storage: Storage, info: PvInfo | None) -> None:
    """Status for a volume with no claim of its own, such as a global share."""
    if info is None:
        storage.status = StorageStatus.FAILED
        storage.status_message = "PersistentVolume 已不存在，可能被集群外部删除"
        return

    phase = (info.phase or "").lower()
    if phase == "bound":
        storage.status = StorageStatus.BOUND
        storage.status_message = None
    elif phase in {"available", "pending", ""}:
        # The resting state of a volume nothing has claimed yet. Not a fault: a
        # global share waits here until the first workload mounts it.
        storage.status = StorageStatus.AVAILABLE
        storage.status_message = None
    elif phase == "released":
        storage.status = StorageStatus.RELEASED
        storage.status_message = "卷已释放，等待重新绑定"
    else:
        storage.status = StorageStatus.FAILED
        storage.status_message = f"PersistentVolume 处于 {info.phase} 状态"


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
    """Only somebody with the manage permission may change a volume.

    Not the owner — ownership governs the *mount*, not the record. A user's volume
    is theirs to use, and every workload they start mounts it, but it is not theirs
    to rename, resize or retire: the directory is part of an installation-wide
    layout the storage administrator owns, so changing what a volume points at is
    an administrator's decision rather than a tenant's.
    """
    policy.require_permission(actor, "storage:manage")


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
        # No kind is inherently read-only any more, so this is purely an
        # administrator's choice — the way a share that is being retired is frozen
        # without taking it away from the people still reading it.
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
    *,
    capacity_gb: int,
) -> Storage:
    """Raise a volume's recorded capacity.

    Only the *record* moves, and that is not a shortcut: a statically provisioned
    claim cannot be expanded at all — the API server refuses with "only
    dynamically provisioned pvc can be resized and the storageclass that provisions
    the pvc must support resize", which was verified against the cluster rather than
    assumed — and a hostPath volume's size is not enforced by anything anyway. This
    number is what the platform accounts for and reports; the capacity declared in
    the cluster is an artefact of the request that made the claim bind, and stays
    where it was.

    Grow-only, because this feeds quota accounting. Lowering what a tenant was
    promised could put a group over budget with nothing to show for it, and the
    honest way to give a directory back is to retire the volume.
    """
    await _ensure_may_manage(actor, storage)
    _validate_capacity(capacity_gb)

    if capacity_gb < storage.capacity_gb:
        raise BadRequestError(
            f"存储容量只能增加，不能减少（当前 {storage.capacity_gb} GB）",
            code="storage_shrink_unsupported",
        )
    if capacity_gb == storage.capacity_gb:
        return storage

    previous = storage.capacity_gb
    storage.capacity_gb = capacity_gb
    storage.status_message = None
    await workload_events.record(
        session,
        kind=WorkloadKind.STORAGE,
        workload_id=storage.id,
        event_type=WorkloadEventType.UPDATED,
        message=f"调整记账容量 {previous} → {capacity_gb} GB（集群中的声明容量不变）",
        actor=actor,
    )
    await session.flush()
    return storage


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
    """Retire a volume: delete its cluster objects, mark the row released.

    Deliberately a *hard* delete of the cluster objects and a *soft* delete of the
    row. The volume must actually stop existing — a retired volume still occupying a
    directory is the failure this exists to prevent — but the record is what lets an
    administrator answer "what happened to that volume?" afterwards. The partial
    unique index is what lets the directory be handed to somebody else while the
    record stays.

    The directory itself is never touched. The reclaim policy the client pins is
    ``Retain``, so this gives up the platform's claim on the data rather than the
    data — the only sane reading of "delete a volume" when the volume *is* somebody's
    home directory.
    """
    await _ensure_may_manage(actor, storage)
    await _ensure_detached(session, storage)

    try:
        # Claim first, then volume. Deleting the volume out from under a live claim
        # leaves the claim reporting `Lost`, and the pods mounting it fail in a way
        # that reads like corruption rather than like an intentional removal.
        if storage.k8s_namespace and storage.pvc_name:
            gateway.delete_workload(storage.k8s_namespace, WorkloadRefs(pvc=storage.pvc_name))
        # By a name derived from the row rather than from the recorded fields, so a
        # volume whose registration only partly succeeded is still cleaned up. A
        # global share never had a claim, and this is the only object it owns.
        await run_gateway(gateway.delete_pv, storage_pv_name(storage.id, storage.name))
    except K8sError as exc:
        raise BadRequestError(
            f"删除集群存储失败：{exc.message}", code="storage_delete_failed"
        ) from exc

    storage.is_active = False
    storage.status = StorageStatus.RELEASED
    storage.status_message = "已删除，目录中的数据保留"
    # Cleared so the row stops claiming cluster objects that no longer exist.
    storage.pvc_name = None
    storage.k8s_namespace = None
    await workload_events.record(
        session,
        kind=WorkloadKind.STORAGE,
        workload_id=storage.id,
        event_type=WorkloadEventType.DELETED,
        message=f"删除存储卷 {storage.name}（{storage.host_path}），目录中的数据保留",
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
    """Volumes the actor may choose to attach in this workspace.

    A user's own volume is absent on purpose: it is not a choice, it is mounted
    everywhere they run. What remains are the shares, which a user opts into.

    A global share is offered regardless of workspace — it belongs to none — while a
    group share is offered only where it has a claim, which is the group's own
    workspace.
    """
    statement = (
        select(Storage)
        .where(Storage.is_active.is_(True))
        .where(Storage.kind != StorageKind.USER_VOLUME.value)
        .where(
            (Storage.kind == StorageKind.GLOBAL_SHARE.value)
            | (Storage.workspace_id == workspace_id)
        )
        .order_by(Storage.kind, Storage.name)
    )
    condition = visible_condition(actor)
    if condition is not None:
        statement = statement.where(condition)
    return list((await session.scalars(statement)).unique())


def _administers(scope: policy.DataScope, group_id: int | None) -> bool:
    """Whether the actor may mount a group's own directory.

    A group administrator, or somebody with installation-wide access. The rule
    exists to stop a *member* from reading their colleagues' homes, and a global
    administrator is not a member.

    "Group administrator" is read from the *scope*, which is the same source
    ``visible_condition`` uses — and it has to be. Group-admin authority comes from
    two places (the flag on the membership, and a group-scoped ``group_admin`` role
    assignment) and ``resolve_scope`` honours the union deliberately. Consulting
    only ``admin_group_ids`` here would let an account *see* a group-level volume
    and then be refused when mounting it: visible in the list, a 400 on use, with
    nothing in the message to explain the difference.

    The scope is passed in rather than derived here so the caller resolves it once
    for a whole batch of volumes.
    """
    if scope.all_access:
        return True
    return group_id is not None and group_id in scope.group_ids


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

    # Both of these depend only on the actor, so they are resolved once for the
    # whole batch rather than per volume.
    condition = visible_condition(actor)
    scope = policy.resolve_scope(actor)

    resolved: list[Storage] = []
    for storage_id in unique:
        storage = by_id.get(storage_id)
        if storage is None:
            raise NotFoundError("存储不存在", code="storage_not_found")

        # Visibility is checked *first*, before any check that describes the
        # volume. Every later message ("it is disabled", "it has no claim here")
        # confirms that the volume exists, and a caller who cannot see it must not
        # learn that it does.
        if condition is not None:
            allowed = await session.scalar(
                select(Storage.id).where(Storage.id == storage_id, condition)
            )
            if allowed is None:
                raise NotFoundError("存储不存在", code="storage_not_found")

        if not storage.is_active:
            raise BadRequestError(f"存储 {storage.name} 已停用，无法挂载", code="storage_inactive")
        if storage.kind is StorageKind.USER_VOLUME:
            raise BadRequestError(
                "用户存储卷不能手动挂载，它会被自动挂载到容器中",
                code="storage_user_volume_implicit",
            )
        # Seeing a volume and mounting it are different privileges, and this is the
        # one place the difference exists: the group's own directory contains every
        # member's home.
        #
        # As it stands this branch is *unreachable*: `visible_condition` grants a
        # group-level volume exactly to `scope.group_ids`, and `_administers` allows
        # exactly that set (plus `all_access`, where visibility returns no condition
        # at all) — so passing the check above implies passing this one. A plain
        # member is refused earlier, with `NotFound` rather than this message.
        #
        # Kept deliberately as a second line of defence, because the two rules
        # answer different questions ("may you know this exists?" vs "may you read
        # every home in this group?") and the failure mode if visibility is ever
        # widened is silent disclosure of colleagues' files. That is worth a guard
        # that currently never fires. If you are here because you widened
        # `visible_condition`: this is the check that saves you, and you should add
        # the test that reaches it.
        if storage.is_group_level and not _administers(scope, storage.group_id):
            raise BadRequestError(
                f"存储 {storage.name} 是用户组本级目录，只有组管理员可以挂载",
                code="storage_group_level_requires_admin",
            )
        # The claim lives in exactly one namespace and a pod can only mount claims
        # from its own. Checked here because the scheduler's version of this failure
        # is a container stuck in ContainerCreating with no useful message. A global
        # share reaches this branch until a claim exists in this namespace.
        if storage.k8s_namespace != workspace.k8s_namespace:
            raise BadRequestError(
                f"存储 {storage.name} 在命名空间 {workspace.k8s_namespace} 中还没有存储声明，"
                "无法挂载",
                code="storage_claim_missing",
            )
        resolved.append(storage)

    # Raises before anything is created, so a bad set never gets half-applied.
    resolve_mount_paths(resolved)
    return resolved


def resolve_mount_paths(storages: list[Storage]) -> list[tuple[Storage, str]]:
    """Pair each volume with the path it will occupy inside the container.

    The first share of a kind takes the kind's default path; later ones of the same
    kind go *under* it, because two volumes cannot occupy one directory. Which
    volume gets the default therefore depends on the whole set rather than on the
    volume — so the result is recorded on the attachment instead of being recomputed
    later, when the set may have changed and the path would move under a running pod.
    """
    counts: dict[str, int] = {}
    seen: set[str] = set()
    assigned: list[tuple[Storage, str]] = []
    for storage in storages:
        base = storage.default_mount_path
        index = counts.get(base, 0)
        counts[base] = index + 1
        path = base if index == 0 else f"{base}/{storage.name}"
        if path in seen:
            raise BadRequestError(
                f"存储 {storage.name} 的挂载路径 {path} 与其他存储冲突",
                code="storage_mount_conflict",
            )
        seen.add(path)
        assigned.append((storage, path))
    return assigned


def to_mounts(storages: list[Storage]) -> list[dict[str, object]]:
    """Turn volumes into the mount descriptors the workload builder expects."""
    return [
        {
            "storage_id": storage.id,
            "mount_path": mount_path,
            "read_only": storage.is_read_only,
            "pvc_name": storage.pvc_name,
            "name": storage.name,
            "kind": str(storage.kind),
        }
        for storage, mount_path in resolve_mount_paths(storages)
    ]


async def record_attachments(
    session: AsyncSession, instance_id: int, storages: list[Storage]
) -> None:
    """Persist the attachment rows after an instance is created.

    The *resolved* path is stored rather than the kind's default: several shares of
    one kind can be attached at once and only one of them gets the default path, so
    the default alone would not say where the others landed.
    """
    for storage, mount_path in resolve_mount_paths(storages):
        session.add(
            InstanceStorage(
                instance_id=instance_id,
                storage_id=storage.id,
                mount_path=mount_path,
                is_read_only=storage.is_read_only,
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
