"""Application catalogue — publish a reusable instance shape and launch it.

Visibility, not row ownership, is what governs who sees what here, so this
module deliberately does **not** use ``policy.scope_filter``. An application is
meant to be shared: a curated entry is visible to the whole university, and a
lab's entry to the whole lab. Narrowing it to "rows you own" would make the
catalogue useless, and widening it to "all rows" would leak another lab's
unpublished work.

The rule is therefore written out once, in :func:`visible_clause`, and every
read goes through it.
"""

from __future__ import annotations

from sqlalchemy import ColumnElement, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import policy
from app.core.config import Settings
from app.core.errors import BadRequestError, ConflictError, NotFoundError
from app.core.logging import get_logger
from app.k8s import K8sGateway
from app.models.application import Application
from app.models.enums import ApplicationVisibility, WorkloadEventType, WorkloadKind
from app.models.group import UserGroupLink
from app.models.user import User
from app.models.workspace import Workspace, WorkspaceMember
from app.schemas.application import (
    ApplicationCreate,
    ApplicationDetail,
    ApplicationLaunch,
    ApplicationSummary,
    ApplicationUpdate,
)
from app.schemas.instance import InstanceCreate
from app.services import instances as instance_service
from app.services import workload_events as events
from app.services.workload_common import Page, workspace_for_actor

logger = get_logger(__name__)


def visible_clause(actor: User) -> ColumnElement[bool]:
    """The rows ``actor`` may see.

    Five independent ways in, OR-ed: you wrote it; it is curated (an
    administrator published it for everyone); it is ``global``; it is ``group``
    and you are in that group; or it is ``workspace`` and you are in that
    workspace. Group and workspace membership are subqueries rather than a join
    so the caller can add its own filters without worrying about duplicates.
    """
    if policy.resolve_scope(actor).all_access:
        # An administrator curates the whole catalogue, including the entries
        # other people have not published yet.
        return Application.id.is_not(None)

    return or_(
        Application.owner_user_id == actor.id,
        Application.is_curated.is_(True),
        Application.visibility == ApplicationVisibility.GLOBAL,
        (
            (Application.visibility == ApplicationVisibility.GROUP)
            & Application.group_id.in_(
                select(UserGroupLink.group_id).where(UserGroupLink.user_id == actor.id)
            )
        ),
        (
            (Application.visibility == ApplicationVisibility.WORKSPACE)
            & Application.workspace_id.in_(
                select(WorkspaceMember.workspace_id).where(WorkspaceMember.user_id == actor.id)
            )
        ),
    )


async def list_applications(
    session: AsyncSession,
    actor: User,
    *,
    category: str | None = None,
    visibility: ApplicationVisibility | None = None,
    owned_only: bool = False,
    search: str | None = None,
    include_inactive: bool = False,
    limit: int = 50,
    offset: int = 0,
) -> Page[Application]:
    statement = select(Application).where(visible_clause(actor))
    count_statement = select(func.count()).select_from(Application).where(visible_clause(actor))

    if not include_inactive:
        statement = statement.where(Application.is_active.is_(True))
        count_statement = count_statement.where(Application.is_active.is_(True))
    if category:
        statement = statement.where(Application.category == category)
        count_statement = count_statement.where(Application.category == category)
    if visibility is not None:
        statement = statement.where(Application.visibility == visibility)
        count_statement = count_statement.where(Application.visibility == visibility)
    if owned_only:
        statement = statement.where(Application.owner_user_id == actor.id)
        count_statement = count_statement.where(Application.owner_user_id == actor.id)
    if search:
        pattern = f"%{search}%"
        clause = Application.name.ilike(pattern) | Application.display_name.ilike(pattern)
        statement = statement.where(clause)
        count_statement = count_statement.where(clause)

    total = await session.scalar(count_statement) or 0
    rows = (
        await session.execute(
            statement.order_by(
                # Curated first, then most-launched, then newest: the catalogue's
                # own ranking, so the useful entries do not sink under new ones.
                Application.is_curated.desc(),
                Application.launch_count.desc(),
                Application.id.desc(),
            )
            .limit(limit)
            .offset(offset)
        )
    ).scalars()
    return Page(items=rows.all(), total=total)


async def get_application(session: AsyncSession, actor: User, app_id: int) -> Application:
    """Fetch one application, treating out-of-visibility as missing.

    A 404 rather than a 403: telling someone that a private application exists
    and belongs to another lab is itself a disclosure.
    """
    app = await session.scalar(
        select(Application).where(Application.id == app_id, visible_clause(actor))
    )
    if app is None:
        raise NotFoundError("应用程序不存在或无权查看", code="application_not_found")
    return app


async def create_application(
    session: AsyncSession,
    actor: User,
    payload: ApplicationCreate,
    settings: Settings,
) -> Application:
    workspace = await workspace_for_actor(session, actor, payload.workspace_id)

    if await session.scalar(
        select(Application).where(
            Application.group_id == workspace.group_id, Application.name == payload.name
        )
    ):
        raise ConflictError(f"本组下已存在应用程序 {payload.name}", code="application_name_taken")

    await _validate_launch_defaults(
        session, workspace, payload.resource_group_id, payload.resource_spec_id
    )

    app = Application(
        name=payload.name,
        display_name=payload.display_name or payload.name,
        summary=payload.summary,
        readme=payload.readme,
        icon=payload.icon,
        category=payload.category,
        workspace_id=workspace.id,
        group_id=workspace.group_id,
        owner_user_id=actor.id,
        created_by=actor.id,
        image=payload.image,
        ide_type=payload.ide_type,
        resource_group_id=payload.resource_group_id,
        resource_spec_id=payload.resource_spec_id,
        default_cpu_milli=payload.default_cpu_milli,
        default_memory_mb=payload.default_memory_mb,
        default_gpu_type=payload.default_gpu_type,
        default_gpu_count=payload.default_gpu_count,
        default_storage_gb=payload.default_storage_gb,
        env=dict(payload.env),
        ports=list(payload.ports),
        visibility=payload.visibility,
    )
    session.add(app)
    await session.flush()
    await events.record(
        session,
        kind=WorkloadKind.APPLICATION,
        workload_id=app.id,
        event_type=WorkloadEventType.CREATED,
        message=f"应用程序 {app.name} 已创建",
        actor=actor,
    )
    await session.commit()
    await session.refresh(app)
    logger.info("application_created", application=app.name)
    return app


async def _validate_launch_defaults(
    session: AsyncSession,
    workspace: Workspace,
    pool_id: int | None,
    spec_id: int | None,
) -> None:
    """If defaults name a pool and spec, they must be usable together.

    Checked at publish time rather than at launch: an application that cannot
    actually be launched is worse than one that refuses to save, because the
    failure surfaces to whoever clicks it, days later.
    """
    if pool_id is None and spec_id is None:
        return
    if pool_id is None or spec_id is None:
        raise BadRequestError(
            "资源组与资源规格必须同时指定或同时留空", code="incomplete_resource_defaults"
        )
    from app.services.workload_common import resolve_pool_and_spec

    await resolve_pool_and_spec(session, workspace, pool_id, spec_id)


async def update_application(
    session: AsyncSession, actor: User, app: Application, payload: ApplicationUpdate
) -> Application:
    _ensure_may_edit(actor, app)

    fields = payload.model_dump(exclude_unset=True)
    if ("resource_group_id" in fields) or ("resource_spec_id" in fields):
        pool_id = fields.get("resource_group_id", app.resource_group_id)
        spec_id = fields.get("resource_spec_id", app.resource_spec_id)
        if pool_id is not None or spec_id is not None:
            workspace = await session.get(Workspace, app.workspace_id)
            if workspace is None:  # pragma: no cover - the FK guarantees one
                raise NotFoundError("工作空间不存在", code="workspace_not_found")
            await _validate_launch_defaults(session, workspace, pool_id, spec_id)

    for key, value in fields.items():
        if value is not None:
            setattr(app, key, value)
    await session.flush()
    await session.refresh(app)
    return app


async def delete_application(session: AsyncSession, actor: User, app: Application) -> None:
    """Take an application out of the catalogue, keeping the row.

    Rows are never removed: an instance created from it points back here, and
    usage history must stay answerable.
    """
    _ensure_may_edit(actor, app)
    app.is_active = False
    await events.record(
        session,
        kind=WorkloadKind.APPLICATION,
        workload_id=app.id,
        event_type=WorkloadEventType.DELETED,
        message=f"应用程序 {app.name} 已下架",
        actor=actor,
    )
    await session.flush()
    logger.info("application_retired", application=app.name)


def _ensure_may_edit(actor: User, app: Application) -> None:
    """The author, a group administrator of the owning group, or a global admin.

    Raises 404 rather than 403, matching :func:`get_application`: an application
    outside your visibility does not exist as far as you are concerned, and the
    message must not reveal that it does.
    """
    scope = policy.resolve_scope(actor)
    if scope.all_access or app.owner_user_id == scope.user_id:
        return
    if app.group_id in scope.group_ids:
        return
    raise NotFoundError("应用程序不存在或无权修改", code="application_not_found")


# ---------------------------------------------------------------------------
# Launch
# ---------------------------------------------------------------------------
async def launch_application(
    session: AsyncSession,
    actor: User,
    app: Application,
    payload: ApplicationLaunch,
    settings: Settings,
    gateway: K8sGateway,
) -> object:
    """Create an instance from this application.

    Delegates to :func:`app.services.instances.create_instance` rather than
    duplicating it: quota, pool grants, SSH keys, the cluster objects and the
    reconciler are all identical, and a second copy of those rules is a second
    place for them to be wrong.
    """
    if not app.is_active:
        raise BadRequestError("该应用程序已下架，无法启动", code="application_inactive")

    if payload.resource_group_id is not None and payload.resource_spec_id is not None:
        pool_id = payload.resource_group_id
        spec_id = payload.resource_spec_id
    elif app.resource_group_id is not None and app.resource_spec_id is not None:
        pool_id = app.resource_group_id
        spec_id = app.resource_spec_id
    else:
        raise BadRequestError(
            "该应用程序未预设资源，请在启动时选择资源组与资源规格",
            code="resource_choice_required",
        )

    name = payload.name or _derive_name(app.name)
    request = InstanceCreate(
        name=name,
        display_name=app.display_name or name,
        description=app.summary or None,
        workspace_id=payload.workspace_id,
        owner_user_id=payload.owner_user_id,
        resource_group_id=pool_id,
        resource_spec_id=spec_id,
        image=app.image,
        ide_type=app.ide_type,
        storage_gb=payload.storage_gb or app.default_storage_gb or None,
        ssh_enabled=payload.ssh_enabled,
        max_runtime_seconds=payload.max_runtime_seconds,
    )

    instance = await instance_service.create_instance(session, actor, request, settings, gateway)

    # Counted only after the launch succeeded, so the figure means "times this
    # actually produced an instance".
    app.launch_count += 1
    await session.flush()
    return instance


def _derive_name(base: str) -> str:
    """A default instance name that satisfies the instance name rules.

    The uniqueness constraint is deliberately left to the caller: quietly
    appending a counter would produce a name the user did not choose and cannot
    predict, and the conflict message already tells them to pick another.
    """
    return (base or "app")[:56]


def to_summary(app: Application) -> ApplicationSummary:
    return ApplicationSummary.from_application(app)


def to_detail(app: Application) -> ApplicationDetail:
    return ApplicationDetail.from_application(app)


__all__ = [
    "Page",
    "create_application",
    "delete_application",
    "get_application",
    "launch_application",
    "list_applications",
    "to_detail",
    "to_summary",
    "update_application",
    "visible_clause",
]
