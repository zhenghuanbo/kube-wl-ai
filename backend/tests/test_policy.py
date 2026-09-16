"""Tests for the permission policy engine (Phase 1, plan §5.5).

These are the security-critical tests: the scope layer decides *which rows* a
caller can reach, so every role is exercised against rows owned by someone else.
"""

from __future__ import annotations

import pytest
from sqlalchemy import Column, Integer, MetaData, Table, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import policy
from app.auth.permissions import ALL_PERMISSION_CODES
from app.core.errors import NotFoundError, PermissionDeniedError
from app.models.enums import GroupKind, GroupRole, RoleScope
from app.models.group import UserGroupLink
from app.models.user import User
from tests import factories


class World:
    """A small fixture universe: two labs, one admin per lab, one plain user."""

    lab_a: object
    lab_b: object
    admin_a: User
    admin_b: User
    user_a: User
    user_b: User
    global_admin: User


@pytest.fixture
async def world(seeded: AsyncSession) -> World:
    w = World()
    w.lab_a = await factories.create_group(seeded, name="lab-a", kind=GroupKind.LAB)
    w.lab_b = await factories.create_group(seeded, name="lab-b", kind=GroupKind.LAB)

    w.admin_a = await factories.create_user(
        seeded,
        username="admin-a",
        role="group_admin",
        group=w.lab_a,
        group_role=GroupRole.GROUP_ADMIN,
    )
    w.admin_b = await factories.create_user(
        seeded,
        username="admin-b",
        role="group_admin",
        group=w.lab_b,
        group_role=GroupRole.GROUP_ADMIN,
    )
    w.user_a = await factories.create_user(seeded, username="user-a", role="user", group=w.lab_a)
    w.user_b = await factories.create_user(seeded, username="user-b", role="user", group=w.lab_b)
    w.global_admin = await factories.create_user(seeded, username="root-admin", role="global_admin")
    await seeded.commit()
    return w


# --- L1: functional permissions ---------------------------------------------
async def test_global_admin_holds_every_permission(seeded: AsyncSession, world: World) -> None:
    assert policy.effective_permissions(world.global_admin) == ALL_PERMISSION_CODES


async def test_superuser_bypasses_role_mapping(seeded: AsyncSession) -> None:
    user = await factories.create_user(seeded, username="super", is_superuser=True)

    assert policy.effective_permissions(user) == ALL_PERMISSION_CODES
    assert policy.is_global_admin(user) is True


async def test_plain_user_can_create_instances_but_not_users(
    seeded: AsyncSession, world: World
) -> None:
    perms = policy.effective_permissions(world.user_a)

    assert "instance:create" in perms
    assert "user:create" not in perms
    assert "group:manage_member" not in perms


async def test_observer_is_read_only(seeded: AsyncSession) -> None:
    observer = await factories.create_user(seeded, username="watcher", role="observer")
    perms = policy.effective_permissions(observer)

    assert "instance:read" in perms
    assert not any(code.endswith((":create", ":update", ":delete")) for code in perms)


async def test_group_admin_can_manage_its_group(seeded: AsyncSession, world: World) -> None:
    perms = policy.effective_permissions(world.admin_a)

    assert "workspace:create" in perms
    assert "workspace:assign_member" in perms
    assert "workspace:grant_resource" in perms
    assert "user:create" not in perms, "group admins must not create platform users"
    assert "group:create" not in perms, "group admins must not create user groups"


async def test_require_permission_raises_with_required_code(
    seeded: AsyncSession, world: World
) -> None:
    with pytest.raises(PermissionDeniedError) as exc:
        policy.require_permission(world.user_a, "user:delete")

    assert exc.value.detail == {"required_permission": "user:delete"}


# --- L2: data scope ----------------------------------------------------------
async def test_scope_kinds(seeded: AsyncSession, world: World) -> None:
    assert policy.resolve_scope(world.global_admin).kind == "all"
    assert policy.resolve_scope(world.admin_a).kind == "group"
    assert policy.resolve_scope(world.user_a).kind == "self"


async def test_group_admin_scope_covers_only_its_group(seeded: AsyncSession, world: World) -> None:
    scope_a = policy.resolve_scope(world.admin_a)

    assert scope_a.group_ids == {world.lab_a.id}  # type: ignore[attr-defined]
    assert world.lab_b.id not in scope_a.group_ids  # type: ignore[attr-defined]


async def test_scope_is_unaffected_by_primary_group_of_plain_user(
    seeded: AsyncSession, world: World
) -> None:
    """Being *in* a group grants no administrative scope over it."""
    assert policy.resolve_scope(world.user_a).group_ids == frozenset()


async def test_user_with_both_global_and_group_roles_gets_union(
    seeded: AsyncSession, world: World
) -> None:
    hybrid = await factories.create_user(seeded, username="hybrid", role="user")
    await factories.assign_role(
        seeded, hybrid, "group_admin", scope=RoleScope.GROUP, group=world.lab_b
    )
    await seeded.refresh(hybrid, ["role_links"])

    scope = policy.resolve_scope(hybrid)
    assert scope.all_access is False
    assert scope.group_ids == {world.lab_b.id}  # type: ignore[attr-defined]


async def test_admin_group_derived_from_membership_flag(seeded: AsyncSession, world: World) -> None:
    """A ``group_admin`` membership flag grants scope even without the role row."""
    plain = await factories.create_user(
        seeded, username="plain-b", group=world.lab_b, group_role=GroupRole.MEMBER
    )
    assert policy.resolve_scope(plain).group_ids == frozenset()

    # Promoted directly on the membership, as the group-members API does.
    plain.group_links[0].role_in_group = GroupRole.GROUP_ADMIN
    await seeded.flush()
    await seeded.refresh(plain, ["group_links"])

    assert policy.resolve_scope(plain).group_ids == {world.lab_b.id}  # type: ignore[attr-defined]


# --- L2: scope_filter on real rows -------------------------------------------
async def test_scope_filter_isolates_users_own_tokens(seeded: AsyncSession, world: World) -> None:
    mine = await factories.create_api_token(seeded, world.user_a, name="mine")
    theirs = await factories.create_api_token(seeded, world.user_b, name="theirs")
    await seeded.commit()

    from app.models.token import ApiToken

    rows = (
        await seeded.scalars(policy.scope_filter(select(ApiToken), ApiToken, world.user_a))
    ).all()

    ids = {row.id for row in rows}
    assert mine.id in ids
    assert theirs.id not in ids


async def test_scope_filter_gives_group_admin_group_rows(
    seeded: AsyncSession, world: World
) -> None:
    """A group admin sees the members of their own group, not of another."""

    rows = (
        await seeded.scalars(
            policy.scope_filter(select(UserGroupLink), UserGroupLink, world.admin_a)
        )
    ).all()

    group_ids = {row.group_id for row in rows}
    assert group_ids == {world.lab_a.id}  # type: ignore[attr-defined]


async def test_scope_filter_on_users_hides_other_groups(seeded: AsyncSession, world: World) -> None:
    stmt = policy.scope_filter(select(User), User, world.admin_a)
    usernames = {user.username for user in (await seeded.scalars(stmt)).all()}

    # Own group's members plus self.
    assert usernames == {"admin-a", "user-a"}
    assert "user-b" not in usernames
    assert "root-admin" not in usernames


async def test_scope_filter_returns_everything_for_global_admin(
    seeded: AsyncSession, world: World
) -> None:

    stmt = policy.scope_filter(select(UserGroupLink), UserGroupLink, world.global_admin)
    rows = (await seeded.scalars(stmt)).all()

    assert {row.group_id for row in rows} == {
        world.lab_a.id,  # type: ignore[attr-defined]
        world.lab_b.id,  # type: ignore[attr-defined]
    }


async def test_scope_filter_fails_closed_for_unknown_model(
    seeded: AsyncSession, world: World
) -> None:
    """An unregistered model must yield nothing, never everything."""

    class Unregistered:
        """A model the policy engine knows nothing about."""

        __table__ = Table("unregistered_probe", MetaData(), Column("id", Integer, primary_key=True))

    stmt = policy.scope_filter(select(User), Unregistered, world.user_a)  # type: ignore[arg-type]
    assert (await seeded.scalars(stmt)).all() == []


# --- L3: object access -------------------------------------------------------
async def test_ensure_object_access_allows_owner(seeded: AsyncSession, world: World) -> None:
    token = await factories.create_api_token(seeded, world.user_a)
    policy.ensure_object_access(world.user_a, token)  # must not raise


async def test_ensure_object_access_hides_other_users_row(
    seeded: AsyncSession, world: World
) -> None:
    token = await factories.create_api_token(seeded, world.user_b)

    with pytest.raises(NotFoundError):
        policy.ensure_object_access(world.user_a, token)


async def test_ensure_object_access_can_report_forbidden(
    seeded: AsyncSession, world: World
) -> None:
    token = await factories.create_api_token(seeded, world.user_b)

    with pytest.raises(PermissionDeniedError):
        policy.ensure_object_access(world.user_a, token, not_found=False)


async def test_ensure_group_access(seeded: AsyncSession, world: World) -> None:
    policy.ensure_group_access(world.admin_a, world.lab_a.id)  # type: ignore[attr-defined]

    with pytest.raises(PermissionDeniedError):
        policy.ensure_group_access(world.admin_a, world.lab_b.id)  # type: ignore[attr-defined]

    policy.ensure_group_access(world.global_admin, world.lab_b.id)  # type: ignore[attr-defined]


async def test_ensure_owner_or_admin(seeded: AsyncSession, world: World) -> None:
    policy.ensure_owner_or_admin(world.user_a, world.user_a.id)

    with pytest.raises(PermissionDeniedError):
        policy.ensure_owner_or_admin(world.user_a, world.user_b.id)

    policy.ensure_owner_or_admin(world.global_admin, world.user_b.id)


async def test_scope_rule_inference_from_column_names(seeded: AsyncSession, world: World) -> None:
    """Models without an explicit rule are inferred from conventional columns."""
    from app.models.token import RefreshToken

    # RefreshToken has an explicit rule; drop it to exercise inference.
    original = policy.SCOPE_RULES.pop(RefreshToken)
    try:
        rule = policy._rule_for(RefreshToken)
        assert rule is not None
        assert rule.owner == "user_id"
    finally:
        policy.SCOPE_RULES[RefreshToken] = original


async def test_data_scope_serialisation(seeded: AsyncSession, world: World) -> None:
    payload = policy.resolve_scope(world.admin_a).to_dict()

    assert payload["kind"] == "group"
    assert payload["all_access"] is False
    assert payload["group_ids"] == [world.lab_a.id]  # type: ignore[attr-defined]
