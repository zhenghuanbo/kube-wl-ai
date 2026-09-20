"""Keeping "who administers this group" consistent across its two records.

A group's administrator is recorded in two places, and both are load-bearing:

* ``UserRole`` with ``scope=GROUP`` and the ``group_admin`` role — this is what
  grants the *permissions*.
* ``UserGroupLink.role_in_group`` — this is what ``resolve_scope`` reads for the
  *data scope*, and what the member list displays.

Supporting both is deliberate (``resolve_scope`` documents why), but it means the
two can be changed independently, and only one of the three places that touched
them used to keep them in step. The asymmetry that produced was nasty precisely
because it is silent:

* flag set, role absent → the member list shows an administrator, the data scope
  includes the group, and every action they attempt is refused. The page renders
  their group's contents and then rejects every button, which reads as a broken
  product rather than a misconfigured account.

The other direction (role present, flag absent) happens to be harmless, because
``resolve_scope`` also honours a group-scoped role assignment. That is luck, not
design, and it is why both directions are written here rather than just the
dangerous one.

Both calling endpoints require permissions only ``global_admin`` holds
(``user:assign_role`` and ``group:manage_member``), so syncing them cannot be used
to acquire authority the caller did not already have.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import GroupRole, RoleCode, RoleScope
from app.models.group import UserGroupLink
from app.models.rbac import Role, UserRole


async def _admin_role_id(session: AsyncSession) -> int | None:
    role_id = await session.scalar(select(Role.id).where(Role.code == RoleCode.GROUP_ADMIN.value))
    return int(role_id) if role_id is not None else None


async def set_membership_flag(
    session: AsyncSession, *, user_id: int, group_id: int, is_admin: bool
) -> None:
    """Set ``role_in_group`` on an existing membership, if there is one.

    Missing membership is not an error: a role can be granted for a group the
    user does not belong to, and inventing a membership row here would grant
    them sight of the group as a member as a side effect.
    """
    link = await session.scalar(
        select(UserGroupLink).where(
            UserGroupLink.user_id == user_id, UserGroupLink.group_id == group_id
        )
    )
    if link is None:
        return
    wanted = GroupRole.GROUP_ADMIN if is_admin else GroupRole.MEMBER
    if link.role_in_group != wanted:
        link.role_in_group = wanted


async def set_group_scoped_role(
    session: AsyncSession, *, user_id: int, group_id: int, is_admin: bool
) -> None:
    """Grant or revoke the ``group_admin`` role at ``group_id``'s scope."""
    role_id = await _admin_role_id(session)
    if role_id is None:  # pragma: no cover - the migration seeds this role
        return

    existing = await session.scalar(
        select(UserRole).where(
            UserRole.user_id == user_id,
            UserRole.role_id == role_id,
            UserRole.group_id == group_id,
        )
    )

    if is_admin:
        if existing is None:
            session.add(
                UserRole(
                    user_id=user_id,
                    role_id=role_id,
                    scope=RoleScope.GROUP,
                    group_id=group_id,
                )
            )
    elif existing is not None:
        await session.delete(existing)


async def sync_group_admin(
    session: AsyncSession, *, user_id: int, group_id: int, is_admin: bool
) -> None:
    """Make both records say the same thing."""
    await set_membership_flag(session, user_id=user_id, group_id=group_id, is_admin=is_admin)
    await set_group_scoped_role(session, user_id=user_id, group_id=group_id, is_admin=is_admin)
