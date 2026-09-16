"""User management business logic.

Every query in this module goes through `app.auth.policy` — that is what keeps
the data-scope guarantees in one place instead of scattered across handlers.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import Select, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import policy
from app.auth.passwords import hash_password
from app.auth.service import ensure_primary_group
from app.core.errors import (
    BadRequestError,
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
)
from app.core.logging import get_logger
from app.models.enums import RoleScope, UserSource
from app.models.group import Group, UserGroupLink
from app.models.rbac import Role, UserRole
from app.models.user import User
from app.schemas.user import UserCreate, UserRoleGrant, UserUpdate

logger = get_logger(__name__)


@dataclass(slots=True)
class UserPage:
    items: Sequence[User]
    total: int


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------
async def list_users(
    session: AsyncSession,
    actor: User,
    *,
    page: int = 1,
    page_size: int = 20,
    search: str | None = None,
    role_code: str | None = None,
    group_id: int | None = None,
    is_active: bool | None = None,
    source: UserSource | None = None,
    include_superusers: bool = True,
) -> UserPage:
    """List users visible to ``actor``, filtered and paginated."""
    stmt: Select[tuple[User]] = select(User)
    stmt = policy.scope_filter(stmt, User, actor)

    if search:
        pattern = f"%{search.strip()}%"
        stmt = stmt.where(
            or_(
                User.username.ilike(pattern),
                User.full_name.ilike(pattern),
                User.email.ilike(pattern),
            )
        )
    if is_active is not None:
        stmt = stmt.where(User.is_active.is_(is_active))
    if source is not None:
        stmt = stmt.where(User.source == source)
    if not include_superusers:
        stmt = stmt.where(User.is_superuser.is_(False))

    if role_code:
        stmt = stmt.where(
            User.id.in_(
                select(UserRole.user_id)
                .join(Role, Role.id == UserRole.role_id)
                .where(Role.code == role_code)
            )
        )
    if group_id is not None:
        # Filtering by a group the caller does not administer is itself denied,
        # rather than silently returning an empty page.
        policy.ensure_group_access(actor, group_id)
        stmt = stmt.where(
            User.id.in_(select(UserGroupLink.user_id).where(UserGroupLink.group_id == group_id))
        )

    total = int(
        await session.scalar(select(func.count()).select_from(stmt.order_by(None).subquery())) or 0
    )
    rows = (
        await session.scalars(
            stmt.order_by(User.username).offset((page - 1) * page_size).limit(page_size)
        )
    ).all()
    return UserPage(items=rows, total=total)


async def get_user(session: AsyncSession, actor: User, user_id: int) -> User:
    """Fetch one user, enforcing the caller's data scope."""
    stmt = policy.scope_filter(select(User).where(User.id == user_id), User, actor)
    user: User | None = await session.scalar(stmt)
    if user is None:
        # Covers both "does not exist" and "not yours" — deliberately
        # indistinguishable so ids cannot be probed.
        raise NotFoundError("用户不存在")
    return user


async def get_user_by_username(session: AsyncSession, username: str) -> User | None:
    user: User | None = await session.scalar(select(User).where(User.username == username))
    return user


# ---------------------------------------------------------------------------
# Privilege-escalation guard
# ---------------------------------------------------------------------------
async def ensure_roles_grantable(
    session: AsyncSession, actor: User, role_codes: Sequence[str]
) -> list[Role]:
    """Refuse to grant a role the actor could not hold themselves.

    A global admin may grant anything. Everyone else may only hand out roles
    whose permissions are a subset of their own — otherwise an administrator
    could mint a peer with capabilities they were never given.
    """
    if not role_codes:
        return []

    roles = list((await session.scalars(select(Role).where(Role.code.in_(role_codes)))).all())
    found = {role.code for role in roles}
    missing = [code for code in role_codes if code not in found]
    if missing:
        raise BadRequestError(f"角色不存在：{', '.join(missing)}", code="unknown_role")

    if policy.is_global_admin(actor):
        return roles

    actor_permissions = policy.effective_permissions(actor)
    for role in roles:
        granted = {grant.permission.code for grant in role.permission_links}
        excess = granted - actor_permissions
        if excess:
            raise PermissionDeniedError(
                f"无法授予超出自身权限的角色：{role.code}",
                detail={"role": role.code, "excess_permissions": sorted(excess)},
            )
    return roles


# ---------------------------------------------------------------------------
# Mutations
# ---------------------------------------------------------------------------
async def create_user(session: AsyncSession, actor: User, payload: UserCreate) -> User:
    """Create a local (break-glass) account.

    Directory-backed users are provisioned automatically on first login; this
    path exists so an administrator can issue an account that works even when
    LDAP is unreachable.
    """
    existing = await get_user_by_username(session, payload.username)
    if existing is not None:
        raise ConflictError(f"用户名 {payload.username} 已存在", code="username_taken")

    if payload.email is not None:
        duplicate = await session.scalar(select(User).where(User.email == payload.email))
        if duplicate is not None:
            raise ConflictError("该邮箱已被使用", code="email_taken")

    roles = await ensure_roles_grantable(session, actor, payload.role_codes)
    groups = await _resolve_groups(session, actor, payload.group_ids)

    user = User(
        username=payload.username,
        email=payload.email,
        full_name=payload.full_name,
        source=UserSource.LOCAL,
        password_hash=hash_password(payload.password) if payload.password else None,
        is_active=payload.is_active,
        expires_at=payload.expires_at,
    )
    session.add(user)
    await session.flush()

    for role in roles:
        session.add(UserRole(user_id=user.id, role_id=role.id, scope=RoleScope.GLOBAL))

    for index, group in enumerate(groups):
        session.add(
            UserGroupLink(
                user_id=user.id,
                group_id=group.id,
                is_primary=index == 0,
                is_directory_managed=False,
            )
        )

    await session.flush()
    await session.refresh(user, ["role_links", "group_links"])
    logger.info("user_created", username=user.username, actor=actor.username)
    return user


async def update_user(session: AsyncSession, actor: User, user: User, payload: UserUpdate) -> User:
    """Apply a partial update. Unset fields are left alone."""
    fields = payload.model_dump(exclude_unset=True)

    if "email" in fields and fields["email"] and fields["email"] != user.email:
        duplicate = await session.scalar(
            select(User).where(User.email == fields["email"], User.id != user.id)
        )
        if duplicate is not None:
            raise ConflictError("该邮箱已被使用", code="email_taken")

    if fields.get("password"):
        _ensure_can_set_password(actor, user)
        user.password_hash = hash_password(fields["password"])
    fields.pop("password", None)

    for key, value in fields.items():
        setattr(user, key, value)

    await session.flush()
    await session.refresh(user, ["role_links", "group_links"])
    return user


async def set_active(session: AsyncSession, actor: User, user: User, *, is_active: bool) -> User:
    """Enable or disable an account.

    Disabling is the safe alternative to deletion: the row (and its history)
    stays, and sessions stop working immediately because every request re-reads
    the user.
    """
    if user.id == actor.id and not is_active:
        raise BadRequestError("不能停用自己的账户", code="cannot_disable_self")

    if not is_active:
        await _ensure_not_last_global_admin(session, user)

    user.is_active = is_active
    await session.flush()
    logger.info(
        "user_activation_changed",
        username=user.username,
        is_active=is_active,
        actor=actor.username,
    )
    return user


async def reset_password(session: AsyncSession, actor: User, user: User, password: str) -> None:
    _ensure_can_set_password(actor, user)
    user.password_hash = hash_password(password)
    await session.flush()
    logger.info("user_password_reset", username=user.username, actor=actor.username)


async def delete_user(session: AsyncSession, actor: User, user: User) -> None:
    """Delete an account. Prefer :func:`set_active` for everyday use."""
    if user.id == actor.id:
        raise BadRequestError("不能删除自己的账户", code="cannot_delete_self")
    if user.is_superuser:
        raise BadRequestError("不能删除超级管理员账户", code="cannot_delete_superuser")

    await _ensure_not_last_global_admin(session, user)
    await session.delete(user)
    await session.flush()
    logger.info("user_deleted", username=user.username, actor=actor.username)


async def set_groups(
    session: AsyncSession,
    actor: User,
    user: User,
    *,
    group_ids: Sequence[int],
    primary_group_id: int | None = None,
) -> User:
    """Replace the user's group memberships.

    Directory-managed memberships are preserved: they are owned by the sync
    process, and letting an administrator delete one here would only have it
    reappear at the next login. Administrators manage the *manual* memberships.
    """
    targets = await _resolve_groups(session, actor, group_ids)

    if primary_group_id is not None and primary_group_id not in {g.id for g in targets}:
        raise BadRequestError("主用户组必须是所属用户组之一", code="invalid_primary_group")

    existing = {
        link.group_id: link
        for link in (
            await session.scalars(select(UserGroupLink).where(UserGroupLink.user_id == user.id))
        ).all()
    }
    target_ids = {group.id for group in targets}

    for group in targets:
        link = existing.get(group.id)
        if link is None:
            session.add(
                UserGroupLink(user_id=user.id, group_id=group.id, is_directory_managed=False)
            )

    for group_id, link in existing.items():
        if group_id not in target_ids and not link.is_directory_managed:
            await session.delete(link)

    await session.flush()

    # Re-read so directory-managed rows that survived are included.
    links = (
        await session.scalars(select(UserGroupLink).where(UserGroupLink.user_id == user.id))
    ).all()

    if primary_group_id is not None:
        for link in links:
            link.is_primary = link.group_id == primary_group_id
    elif user.group_links and not any(link.is_primary for link in links):
        links[0].is_primary = True

    await session.flush()
    await ensure_primary_group(session, user)
    await session.refresh(user, ["group_links", "role_links"])
    return user


async def grant_role(
    session: AsyncSession, actor: User, user: User, payload: UserRoleGrant
) -> User:
    """Grant a role globally or scoped to a single group."""
    if payload.scope == RoleScope.GROUP:
        if payload.group_id is None:
            raise BadRequestError("组范围的角色必须指定 groupId", code="missing_group_id")
        policy.ensure_group_access(actor, payload.group_id)
    elif payload.group_id is not None:
        raise BadRequestError("全局范围的角色不能指定 groupId", code="unexpected_group_id")

    (role,) = await ensure_roles_grantable(session, actor, [payload.role_code])

    duplicate = await session.scalar(
        select(UserRole).where(
            UserRole.user_id == user.id,
            UserRole.role_id == role.id,
            UserRole.group_id == payload.group_id
            if payload.group_id is not None
            else UserRole.group_id.is_(None),
        )
    )
    if duplicate is not None:
        raise ConflictError("该角色已分配", code="role_already_granted")

    session.add(
        UserRole(
            user_id=user.id,
            role_id=role.id,
            scope=payload.scope,
            group_id=payload.group_id,
        )
    )
    await session.flush()
    await session.refresh(user, ["role_links", "group_links"])
    logger.info(
        "role_granted",
        username=user.username,
        role=payload.role_code,
        scope=payload.scope.value,
        actor=actor.username,
    )
    return user


async def revoke_role(
    session: AsyncSession,
    actor: User,
    user: User,
    *,
    role_code: str,
    group_id: int | None = None,
) -> User:
    """Remove a role assignment."""
    stmt = (
        select(UserRole)
        .join(Role, Role.id == UserRole.role_id)
        .where(UserRole.user_id == user.id, Role.code == role_code)
    )
    stmt = stmt.where(
        UserRole.group_id == group_id if group_id is not None else UserRole.group_id.is_(None)
    )
    link = await session.scalar(stmt)
    if link is None:
        raise NotFoundError("该用户没有此角色分配")

    if group_id is not None:
        policy.ensure_group_access(actor, group_id)

    if user.id == actor.id and group_id is None and role_code == "global_admin":
        # Always refused: an administrator who strips their own rights is locked
        # out immediately, and there is no recovery path except the shell.
        raise BadRequestError(
            "不能撤销自己的全局管理员角色，请由其他管理员操作",
            code="cannot_revoke_own_admin",
        )

    await _ensure_not_last_global_admin(session, user)

    await session.delete(link)
    await session.flush()
    await session.refresh(user, ["role_links", "group_links"])
    logger.info("role_revoked", username=user.username, role=role_code, actor=actor.username)
    return user


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------
def _ensure_can_set_password(actor: User, user: User) -> None:
    """Only local accounts carry a password; external ones live in the directory."""
    if user.source != UserSource.LOCAL:
        raise BadRequestError(
            f"{user.username} 由外部目录（{user.source.value}）管理，请在目录中修改密码",
            code="external_account",
        )


async def _resolve_groups(
    session: AsyncSession, actor: User, group_ids: Sequence[int]
) -> list[Group]:
    if not group_ids:
        return []

    groups = list((await session.scalars(select(Group).where(Group.id.in_(group_ids)))).all())
    found = {group.id for group in groups}
    missing = [group_id for group_id in group_ids if group_id not in found]
    if missing:
        raise BadRequestError(
            f"用户组不存在：{missing}", code="unknown_group", detail={"group_ids": missing}
        )

    for group in groups:
        policy.ensure_group_access(actor, group.id)
    return groups


async def _ensure_not_last_global_admin(session: AsyncSession, user: User) -> None:
    """Refuse an operation that would leave the platform with no administrator."""
    if not policy.is_global_admin(user):
        return

    admin_role = await session.scalar(select(Role).where(Role.code == "global_admin"))
    if admin_role is None:  # pragma: no cover - seeded
        return

    remaining = int(
        await session.scalar(
            select(func.count())
            .select_from(UserRole)
            .join(User, User.id == UserRole.user_id)
            .where(
                UserRole.role_id == admin_role.id,
                UserRole.scope == RoleScope.GLOBAL,
                User.id != user.id,
                User.is_active.is_(True),
                or_(User.expires_at.is_(None), User.expires_at > datetime.now(UTC)),
            )
        )
        or 0
    )
    if remaining == 0:
        raise BadRequestError(
            "系统必须保留至少一个可用的全局管理员",
            code="last_global_admin",
        )
