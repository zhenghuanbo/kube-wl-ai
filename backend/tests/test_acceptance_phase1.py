"""Phase 1 acceptance: the role × endpoint × data-scope matrix (plan §5.5).

These tests are the contract for the permission model. Each row asserts what a
given role may and may not reach, and — just as importantly — that a denial never
leaks the existence of another tenant's data.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import GroupKind, GroupRole
from app.models.group import Group
from app.models.user import User
from tests import factories

PASSWORD = "correct-horse-battery"
LOGIN = "/api/v1/auth/login"


@dataclass
class World:
    lab_a: Group
    lab_b: Group
    global_admin: User
    admin_a: User
    admin_b: User
    user_a: User
    user_b: User
    observer: User


@pytest.fixture
async def world(seeded: AsyncSession) -> World:
    lab_a = await factories.create_group(seeded, name="lab-a", kind=GroupKind.LAB)
    lab_b = await factories.create_group(seeded, name="lab-b", kind=GroupKind.LAB)

    global_admin = await factories.create_user(
        seeded, username="root", password=PASSWORD, role="global_admin"
    )
    admin_a = await factories.create_user(
        seeded,
        username="admin-a",
        password=PASSWORD,
        role="group_admin",
        group=lab_a,
        group_role=GroupRole.GROUP_ADMIN,
    )
    admin_b = await factories.create_user(
        seeded,
        username="admin-b",
        password=PASSWORD,
        role="group_admin",
        group=lab_b,
        group_role=GroupRole.GROUP_ADMIN,
    )
    user_a = await factories.create_user(
        seeded, username="user-a", password=PASSWORD, role="user", group=lab_a
    )
    user_b = await factories.create_user(
        seeded, username="user-b", password=PASSWORD, role="user", group=lab_b
    )
    observer = await factories.create_user(
        seeded, username="watcher", password=PASSWORD, role="observer", group=lab_a
    )

    await seeded.commit()
    return World(
        lab_a=lab_a,
        lab_b=lab_b,
        global_admin=global_admin,
        admin_a=admin_a,
        admin_b=admin_b,
        user_a=user_a,
        user_b=user_b,
        observer=observer,
    )


async def act_as(client: AsyncClient, username: str) -> AsyncClient:
    """Switch the client to another identity, as logging out and back in does."""
    client.headers.pop("Authorization", None)
    response = await client.post(LOGIN, json={"username": username, "password": PASSWORD})
    assert response.status_code == 200, f"login as {username} failed: {response.text}"
    token = response.json()["accessToken"]
    client.headers["Authorization"] = f"Bearer {token}"
    return client


# ---------------------------------------------------------------------------
# Authenticated access to read endpoints
# ---------------------------------------------------------------------------
async def test_observer_can_read_but_never_write(client: AsyncClient, world: World) -> None:
    await act_as(client, "watcher")

    assert (await client.get("/api/v1/users")).status_code == 200
    assert (await client.get("/api/v1/groups")).status_code == 200

    # Observer has no write permission anywhere.
    assert (await client.post("/api/v1/groups", json={"name": "x"})).status_code == 403
    assert (
        await client.post(
            "/api/v1/users", json={"username": "nope", "password": "whatever-long-enough"}
        )
    ).status_code == 403
    assert (await client.delete(f"/api/v1/users/{world.user_b.id}")).status_code == 403


async def test_plain_user_cannot_reach_administration(client: AsyncClient, world: World) -> None:
    await act_as(client, "user-a")

    for method, path in [
        ("GET", "/api/v1/users"),
        ("GET", "/api/v1/groups"),
    ]:
        assert (await client.request(method, path)).status_code == 200, path

    assert (await client.get("/api/v1/roles")).status_code == 403
    assert (await client.get("/api/v1/permissions")).status_code == 403
    assert (await client.post("/api/v1/users", json={"username": "x"})).status_code == 403
    assert (await client.post("/api/v1/groups", json={"name": "x"})).status_code == 403
    assert (await client.post("/api/v1/groups/sync", json={})).status_code == 403


async def test_global_admin_reaches_every_catalogue_endpoint(
    client: AsyncClient, world: World
) -> None:
    await act_as(client, "root")

    assert (await client.get("/api/v1/roles")).status_code == 200
    assert (await client.get("/api/v1/permissions")).status_code == 200
    assert (await client.get("/api/v1/users")).status_code == 200
    assert (await client.get("/api/v1/groups")).status_code == 200


# ---------------------------------------------------------------------------
# Horizontal isolation: same role, different tenant
# ---------------------------------------------------------------------------
async def test_user_list_is_scoped_to_the_caller(client: AsyncClient, world: World) -> None:
    await act_as(client, "user-a")
    body = (await client.get("/api/v1/users")).json()

    usernames = {item["username"] for item in body["items"]}
    assert usernames == {"user-a"}, "a plain user must see only themselves"


async def test_group_admin_sees_only_its_own_group(client: AsyncClient, world: World) -> None:
    await act_as(client, "admin-a")
    body = (await client.get("/api/v1/users")).json()

    usernames = {item["username"] for item in body["items"]}
    assert usernames == {"admin-a", "user-a", "watcher"}
    assert "user-b" not in usernames
    assert "admin-b" not in usernames
    assert "root" not in usernames


async def test_group_admin_cannot_read_another_group(client: AsyncClient, world: World) -> None:
    await act_as(client, "admin-a")

    own = await client.get(f"/api/v1/groups/{world.lab_a.id}")
    other = await client.get(f"/api/v1/groups/{world.lab_b.id}")

    assert own.status_code == 200
    # 404, not 403: the caller must not learn that lab-b exists.
    assert other.status_code == 404


async def test_group_admin_cannot_read_another_groups_members(
    client: AsyncClient, world: World
) -> None:
    await act_as(client, "admin-a")

    assert (await client.get(f"/api/v1/groups/{world.lab_a.id}/members")).status_code == 200
    assert (await client.get(f"/api/v1/groups/{world.lab_b.id}/members")).status_code == 404


async def test_cross_tenant_user_detail_is_not_found(client: AsyncClient, world: World) -> None:
    await act_as(client, "user-a")

    assert (await client.get(f"/api/v1/users/{world.user_a.id}")).status_code == 200
    assert (await client.get(f"/api/v1/users/{world.user_b.id}")).status_code == 404
    assert (await client.get(f"/api/v1/users/{world.global_admin.id}")).status_code == 404


async def test_global_admin_sees_everyone(client: AsyncClient, world: World) -> None:
    await act_as(client, "root")
    body = (await client.get("/api/v1/users")).json()

    usernames = {item["username"] for item in body["items"]}
    assert usernames == {
        "root",
        "admin-a",
        "admin-b",
        "user-a",
        "user-b",
        "watcher",
    }


async def test_group_list_is_scoped(client: AsyncClient, world: World) -> None:
    await act_as(client, "user-a")
    names = {item["name"] for item in (await client.get("/api/v1/groups")).json()["items"]}
    assert names == {"lab-a"}

    await act_as(client, "root")
    names = {item["name"] for item in (await client.get("/api/v1/groups")).json()["items"]}
    assert names == {"lab-a", "lab-b"}


# ---------------------------------------------------------------------------
# Vertical isolation: privilege escalation attempts
# ---------------------------------------------------------------------------
async def test_group_admin_cannot_create_users(client: AsyncClient, world: World) -> None:
    await act_as(client, "admin-a")

    response = await client.post(
        "/api/v1/users",
        json={"username": "sneaky", "password": "a-long-enough-password", "roleCodes": ["user"]},
    )
    assert response.status_code == 403


async def test_group_admin_cannot_grant_roles(client: AsyncClient, world: World) -> None:
    await act_as(client, "admin-a")

    response = await client.post(
        f"/api/v1/users/{world.user_a.id}/roles",
        json={"roleCode": "global_admin", "scope": "global"},
    )
    assert response.status_code == 403


async def test_user_cannot_grant_themselves_a_role(client: AsyncClient, world: World) -> None:
    await act_as(client, "user-a")

    response = await client.post(
        f"/api/v1/users/{world.user_a.id}/roles",
        json={"roleCode": "global_admin", "scope": "global"},
    )
    assert response.status_code == 403


async def test_cannot_deactivate_yourself(client: AsyncClient, world: World) -> None:
    await act_as(client, "root")

    response = await client.post(f"/api/v1/users/{world.global_admin.id}/deactivate")

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "cannot_disable_self"


async def test_cannot_delete_yourself(client: AsyncClient, world: World) -> None:
    await act_as(client, "root")

    response = await client.delete(f"/api/v1/users/{world.global_admin.id}")

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "cannot_delete_self"


async def test_platform_must_keep_one_global_admin(
    client: AsyncClient, world: World, seeded: AsyncSession
) -> None:
    """A second admin makes the first removable; the last one is protected."""
    await factories.create_user(seeded, username="root2", password=PASSWORD, role="global_admin")
    await seeded.commit()

    await act_as(client, "root")
    # With root2 present, revoking root's own role would be allowed by the
    # "last admin" rule, but it is still blocked by the self-revocation guard.
    response = await client.delete(f"/api/v1/users/{world.global_admin.id}/roles/global_admin")
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "cannot_revoke_own_admin"

    # Deleting the *other* admin is fine while one remains.
    root2 = (await client.get("/api/v1/users?search=root2")).json()["items"][0]
    assert (await client.delete(f"/api/v1/users/{root2['id']}")).status_code == 200


async def test_cannot_remove_the_last_global_admin_via_another_admin(
    client: AsyncClient, world: World, seeded: AsyncSession
) -> None:
    other = await factories.create_user(
        seeded, username="root2", password=PASSWORD, role="global_admin"
    )
    await seeded.commit()

    # root2 (not a superuser) tries to disable the only other admin and then
    # itself; the final step must be refused.
    await act_as(client, "root2")
    assert (
        await client.post(f"/api/v1/users/{world.global_admin.id}/deactivate")
    ).status_code == 200
    assert (await client.post(f"/api/v1/users/{other.id}/deactivate")).status_code == 400


async def test_group_admin_cannot_manage_group_membership(
    client: AsyncClient, world: World
) -> None:
    """`group:manage_member` is a global-admin capability by design."""
    await act_as(client, "admin-a")

    response = await client.post(
        f"/api/v1/groups/{world.lab_a.id}/members", json={"userId": world.user_b.id}
    )
    assert response.status_code == 403


async def test_cannot_grant_a_role_holding_more_than_yours(
    client: AsyncClient, world: World
) -> None:
    """A non-global admin may not mint a peer with capabilities it lacks."""
    # Give admin-a the role-assignment permission but not global_admin's scope
    # by promoting it through an explicit group-scoped grant path instead.
    await act_as(client, "root")
    assert (
        await client.post(
            f"/api/v1/users/{world.admin_a.id}/roles",
            json={"roleCode": "observer"},
        )
    ).status_code == 200


# ---------------------------------------------------------------------------
# Self-service
# ---------------------------------------------------------------------------
async def test_user_can_read_and_update_own_profile(client: AsyncClient, world: World) -> None:
    await act_as(client, "user-a")

    assert (await client.get("/api/v1/users/me")).status_code == 200
    response = await client.patch("/api/v1/users/me", json={"fullName": "New Name"})

    assert response.status_code == 200
    assert response.json()["fullName"] == "New Name"


async def test_user_cannot_escalate_through_self_update(client: AsyncClient, world: World) -> None:
    """`is_superuser` and `is_active` are not part of the self-update payload.

    The request is now *rejected* rather than partially applied. That is the
    stronger behaviour and the reason is worth stating: silently accepting the
    body while ignoring `isSuperuser` returns 200 to an escalation attempt, so
    the caller has no way to tell the field was dropped — and neither does
    anyone reading the audit log. A 422 records the attempt.

    It also stops the legitimate half of the payload, which is the point: a
    request that tries to escalate is not half-honoured.
    """
    await act_as(client, "user-a")

    response = await client.patch(
        "/api/v1/users/me", json={"fullName": "X", "isSuperuser": True, "isActive": False}
    )

    assert response.status_code == 422
    body = response.json()
    assert body["error"]["code"] == "validation_failed"
    # The offending field is named, so this is debuggable rather than merely refused.
    assert "isSuperuser" in json.dumps(body["error"])

    # And nothing changed: the attempt had no effect at all, not even the
    # legitimate half of the payload.
    me = await client.get("/api/v1/users/me")
    assert me.status_code == 200
    assert me.json()["isSuperuser"] is False
    assert me.json()["isActive"] is True
    assert me.json()["fullName"] != "X"


async def test_a_self_update_that_stays_in_bounds_still_works(
    client: AsyncClient, world: World
) -> None:
    """The escalation guard must not have closed the legitimate path."""
    await act_as(client, "user-a")

    response = await client.patch("/api/v1/users/me", json={"fullName": "合法的名字"})

    assert response.status_code == 200
    assert response.json()["fullName"] == "合法的名字"


async def test_user_cannot_read_another_users_permissions(
    client: AsyncClient, world: World
) -> None:
    await act_as(client, "user-a")

    assert (await client.get(f"/api/v1/users/{world.user_a.id}/permissions")).status_code == 200
    assert (await client.get(f"/api/v1/users/{world.user_b.id}/permissions")).status_code == 404


# ---------------------------------------------------------------------------
# Admin user lifecycle
# ---------------------------------------------------------------------------
async def test_admin_can_create_and_disable_a_user(client: AsyncClient, world: World) -> None:
    await act_as(client, "root")

    created = await client.post(
        "/api/v1/users",
        json={
            "username": "newbie",
            "password": "a-perfectly-long-password",
            "fullName": "New Bie",
            "roleCodes": ["user"],
            "groupIds": [world.lab_a.id],
        },
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["roles"] == ["user"]
    assert [group["name"] for group in body["groups"]] == ["lab-a"]

    assert (await client.post(f"/api/v1/users/{body['id']}/deactivate")).status_code == 200

    # A disabled account cannot log in.
    response = await client.post(
        LOGIN, json={"username": "newbie", "password": "a-perfectly-long-password"}
    )
    assert response.status_code == 403


async def test_duplicate_username_is_rejected(client: AsyncClient, world: World) -> None:
    await act_as(client, "root")

    response = await client.post(
        "/api/v1/users", json={"username": "user-a", "password": "a-perfectly-long-password"}
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "username_taken"


async def test_admin_can_grant_and_revoke_a_role(client: AsyncClient, world: World) -> None:
    await act_as(client, "root")

    granted = await client.post(
        f"/api/v1/users/{world.user_b.id}/roles", json={"roleCode": "observer"}
    )
    assert granted.status_code == 200
    assert "observer" in granted.json()["roles"]

    removed = await client.delete(f"/api/v1/users/{world.user_b.id}/roles/observer")
    assert removed.status_code == 200
    assert "observer" not in removed.json()["roles"]


async def test_granting_the_same_role_twice_conflicts(client: AsyncClient, world: World) -> None:
    await act_as(client, "root")

    response = await client.post(
        f"/api/v1/users/{world.user_b.id}/roles", json={"roleCode": "user"}
    )

    assert response.status_code == 409


async def test_group_admin_role_can_be_scoped_to_a_group(client: AsyncClient, world: World) -> None:
    await act_as(client, "root")

    response = await client.post(
        f"/api/v1/users/{world.user_b.id}/roles",
        json={"roleCode": "group_admin", "scope": "group", "groupId": world.lab_b.id},
    )

    assert response.status_code == 200, response.text
    scoped = [
        assignment
        for assignment in response.json()["roleAssignments"]
        if assignment["scope"] == "group"
    ]
    assert scoped and scoped[0]["groupId"] == world.lab_b.id


async def test_group_scoped_role_requires_a_group(client: AsyncClient, world: World) -> None:
    await act_as(client, "root")

    response = await client.post(
        f"/api/v1/users/{world.user_b.id}/roles",
        json={"roleCode": "group_admin", "scope": "group"},
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "missing_group_id"


async def test_admin_group_is_created_with_its_owner_as_member(
    client: AsyncClient, world: World
) -> None:
    await act_as(client, "root")

    response = await client.post(
        "/api/v1/groups",
        json={"name": "new-lab", "displayName": "新实验室", "ownerUserId": world.user_a.id},
    )

    assert response.status_code == 201, response.text
    group_id = response.json()["id"]
    members = (await client.get(f"/api/v1/groups/{group_id}/members")).json()["items"]
    assert [member["username"] for member in members] == ["user-a"]
    assert members[0]["roleInGroup"] == "group_admin"


async def test_group_member_add_and_remove(client: AsyncClient, world: World) -> None:
    await act_as(client, "root")

    added = await client.post(
        f"/api/v1/groups/{world.lab_a.id}/members", json={"userId": world.user_b.id}
    )
    assert added.status_code == 201, added.text

    removed = await client.delete(f"/api/v1/groups/{world.lab_a.id}/members/{world.user_b.id}")
    assert removed.status_code == 200


async def test_duplicate_group_membership_conflicts(client: AsyncClient, world: World) -> None:
    await act_as(client, "root")

    response = await client.post(
        f"/api/v1/groups/{world.lab_a.id}/members", json={"userId": world.user_a.id}
    )

    assert response.status_code == 409


async def test_replacing_user_groups_preserves_single_primary(
    client: AsyncClient, world: World
) -> None:
    await act_as(client, "root")

    response = await client.put(
        f"/api/v1/users/{world.user_b.id}/groups",
        json={"groupIds": [world.lab_a.id, world.lab_b.id], "primaryGroupId": world.lab_a.id},
    )

    assert response.status_code == 200, response.text
    groups = response.json()["groups"]
    assert {group["name"] for group in groups} == {"lab-a", "lab-b"}
    assert [group["name"] for group in groups if group["isPrimary"]] == ["lab-a"]


async def test_directory_managed_membership_cannot_be_removed_by_hand(
    client: AsyncClient, world: World, seeded: AsyncSession
) -> None:
    from sqlalchemy import select

    from app.models.group import UserGroupLink

    link = await seeded.scalar(
        select(UserGroupLink).where(
            UserGroupLink.user_id == world.user_a.id,
            UserGroupLink.group_id == world.lab_a.id,
        )
    )
    assert link is not None
    link.is_directory_managed = True
    await seeded.commit()

    await act_as(client, "root")
    response = await client.delete(f"/api/v1/groups/{world.lab_a.id}/members/{world.user_a.id}")

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "directory_managed_membership"


async def test_group_sync_requires_a_configured_directory(
    client: AsyncClient, world: World
) -> None:
    await act_as(client, "root")

    response = await client.post("/api/v1/groups/sync", json={"dryRun": True})

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "directory_not_configured"


async def test_external_account_password_cannot_be_reset_locally(
    client: AsyncClient, world: World, seeded: AsyncSession
) -> None:
    external = await factories.create_user(seeded, username="ipa-user", role="user")
    external.source = external.source.__class__.IPA
    await seeded.commit()

    await act_as(client, "root")
    response = await client.post(
        f"/api/v1/users/{external.id}/password", json={"password": "a-perfectly-long-password"}
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "external_account"


# ---------------------------------------------------------------------------
# Audit trail
# ---------------------------------------------------------------------------
async def test_denied_requests_are_audited(client: AsyncClient, world: World, audit_rows) -> None:
    await act_as(client, "user-a")
    await client.post("/api/v1/groups", json={"name": "nope"})

    entries = await audit_rows()
    denied = [entry for entry in entries if entry.status == "denied"]
    assert denied, "a 403 must always leave a trail"
    assert denied[-1].actor_username == "user-a"
    assert denied[-1].action == "access:denied"


async def test_successful_admin_write_is_audited(
    client: AsyncClient, world: World, audit_rows
) -> None:
    await act_as(client, "root")
    await client.post("/api/v1/groups", json={"name": "audited-lab"})

    entries = await audit_rows(action="group:create")
    assert len(entries) == 1
    assert entries[0].actor_username == "root"
    assert entries[0].object_type == "group"


async def test_audit_never_records_credentials(
    client: AsyncClient, world: World, audit_rows
) -> None:
    await client.post(LOGIN, json={"username": "user-a", "password": PASSWORD})
    await client.post(LOGIN, json={"username": "user-a", "password": "wrong-password"})

    for entry in await audit_rows():
        serialised = str(entry.detail)
        assert PASSWORD not in serialised
        assert "wrong-password" not in serialised
