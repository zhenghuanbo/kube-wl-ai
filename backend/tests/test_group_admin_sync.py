"""The two records of "who administers this group" must agree.

``DataScope`` accepts a group-scoped ``group_admin`` role *or* the membership's
``role_in_group`` flag, while permissions come from the role alone. Every path
that changes one therefore has to change the other, and before this suite only
the account-creation path did.

The failure it prevented is quiet rather than loud: a member promoted through the
members API saw the group's contents and was refused every action in it, which
reads as a broken page rather than a misconfigured account.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import policy, service
from app.auth.providers import ExternalIdentity
from app.core.config import Settings
from app.models.enums import GroupRole, RoleCode, RoleScope, UserSource
from app.models.group import UserGroupLink
from app.models.rbac import UserRole
from app.models.user import User
from app.schemas.group import GroupMemberAdd, GroupMemberUpdate
from app.schemas.user import UserRoleGrant
from app.services import group_admin, groups, users
from tests import factories

ADMIN = RoleCode.GROUP_ADMIN.value


async def _group_scoped_role_codes(session: AsyncSession, user_id: int) -> set[str]:
    """Role codes this user holds scoped to some group."""
    rows = (
        await session.scalars(
            select(UserRole).where(UserRole.user_id == user_id, UserRole.group_id.is_not(None))
        )
    ).all()
    codes: set[str] = set()
    for row in rows:
        await session.refresh(row, ["role"])
        codes.add(row.role.code)
    return codes


async def _link(session: AsyncSession, user_id: int, group_id: int) -> UserGroupLink:
    link = await session.scalar(
        select(UserGroupLink).where(
            UserGroupLink.user_id == user_id, UserGroupLink.group_id == group_id
        )
    )
    assert link is not None
    return link


async def _grant_group_admin(
    session: AsyncSession, actor: User, target: User, group_id: int
) -> None:
    await users.grant_role(
        session,
        actor,
        target,
        UserRoleGrant(role_code=ADMIN, scope=RoleScope.GROUP, group_id=group_id),
    )
    await session.flush()


# --- granting the group-scoped role mirrors the flag ---------------------------
async def test_granting_group_scoped_admin_sets_the_membership_flag(
    seeded: AsyncSession, admin: User
) -> None:
    group = await factories.create_group(seeded, name="lab-a")
    member = await factories.create_user(seeded, username="member-a", group=group)
    await seeded.flush()

    await _grant_group_admin(seeded, admin, member, group.id)

    link = await _link(seeded, member.id, group.id)
    assert link.role_in_group == GroupRole.GROUP_ADMIN
    assert ADMIN in await _group_scoped_role_codes(seeded, member.id)


async def test_revoking_group_scoped_admin_clears_the_membership_flag(
    seeded: AsyncSession, admin: User
) -> None:
    group = await factories.create_group(seeded, name="lab-a")
    member = await factories.create_user(seeded, username="member-a", group=group)
    await seeded.flush()

    await _grant_group_admin(seeded, admin, member, group.id)
    await users.revoke_role(seeded, admin, member, role_code=ADMIN, group_id=group.id)
    await seeded.flush()

    link = await _link(seeded, member.id, group.id)
    assert link.role_in_group == GroupRole.MEMBER
    assert ADMIN not in await _group_scoped_role_codes(seeded, member.id)


# --- the members API is the other door to the same state -----------------------
async def test_promoting_via_members_api_grants_scope_and_permissions(
    seeded: AsyncSession, admin: User
) -> None:
    """The regression: a promotion used to give scope without permissions."""
    group = await factories.create_group(seeded, name="lab-a")
    member = await factories.create_user(seeded, username="member-a", group=group)
    await seeded.flush()

    await groups.update_member(
        seeded,
        admin,
        group,
        member.id,
        GroupMemberUpdate(role_in_group=GroupRole.GROUP_ADMIN),
    )
    await seeded.flush()
    await seeded.refresh(member, ["role_links", "group_links"])

    # Scope, as before.
    assert policy.resolve_scope(member).group_ids == {group.id}
    # ...and now the permissions that make that scope usable.
    assert policy.has_permission(member, "user:read")
    assert ADMIN in {link.role.code for link in member.role_links if link.role is not None}


async def test_demoting_via_members_api_revokes_the_role_too(
    seeded: AsyncSession, admin: User
) -> None:
    group = await factories.create_group(seeded, name="lab-a")
    member = await factories.create_user(
        seeded, username="member-a", group=group, group_role=GroupRole.GROUP_ADMIN
    )
    await group_admin.set_group_scoped_role(
        seeded, user_id=member.id, group_id=group.id, is_admin=True
    )
    await seeded.flush()

    await groups.update_member(
        seeded,
        admin,
        group,
        member.id,
        GroupMemberUpdate(role_in_group=GroupRole.MEMBER),
    )
    await seeded.flush()

    link = await _link(seeded, member.id, group.id)
    assert link.role_in_group == GroupRole.MEMBER
    assert ADMIN not in await _group_scoped_role_codes(seeded, member.id)


async def test_adding_a_group_admin_member_confers_the_role(
    seeded: AsyncSession, admin: User
) -> None:
    group = await factories.create_group(seeded, name="lab-a")
    member = await factories.create_user(seeded, username="member-a")
    await seeded.flush()

    await groups.add_member(
        seeded,
        admin,
        group,
        GroupMemberAdd(user_id=member.id, role_in_group=GroupRole.GROUP_ADMIN),
    )
    await seeded.flush()

    assert ADMIN in await _group_scoped_role_codes(seeded, member.id)


async def test_adding_a_plain_member_confers_nothing(seeded: AsyncSession, admin: User) -> None:
    group = await factories.create_group(seeded, name="lab-a")
    member = await factories.create_user(seeded, username="member-a")
    await seeded.flush()

    await groups.add_member(seeded, admin, group, GroupMemberAdd(user_id=member.id))
    await seeded.flush()

    assert await _group_scoped_role_codes(seeded, member.id) == set()


# --- the helper itself --------------------------------------------------------
async def test_setting_the_flag_without_a_membership_is_a_no_op(seeded: AsyncSession) -> None:
    """Granting a group-scoped role must not invent a membership.

    A role can legitimately be granted for a group the user does not belong to;
    creating the link as a side effect would quietly make them a member and give
    them sight of the group.
    """
    group = await factories.create_group(seeded, name="lab-a")
    outsider = await factories.create_user(seeded, username="outsider")
    await seeded.flush()

    await group_admin.set_membership_flag(
        seeded, user_id=outsider.id, group_id=group.id, is_admin=True
    )
    await seeded.flush()

    link = await seeded.scalar(
        select(UserGroupLink).where(
            UserGroupLink.user_id == outsider.id, UserGroupLink.group_id == group.id
        )
    )
    assert link is None


async def test_group_scoped_role_is_written_at_group_scope(seeded: AsyncSession) -> None:
    group = await factories.create_group(seeded, name="lab-a")
    member = await factories.create_user(seeded, username="member-a", group=group)
    await seeded.flush()

    await group_admin.set_group_scoped_role(
        seeded, user_id=member.id, group_id=group.id, is_admin=True
    )
    await seeded.flush()

    row = await seeded.scalar(
        select(UserRole).where(UserRole.user_id == member.id, UserRole.group_id == group.id)
    )
    assert row is not None
    assert row.scope == RoleScope.GROUP


async def test_granting_twice_does_not_duplicate(seeded: AsyncSession) -> None:
    group = await factories.create_group(seeded, name="lab-a")
    member = await factories.create_user(seeded, username="member-a", group=group)
    await seeded.flush()

    for _ in range(2):
        await group_admin.set_group_scoped_role(
            seeded, user_id=member.id, group_id=group.id, is_admin=True
        )
        await seeded.flush()

    rows = (
        await seeded.scalars(
            select(UserRole).where(UserRole.user_id == member.id, UserRole.group_id == group.id)
        )
    ).all()
    assert len(rows) == 1


async def test_revoking_an_absent_role_is_a_no_op(seeded: AsyncSession) -> None:
    group = await factories.create_group(seeded, name="lab-a")
    member = await factories.create_user(seeded, username="member-a", group=group)
    await seeded.flush()

    await group_admin.set_group_scoped_role(
        seeded, user_id=member.id, group_id=group.id, is_admin=False
    )
    await seeded.flush()

    assert await _group_scoped_role_codes(seeded, member.id) == set()


# --- scope of the sync --------------------------------------------------------
async def test_globally_scoped_role_does_not_touch_the_flag(
    seeded: AsyncSession, admin: User
) -> None:
    """Only a group-scoped grant should move the membership flag."""
    group = await factories.create_group(seeded, name="lab-a")
    member = await factories.create_user(seeded, username="member-a", group=group)
    await seeded.flush()

    await users.grant_role(
        seeded,
        admin,
        member,
        UserRoleGrant(role_code=RoleCode.OBSERVER.value, scope=RoleScope.GLOBAL),
    )
    await seeded.flush()

    link = await _link(seeded, member.id, group.id)
    assert link.role_in_group == GroupRole.MEMBER


async def test_another_group_scoped_role_leaves_the_admin_flag_alone(
    seeded: AsyncSession, admin: User
) -> None:
    """Granting an unrelated role in a group says nothing about who administers it.

    The flag is the *group-admin* marker, so only the group-admin role may move
    it. Otherwise an administrator who is also, say, an observer of their own
    group would silently lose the marker and drop out of the member list's
    administrator view.
    """
    group = await factories.create_group(seeded, name="lab-a")
    member = await factories.create_user(
        seeded, username="member-a", group=group, group_role=GroupRole.GROUP_ADMIN
    )
    await _grant_group_admin(seeded, admin, member, group.id)

    await users.grant_role(
        seeded,
        admin,
        member,
        UserRoleGrant(role_code=RoleCode.OBSERVER.value, scope=RoleScope.GROUP, group_id=group.id),
    )
    await seeded.flush()

    link = await _link(seeded, member.id, group.id)
    assert link.role_in_group == GroupRole.GROUP_ADMIN
    assert ADMIN in await _group_scoped_role_codes(seeded, member.id)


async def test_revoking_an_unrelated_role_leaves_the_admin_flag_alone(
    seeded: AsyncSession, admin: User
) -> None:
    group = await factories.create_group(seeded, name="lab-a")
    member = await factories.create_user(
        seeded, username="member-a", group=group, group_role=GroupRole.GROUP_ADMIN
    )
    await _grant_group_admin(seeded, admin, member, group.id)
    await users.grant_role(
        seeded,
        admin,
        member,
        UserRoleGrant(role_code=RoleCode.OBSERVER.value, scope=RoleScope.GROUP, group_id=group.id),
    )
    await seeded.flush()

    await users.revoke_role(
        seeded, admin, member, role_code=RoleCode.OBSERVER.value, group_id=group.id
    )
    await seeded.flush()

    link = await _link(seeded, member.id, group.id)
    assert link.role_in_group == GroupRole.GROUP_ADMIN
    assert ADMIN in await _group_scoped_role_codes(seeded, member.id)


@pytest.mark.parametrize("is_admin", [True, False])
async def test_sync_keeps_both_records_agreeing(seeded: AsyncSession, is_admin: bool) -> None:
    group = await factories.create_group(seeded, name="lab-a")
    member = await factories.create_user(seeded, username="member-a", group=group)
    await seeded.flush()

    await group_admin.sync_group_admin(
        seeded, user_id=member.id, group_id=group.id, is_admin=is_admin
    )
    await seeded.flush()

    link = await _link(seeded, member.id, group.id)
    expected = GroupRole.GROUP_ADMIN if is_admin else GroupRole.MEMBER
    assert link.role_in_group == expected
    assert (ADMIN in await _group_scoped_role_codes(seeded, member.id)) is is_admin


# --- losing the directory membership must lose the authority -------------------
async def test_directory_removal_revokes_the_group_admin_role(seeded: AsyncSession) -> None:
    """The dangerous direction: authority outliving the membership.

    Sync never *grants* group admin, but it can remove a membership that carried
    one — and dropping only the membership would leave the account holding
    group-admin power over a group it is no longer in, invisible in the member
    list because the row is gone.
    """
    group = await factories.create_group(seeded, name="lab-a")
    member = await factories.create_user(
        seeded, username="member-a", group=group, group_role=GroupRole.GROUP_ADMIN
    )
    link = await _link(seeded, member.id, group.id)
    link.is_directory_managed = True
    await group_admin.set_group_scoped_role(
        seeded, user_id=member.id, group_id=group.id, is_admin=True
    )
    await seeded.flush()

    state = groups._SyncState()
    # The directory now lists somebody else and no longer lists our member.
    await groups._reconcile_group(seeded, group, ["someone-else"], state, dry_run=False)
    await seeded.flush()

    assert state.removed == 1
    assert (
        await seeded.scalar(
            select(UserGroupLink).where(
                UserGroupLink.user_id == member.id, UserGroupLink.group_id == group.id
            )
        )
        is None
    )
    assert ADMIN not in await _group_scoped_role_codes(seeded, member.id)


async def test_directory_removal_dry_run_changes_nothing(seeded: AsyncSession) -> None:
    group = await factories.create_group(seeded, name="lab-a")
    member = await factories.create_user(
        seeded, username="member-a", group=group, group_role=GroupRole.GROUP_ADMIN
    )
    link = await _link(seeded, member.id, group.id)
    link.is_directory_managed = True
    await group_admin.set_group_scoped_role(
        seeded, user_id=member.id, group_id=group.id, is_admin=True
    )
    await seeded.flush()

    state = groups._SyncState()
    await groups._reconcile_group(seeded, group, ["someone-else"], state, dry_run=True)
    await seeded.flush()

    assert state.removed == 1, "a dry run still reports what it would do"
    assert await _link(seeded, member.id, group.id) is not None
    assert ADMIN in await _group_scoped_role_codes(seeded, member.id)


async def test_per_login_sync_removal_revokes_the_group_admin_role(
    seeded: AsyncSession, settings: Settings
) -> None:
    """The same rule on the per-login path, which has its own copy of the loop."""
    keep = await factories.create_group(seeded, name="vision-lab")
    dropped = await factories.create_group(seeded, name="other-lab")
    member = await factories.create_user(seeded, username="frank", group=keep)
    await factories.add_to_group(
        seeded,
        member,
        dropped,
        role_in_group=GroupRole.GROUP_ADMIN,
        is_directory_managed=True,
    )
    await group_admin.set_group_scoped_role(
        seeded, user_id=member.id, group_id=dropped.id, is_admin=True
    )
    await seeded.flush()

    # The directory no longer reports ``other-lab``. It must still report
    # something, because an empty group list short-circuits by design.
    identity = ExternalIdentity(username="frank", groups=("vision-lab",))
    await service.sync_directory_groups(seeded, member, identity, source=UserSource.IPA)
    await seeded.flush()

    assert (
        await seeded.scalar(
            select(UserGroupLink).where(
                UserGroupLink.user_id == member.id, UserGroupLink.group_id == dropped.id
            )
        )
        is None
    )
    assert ADMIN not in await _group_scoped_role_codes(seeded, member.id)


async def test_per_login_sync_keeps_the_role_while_the_group_remains(
    seeded: AsyncSession, settings: Settings
) -> None:
    group = await factories.create_group(seeded, name="vision-lab")
    member = await factories.create_user(
        seeded, username="frank", group=group, group_role=GroupRole.GROUP_ADMIN
    )
    link = await _link(seeded, member.id, group.id)
    link.is_directory_managed = True
    await group_admin.set_group_scoped_role(
        seeded, user_id=member.id, group_id=group.id, is_admin=True
    )
    await seeded.flush()

    identity = ExternalIdentity(username="frank", groups=("vision-lab",))
    await service.sync_directory_groups(seeded, member, identity, source=UserSource.IPA)
    await seeded.flush()

    assert await _link(seeded, member.id, group.id) is not None
    assert ADMIN in await _group_scoped_role_codes(seeded, member.id)
