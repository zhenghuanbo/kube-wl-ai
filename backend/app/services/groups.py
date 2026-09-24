"""User-group management business logic, including directory reconciliation."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from sqlalchemy import Select, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import policy
from app.auth.providers import AuthProvider, build_providers
from app.auth.service import ensure_primary_group
from app.core.config import Settings
from app.core.errors import BadRequestError, ConflictError, NotFoundError
from app.core.logging import get_logger
from app.models.enums import GroupKind, GroupRole
from app.models.group import Group, UserGroupLink
from app.models.user import User
from app.schemas.group import (
    GroupCreate,
    GroupMemberAdd,
    GroupMemberUpdate,
    GroupSyncResult,
    GroupUpdate,
)
from app.services import group_admin

logger = get_logger(__name__)


@dataclass(slots=True)
class GroupPage:
    items: Sequence[Group]
    total: int


@dataclass(slots=True)
class MemberPage:
    items: Sequence[UserGroupLink]
    total: int


@dataclass(slots=True)
class _SyncState:
    added: int = 0
    removed: int = 0
    updated: int = 0
    errors: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Groups
# ---------------------------------------------------------------------------
async def list_groups(
    session: AsyncSession,
    actor: User,
    *,
    page: int = 1,
    page_size: int = 20,
    search: str | None = None,
    kind: GroupKind | None = None,
    is_active: bool | None = None,
) -> GroupPage:
    stmt: Select[tuple[Group]] = select(Group)
    stmt = policy.scope_filter(stmt, Group, actor)

    if search:
        pattern = f"%{search.strip()}%"
        stmt = stmt.where(or_(Group.name.ilike(pattern), Group.display_name.ilike(pattern)))
    if kind is not None:
        stmt = stmt.where(Group.kind == kind)
    if is_active is not None:
        stmt = stmt.where(Group.is_active.is_(is_active))

    total = int(
        await session.scalar(select(func.count()).select_from(stmt.order_by(None).subquery())) or 0
    )
    rows = (
        await session.scalars(
            stmt.order_by(Group.name).offset((page - 1) * page_size).limit(page_size)
        )
    ).all()
    return GroupPage(items=rows, total=total)


async def get_group(session: AsyncSession, actor: User, group_id: int) -> Group:
    stmt = policy.scope_filter(select(Group).where(Group.id == group_id), Group, actor)
    group: Group | None = await session.scalar(stmt)
    if group is None:
        raise NotFoundError("用户组不存在")
    return group


async def _ensure_directory_key_free(
    session: AsyncSession, group: Group | None, *, directory_key: str
) -> None:
    """Refuse a group whose directory key another group already answers to.

    The database enforces uniqueness of ``directory_name`` on its own, but the
    matching key is ``directory_name or name`` — two columns — so the case that
    matters most spans both: one group setting an explicit ``directory_name``
    equal to another group's ``name``. Left unchecked, the directory sync would
    build a name→group map with a silently overwritten entry and then reconcile
    the wrong group's membership.
    """
    stmt = select(Group).where(func.coalesce(Group.directory_name, Group.name) == directory_key)
    if group is not None and group.id is not None:
        stmt = stmt.where(Group.id != group.id)
    clash = await session.scalar(stmt)
    if clash is not None:
        raise ConflictError(
            f"目录组名 {directory_key} 已被用户组 {clash.name} 使用",
            code="directory_name_taken",
        )


async def create_group(session: AsyncSession, actor: User, payload: GroupCreate) -> Group:
    existing = await session.scalar(select(Group).where(Group.name == payload.name))
    if existing is not None:
        raise ConflictError(f"用户组 {payload.name} 已存在", code="group_name_taken")

    await _ensure_directory_key_free(
        session, None, directory_key=payload.directory_name or payload.name
    )

    owner_id = payload.owner_user_id
    if owner_id is not None:
        owner = await session.get(User, owner_id)
        if owner is None:
            raise BadRequestError("指定的负责人不存在", code="unknown_owner")

    group = Group(
        name=payload.name,
        directory_name=payload.directory_name,
        display_name=payload.display_name or payload.name,
        description=payload.description,
        kind=payload.kind,
        owner_user_id=owner_id,
    )
    session.add(group)
    await session.flush()

    if owner_id is not None:
        # The owner is a member from the outset; otherwise the group would be
        # orphaned the moment it is created.
        session.add(
            UserGroupLink(
                user_id=owner_id,
                group_id=group.id,
                role_in_group=GroupRole.GROUP_ADMIN,
                is_primary=False,
                is_directory_managed=False,
            )
        )
        await session.flush()
        owner = await session.get(User, owner_id)
        if owner is not None:
            await ensure_primary_group(session, owner)

    # The collection is not populated on a freshly added object, and touching it
    # later would trip a lazy load outside async context.
    await session.refresh(group, ["member_links"])
    logger.info("group_created", group=group.name, actor=actor.username)
    return group


async def update_group(
    session: AsyncSession, actor: User, group: Group, payload: GroupUpdate
) -> Group:
    fields = payload.model_dump(exclude_unset=True)

    owner_id = fields.get("owner_user_id")
    if owner_id is not None and await session.get(User, owner_id) is None:
        raise BadRequestError("指定的负责人不存在", code="unknown_owner")

    if "directory_name" in fields:
        # ``name`` is immutable here, so this field is the only thing that can
        # move the group's matching key.
        await _ensure_directory_key_free(
            session, group, directory_key=fields["directory_name"] or group.name
        )

    for key, value in fields.items():
        setattr(group, key, value)

    await session.flush()
    return group


async def delete_group(session: AsyncSession, actor: User, group: Group) -> None:
    """Delete a group.

    Phase 2 adds a guard here: a group that still owns workspaces must not be
    deletable, because that would orphan running workloads.
    """
    name = group.name
    await session.delete(group)
    await session.flush()
    logger.info("group_deleted", group=name, actor=actor.username)


# ---------------------------------------------------------------------------
# Members
# ---------------------------------------------------------------------------
async def list_members(
    session: AsyncSession,
    actor: User,
    group: Group,
    *,
    page: int = 1,
    page_size: int = 50,
    search: str | None = None,
) -> MemberPage:
    stmt = (
        select(UserGroupLink)
        .join(User, User.id == UserGroupLink.user_id)
        .where(UserGroupLink.group_id == group.id)
    )
    if search:
        pattern = f"%{search.strip()}%"
        stmt = stmt.where(or_(User.username.ilike(pattern), User.full_name.ilike(pattern)))

    total = int(
        await session.scalar(select(func.count()).select_from(stmt.order_by(None).subquery())) or 0
    )
    rows = (
        await session.scalars(
            stmt.order_by(User.username).offset((page - 1) * page_size).limit(page_size)
        )
    ).all()
    return MemberPage(items=rows, total=total)


async def add_member(
    session: AsyncSession, actor: User, group: Group, payload: GroupMemberAdd
) -> UserGroupLink:
    user = await session.get(User, payload.user_id)
    if user is None:
        raise NotFoundError("用户不存在")

    existing = await session.scalar(
        select(UserGroupLink).where(
            UserGroupLink.user_id == user.id, UserGroupLink.group_id == group.id
        )
    )
    if existing is not None:
        raise ConflictError("该用户已在此用户组中", code="already_member")

    link = UserGroupLink(
        user_id=user.id,
        group_id=group.id,
        role_in_group=payload.role_in_group,
        is_primary=payload.is_primary,
        is_directory_managed=False,
    )
    session.add(link)
    await session.flush()

    if payload.role_in_group == GroupRole.GROUP_ADMIN:
        # Adding someone as the group's administrator has to confer the role too,
        # or they can see the group and do nothing in it.
        await group_admin.set_group_scoped_role(
            session, user_id=user.id, group_id=group.id, is_admin=True
        )

    await ensure_primary_group(session, user)
    await session.refresh(link, ["group", "user"])
    await session.refresh(user, ["role_links"])
    logger.info(
        "group_member_added", group=group.name, username=user.username, actor=actor.username
    )
    return link


async def update_member(
    session: AsyncSession, actor: User, group: Group, user_id: int, payload: GroupMemberUpdate
) -> UserGroupLink:
    link: UserGroupLink | None = await session.scalar(
        select(UserGroupLink).where(
            UserGroupLink.user_id == user_id, UserGroupLink.group_id == group.id
        )
    )
    if link is None:
        raise NotFoundError("该用户不在此用户组中")

    fields = payload.model_dump(exclude_unset=True)
    for key, value in fields.items():
        setattr(link, key, value)

    await session.flush()

    if "role_in_group" in fields:
        # Both directions. A promotion that only set the flag would leave the
        # member list showing an administrator whose every action is refused;
        # a demotion that only cleared the flag would leave the permissions
        # behind.
        await group_admin.set_group_scoped_role(
            session,
            user_id=user_id,
            group_id=group.id,
            is_admin=link.role_in_group == GroupRole.GROUP_ADMIN,
        )

    user = await session.get(User, user_id)
    if user is not None:
        await ensure_primary_group(session, user)
        await session.refresh(user, ["role_links"])
    await session.refresh(link, ["group", "user"])
    return link


async def remove_member(session: AsyncSession, actor: User, group: Group, user_id: int) -> None:
    link: UserGroupLink | None = await session.scalar(
        select(UserGroupLink).where(
            UserGroupLink.user_id == user_id, UserGroupLink.group_id == group.id
        )
    )
    if link is None:
        raise NotFoundError("该用户不在此用户组中")

    if link.is_directory_managed:
        raise BadRequestError(
            "该成员由目录同步维护，请在目录中调整后重新同步",
            code="directory_managed_membership",
        )

    user = await session.get(User, user_id)
    await session.delete(link)
    await session.flush()

    if user is not None:
        await ensure_primary_group(session, user)
    logger.info("group_member_removed", group=group.name, user_id=user_id, actor=actor.username)


# ---------------------------------------------------------------------------
# Directory reconciliation
# ---------------------------------------------------------------------------
async def sync_directory(
    session: AsyncSession,
    actor: User,
    *,
    dry_run: bool = False,
    settings: Settings | None = None,
) -> GroupSyncResult:
    """Reconcile group memberships with the directory.

    Only groups that already exist locally by name are touched — the directory
    never creates groups here. Only memberships flagged ``is_directory_managed``
    are revoked, so a hand-granted membership always survives.

    ``settings`` is threaded in from the request rather than read from the
    process-global cache: which directory is configured is a property of the
    running application, and a caller that was handed one set of settings must
    not silently have a different set consulted underneath it.
    """
    provider = _directory_provider(settings)
    if provider is None:
        raise BadRequestError(
            "未配置可用的目录服务（LDAP），无法同步",
            code="directory_not_configured",
        )

    directory_groups = await provider.directory_groups() or []
    by_directory_key: dict[str, Group] = {}
    contested: dict[str, list[str]] = {}
    for group in (await session.scalars(select(Group))).all():
        key = group.directory_key
        if key in by_directory_key:
            # Two groups answering to the same directory name. Reconciling
            # either one would be a guess, and guessing means writing
            # memberships into a group the directory never mentioned. Report it
            # and touch neither.
            contested.setdefault(key, [by_directory_key[key].name]).append(group.name)
            continue
        by_directory_key[key] = group

    for key in contested:
        by_directory_key.pop(key, None)

    matched = sorted(name for name in directory_groups if name in by_directory_key)

    result = GroupSyncResult(
        directory_groups=sorted(directory_groups),
        matched_groups=matched,
        unmatched_groups=sorted(name for name in directory_groups if name not in by_directory_key),
        dry_run=dry_run,
    )
    state = _SyncState()
    for key, names in sorted(contested.items()):
        state.errors.append(
            f"目录组 {key} 同时对应 {len(names)} 个用户组（{', '.join(names)}），已跳过，请修正"
        )

    if matched:
        # One pass where the provider supports it. Falling back to per-group
        # calls is correct but costs a connection handshake each.
        bulk = await provider.directory_group_members_many(matched)
        for name in matched:
            if bulk is not None:
                members = bulk.get(name, [])
            else:
                members = await provider.directory_group_members(name) or []
            await _reconcile_group(session, by_directory_key[name], members, state, dry_run=dry_run)

    if not dry_run:
        await session.flush()

    result.memberships_added = state.added
    result.memberships_removed = state.removed
    result.memberships_updated = state.updated
    result.errors = state.errors
    logger.info(
        "directory_sync_completed",
        actor=actor.username,
        dry_run=dry_run,
        added=state.added,
        removed=state.removed,
        updated=state.updated,
    )
    return result


def _directory_provider(settings: Settings | None = None) -> AuthProvider | None:
    for provider in build_providers(settings):
        if provider.name == "ldap":
            return provider
    return None


async def _reconcile_group(
    session: AsyncSession,
    group: Group,
    member_usernames: Sequence[str],
    state: _SyncState,
    *,
    dry_run: bool,
) -> None:
    usernames = {name.strip() for name in member_usernames if name.strip()}
    if not usernames:
        return

    users = {
        user.username: user
        for user in (await session.scalars(select(User).where(User.username.in_(usernames)))).all()
    }
    unknown = sorted(usernames - users.keys())
    if unknown:
        # They are provisioned on first login, at which point the per-login sync
        # picks them up. Recording it keeps the operator informed.
        state.errors.append(
            f"{group.name}: {len(unknown)} 个目录成员尚未登录过平台，已跳过（{', '.join(unknown[:5])}）"
        )

    links = {
        link.user_id: link
        for link in (
            await session.scalars(select(UserGroupLink).where(UserGroupLink.group_id == group.id))
        ).all()
    }
    target_user_ids = {user.id for user in users.values()}

    for user in users.values():
        link = links.get(user.id)
        if link is None:
            state.added += 1
            if not dry_run:
                session.add(
                    UserGroupLink(
                        user_id=user.id,
                        group_id=group.id,
                        role_in_group=GroupRole.MEMBER,
                        is_primary=False,
                        is_directory_managed=True,
                    )
                )
        elif not link.is_directory_managed:
            state.updated += 1
            if not dry_run:
                link.is_directory_managed = True

    for user_id, link in links.items():
        if link.is_directory_managed and user_id not in target_user_ids:
            state.removed += 1
            if not dry_run:
                if link.role_in_group == GroupRole.GROUP_ADMIN:
                    # Leaving the directory group has to take the group-admin
                    # role with it. Dropping only the membership would leave the
                    # account holding group-admin authority over a group it is
                    # no longer in — the dangerous direction of the drift, since
                    # nothing in the member list would show it.
                    await group_admin.set_group_scoped_role(
                        session, user_id=user_id, group_id=group.id, is_admin=False
                    )
                await session.delete(link)

    if not dry_run and (state.added or state.removed):
        await session.flush()
        for user in users.values():
            await ensure_primary_group(session, user)
