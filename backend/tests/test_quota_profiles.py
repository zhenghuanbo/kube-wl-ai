"""Quota profiles: defining, editing and retiring compute ceilings.

These go through the HTTP layer with real permissions and a real database, because
the parts most likely to be wrong are exactly the ones a unit test would stub out:
whether the wire format is camelCase, whether a group administrator really cannot
write, and whether the invariants that protect a ceiling somebody is already on are
enforced on the path a request actually takes.
"""

from __future__ import annotations

from typing import Any

import pytest
from httpx import AsyncClient, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.models.enums import GroupRole, QuotaProfileKind
from app.models.user import User
from app.services import quota_profiles as service
from tests import factories

PASSWORD = "user-password"


def _error_code(response: Response) -> str:
    return str(response.json()["error"]["code"])


async def _login(client: AsyncClient, username: str) -> None:
    response = await client.post(
        "/api/v1/auth/login", json={"username": username, "password": PASSWORD}
    )
    assert response.status_code == 200, response.text
    client.headers["Authorization"] = f"Bearer {response.json()['accessToken']}"


async def _create(client: AsyncClient, **payload: Any) -> dict[str, Any]:
    response = await client.post("/api/v1/quota-profiles", json=payload)
    assert response.status_code == 201, response.text
    result: dict[str, Any] = response.json()
    return result


async def _default(client: AsyncClient, kind: str) -> dict[str, Any]:
    """The kind's default profile, which the fixture seeds from configuration."""
    listed = (await client.get(f"/api/v1/quota-profiles?kind={kind}")).json()
    return next(profile for profile in listed if profile["isDefault"])


# ---------------------------------------------------------------------------
# Seeding
# ---------------------------------------------------------------------------
async def test_seeding_the_defaults_comes_from_configuration(
    session: AsyncSession, settings: Settings
) -> None:
    """Nothing about the default ceilings may live in the code that creates them."""
    tuned = settings.model_copy(
        update={
            "default_user_quota_cpu_cores": 7,
            "default_user_quota_memory_gb": 32,
            "default_user_quota_gpu": 2,
            "default_user_quota_max_instances": 3,
            "default_user_quota_max_applications": 4,
            "default_user_quota_max_tasks": 5,
            "default_workspace_quota_cpu_cores": 9,
            "default_workspace_quota_memory_gb": 64,
            "default_workspace_quota_gpu": 1,
            "workspace_default_max_pods": 26,
            "workspace_default_max_deployments": 12,
            "workspace_default_max_services": 13,
            "workspace_default_max_jobs": 14,
        }
    )

    # The fixture already seeded the defaults from the same settings, so clear the
    # slate to observe the seeding itself rather than the fixture.
    for kind in QuotaProfileKind:
        seeded_default = await service.default_profile(session, kind)
        assert seeded_default is not None
        await session.delete(seeded_default)
    await session.flush()

    created = await service.ensure_default_profiles(session, tuned)
    assert {profile.kind for profile in created} == {
        QuotaProfileKind.USER,
        QuotaProfileKind.WORKSPACE,
    }

    user_profile = await service.default_profile(session, QuotaProfileKind.USER)
    assert user_profile is not None
    assert (user_profile.cpu_cores, user_profile.memory_gb, user_profile.gpu) == (7, 32, 2)
    assert (
        user_profile.max_instances,
        user_profile.max_applications,
        user_profile.max_tasks,
    ) == (3, 4, 5)
    # A dimension belonging to the other kind is absent, not defaulted to zero.
    assert user_profile.max_pods is None

    space_profile = await service.default_profile(session, QuotaProfileKind.WORKSPACE)
    assert space_profile is not None
    assert (space_profile.cpu_cores, space_profile.memory_gb, space_profile.gpu) == (9, 64, 1)
    assert (
        space_profile.max_pods,
        space_profile.max_deployments,
        space_profile.max_services,
        space_profile.max_jobs,
    ) == (26, 12, 13, 14)
    assert space_profile.max_instances is None

    # Idempotent: a second attempt must not create a second default.
    assert await service.ensure_default_profiles(session, tuned) == []


# ---------------------------------------------------------------------------
# Creating
# ---------------------------------------------------------------------------
async def test_creating_a_user_profile_takes_omitted_counts_from_config(
    admin_client: AsyncClient,
) -> None:
    body = await _create(
        admin_client,
        kind="user",
        name="small",
        displayName="小配额",
        cpuCores=4,
        memoryGb=8,
        gpu=0,
    )

    assert body["kind"] == "user"
    assert body["name"] == "small"
    assert body["displayName"] == "小配额"
    assert (body["cpuCores"], body["memoryGb"], body["gpu"]) == (4, 8, 0)
    # Not sent, so they must come from the configured defaults rather than zero.
    assert (body["maxInstances"], body["maxApplications"], body["maxTasks"]) == (10, 10, 10)
    assert body["maxPods"] is None
    # The kind's default already exists (the fixture seeds it from the same
    # settings), so this profile is simply another one. "The first profile of a kind
    # becomes its default" is asserted where seeding is observed.
    assert body["isDefault"] is False
    assert body["isActive"] is True


async def test_a_later_profile_of_the_same_kind_is_not_the_default(
    admin_client: AsyncClient,
) -> None:
    await _create(admin_client, kind="user", name="first")
    second = await _create(admin_client, kind="user", name="second")
    assert second["isDefault"] is False


async def test_minus_one_means_the_dimension_is_not_checked(
    admin_client: AsyncClient,
) -> None:
    body = await _create(admin_client, kind="user", name="wide", cpuCores=-1, memoryGb=-1, gpu=-1)
    assert (body["cpuCores"], body["memoryGb"], body["gpu"]) == (-1, -1, -1)


async def test_zero_still_means_none_allowed(admin_client: AsyncClient) -> None:
    """The sentinel must not swallow the legitimate value next to it."""
    body = await _create(admin_client, kind="user", name="none", gpu=0)
    assert body["gpu"] == 0


async def test_a_workspace_profile_carries_the_namespace_counts(
    admin_client: AsyncClient,
) -> None:
    body = await _create(admin_client, kind="workspace", name="big", maxPods=450)
    assert body["kind"] == "workspace"
    assert body["maxPods"] == 450
    # The rest come from configuration.
    assert (body["maxDeployments"], body["maxServices"], body["maxJobs"]) == (200, 400, 200)
    assert body["maxInstances"] is None


async def test_workspace_counts_that_cannot_all_hold_are_refused(
    admin_client: AsyncClient,
) -> None:
    """A pod ceiling below deployments + jobs makes the other two unreachable."""
    response = await admin_client.post(
        "/api/v1/quota-profiles",
        json={
            "kind": "workspace",
            "name": "unsatisfiable",
            "maxDeployments": 20,
            "maxJobs": 10,
            "maxPods": 25,
        },
    )
    assert response.status_code == 400, response.text
    assert response.json()["error"]["code"] == "quota_profile_counts_unsatisfiable"


async def test_a_pod_ceiling_exactly_covering_the_others_is_accepted(
    admin_client: AsyncClient,
) -> None:
    """Equality is the boundary and is satisfiable, so it must not be refused."""
    body = await _create(
        admin_client,
        kind="workspace",
        name="tight",
        maxDeployments=20,
        maxJobs=10,
        maxPods=30,
    )
    assert body["maxPods"] == 30


async def test_an_unchecked_pod_ceiling_cannot_contradict_the_others(
    admin_client: AsyncClient,
) -> None:
    """``-1`` removes the pods key entirely, so there is nothing to contradict."""
    body = await _create(
        admin_client,
        kind="workspace",
        name="nopods",
        maxDeployments=200,
        maxJobs=200,
        maxPods=-1,
    )
    assert body["maxPods"] == -1


async def test_the_count_rule_is_checked_on_the_merged_profile_not_the_payload(
    admin_client: AsyncClient,
) -> None:
    """A patch carries one field; the contradiction appears only after merging."""
    created = await _create(admin_client, kind="workspace", name="merge", maxPods=500)
    response = await admin_client.patch(
        f"/api/v1/quota-profiles/{created['id']}", json={"maxDeployments": 600}
    )
    assert response.status_code == 400, response.text
    assert response.json()["error"]["code"] == "quota_profile_counts_unsatisfiable"


async def test_the_same_name_may_exist_under_both_kinds(admin_client: AsyncClient) -> None:
    """`default` has to be meaningful for users *and* for workspaces."""
    await _create(admin_client, kind="user", name="shared")
    await _create(admin_client, kind="workspace", name="shared")


async def test_a_duplicate_name_within_a_kind_is_refused(admin_client: AsyncClient) -> None:
    await _create(admin_client, kind="user", name="dup")
    response = await admin_client.post(
        "/api/v1/quota-profiles", json={"kind": "user", "name": "dup"}
    )
    assert response.status_code == 409
    assert _error_code(response) == "quota_profile_name_taken"


@pytest.mark.parametrize(
    ("kind", "field"),
    [
        ("user", "maxPods"),
        ("user", "maxDeployments"),
        ("workspace", "maxInstances"),
        ("workspace", "maxTasks"),
    ],
)
async def test_a_dimension_from_the_other_kind_is_refused(
    admin_client: AsyncClient, kind: str, field: str
) -> None:
    """Accepting the field silently would look exactly like it had taken effect."""
    response = await admin_client.post(
        "/api/v1/quota-profiles",
        json={"kind": kind, "name": f"bad-{field}", field: 5},
    )
    assert response.status_code == 422, response.text


async def test_an_unknown_kind_is_refused(admin_client: AsyncClient) -> None:
    response = await admin_client.post("/api/v1/quota-profiles", json={"kind": "team", "name": "x"})
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# Editing
# ---------------------------------------------------------------------------
async def test_promoting_a_profile_demotes_the_previous_default(
    admin_client: AsyncClient,
) -> None:
    incumbent = await _default(admin_client, "user")
    promoted = await _create(admin_client, kind="user", name="second")
    assert promoted["isDefault"] is False

    response = await admin_client.patch(
        f"/api/v1/quota-profiles/{promoted['id']}", json={"isDefault": True}
    )
    assert response.status_code == 200, response.text
    assert response.json()["isDefault"] is True

    listed = (await admin_client.get("/api/v1/quota-profiles?kind=user")).json()
    assert [profile["id"] for profile in listed if profile["isDefault"]] == [promoted["id"]]
    # The one that used to be the default is now an ordinary profile. This is the
    # half that matters: two defaults would make the fallback ambiguous.
    demoted = next(profile for profile in listed if profile["id"] == incumbent["id"])
    assert demoted["isDefault"] is False


async def test_a_dimension_from_the_other_kind_is_refused_on_update(
    admin_client: AsyncClient,
) -> None:
    profile = await _create(admin_client, kind="user", name="u")
    response = await admin_client.patch(
        f"/api/v1/quota-profiles/{profile['id']}", json={"maxPods": 3}
    )
    assert response.status_code == 400
    assert _error_code(response) == "quota_profile_dimension_mismatch"


async def test_the_default_cannot_be_unset_deactivated_or_deleted(
    admin_client: AsyncClient,
) -> None:
    profile = await _default(admin_client, "user")

    unset = await admin_client.patch(
        f"/api/v1/quota-profiles/{profile['id']}", json={"isDefault": False}
    )
    assert unset.status_code == 400
    assert _error_code(unset) == "quota_profile_default_required"

    deactivate = await admin_client.patch(
        f"/api/v1/quota-profiles/{profile['id']}", json={"isActive": False}
    )
    assert deactivate.status_code == 400
    assert _error_code(deactivate) == "quota_profile_default_required"

    delete = await admin_client.delete(f"/api/v1/quota-profiles/{profile['id']}")
    assert delete.status_code == 400
    assert _error_code(delete) == "quota_profile_default_required"


async def test_a_profile_somebody_is_on_cannot_be_deleted_or_deactivated(
    admin_client: AsyncClient, admin: User, seeded: AsyncSession
) -> None:
    await _create(admin_client, kind="user", name="default-ish")
    attached = await _create(admin_client, kind="user", name="individual")

    admin.quota_profile_id = attached["id"]
    await seeded.commit()

    delete = await admin_client.delete(f"/api/v1/quota-profiles/{attached['id']}")
    assert delete.status_code == 409
    assert _error_code(delete) == "quota_profile_in_use"

    deactivate = await admin_client.patch(
        f"/api/v1/quota-profiles/{attached['id']}", json={"isActive": False}
    )
    assert deactivate.status_code == 409
    assert _error_code(deactivate) == "quota_profile_in_use"


async def test_an_unused_profile_can_be_deleted(admin_client: AsyncClient) -> None:
    await _create(admin_client, kind="user", name="kept")
    doomed = await _create(admin_client, kind="user", name="doomed")

    response = await admin_client.delete(f"/api/v1/quota-profiles/{doomed['id']}")
    assert response.status_code == 200, response.text

    assert (await admin_client.get(f"/api/v1/quota-profiles/{doomed['id']}")).status_code == 404


async def test_patching_an_unknown_profile_is_a_404(admin_client: AsyncClient) -> None:
    response = await admin_client.patch("/api/v1/quota-profiles/9999", json={"gpu": 1})
    assert response.status_code == 404
    assert _error_code(response) == "quota_profile_not_found"


# ---------------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------------
async def test_listing_can_be_filtered_by_kind(admin_client: AsyncClient) -> None:
    await _create(admin_client, kind="user", name="u")
    await _create(admin_client, kind="workspace", name="w")

    # The fixture seeds one default per kind, which is listed ahead of the rest.
    users = (await admin_client.get("/api/v1/quota-profiles?kind=user")).json()
    assert [profile["name"] for profile in users] == ["default", "u"]

    spaces = (await admin_client.get("/api/v1/quota-profiles?kind=workspace")).json()
    assert [profile["name"] for profile in spaces] == ["default", "w"]

    everything = (await admin_client.get("/api/v1/quota-profiles")).json()
    assert len(everything) == 4


# ---------------------------------------------------------------------------
# Assigning
# ---------------------------------------------------------------------------
async def test_a_user_can_be_switched_to_a_profile_and_back(
    admin_client: AsyncClient, seeded: AsyncSession
) -> None:
    """``None`` is the assignment that means "follow the default"."""
    created = await _create(admin_client, kind="user", name="tight", maxInstances=1)
    someone = await factories.create_user(seeded, username="assigned")
    await seeded.commit()

    attached = await admin_client.put(
        f"/api/v1/users/{someone.id}/quota-profile", json={"profileId": created["id"]}
    )
    assert attached.status_code == 200, attached.text
    assert attached.json()["quotaProfileId"] == created["id"]

    cleared = await admin_client.put(
        f"/api/v1/users/{someone.id}/quota-profile", json={"profileId": None}
    )
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["quotaProfileId"] is None


async def test_a_workspace_profile_cannot_be_attached_to_a_user(
    admin_client: AsyncClient, seeded: AsyncSession
) -> None:
    """Wrong kind is refused, not ignored: silently falling back would change a ceiling."""
    space = await _create(admin_client, kind="workspace", name="space-only")
    someone = await factories.create_user(seeded, username="crossed")
    await seeded.commit()

    response = await admin_client.put(
        f"/api/v1/users/{someone.id}/quota-profile", json={"profileId": space["id"]}
    )
    assert response.status_code == 400, response.text
    assert _error_code(response) == "quota_profile_kind_mismatch"


async def test_a_deactivated_profile_cannot_be_attached(
    admin_client: AsyncClient, seeded: AsyncSession
) -> None:
    retired = await _create(admin_client, kind="user", name="retired")
    off = await admin_client.patch(
        f"/api/v1/quota-profiles/{retired['id']}", json={"isActive": False}
    )
    assert off.status_code == 200, off.text
    someone = await factories.create_user(seeded, username="late")
    await seeded.commit()

    response = await admin_client.put(
        f"/api/v1/users/{someone.id}/quota-profile", json={"profileId": retired["id"]}
    )
    assert response.status_code == 400, response.text
    assert _error_code(response) == "quota_profile_inactive"


async def test_a_group_admin_cannot_assign_a_profile(
    client: AsyncClient, seeded: AsyncSession
) -> None:
    """`quota_profile:assign` is its own code, so `user:update` does not imply it."""
    lab = await factories.create_group(seeded, name="lab-b")
    await factories.create_user(
        seeded,
        username="ga-quota",
        password=PASSWORD,
        role="group_admin",
        group=lab,
        group_role=GroupRole.GROUP_ADMIN,
    )
    target = await factories.create_user(seeded, username="quota-target")
    await seeded.commit()
    await _login(client, "ga-quota")

    response = await client.put(
        f"/api/v1/users/{target.id}/quota-profile", json={"profileId": None}
    )

    assert response.status_code == 403, response.text


# ---------------------------------------------------------------------------
# Permissions
# ---------------------------------------------------------------------------
async def test_a_group_admin_may_read_but_not_write(
    client: AsyncClient, seeded: AsyncSession
) -> None:
    lab = await factories.create_group(seeded, name="lab-a")
    await factories.create_user(
        seeded,
        username="ga",
        password=PASSWORD,
        role="group_admin",
        group=lab,
        group_role=GroupRole.GROUP_ADMIN,
    )
    await seeded.commit()
    await _login(client, "ga")

    assert (await client.get("/api/v1/quota-profiles")).status_code == 200

    create = await client.post("/api/v1/quota-profiles", json={"kind": "user", "name": "x"})
    assert create.status_code == 403


async def test_a_normal_user_cannot_even_read_the_catalogue(
    client: AsyncClient, seeded: AsyncSession
) -> None:
    """Ceilings are between the platform and its administrators, not public."""
    lab = await factories.create_group(seeded, name="lab-a")
    await factories.create_user(seeded, username="plain", password=PASSWORD, role="user", group=lab)
    await seeded.commit()
    await _login(client, "plain")

    assert (await client.get("/api/v1/quota-profiles")).status_code == 403
