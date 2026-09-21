"""Group scope narrows the *data*, never the permission set.

``effective_permissions`` unions the permissions of every ``UserRole`` link
without looking at ``link.scope``. That is the intended design — "whether you may
do it" is a role question, "which rows" is a scope question — but it has one
sharp consequence: a role granted at group scope still carries its *full*
permission set, so the group scope limits only the rows, leaving any permission
without a group dimension (``system:config``, ``user:create``, ``node:sync``)
completely unconstrained.

A group-scoped ``global_admin`` was therefore reachable through
``POST /users/{id}/roles`` and granted all permissions system-wide, while
``effective_role_codes`` reported no role at all — the UI hid every admin page
from a user who could call the entire API. ``GROUP_SCOPED_ROLES`` closes that at
the grant.
"""

from __future__ import annotations

import pytest
from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import policy
from app.auth.permissions import ALL_PERMISSION_CODES, ROLE_PERMISSIONS
from app.core.errors import BadRequestError
from app.models.enums import RoleScope
from app.models.rbac import UserRole
from app.models.user import User
from app.schemas.user import UserRoleGrant
from app.services import users
from tests import factories

ADMIN = "group_admin"


# --- the invariant itself -----------------------------------------------------
async def test_a_group_scoped_global_admin_cannot_be_granted(
    seeded: AsyncSession, admin: User
) -> None:
    """The reachable escalation this whole rule exists for."""
    group = await factories.create_group(seeded, name="lab-a")
    target = await factories.create_user(seeded, username="target")

    with pytest.raises(BadRequestError) as exc:
        await users.grant_role(
            seeded,
            admin,
            target,
            UserRoleGrant(role_code="global_admin", scope=RoleScope.GROUP, group_id=group.id),
        )

    assert exc.value.code == "role_not_group_scoped"
    assert exc.value.detail["roleCode"] == "global_admin"


@pytest.mark.parametrize("role_code", ["observer", "user", "global_admin"])
async def test_only_group_admin_may_be_granted_at_group_scope(
    seeded: AsyncSession, admin: User, role_code: str
) -> None:
    group = await factories.create_group(seeded, name="lab-a")
    target = await factories.create_user(seeded, username="target")

    with pytest.raises(BadRequestError) as exc:
        await users.grant_role(
            seeded,
            admin,
            target,
            UserRoleGrant(role_code=role_code, scope=RoleScope.GROUP, group_id=group.id),
        )

    assert exc.value.code == "role_not_group_scoped"


async def test_no_row_is_written_when_the_grant_is_refused(
    seeded: AsyncSession, admin: User
) -> None:
    """Refusing must not leave a half-applied grant behind."""
    group = await factories.create_group(seeded, name="lab-a")
    target = await factories.create_user(seeded, username="target")

    with pytest.raises(BadRequestError):
        await users.grant_role(
            seeded,
            admin,
            target,
            UserRoleGrant(role_code="global_admin", scope=RoleScope.GROUP, group_id=group.id),
        )
    await seeded.flush()

    assert (await seeded.scalars(select_links(target.id))).all() == []


def select_links(user_id: int) -> Select[tuple[UserRole]]:
    return select(UserRole).where(UserRole.user_id == user_id)


# --- what must keep working ---------------------------------------------------
async def test_group_admin_is_still_grantable_at_group_scope(
    seeded: AsyncSession, admin: User
) -> None:
    group = await factories.create_group(seeded, name="lab-a")
    target = await factories.create_user(seeded, username="target")

    await users.grant_role(
        seeded,
        admin,
        target,
        UserRoleGrant(role_code=ADMIN, scope=RoleScope.GROUP, group_id=group.id),
    )
    await seeded.flush()
    await seeded.refresh(target, ["role_links", "group_links"])

    assert policy.has_permission(target, "group:read")
    assert policy.resolve_scope(target).group_ids == {group.id}
    # The role is reported, which is the half that used to break: a group-scoped
    # `global_admin` produced `effective_role_codes == []`, so the UI hid every
    # admin page from a user holding all permissions.
    assert ADMIN in policy.effective_role_codes(target)


@pytest.mark.parametrize("role_code", ["observer", "user", "global_admin"])
async def test_global_scope_still_accepts_every_role(
    seeded: AsyncSession, admin: User, role_code: str
) -> None:
    """The restriction is about group scope only."""
    target = await factories.create_user(seeded, username="target")

    await users.grant_role(
        seeded, admin, target, UserRoleGrant(role_code=role_code, scope=RoleScope.GLOBAL)
    )
    await seeded.flush()
    await seeded.refresh(target, ["role_links", "group_links"])

    assert role_code in policy.effective_role_codes(target)


# --- the invariant that made the escalation severe -----------------------------
async def test_a_group_scoped_role_still_carries_its_full_permission_set(
    seeded: AsyncSession, admin: User
) -> None:
    """Pins the *reason* for the rule, so a future refactor cannot quietly remove it.

    If `effective_permissions` ever started narrowing group-scoped roles to
    group-relevant codes, `GROUP_SCOPED_ROLES` could safely grow — but until then,
    granting any role beyond `group_admin` at group scope hands out its whole
    permission set with only the rows constrained. This test fails loudly if that
    assumption changes.
    """
    group = await factories.create_group(seeded, name="lab-a")
    target = await factories.create_user(seeded, username="target")

    await users.grant_role(
        seeded,
        admin,
        target,
        UserRoleGrant(role_code=ADMIN, scope=RoleScope.GROUP, group_id=group.id),
    )
    await seeded.flush()
    await seeded.refresh(target, ["role_links", "group_links"])

    # Same codes as a *global* group_admin: the scope narrowed nothing here.
    granted = policy.effective_permissions(target)
    assert "user:read" in granted
    assert "workspace:create" in granted
    # …and nothing outside the group-admin set leaked in.
    assert granted <= ROLE_PERMISSIONS[ADMIN]


# --- the dead codes are gone ---------------------------------------------------
def test_the_usage_read_codes_that_nothing_checked_are_gone() -> None:
    """`usage:read_group` / `usage:read_all` described a capability that did not exist.

    Every `/usage/*` read endpoint gates on `usage:read_self` and lets the data
    scope decide the breadth, so granting `read_all` to a plain user changed
    nothing while a group admin holding only `read_self` still saw their whole
    group. A code that is never checked but shows up in the permission catalogue
    and the grant UI is worse than no code: whoever grants it believes it works.
    """
    assert "usage:read_group" not in ALL_PERMISSION_CODES
    assert "usage:read_all" not in ALL_PERMISSION_CODES
    # The two real usage capabilities survive.
    assert "usage:read_self" in ALL_PERMISSION_CODES
    assert "usage:export" in ALL_PERMISSION_CODES
    assert "usage:manage" in ALL_PERMISSION_CODES
    for role, codes in ROLE_PERMISSIONS.items():
        assert "usage:read_group" not in codes, role
        assert "usage:read_all" not in codes, role


def test_monitoring_keeps_its_three_tiers_because_they_are_enforced() -> None:
    """The asymmetry is deliberate and is the reason usage lost its tiers.

    `monitor:*` gates *different dashboards*, so the codes are checked
    (`monitoring/grafana.py` filters the catalogue, `api/v1/monitor.py` re-checks
    on open). Usage has one page, so breadth belongs to the scope.
    """
    for code in ("monitor:read_self", "monitor:read_group", "monitor:read_all"):
        assert code in ALL_PERMISSION_CODES


def test_every_catalogue_code_is_reachable_from_some_role() -> None:
    """A code no role holds is a code nobody can ever use."""
    granted: set[str] = set()
    for codes in ROLE_PERMISSIONS.values():
        granted |= set(codes)

    assert set(ALL_PERMISSION_CODES) - granted == set()


def test_group_admin_holds_only_read_only_codes_outside_group_resources() -> None:
    """Why `group_admin` is the one safe role to scope to a group.

    It must hold no system-wide *write* permission, because the group scope cannot
    constrain one — that is exactly the property whose absence made a group-scoped
    `global_admin` dangerous.
    """
    granted = ROLE_PERMISSIONS["group_admin"]
    # Anything from these resources must be a read, since the scope cannot narrow it.
    unscoped_resources = {
        "system",
        "user",
        "group",
        "node",
        "role",
        "permission",
        "resource_group",
        "resource_spec",
        "runtime_limit",
    }
    writes = {
        code
        for code in granted
        if code.split(":", 1)[0] in unscoped_resources and not code.endswith(":read")
    }
    assert writes == set()
    assert len(granted) < len(ALL_PERMISSION_CODES)
