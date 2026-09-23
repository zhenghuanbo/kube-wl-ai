"""The image catalogue.

The catalogue exists so that nobody has to type an image reference from memory.
That framing decides almost everything below:

**Harbor is the source of truth; this table is a cache.** A sync copies what the
registry reports. It is therefore allowed to *delete* rows the registry no longer
reports — with one exception, ``is_pinned``. A curated image disappearing from a
listing is usually a misconfigured project rather than a decision to stop
offering it, and silently dropping it from the picker would break every saved
application that referenced it.

**A reference is not an identity.** The same tag can be repushed, so a row keeps
the digest too. The picker shows the tag, because that is what people recognise.

**Registering is not the same as being allowed to use.** Anyone may add an image
to their own private list; sharing it with a group or the whole installation is
an administrative act, because a shared image is one every lab will pull.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import ColumnElement, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import policy
from app.core.config import Settings
from app.core.errors import BadRequestError, ConflictError, NotFoundError
from app.core.logging import get_logger
from app.models.application import Application
from app.models.enums import (
    ApplicationCategory,
    ImageKind,
    ImageVisibility,
    WorkloadEventType,
    WorkloadKind,
)
from app.models.group import Group
from app.models.image import Image
from app.models.instance import Instance
from app.models.task import Task
from app.models.user import User
from app.registries import RegistryClient, RegistryError, RegistryTag, parse_reference
from app.services import workload_events
from app.services.workload_common import Page

logger = get_logger(__name__)

#: How many projects one pass will walk.
#:
#: The bound exists so a registry with something wrong with it cannot make a pass
#: run unbounded. It also counts *projects*, not images: a project's repositories
#: and tags are all walked, because a cap on images would silently truncate a
#: large lab and the catalogue would look arbitrarily incomplete.
#:
#: Overrunning it is recorded as an error rather than passed over quietly, and
#: that is not cosmetic: `_retire_missing` declines to act when a pass reported
#: errors, and without the error the projects beyond the cap would read as "gone
#: from the registry" — so a truncated pass would retire the images it never
#: looked at.
MAX_PROJECTS_PER_SYNC = 500

#: The lock that keeps two catalogue syncs from running at once.
#:
#: Declared here rather than in the worker because both callers need it — the
#: scheduled task and the manual endpoint — and the API has no business importing
#: from the worker package to find it.
SYNC_LOCK_KEY = "kube-wl-ai:image-sync"
#: Long enough for any plausible pass, short enough that a worker killed mid-sync
#: does not block the next run for a day.
SYNC_LOCK_TTL_SECONDS = 1800

#: Kinds whose entries a normal user may create. A user may catalogue images for
#: themselves; "official" and "shared" are claims about what everyone else will
#: pull, and those are made by administrators.
SELF_SERVICE_KINDS = frozenset({ImageKind.PERSONAL})

#: Visibility levels a normal user may choose.
SELF_SERVICE_VISIBILITIES = frozenset({ImageVisibility.PRIVATE, ImageVisibility.WORKSPACE})

#: The one Harbor project that does not belong to a group.
#:
#: The platform's registry layout is *one project per group*, with the owner's
#: username as the repository's first segment — ``lab-vision/zhangsan/my-env``.
#: ``library`` is the exception: the installation's own space, holding the
#: official base images every account may pull and the entries an administrator
#: curates for everyone. Harbor project names are lowercase slugs
#: (``[a-z0-9][a-z0-9._-]*``), which is exactly what ``Group.name`` already is,
#: so a group's project is its name — not ``directory_name``, which may hold a
#: capitalised directory ``cn`` that no registry would accept.
OFFICIAL_PROJECT = "library"

#: First repository segment reserved for a group's agreed images, as opposed to
#: one member's own (``lab-vision/share/cuda-toolkit``).
SHARED_REPOSITORY_PREFIX = "share"


@dataclass(slots=True)
class SyncResult:
    """What one cataloguing pass changed, for the admin page and the log."""

    created: int = 0
    updated: int = 0
    unchanged: int = 0
    removed: int = 0
    failed: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return self.created + self.updated + self.unchanged


# ---------------------------------------------------------------------------
# Visibility
# ---------------------------------------------------------------------------
def visible_condition(actor: User) -> ColumnElement[bool] | None:
    """Rows this account may read, or ``None`` for "everything".

    A bare condition rather than a filtered ``Select``, for the same reason as
    the storage service: visibility here depends on the *kind* of row, so a
    single row-level scope cannot express it.

    The rules, widest first:

    * a global administrator sees everything;
    * anything ``GLOBAL`` is public to every signed-in account — that is the
      point of marking it so, and it is what makes the default catalogue usable
      by a brand-new user who belongs to nothing yet;
    * ``GROUP`` is visible within its group;
    * ``WORKSPACE`` is visible within its workspace;
    * otherwise only the owner, and ``OFFICIAL`` images are treated as global
      because an official image belongs to the installation, not to a person.
    """
    scope = policy.resolve_scope(actor)
    if scope.all_access:
        return None

    clauses: list[ColumnElement[bool]] = [
        Image.visibility == ImageVisibility.GLOBAL.value,
        Image.kind == ImageKind.OFFICIAL.value,
        Image.owner_user_id == actor.id,
    ]
    # `scope.group_ids` holds the groups this account *administers*, which is not
    # the same as the groups it belongs to. Sourcing "shared with my group" from
    # it made every group-shared image invisible to the ordinary members it was
    # shared with — the exact people it exists for. Membership is the right
    # source; the administrative grant is unioned in so a group administrator
    # still sees everything they govern.
    groups = frozenset(actor.group_ids) | scope.group_ids
    if groups:
        clauses.append(
            (Image.visibility == ImageVisibility.GROUP.value) & Image.group_id.in_(groups)
        )
    if scope.workspace_ids:
        clauses.append(
            (Image.visibility == ImageVisibility.WORKSPACE.value)
            & Image.workspace_id.in_(scope.workspace_ids)
        )
    return or_(*clauses)


def _ensure_may_publish(actor: User, kind: ImageKind, visibility: ImageVisibility) -> None:
    """Guard the two fields that make an image affect other people.

    Sharing is the interesting one: a private image is the caller's own problem,
    but a shared image is one every lab will pull, and an official image is one
    the platform is implicitly vouching for.
    """
    if kind not in SELF_SERVICE_KINDS:
        policy.require_permission(actor, "image:share")
    if visibility not in SELF_SERVICE_VISIBILITIES:
        policy.require_permission(actor, "image:share")


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------
async def list_images(
    session: AsyncSession,
    actor: User,
    *,
    category: ApplicationCategory | None = None,
    kind: ImageKind | None = None,
    visibility: ImageVisibility | None = None,
    framework: str | None = None,
    search: str | None = None,
    owned_only: bool = False,
    include_inactive: bool = False,
    page: int = 1,
    page_size: int = 20,
) -> Page[Image]:
    statement = select(Image)
    count_statement = select(func.count(Image.id))

    condition = visible_condition(actor)
    if condition is not None:
        statement = statement.where(condition)
        count_statement = count_statement.where(condition)

    if not include_inactive:
        statement = statement.where(Image.is_active.is_(True))
        count_statement = count_statement.where(Image.is_active.is_(True))
    if owned_only:
        statement = statement.where(Image.owner_user_id == actor.id)
        count_statement = count_statement.where(Image.owner_user_id == actor.id)
    if kind is not None:
        statement = statement.where(Image.kind == kind.value)
        count_statement = count_statement.where(Image.kind == kind.value)
    if visibility is not None:
        statement = statement.where(Image.visibility == visibility.value)
        count_statement = count_statement.where(Image.visibility == visibility.value)
    if category is not None:
        statement = statement.where(Image.category == category.value)
        count_statement = count_statement.where(Image.category == category.value)
    if framework:
        statement = statement.where(Image.framework == framework)
        count_statement = count_statement.where(Image.framework == framework)
    if search:
        # Match the human name and the reference: people search for "pytorch"
        # and for "2.4-cuda12", and either should find the same row.
        pattern = f"%{search.lower()}%"
        clause = or_(
            func.lower(Image.display_name).like(pattern),
            func.lower(Image.repository).like(pattern),
            func.lower(Image.tag).like(pattern),
            func.lower(Image.framework).like(pattern),
        )
        statement = statement.where(clause)
        count_statement = count_statement.where(clause)

    total = int(await session.scalar(count_statement) or 0)
    # Official first, then most-used, then newest: the picker should lead with
    # what a new user most likely wants.
    statement = (
        statement.order_by(
            (Image.kind == ImageKind.OFFICIAL.value).desc(),
            Image.usage_count.desc(),
            Image.id.desc(),
        )
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    rows = list((await session.scalars(statement)).unique())
    return Page(items=rows, total=total)


async def get_image(session: AsyncSession, actor: User, image_id: int) -> Image:
    """Load one image, 404 rather than 403 when it is out of scope.

    A 403 would confirm that the image exists, which is itself information the
    caller is not entitled to.
    """
    statement = select(Image).where(Image.id == image_id)
    condition = visible_condition(actor)
    if condition is not None:
        statement = statement.where(condition)
    image = (await session.scalars(statement)).unique().one_or_none()
    if image is None:
        raise NotFoundError("镜像不存在", code="image_not_found")
    return image


async def find_by_reference(session: AsyncSession, reference: str) -> Image | None:
    """Look up a catalogue row by the string a workload carries.

    ``Instance.image`` is a plain string, so matching it back to a catalogue row
    means reconstructing the reference the same way the catalogue does.
    """
    try:
        parts = parse_reference(reference)
    except RegistryError:
        return None
    return (
        (
            await session.scalars(
                select(Image).where(
                    Image.registry_host == parts["host"],
                    Image.project == parts["project"],
                    Image.repository == parts["repository"],
                    Image.tag == parts["tag"],
                )
            )
        )
        .unique()
        .one_or_none()
    )


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------
async def register_image(
    session: AsyncSession,
    actor: User,
    client: RegistryClient,
    settings: Settings,
    *,
    reference: str,
    display_name: str = "",
    description: str | None = None,
    category: ApplicationCategory = ApplicationCategory.OTHER,
    kind: ImageKind = ImageKind.PERSONAL,
    visibility: ImageVisibility = ImageVisibility.PRIVATE,
    workspace_id: int | None = None,
    group_id: int | None = None,
    framework: str | None = None,
    framework_version: str | None = None,
    python_version: str | None = None,
    cuda_version: str | None = None,
    os_version: str | None = None,
    is_pinned: bool = False,
) -> Image:
    """Add one reference to the catalogue, confirming it with the registry first."""
    _ensure_may_publish(actor, kind, visibility)

    parts = parse_reference(reference, default_host=client.host)
    project_group = await _ensure_project_managed(
        session,
        actor,
        project=parts["project"],
        repository=parts["repository"],
        kind=kind,
    )
    if project_group is not None:
        # The project is authoritative about the group. A caller asking for a
        # different one is asking for a row whose group and project disagree —
        # exactly the state the scheme exists to prevent.
        if group_id is not None and group_id != project_group.id:
            raise BadRequestError(
                f"镜像位于项目 {parts['project']}，不属于所选用户组",
                code="image_project_group_mismatch",
                detail={
                    "project": parts["project"],
                    "projectGroupId": project_group.id,
                    "requestedGroupId": group_id,
                },
            )
        group_id = project_group.id
    group_id = _ensure_placeable(actor, visibility, workspace_id, group_id)

    existing = await session.scalars(
        select(Image).where(
            Image.registry_host == parts["host"],
            Image.project == parts["project"],
            Image.repository == parts["repository"],
            Image.tag == parts["tag"],
        )
    )
    if existing.unique().one_or_none() is not None:
        raise ConflictError(
            f"镜像 {parts['repository']}:{parts['tag']} 已在目录中", code="image_exists"
        )

    # Ask the registry before storing anything. Cataloguing a reference that does
    # not exist is how a typo becomes an `ImagePullBackOff` ten minutes into a
    # job — exactly the failure this catalogue exists to prevent.
    digest = parts["digest"]
    size_bytes: int | None = None
    layer_count: int | None = None
    sync_error: str | None = None
    # Ask by digest when the caller gave one: a digest reference has no tag, and
    # querying for `latest` would reject a perfectly valid pinned image.
    lookup = parts["digest"] or parts["tag"]
    try:
        found = await _get_tag(client, parts["project"], parts["repository"], lookup)
    except RegistryError as exc:
        raise BadRequestError(
            f"无法在镜像仓库中确认该镜像：{exc.message}", code="image_not_verified"
        ) from exc
    if found is None:
        raise BadRequestError(
            f"镜像仓库中不存在 {parts['repository']}:{parts['tag']}",
            code="image_not_found_upstream",
        )
    digest = found.digest or digest
    size_bytes = found.size_bytes
    layer_count = found.layer_count

    image = Image(
        registry_host=parts["host"],
        project=parts["project"],
        repository=parts["repository"],
        tag=parts["tag"],
        digest=digest,
        display_name=display_name or f"{parts['repository']}:{parts['tag']}",
        description=description,
        category=category.value,
        kind=kind.value,
        visibility=visibility.value,
        workspace_id=workspace_id,
        group_id=group_id,
        owner_user_id=actor.id,
        created_by=actor.id,
        framework=framework,
        framework_version=framework_version,
        python_version=python_version,
        cuda_version=cuda_version,
        os_version=os_version,
        size_bytes=size_bytes,
        layer_count=layer_count,
        synced_at=datetime.now(UTC),
        sync_error=sync_error,
        is_pinned=is_pinned,
    )
    session.add(image)
    await session.flush()

    # See the note in `storages.create_storage`: a freshly added row has no
    # loaded `lazy="joined"` relationships, and serialising it would trigger a
    # lazy load from sync context.
    image.owner = actor
    await session.flush()

    await workload_events.record(
        session,
        kind=WorkloadKind.IMAGE,
        workload_id=image.id,
        event_type=WorkloadEventType.CREATED,
        message=f"登记镜像 {image.reference}",
        actor=actor,
    )
    logger.info("image_registered", image=image.reference, actor=actor.username)
    return image


def _ensure_placeable(
    actor: User,
    visibility: ImageVisibility,
    workspace_id: int | None,
    group_id: int | None,
) -> int | None:
    """A shared image must say *where* it is shared.

    Without this, ``visibility=group`` with ``group_id=NULL`` matches nothing and
    the image becomes invisible to everyone including its owner — a silent
    failure that looks exactly like the image was never created.

    Returns the group to record, which may have been inferred. The caller has to
    use the return value: inferring a group and then discarding it reproduces the
    very bug this function exists to prevent.
    """
    if visibility == ImageVisibility.GROUP:
        if group_id is None:
            # Inferring is only safe when the answer is not in doubt. An
            # administrator who belongs to several groups must say which one.
            groups = sorted(frozenset(actor.group_ids))
            if len(groups) != 1:
                raise BadRequestError(
                    "共享到用户组的镜像必须指定用户组", code="image_group_required"
                )
            group_id = groups[0]
        elif not policy.has_permission(actor, "image:share"):
            policy.ensure_group_access(actor, group_id)
    if visibility == ImageVisibility.WORKSPACE and workspace_id is None:
        raise BadRequestError(
            "共享到工作空间的镜像必须指定工作空间", code="image_workspace_required"
        )
    return group_id


async def _ensure_project_managed(
    session: AsyncSession,
    actor: User,
    *,
    project: str,
    repository: str,
    kind: ImageKind,
) -> Group | None:
    """Check a reference against the platform's registry layout.

    The layout is one Harbor project per platform group, with the owner's
    username as the repository's first segment::

        library/pytorch:2.4-cuda12            official base images
        lab-vision/share/cuda-toolkit:12.4   the group's agreed images
        lab-vision/zhangsan/my-env:v3         one member's own image

    The convention is enforced here because Harbor cannot enforce it. Harbor's
    RBAC stops at the project — a ``Developer`` may create *any* repository in
    it, including one named after a colleague — so there is no registry-side
    setting that says "this member may push here but not there". Rejecting a
    mismatched prefix at the door is what keeps ownership legible, and it is
    what would let each group be handed a robot scoped to its own prefix later.

    Returns the group the project belongs to, or ``None`` for ``library``.
    Raises rather than silently recording a reference nobody will be able to
    account for.
    """
    if project == OFFICIAL_PROJECT:
        # The installation's own space. Deliberately exempt from the username
        # rule: `library/pytorch` is nobody's personal image, and the catalogue
        # entries pointing at it are curated by administrators, which
        # `_ensure_may_publish` has already checked.
        return None

    group = await session.scalar(select(Group).where(Group.name == project))
    if group is None:
        raise BadRequestError(
            f"镜像仓库项目 {project} 不对应任何用户组；项目名必须是用户组名或 {OFFICIAL_PROJECT}",
            code="image_project_unknown",
            detail={"project": project, "officialProject": OFFICIAL_PROJECT},
        )
    # Membership is enough — this is "may I keep my images here", not "may I
    # administer this group". Raises NotFound for an outsider so group names
    # cannot be probed.
    policy.ensure_group_visible(actor, group.id)

    if kind == ImageKind.PERSONAL:
        owner = repository.split("/", 1)[0]
        if owner != actor.username:
            raise BadRequestError(
                f"个人镜像的仓库名必须以自己的用户名开头"
                f"（{actor.username}/…），当前为 {repository}",
                code="image_prefix_mismatch",
                detail={
                    "expectedPrefix": actor.username,
                    "repository": repository,
                    "hint": f"{project}/{actor.username}/{repository.split('/', 1)[-1]}",
                },
            )
    return group


async def update_image(
    session: AsyncSession,
    actor: User,
    image: Image,
    *,
    display_name: str | None = None,
    description: str | None = None,
    category: ApplicationCategory | None = None,
    framework: str | None = None,
    framework_version: str | None = None,
    python_version: str | None = None,
    cuda_version: str | None = None,
    os_version: str | None = None,
    is_active: bool | None = None,
    is_pinned: bool | None = None,
    visibility: ImageVisibility | None = None,
    kind: ImageKind | None = None,
) -> Image:
    _ensure_may_manage(actor, image)

    if visibility is not None and visibility != ImageVisibility(image.visibility):
        _ensure_may_publish(actor, kind or ImageKind(image.kind), visibility)
        # Widening a group share without saying which group leaves it matching
        # nothing. Keep the existing one if there is one.
        if visibility == ImageVisibility.GROUP and image.group_id is None:
            groups = sorted(frozenset(actor.group_ids))
            if len(groups) != 1:
                raise BadRequestError(
                    "共享到用户组的镜像必须指定用户组", code="image_group_required"
                )
            image.group_id = groups[0]
        image.visibility = visibility
    if kind is not None and kind.value != image.kind:
        _ensure_may_publish(actor, kind, ImageVisibility(image.visibility))
        image.kind = kind

    for attribute, value in (
        ("display_name", display_name),
        ("description", description),
        ("framework", framework),
        ("framework_version", framework_version),
        ("python_version", python_version),
        ("cuda_version", cuda_version),
        ("is_active", is_active),
        ("is_pinned", is_pinned),
    ):
        if value is not None:
            setattr(image, attribute, value)
    if category is not None:
        image.category = category

    await session.flush()
    await workload_events.record(
        session,
        kind=WorkloadKind.IMAGE,
        workload_id=image.id,
        event_type=WorkloadEventType.UPDATED,
        message=f"更新镜像 {image.reference}",
        actor=actor,
    )
    return image


def _ensure_may_manage(actor: User, image: Image) -> None:
    """Owners manage their own entries; everything else needs a curator."""
    if image.owner_user_id == actor.id:
        return
    if image.kind == ImageKind.OFFICIAL.value or image.visibility == ImageVisibility.GLOBAL.value:
        policy.require_permission(actor, "image:share")
        return
    if image.owner_user_id is None:
        # A synced entry belongs to the installation, not to a person, so it is
        # an administrator's to manage.
        policy.require_permission(actor, "image:share")
        return
    policy.ensure_owner_or_admin(actor, image.owner_user_id)


async def usage_count(session: AsyncSession, image: Image) -> int:
    """How many workloads currently reference this image.

    Counted on demand rather than trusted from the denormalised column: the
    column is a display optimisation, and a stale one must never be the reason a
    delete is allowed.
    """
    reference = image.reference
    total = 0
    for model in (Instance, Application, Task):
        total += int(
            await session.scalar(select(func.count(model.id)).where(model.image == reference)) or 0
        )
    return total


async def delete_image(
    session: AsyncSession, actor: User, image: Image, client: RegistryClient
) -> Image:
    """Remove an entry from the catalogue.

    Two separate questions, deliberately kept separate:

    * *Is anything using it?* If so, refuse. Deleting the catalogue row would
      not stop the running pods, but it would break the picker for every saved
      application that references it, and the user would have no way to tell why.
    * *Should the registry tag go too?* Only for entries the platform itself
      created. An official base image is referenced by things this catalogue
      cannot see, and deleting a shared tag is not a decision the platform gets
      to make on a user's behalf.

    The row is retired rather than removed, so history survives and a later sync
    can revive it.
    """
    _ensure_may_manage(actor, image)

    if image.is_pinned:
        raise BadRequestError("该镜像为管理员保留项，请先取消保留", code="image_pinned")

    in_use = await usage_count(session, image)
    if in_use:
        raise ConflictError(
            f"该镜像仍被 {in_use} 个工作负载引用，无法移除",
            code="image_in_use",
            detail={"usageCount": in_use},
        )

    if image.kind == ImageKind.PERSONAL.value:
        try:
            await _delete_tag(client, image.project, image.repository, image.tag)
        except RegistryError as exc:
            # A tag already gone from the registry is not a reason to refuse:
            # the catalogue is a cache, and the desired end state is reached.
            if not exc.not_found:
                raise BadRequestError(
                    f"镜像仓库拒绝删除：{exc.message}", code="image_registry_delete_failed"
                ) from exc

    image.is_active = False
    await session.flush()
    await workload_events.record(
        session,
        kind=WorkloadKind.IMAGE,
        workload_id=image.id,
        event_type=WorkloadEventType.DELETED,
        message=f"移除镜像 {image.reference}",
        actor=actor,
    )
    return image


# ---------------------------------------------------------------------------
# Syncing from the registry
# ---------------------------------------------------------------------------
async def sync_images(
    session: AsyncSession,
    actor: User | None,
    client: RegistryClient,
    settings: Settings,
    *,
    projects: list[str] | None = None,
) -> SyncResult:
    """Refresh the catalogue from the registry.

    Creates rows for tags that are new, refreshes metadata for ones that exist,
    and retires rows the registry no longer reports — except pinned ones, which
    are kept deliberately. Every registry failure is recorded on the affected row
    as ``sync_error`` rather than aborting the pass: one broken project must not
    stop the rest of the catalogue from updating.
    """
    result = SyncResult()
    status = client.status()
    if not status.reachable:
        result.errors.append(status.message or "镜像仓库不可达")
        return result

    wanted = projects or status.projects or []
    seen: set[str] = set()
    # Resolved once: every row this pass touches must carry the same host, and a
    # client whose host changed mid-pass would otherwise write rows that look
    # like they came from different registries.
    host = client.host

    # Resolved once for the whole pass. Every synced row has to be attributed to
    # the group whose project it sits in: without this a tag someone pushed to
    # `lab-vision/zhangsan/secret-model` lands in the catalogue as a global share
    # and shows up in every lab's picker, which is the opposite of what a
    # per-group project is for.
    # `.tuples()` rather than iterating Rows: this hands back plain tuples, which
    # is what `dict()` is typed for.
    groups_by_name: dict[str, int] = dict(
        (await session.execute(select(Group.name, Group.id))).tuples().all()
    )
    users_by_name: dict[str, int] = dict(
        (await session.execute(select(User.username, User.id))).tuples().all()
    )

    if len(wanted) > MAX_PROJECTS_PER_SYNC:
        # Recorded before any work, so `_retire_missing` sees it no matter which
        # project the pass reaches before it stops.
        result.errors.append(
            f"项目数 {len(wanted)} 超过单次同步上限 {MAX_PROJECTS_PER_SYNC}，"
            f"本次只处理前 {MAX_PROJECTS_PER_SYNC} 个；因结果不完整，未执行停用。"
            "请分批传 projects 参数同步。"
        )

    for project in wanted[:MAX_PROJECTS_PER_SYNC]:
        try:
            repositories = await _repositories(client, project)
        except RegistryError as exc:
            result.errors.append(f"{project}: {exc.message}")
            continue
        for repository in repositories:
            try:
                tags = await _list_tags(client, project, repository)
            except RegistryError as exc:
                result.errors.append(f"{project}/{repository}: {exc.message}")
                continue
            for tag in tags:
                seen.add(_reference_key(tag))
                try:
                    await _absorb(
                        session,
                        host,
                        actor,
                        tag,
                        result,
                        groups_by_name=groups_by_name,
                        users_by_name=users_by_name,
                    )
                except Exception as exc:
                    result.failed += 1
                    result.errors.append(f"{project}/{repository}:{tag.tag}: {exc}")

    await session.flush()
    result.removed = await _retire_missing(session, seen, result)
    await session.flush()

    # Refresh the denormalised usage counts once, at the end, rather than per row.
    await refresh_usage_counts(session)
    await session.flush()

    logger.info(
        "images_synced",
        created=result.created,
        updated=result.updated,
        removed=result.removed,
        failed=result.failed,
    )
    return result


def _reference_key(tag: RegistryTag) -> str:
    return f"{tag.project}/{tag.repository}:{tag.tag}"


def _classify_synced(
    tag: RegistryTag,
    *,
    groups_by_name: dict[str, int],
    users_by_name: dict[str, int],
) -> dict[str, object]:
    """Decide how a freshly discovered tag should be catalogued.

    Classification follows the registry layout rather than guessing:
    ``library`` is the installation's official space and stays globally visible;
    a group's project yields rows the group can see, with the repository's first
    segment saying whether they are the group's agreed images (``share/``) or
    one member's own.

    Only ever used for *new* rows. An existing row keeps whatever an
    administrator set on it — a sync must not undo curation.
    """
    if tag.project == OFFICIAL_PROJECT:
        return {
            "kind": ImageKind.OFFICIAL.value,
            "visibility": ImageVisibility.GLOBAL.value,
            "group_id": None,
            "owner_user_id": None,
        }

    group_id = groups_by_name.get(tag.project)
    owner_segment = tag.repository.split("/", 1)[0] if "/" in tag.repository else ""
    # `share/` is reserved, so it is never treated as an owner even in the
    # unlikely event that an account carries that name.
    owner_id = (
        None if owner_segment == SHARED_REPOSITORY_PREFIX else users_by_name.get(owner_segment)
    )

    # A sync may not grant *more* visibility than registering the same reference
    # would. `register_image` only ever lets a personal image be `private` or
    # `workspace`, and only lets an administrator widen a shared one — so a
    # discovered personal image is private to its owner, and a discovered shared
    # one belongs to the group. Neither becomes a global share.
    if owner_id is not None:
        kind, visibility = ImageKind.PERSONAL.value, ImageVisibility.PRIVATE.value
    elif group_id is not None:
        kind, visibility = ImageKind.SHARED.value, ImageVisibility.GROUP.value
    else:
        # A project that is neither a group's nor `library`: a stray Harbor
        # project nobody claimed. Nothing can say who should see it, so it is
        # catalogued for administrators and offered to nobody — `private` with
        # no owner matches no clause of `visible_condition` except the
        # administrator's. Filing it `global` instead, which is what this used
        # to do, put an unclaimed project's images in every account's picker.
        kind, visibility = ImageKind.SHARED.value, ImageVisibility.PRIVATE.value

    return {
        "kind": kind,
        "visibility": visibility,
        "group_id": group_id,
        "owner_user_id": owner_id,
    }


async def _absorb(
    session: AsyncSession,
    host: str,
    actor: User | None,
    tag: RegistryTag,
    result: SyncResult,
    *,
    groups_by_name: dict[str, int],
    users_by_name: dict[str, int],
) -> None:
    """Create or refresh one row from what the registry reported."""
    statement = select(Image).where(
        Image.registry_host == host,
        Image.project == tag.project,
        Image.repository == tag.repository,
        Image.tag == tag.tag,
    )
    image = (await session.scalars(statement)).unique().one_or_none()

    if image is None:
        session.add(
            Image(
                registry_host=host,
                project=tag.project,
                repository=tag.repository,
                tag=tag.tag,
                digest=tag.digest,
                display_name=f"{tag.repository}:{tag.tag}",
                category=ApplicationCategory.OTHER.value,
                size_bytes=tag.size_bytes,
                layer_count=tag.layer_count,
                synced_at=datetime.now(UTC),
                created_by=actor.id if actor else None,
                **_classify_synced(tag, groups_by_name=groups_by_name, users_by_name=users_by_name),
            )
        )
        result.created += 1
        return

    changed = (
        image.digest != tag.digest
        or image.size_bytes != tag.size_bytes
        or image.layer_count != tag.layer_count
    )
    image.digest = tag.digest or image.digest
    image.size_bytes = tag.size_bytes if tag.size_bytes is not None else image.size_bytes
    image.layer_count = tag.layer_count if tag.layer_count is not None else image.layer_count
    image.synced_at = datetime.now(UTC)
    image.sync_error = None
    if not image.is_active:
        # It came back: a tag repushed after being retired should reappear in
        # the picker rather than staying hidden forever.
        image.is_active = True
    if changed:
        result.updated += 1
    else:
        result.unchanged += 1


async def _retire_missing(session: AsyncSession, seen: set[str], result: SyncResult) -> int:
    """Retire catalogue rows the registry no longer reports.

    Pinned rows are skipped: a curated image vanishing from a listing is usually
    a misconfigured project, and dropping it would silently break every saved
    application that referenced it.

    The guard is on whether the *pass* succeeded rather than on whether it saw
    anything. A registry that legitimately holds no repositories is not a
    failure, and refusing to retire on an empty listing would make a genuine
    cleanup impossible; but a pass that hit errors has an incomplete picture, and
    acting on an incomplete picture is what would actually be destructive.
    """
    if result.errors:
        return 0
    rows = list(
        (
            await session.scalars(
                select(Image).where(
                    Image.is_active.is_(True),
                    Image.is_pinned.is_(False),
                    Image.kind != ImageKind.PERSONAL.value,
                )
            )
        ).unique()
    )
    removed = 0
    for image in rows:
        if f"{image.project}/{image.repository}:{image.tag}" not in seen:
            image.is_active = False
            removed += 1
    return removed


async def refresh_usage_counts(session: AsyncSession) -> None:
    """Recompute the denormalised ``usage_count`` for every image.

    One query per model rather than one per image: the catalogue is small and the
    workload tables are not, so counting in bulk keeps a sync from issuing
    thousands of queries.
    """
    counts: dict[str, int] = {}
    for model in (Instance, Application, Task):
        rows = await session.execute(
            select(model.image, func.count(model.id)).group_by(model.image)
        )
        for reference, count in rows:
            counts[str(reference)] = counts.get(str(reference), 0) + int(count)

    images = list((await session.scalars(select(Image))).unique())
    for image in images:
        image.usage_count = counts.get(image.reference, 0)


def registry_status(client: RegistryClient) -> dict[str, object]:
    """Health of the registry, for the admin page's banner.

    Synchronous, like the client it wraps; callers put it on a thread.
    """
    status = client.status()
    return {
        "reachable": status.reachable,
        "version": status.version,
        "message": status.message,
        "host": client.host,
        "projects": status.projects,
    }


# ---------------------------------------------------------------------------
# Registry calls, off the event loop
#
# The registry client is synchronous, for the same reason the Kubernetes client
# is. Rather than sprinkle `to_thread` through the service, every call goes
# through one of these, so there is a single place to change.
# ---------------------------------------------------------------------------
async def _get_tag(
    client: RegistryClient, project: str, repository: str, tag: str
) -> RegistryTag | None:
    return await asyncio.to_thread(client.get_tag, project, repository, tag)


async def _delete_tag(client: RegistryClient, project: str, repository: str, tag: str) -> None:
    await asyncio.to_thread(client.delete_tag, project, repository, tag)


async def _list_tags(client: RegistryClient, project: str, repository: str) -> list[RegistryTag]:
    return await asyncio.to_thread(client.list_tags, project, repository)


async def _repositories(client: RegistryClient, project: str) -> list[str]:
    return await asyncio.to_thread(client.list_repositories, project)
