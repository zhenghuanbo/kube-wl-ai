"""Quota profiles: the named compute ceilings an administrator hands out.

Kept apart from ``app.services.resources`` because a profile is *policy*, not
inventory: nothing here talks to the cluster and nothing here counts usage. A
profile only ever says what **may** be used; the comparison against what *is* used
lives in ``app.services.quota_guard``, which recomputes usage from the workloads
that exist right now.

Three invariants live here rather than in the database, because all three are about
not silently changing somebody's ceiling:

* **Exactly one default per kind.** Two defaults would make "which profile does a
  user with no profile of their own follow" depend on row order.
* **The default cannot be deleted, un-defaulted or deactivated.** Each of those
  would leave everyone who follows it with no ceiling, or pick a new one by luck.
* **A profile that is in use cannot be deleted or deactivated.** Either would move
  people onto a different ceiling without anybody asking and without anything to
  report it.
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.errors import AppError, BadRequestError, ConflictError, NotFoundError
from app.core.logging import get_logger
from app.models.enums import QuotaProfileKind
from app.models.quota_profile import QuotaProfile
from app.models.user import User
from app.models.workspace import Workspace
from app.schemas.quota_profile import (
    QuotaProfileCreate,
    QuotaProfileUpdate,
    applicability_error,
    misplaced_dimensions,
)

logger = get_logger(__name__)

#: The name the migration seeds and :func:`ensure_default_profiles` recreates. Fixed
#: so that "put the defaults back" needs no guessing about what they were called.
DEFAULT_PROFILE_NAME = "default"

#: Every dimension a profile can carry, in one place. Assigning through this tuple
#: (rather than naming each column) keeps create/update/seed from drifting apart.
_PROFILE_DIMENSIONS = (
    "cpu_cores",
    "memory_gb",
    "gpu",
    "max_instances",
    "max_applications",
    "max_tasks",
    "max_pods",
    "max_deployments",
    "max_services",
    "max_jobs",
)


def default_dimensions(kind: QuotaProfileKind, settings: Settings) -> dict[str, int]:
    """What a fresh default profile carries, straight from configuration.

    Only the dimensions that apply to ``kind`` are returned; the rest stay unset
    (``NULL``) in the row, which is how the schema knows they are not applicable.
    """
    if kind is QuotaProfileKind.USER:
        return {
            "cpu_cores": settings.default_user_quota_cpu_cores,
            "memory_gb": settings.default_user_quota_memory_gb,
            "gpu": settings.default_user_quota_gpu,
            "max_instances": settings.default_user_quota_max_instances,
            "max_applications": settings.default_user_quota_max_applications,
            "max_tasks": settings.default_user_quota_max_tasks,
        }
    return {
        "cpu_cores": settings.default_workspace_quota_cpu_cores,
        "memory_gb": settings.default_workspace_quota_memory_gb,
        "gpu": settings.default_workspace_quota_gpu,
        "max_pods": settings.workspace_default_max_pods,
        "max_deployments": settings.workspace_default_max_deployments,
        "max_services": settings.workspace_default_max_services,
        "max_jobs": settings.workspace_default_max_jobs,
    }


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------
async def list_profiles(
    session: AsyncSession,
    *,
    kind: QuotaProfileKind | None = None,
    is_active: bool | None = None,
) -> list[QuotaProfile]:
    """All profiles, ordered by kind, the default first within each kind."""
    stmt = select(QuotaProfile).order_by(
        QuotaProfile.kind, QuotaProfile.is_default.desc(), QuotaProfile.id
    )
    if kind is not None:
        stmt = stmt.where(QuotaProfile.kind == kind)
    if is_active is not None:
        stmt = stmt.where(QuotaProfile.is_active == is_active)
    return list((await session.scalars(stmt)).all())


async def get_profile(session: AsyncSession, profile_id: int) -> QuotaProfile:
    profile = await session.get(QuotaProfile, profile_id)
    if profile is None:
        raise NotFoundError("配额模板不存在", code="quota_profile_not_found")
    return profile


async def default_profile(session: AsyncSession, kind: QuotaProfileKind) -> QuotaProfile | None:
    """The profile followed by everyone with no profile of their own.

    Deliberately unfiltered by ``is_active``: the default is the row the fallback
    depends on, and deactivating it is refused so that it can never be retired out
    from under the users following it.
    """
    # Annotated, not inferred: `session.scalar` is typed `Any`, and mypy would
    # otherwise reject the `Any` coming back out of a function that promises a
    # `QuotaProfile | None`.
    profile: QuotaProfile | None = await session.scalar(
        select(QuotaProfile).where(QuotaProfile.kind == kind, QuotaProfile.is_default.is_(True))
    )
    return profile


async def resolve_user_profile(session: AsyncSession, user: User) -> QuotaProfile:
    """The profile governing this user: their own, or the default for users."""
    return await _resolve_assigned(
        session, QuotaProfileKind.USER, user.quota_profile_id, f"用户 {user.username}"
    )


async def resolve_workspace_profile(session: AsyncSession, workspace: Workspace) -> QuotaProfile:
    """The profile governing this workspace: its own, or the default for spaces."""
    return await _resolve_assigned(
        session,
        QuotaProfileKind.WORKSPACE,
        workspace.quota_profile_id,
        f"工作空间 {workspace.name}",
    )


async def profile_for_assignment(
    session: AsyncSession, profile_id: int | None, kind: QuotaProfileKind
) -> QuotaProfile | None:
    """Resolve a template being attached, refusing one that does not fit.

    ``None`` clears the assignment, which means "follow the kind's default" — the
    one place inheriting is expressed, so promoting a new default moves everyone who
    never asked for a template of their own.
    """
    if profile_id is None:
        return None
    profile = await session.get(QuotaProfile, profile_id)
    if profile is None:
        raise NotFoundError(f"配额模板 {profile_id} 不存在", code="quota_profile_not_found")
    if profile.kind is not kind:
        raise BadRequestError(
            f"这是{profile.kind.value}配额模板，不能关联到"
            f"{'用户' if kind is QuotaProfileKind.USER else '工作空间'}",
            code="quota_profile_kind_mismatch",
        )
    if not profile.is_active:
        raise BadRequestError(
            f"配额模板 {profile.name} 已停用，请先启用再关联", code="quota_profile_inactive"
        )
    return profile


async def _resolve_assigned(
    session: AsyncSession,
    kind: QuotaProfileKind,
    profile_id: int | None,
    subject: str,
) -> QuotaProfile:
    """Follow an assignment, or fall back to the kind's default.

    A dangling or wrong-kind assignment raises rather than falling back. Falling
    back would silently *change* a ceiling that somebody deliberately set, and the
    quiet version of that is exactly the failure this whole change exists to remove.
    """
    if profile_id is not None:
        assigned = await session.get(QuotaProfile, profile_id)
        if assigned is None or assigned.kind is not kind:
            raise AppError(
                f"{subject} 关联的配额模板不存在或类型不符，请联系管理员重新分配",
                code="quota_profile_assignment_invalid",
                detail={"kind": kind.value, "profileId": profile_id},
            )
        return assigned

    profile = await default_profile(session, kind)
    if profile is None:
        raise AppError(
            f"系统未配置默认的{kind.value}配额模板，请管理员在「配额管理」中创建",
            code="quota_profile_unconfigured",
            detail={"kind": kind.value},
        )
    return profile


async def attachment_counts(session: AsyncSession, profile: QuotaProfile) -> tuple[int, int]:
    """How many users and workspaces point at this profile, as ``(users, spaces)``."""
    users = int(
        await session.scalar(
            select(func.count()).select_from(User).where(User.quota_profile_id == profile.id)
        )
        or 0
    )
    workspaces = int(
        await session.scalar(
            select(func.count())
            .select_from(Workspace)
            .where(Workspace.quota_profile_id == profile.id)
        )
        or 0
    )
    return users, workspaces


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------
async def ensure_default_profiles(session: AsyncSession, settings: Settings) -> list[QuotaProfile]:
    """Create a default profile for any kind that has none. Idempotent.

    Called at startup so that an installation always has something to fall back to,
    and so that a fresh database works without waiting for the first administrator
    to visit the quota page. It never *edits* an existing default: the numbers here
    come from configuration and are only a starting point, and overwriting them on
    every boot would silently undo whatever the administrator had since tuned.

    The resolver deliberately does not call this. Creating a row in the middle of a
    read path would hide a broken installation behind a write; raising a message
    that names the missing template tells the administrator what to fix.
    """
    created: list[QuotaProfile] = []
    for kind in QuotaProfileKind:
        if await default_profile(session, kind) is not None:
            continue
        profile = await create_profile(
            session,
            None,
            QuotaProfileCreate(
                kind=kind, name=DEFAULT_PROFILE_NAME, display_name="默认配额", is_default=True
            ),
            settings,
        )
        created.append(profile)
        logger.info("quota_profile_seeded", kind=kind.value, quota_profile=profile.name)
    return created


async def create_profile(
    session: AsyncSession,
    actor: User | None,
    payload: QuotaProfileCreate,
    settings: Settings,
) -> QuotaProfile:
    existing = await session.scalar(
        select(QuotaProfile).where(
            QuotaProfile.kind == payload.kind, QuotaProfile.name == payload.name
        )
    )
    if existing is not None:
        raise ConflictError(
            f"{payload.kind.value} 配额模板 {payload.name} 已存在",
            code="quota_profile_name_taken",
        )

    defaults = default_dimensions(payload.kind, settings)
    values: dict[str, int | None] = {name: defaults.get(name) for name in _PROFILE_DIMENSIONS}
    for name in _PROFILE_DIMENSIONS:
        supplied = getattr(payload, name, None)
        if supplied is not None:
            values[name] = supplied

    # The first profile of a kind becomes its default: a kind with no default would
    # leave everyone following it with nothing.
    has_default = await default_profile(session, payload.kind)
    profile = QuotaProfile(
        kind=payload.kind,
        name=payload.name,
        display_name=payload.display_name or payload.name,
        description=payload.description,
        is_default=payload.is_default or has_default is None,
    )
    for name in _PROFILE_DIMENSIONS:
        setattr(profile, name, values[name])
    _check_workspace_counts(profile)
    session.add(profile)
    await session.flush()
    if profile.is_default:
        await _demote_other_defaults(session, profile)

    logger.info(
        "quota_profile_created",
        quota_profile=profile.name,
        kind=profile.kind.value,
        is_default=profile.is_default,
        actor=actor.username if actor else None,
    )
    return profile


async def update_profile(
    session: AsyncSession,
    actor: User,
    profile: QuotaProfile,
    payload: QuotaProfileUpdate,
) -> QuotaProfile:
    provided = payload.model_dump(exclude_unset=True)

    offending = misplaced_dimensions(profile.kind, provided)
    if offending:
        raise BadRequestError(
            applicability_error(profile.kind, offending),
            code="quota_profile_dimension_mismatch",
        )

    if profile.is_default:
        if provided.get("is_default") is False:
            raise BadRequestError(
                "不能取消默认模板，请先把另一个模板设为默认",
                code="quota_profile_default_required",
            )
        if provided.get("is_active") is False:
            raise BadRequestError(
                "默认模板不能停用，请先把另一个模板设为默认",
                code="quota_profile_default_required",
            )
    elif provided.get("is_active") is False and profile.is_active:
        users, workspaces = await attachment_counts(session, profile)
        if users or workspaces:
            raise ConflictError(
                f"该模板仍被 {users} 个用户、{workspaces} 个工作空间使用，无法停用",
                code="quota_profile_in_use",
            )

    for name, value in provided.items():
        setattr(profile, name, value)
    _check_workspace_counts(profile)
    await session.flush()
    if profile.is_default:
        await _demote_other_defaults(session, profile)

    logger.info(
        "quota_profile_updated",
        quota_profile=profile.name,
        kind=profile.kind.value,
        is_default=profile.is_default,
        actor=actor.username,
    )
    return profile


async def delete_profile(session: AsyncSession, actor: User, profile: QuotaProfile) -> None:
    if profile.is_default:
        raise BadRequestError(
            "默认模板不能删除，请先把另一个模板设为默认",
            code="quota_profile_default_required",
        )
    users, workspaces = await attachment_counts(session, profile)
    if users or workspaces:
        raise ConflictError(
            f"该模板仍被 {users} 个用户、{workspaces} 个工作空间使用，无法删除",
            code="quota_profile_in_use",
        )
    name, kind = profile.name, profile.kind.value
    await session.delete(profile)
    await session.flush()
    logger.info("quota_profile_deleted", quota_profile=name, kind=kind, actor=actor.username)


def _check_workspace_counts(profile: QuotaProfile) -> None:
    """Refuse namespace counts that cannot all be satisfied at once.

    ``pods`` has to cover the deployments plus the jobs. Kubernetes evaluates the
    counts independently, so a workspace whose pod ceiling is reached first is one
    that cannot run the work it was explicitly allowed to run: the deployment
    allowance is unreachable rather than merely small, which is the kind of
    configuration that gets discovered as an incident.

    Checked only when all three are checked. ``-1`` on any of them means the
    contradiction cannot be evaluated, and guessing would refuse configurations that
    are in fact fine.
    """
    if profile.kind is not QuotaProfileKind.WORKSPACE:
        return
    pods, deployments, jobs = profile.max_pods, profile.max_deployments, profile.max_jobs
    if pods is None or deployments is None or jobs is None:
        return
    if pods < 0 or deployments < 0 or jobs < 0:
        return
    if pods < deployments + jobs:
        raise BadRequestError(
            f"Pod 上限（{pods}）不能小于部署数（{deployments}）与任务数（{jobs}）之和，"
            "否则工作空间会在用满部署或任务额度之前先被 Pod 上限挡住",
            code="quota_profile_counts_unsatisfiable",
        )


async def _demote_other_defaults(session: AsyncSession, keep: QuotaProfile) -> None:
    """Exactly one profile per kind may be the default."""
    others = (
        await session.scalars(
            select(QuotaProfile).where(
                QuotaProfile.id != keep.id,
                QuotaProfile.kind == keep.kind,
                QuotaProfile.is_default.is_(True),
            )
        )
    ).all()
    for other in others:
        other.is_default = False
    await session.flush()
