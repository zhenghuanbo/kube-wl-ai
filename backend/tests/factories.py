"""Test factories.

Small helpers that build valid entities directly, so tests state only what they
care about instead of constructing the full object graph by hand.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.passwords import hash_password
from app.auth.tokens import hash_token
from app.models.enums import GroupKind, GroupRole, RoleScope, UserSource
from app.models.group import Group, UserGroupLink
from app.models.rbac import Role, UserRole
from app.models.token import ApiToken
from app.models.user import User


async def create_group(
    session: AsyncSession,
    *,
    name: str,
    display_name: str | None = None,
    kind: GroupKind = GroupKind.LAB,
    is_active: bool = True,
) -> Group:
    group = Group(
        name=name,
        display_name=display_name or name,
        kind=kind,
        is_active=is_active,
    )
    session.add(group)
    await session.flush()
    return group


async def reload_user(session: AsyncSession, user: User) -> User:
    """Return ``user`` with its eager-loaded relationships actually populated.

    Objects built with ``session.add()`` have no relationship state, so touching
    ``user.role_links`` afterwards would trigger a lazy load — which raises
    ``MissingGreenlet`` under an async session. Production code always re-reads
    the user via a query, so the factory mirrors that.
    """
    stmt = select(User).where(User.id == user.id).execution_options(populate_existing=True)
    return (await session.scalars(stmt)).one()


async def get_role(session: AsyncSession, code: str) -> Role:
    role = await session.scalar(select(Role).where(Role.code == code))
    if role is None:
        raise AssertionError(f"role {code!r} not found — was sync_rbac() called?")
    return role


async def assign_role(
    session: AsyncSession,
    user: User,
    code: str,
    *,
    scope: RoleScope = RoleScope.GLOBAL,
    group: Group | None = None,
) -> UserRole:
    role = await get_role(session, code)
    link = UserRole(
        user_id=user.id,
        role_id=role.id,
        scope=scope,
        group_id=group.id if group else None,
    )
    session.add(link)
    await session.flush()
    return link


async def add_to_group(
    session: AsyncSession,
    user: User,
    group: Group,
    *,
    role_in_group: GroupRole = GroupRole.MEMBER,
    is_primary: bool = False,
    is_directory_managed: bool = False,
) -> UserGroupLink:
    link = UserGroupLink(
        user_id=user.id,
        group_id=group.id,
        role_in_group=role_in_group,
        is_primary=is_primary,
        is_directory_managed=is_directory_managed,
    )
    session.add(link)
    await session.flush()
    return link


async def create_api_token(
    session: AsyncSession,
    user: User,
    *,
    name: str = "token",
) -> ApiToken:
    """A personal API token owned by ``user`` — handy for scope-filter tests."""
    token = ApiToken(
        user_id=user.id,
        name=name,
        token_hash=hash_token(f"{user.username}-{name}"),
        prefix=f"wl_{user.id:04d}",
        scopes="",
    )
    session.add(token)
    await session.flush()
    return token


async def create_user(
    session: AsyncSession,
    *,
    username: str,
    password: str | None = None,
    role: str | None = None,
    roles: tuple[str, ...] = (),
    group: Group | None = None,
    group_role: GroupRole = GroupRole.MEMBER,
    email: str | None = None,
    full_name: str = "",
    source: UserSource = UserSource.LOCAL,
    is_active: bool = True,
    is_superuser: bool = False,
) -> User:
    """Create a user, optionally with a password, group and global roles."""
    user = User(
        username=username,
        email=email or f"{username}@example.edu",
        full_name=full_name or username.title(),
        source=source,
        password_hash=hash_password(password) if password else None,
        is_active=is_active,
        is_superuser=is_superuser,
    )
    session.add(user)
    await session.flush()

    role_codes = tuple(roles) if roles else ((role,) if role else ())
    for code in role_codes:
        await assign_role(session, user, code)

    if group is not None:
        await add_to_group(
            session,
            user,
            group,
            role_in_group=group_role,
            is_primary=True,
        )

    await session.flush()
    return await reload_user(session, user)
