"""Phase 5 acceptance: a storage volume is a directory.

The rule this file exists to pin is **one directory, one volume**. A volume is no
longer an allocation carved out of a pool; it is the platform's name for a
directory somebody provisioned outside the platform, and the platform's whole job
is to keep the row and the directory in agreement. Almost everything else follows
from that:

* the directory is *derived*, never chosen by the caller — a user's volume is
  ``{root}/{group}/{username}``, a group share is a named child of the group's own
  directory, a group share with no name *is* the group's directory, and a global
  share is ``{root}/_global/{name}``;
* because a directory is an identity, two live rows may not name the same one —
  and the database is the arbiter, not a pre-flight check that two concurrent
  creates would both pass;
* because the directory already exists and already holds somebody's data, the
  volume is *statically* provisioned (a ``hostPath`` PersistentVolume pre-bound to
  its claim, claim pinned with no StorageClass) and the recorded capacity is
  accounting rather than enforcement;
* a group's own directory contains every member's home, so it is invisible and
  unmountable to a plain member and reachable only by the group's administrators;
* placing a directory is an administrator's act — a mistake in the layout is not
  self-correcting — so *every* mutation is gated on ``storage:manage``, which only
  a global administrator holds;
* retiring a volume deletes the cluster objects and keeps the record, because the
  partial unique index is what lets the directory be handed to somebody else while
  the history survives.

Every cluster interaction goes through ``FakeK8sGateway``, so these assertions are
about what the platform *submits* — which is the part it is responsible for.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.errors import BadRequestError
from app.k8s import FakeK8sGateway
from app.k8s.gateway import PvcInfo, PvInfo
from app.models.enums import GroupKind, GroupRole, RoleScope, StorageKind
from app.models.group import Group
from app.models.storage import Storage
from app.models.user import User
from app.services import storage_paths
from app.services.storages import resolve_mount_paths
from tests import factories

PASSWORD = "correct-horse-battery"
LOGIN = "/api/v1/auth/login"
STORAGES = "/api/v1/storages"


@dataclass
class World:
    lab_a: Group
    lab_b: Group
    global_admin: User
    admin_a: User
    user_a: User
    user_b: User
    observer: User
    pool_id: int
    spec_id: int
    workspace_id: int
    namespace: str


async def act_as(client: AsyncClient, username: str) -> AsyncClient:
    """Point the client at one of the seeded accounts.

    The permission model is the subject of several tests here, so each test says
    which account it acts as instead of inheriting whatever the last request left
    behind.
    """
    client.headers.pop("Authorization", None)
    response = await client.post(LOGIN, json={"username": username, "password": PASSWORD})
    assert response.status_code == 200, response.text
    client.headers["Authorization"] = f"Bearer {response.json()['accessToken']}"
    return client


async def build_world(admin_client: AsyncClient, seeded: AsyncSession) -> World:
    lab_a = await factories.create_group(seeded, name="s5-lab-a", kind=GroupKind.LAB)
    lab_b = await factories.create_group(seeded, name="s5-lab-b", kind=GroupKind.LAB)

    global_admin = await factories.create_user(
        seeded, username="s5-root", password=PASSWORD, role="global_admin"
    )
    admin_a = await factories.create_user(
        seeded,
        username="s5-boss",
        password=PASSWORD,
        role="group_admin",
        group=lab_a,
        group_role=GroupRole.GROUP_ADMIN,
    )
    user_a = await factories.create_user(
        seeded, username="s5-dana", password=PASSWORD, role="user", group=lab_a
    )
    # ``s5-erin`` belongs to the lab that deliberately has *no* workspace: a group
    # with nowhere to put a claim is a case the storage rules have to answer.
    user_b = await factories.create_user(
        seeded, username="s5-erin", password=PASSWORD, role="user", group=lab_b
    )
    observer = await factories.create_user(
        seeded, username="s5-gazer", password=PASSWORD, role="observer", group=lab_a
    )
    await seeded.commit()

    pool = await admin_client.post(
        "/api/v1/resource-groups",
        json={
            "name": "s5-pool",
            "displayName": "存储测试池",
            "kind": "public",
            "networkType": "internal",
            "maxRuntimeSeconds": 3600,
        },
    )
    assert pool.status_code == 201, pool.text
    pool_id = pool.json()["id"]

    spec = await admin_client.post(
        "/api/v1/resource-specs",
        json={
            "name": "s5-small",
            "displayName": "小规格",
            "cpuMilli": 1000,
            "memoryMb": 1024,
            "gpuType": "none",
            "gpuCount": 0,
        },
    )
    assert spec.status_code == 201, spec.text
    spec_id = spec.json()["id"]
    await admin_client.put(f"/api/v1/resource-groups/{pool_id}/specs", json=[spec_id])

    # One group, one workspace: the namespace of this workspace is where every
    # volume of the group gets its claim.
    workspace = await admin_client.post(
        "/api/v1/workspaces",
        json={"name": "s5-alpha", "groupId": lab_a.id, "resourceGroupIds": [pool_id]},
    )
    assert workspace.status_code == 201, workspace.text
    workspace_id = workspace.json()["id"]
    namespace = workspace.json()["k8sNamespace"]

    # Membership is what lets an account own a workload; a group share is visible
    # to the group regardless, so `s5-gazer` is moved in to prove that being a
    # member of the group is enough to *read* a share without being enough to
    # mount the group's own directory.
    for account in (user_a, admin_a, observer):
        await add_member(admin_client, workspace_id, account.id)

    return World(
        lab_a=lab_a,
        lab_b=lab_b,
        global_admin=global_admin,
        admin_a=admin_a,
        user_a=user_a,
        user_b=user_b,
        observer=observer,
        pool_id=pool_id,
        spec_id=spec_id,
        workspace_id=workspace_id,
        namespace=namespace,
    )


async def add_member(admin_client: AsyncClient, workspace_id: int, user_id: int) -> None:
    response = await admin_client.post(
        f"/api/v1/workspaces/{workspace_id}/members",
        json={"userId": user_id, "moveFromExisting": True},
    )
    assert response.status_code in (200, 201), response.text


@pytest.fixture
async def world(admin_client: AsyncClient, seeded: AsyncSession) -> World:
    return await build_world(admin_client, seeded)


def instance_payload(world: World, name: str, **overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "name": name,
        "workspaceId": world.workspace_id,
        "ownerUserId": world.user_a.id,
        "resourceGroupId": world.pool_id,
        "resourceSpecId": world.spec_id,
        "image": "registry.local/base:1",
        "ideType": "none",
        "sshEnabled": False,
        "storageGb": 5,
    }
    payload.update(overrides)
    return payload


async def make_storage(
    client: AsyncClient, name: str, **overrides: object
) -> tuple[int, dict[str, object]]:
    """Place a volume. The caller must already be acting as a global admin."""
    payload: dict[str, object] = {"name": name, "capacityGb": 10, "kind": "global_share"}
    payload.update(overrides)
    response = await client.post(STORAGES, json=payload)
    assert response.status_code == 201, response.text
    body = response.json()
    return int(body["id"]), body


async def make_instance(client: AsyncClient, world: World, name: str, **overrides: object) -> int:
    response = await client.post(
        "/api/v1/instances", json=instance_payload(world, name, **overrides)
    )
    assert response.status_code == 201, response.text
    return int(response.json()["id"])


def error_code(response: object) -> str:
    return response.json()["error"]["code"]  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# The layout: the directory is derived from who the volume belongs to
# ---------------------------------------------------------------------------
async def test_a_user_volume_is_the_users_directory_inside_their_group(
    client: AsyncClient, world: World, settings: Settings
) -> None:
    """``{root}/{group}/{username}`` — the group comes from the user, not the caller.

    Taking the group from the request would let an administrator place one group's
    user inside another group's directory, which is precisely the confusion the
    derived path exists to make impossible.
    """
    await act_as(client, "s5-root")
    _, home = await make_storage(client, "home", kind="user_volume", ownerUserId=world.user_a.id)

    assert home["kind"] == "user_volume"
    assert home["hostPath"] == f"{settings.storage_root}/s5-lab-a/s5-dana"
    assert home["dirName"] is None
    assert home["isGroupLevel"] is False
    assert home["defaultMountPath"] == "/home/s5-dana"


async def test_a_named_group_share_is_a_child_of_the_group_directory(
    client: AsyncClient, world: World, settings: Settings
) -> None:
    await act_as(client, "s5-root")
    _, share = await make_storage(
        client, "scratch", kind="group_share", groupId=world.lab_a.id, dirName="scratch"
    )

    assert share["hostPath"] == f"{settings.storage_root}/s5-lab-a/scratch"
    assert share["dirName"] == "scratch"
    assert share["isGroupLevel"] is False
    assert share["defaultMountPath"] == "/mnt/group_shared"


async def test_a_group_share_without_a_directory_name_is_the_group_directory(
    client: AsyncClient, world: World, settings: Settings
) -> None:
    """Omitting the directory name is meaningful, not "unset".

    ``dir_name IS NULL`` is the recorded form of "this volume *is* the group's
    directory". It has to be a column rather than something re-derived from the
    path, because the visibility rule is a SQL predicate and a Python post-filter
    would silently break pagination.
    """
    await act_as(client, "s5-root")
    _, root_volume = await make_storage(
        client, "s5-whole-group", kind="group_share", groupId=world.lab_a.id
    )

    assert root_volume["hostPath"] == f"{settings.storage_root}/s5-lab-a"
    assert root_volume["dirName"] is None
    assert root_volume["isGroupLevel"] is True


async def test_a_global_share_lives_outside_every_group(
    client: AsyncClient, world: World, settings: Settings
) -> None:
    await act_as(client, "s5-root")
    _, share = await make_storage(client, "public-data", kind="global_share")

    assert share["hostPath"] == f"{settings.storage_root}/_global/public-data"
    assert share["workspaceId"] is None
    assert share["groupId"] is None
    assert share["ownerUserId"] is None
    assert share["defaultMountPath"] == "/mnt/global_shared"


def test_the_root_and_the_global_segment_are_configurable(settings: Settings) -> None:
    """The installation prefix is a deployment decision, not a constant.

    ``/storage/kube-wl`` is the default, not a rule: an operator who puts the
    filesystem somewhere else must be able to say so without editing code, and the
    four kinds must move together.
    """
    elsewhere = settings.model_copy(
        update={"storage_root": "/hpc", "storage_global_segment": "shared"}
    )

    assert (
        storage_paths.volume_path(
            elsewhere, StorageKind.USER_VOLUME, group_name="g", username="u", name="n"
        )
        == "/hpc/g/u"
    )
    assert (
        storage_paths.volume_path(
            elsewhere, StorageKind.GROUP_SHARE, group_name="g", dir_name="d", name="n"
        )
        == "/hpc/g/d"
    )
    assert (
        storage_paths.volume_path(elsewhere, StorageKind.GROUP_SHARE, group_name="g", name="n")
        == "/hpc/g"
    )
    assert (
        storage_paths.volume_path(elsewhere, StorageKind.GLOBAL_SHARE, name="n") == "/hpc/shared/n"
    )


def test_a_relative_root_is_refused(settings: Settings) -> None:
    """A relative root would resolve against whatever the process happened to be in."""
    relative = settings.model_copy(update={"storage_root": "storage/kube-wl"})

    with pytest.raises(BadRequestError) as raised:
        storage_paths.volume_path(relative, StorageKind.GLOBAL_SHARE, name="n")
    assert raised.value.code == "invalid_storage_root"


@pytest.mark.parametrize("dir_name", ["..", "../escape", "a/b", ".hidden", "a\\b"])
async def test_a_directory_name_that_is_not_one_component_is_refused(
    client: AsyncClient, world: World, dir_name: str
) -> None:
    """Refused by name rather than slugged.

    ``k8s.naming.slug`` maps ``Lab_Vision`` to ``lab-vision`` — a directory that
    does not exist. The layout is provisioned outside the platform, so an
    identifier the platform cannot use verbatim is an error, not something to
    rewrite silently.
    """
    await act_as(client, "s5-root")
    response = await client.post(
        STORAGES,
        json={
            "name": "escape",
            "kind": "group_share",
            "groupId": world.lab_a.id,
            "dirName": dir_name,
        },
    )

    assert response.status_code == 400, response.text
    assert error_code(response) == "invalid_path_component"


# ---------------------------------------------------------------------------
# One directory, one volume
# ---------------------------------------------------------------------------
async def test_a_live_volume_blocks_a_second_claim_on_its_directory(
    client: AsyncClient, world: World
) -> None:
    """The message names the volume that won, which is what an admin needs to know."""
    await act_as(client, "s5-root")
    _, home = await make_storage(client, "home", kind="user_volume", ownerUserId=world.user_a.id)

    response = await client.post(
        STORAGES,
        json={
            "name": "s5-dana",
            "kind": "group_share",
            "groupId": world.lab_a.id,
            "dirName": "s5-dana",
        },
    )

    assert response.status_code == 409, response.text
    assert error_code(response) == "storage_path_taken"
    assert "home" in response.json()["error"]["message"]
    assert home["hostPath"] in response.json()["error"]["message"]


async def test_a_user_volume_ignores_a_directory_name(client: AsyncClient, world: World) -> None:
    """A user's directory is decided by their username, so ``dirName`` must not move it."""
    await act_as(client, "s5-root")
    _, home = await make_storage(
        client, "home", kind="user_volume", ownerUserId=world.user_a.id, dirName="sub"
    )

    assert home["hostPath"].endswith("/s5-lab-a/s5-dana")
    assert home["dirName"] is None


async def test_one_user_cannot_have_two_volumes(client: AsyncClient, world: World) -> None:
    await act_as(client, "s5-root")
    await make_storage(client, "home", kind="user_volume", ownerUserId=world.user_a.id)

    response = await client.post(
        STORAGES,
        json={"name": "home-again", "kind": "user_volume", "ownerUserId": world.user_a.id},
    )

    assert response.status_code == 409, response.text
    assert error_code(response) == "storage_path_taken"


async def test_two_volumes_in_one_workspace_cannot_share_a_name(
    client: AsyncClient, world: World
) -> None:
    """The claim name is derived from it, so two rows would fight on every reconcile."""
    await act_as(client, "s5-root")
    await make_storage(client, "shared", kind="group_share", groupId=world.lab_a.id, dirName="one")

    response = await client.post(
        STORAGES,
        json={"name": "shared", "kind": "group_share", "groupId": world.lab_a.id, "dirName": "two"},
    )

    assert response.status_code == 409, response.text
    assert error_code(response) == "storage_name_taken"


async def test_retiring_a_volume_frees_its_directory(client: AsyncClient, world: World) -> None:
    """The uniqueness is partial on ``is_active``, and this is why.

    A retired row keeps its path so the record survives. If that kept occupying the
    directory, deleting a volume would make its directory permanently unusable —
    the opposite of what deleting it is for.
    """
    await act_as(client, "s5-root")
    first_id, home = await make_storage(
        client, "home", kind="user_volume", ownerUserId=world.user_a.id
    )
    assert (await client.delete(f"{STORAGES}/{first_id}")).status_code == 200

    # Same directory *and* same name: both indexes are partial, so both are free.
    second_id, again = await make_storage(
        client, "home", kind="user_volume", ownerUserId=world.user_a.id
    )

    assert second_id != first_id
    assert again["hostPath"] == home["hostPath"]

    retired = await client.get(f"{STORAGES}/{first_id}")
    assert retired.status_code == 200
    assert retired.json()["isActive"] is False


# ---------------------------------------------------------------------------
# The group's own directory
# ---------------------------------------------------------------------------
async def test_the_group_directory_is_the_parent_of_every_members_home(
    client: AsyncClient, world: World
) -> None:
    """The relationship is structural, which is why a member may not be handed it."""
    await act_as(client, "s5-root")
    _, home = await make_storage(client, "home", kind="user_volume", ownerUserId=world.user_a.id)
    _, whole_group = await make_storage(
        client, "s5-whole-group", kind="group_share", groupId=world.lab_a.id
    )

    assert Path(home["hostPath"]).parent == Path(whole_group["hostPath"])


async def test_a_plain_member_cannot_see_the_group_directory(
    client: AsyncClient, world: World
) -> None:
    """404 rather than 403: a refusal would confirm that the directory exists."""
    await act_as(client, "s5-root")
    volume_id, _ = await make_storage(
        client, "s5-whole-group", kind="group_share", groupId=world.lab_a.id
    )

    await act_as(client, "s5-dana")
    response = await client.get(f"{STORAGES}/{volume_id}")

    assert response.status_code == 404, response.text
    assert error_code(response) == "storage_not_found"


async def test_a_plain_member_cannot_mount_the_group_directory(
    client: AsyncClient, world: World
) -> None:
    """Visibility is checked before anything that describes the volume."""
    await act_as(client, "s5-root")
    volume_id, _ = await make_storage(
        client, "s5-whole-group", kind="group_share", groupId=world.lab_a.id
    )

    await act_as(client, "s5-dana")
    response = await client.post(
        "/api/v1/instances",
        json=instance_payload(world, "peek", storageIds=[volume_id]),
    )

    assert response.status_code == 404, response.text
    assert error_code(response) == "storage_not_found"


async def test_a_plain_member_is_not_offered_the_group_directory(
    client: AsyncClient, world: World
) -> None:
    await act_as(client, "s5-root")
    await make_storage(client, "s5-whole-group", kind="group_share", groupId=world.lab_a.id)
    await make_storage(
        client, "scratch", kind="group_share", groupId=world.lab_a.id, dirName="scratch"
    )

    await act_as(client, "s5-dana")
    response = await client.get(
        f"{STORAGES}/attachable", params={"workspaceId": world.workspace_id}
    )

    assert response.status_code == 200, response.text
    offered = [row["name"] for row in response.json()]
    assert "scratch" in offered
    assert "s5-whole-group" not in offered


async def test_a_group_admin_can_see_and_mount_the_group_directory(
    client: AsyncClient, world: World, gateway: FakeK8sGateway
) -> None:
    """The group administrator is who the group's own directory exists for.

    Analysing a whole group's usage means reading the directory that holds every
    member's home — which is exactly why nobody else may.
    """
    await act_as(client, "s5-root")
    volume_id, whole_group = await make_storage(
        client, "s5-whole-group", kind="group_share", groupId=world.lab_a.id
    )

    await act_as(client, "s5-boss")
    detail = await client.get(f"{STORAGES}/{volume_id}")
    assert detail.status_code == 200, detail.text
    assert detail.json()["isGroupLevel"] is True

    instance_id = await make_instance(
        client, world, "audit", ownerUserId=world.admin_a.id, storageIds=[volume_id]
    )
    deployment = gateway.deployments[f"{world.namespace}/wl-audit-{instance_id}"]
    assert [mount.mount_path for mount in deployment.extra_mounts] == ["/mnt/group_shared"]
    assert whole_group["isGroupLevel"] is True


async def test_a_group_admin_by_role_grant_can_see_and_mount_the_group_directory(
    client: AsyncClient, world: World, seeded: AsyncSession, admin_client: AsyncClient
) -> None:
    """Both sources of group-admin authority mean the same thing to storage.

    ``resolve_scope`` deliberately unions two of them: the ``group_admin`` flag on
    the membership, and a group-scoped ``group_admin`` role assignment. Storage has
    to agree with itself about which set that is — visibility and the mount rule
    must consult the *same* one, or an account can be shown a volume it is then
    refused. This account holds only the second kind.
    """
    by_role = await factories.create_user(
        seeded, username="s5-by-role", password=PASSWORD, role="user", group=world.lab_a
    )
    await factories.assign_role(
        seeded, by_role, "group_admin", scope=RoleScope.GROUP, group=world.lab_a
    )
    await seeded.commit()
    await add_member(admin_client, world.workspace_id, by_role.id)

    await act_as(client, "s5-root")
    volume_id, _ = await make_storage(
        client, "s5-whole-group", kind="group_share", groupId=world.lab_a.id
    )

    await act_as(client, "s5-by-role")
    assert (await client.get(f"{STORAGES}/{volume_id}")).status_code == 200
    listing = await client.get(STORAGES, params={"pageSize": 50})
    assert volume_id in [row["id"] for row in listing.json()["items"]]

    response = await client.post(
        "/api/v1/instances",
        json=instance_payload(
            world, "audit-by-role", ownerUserId=by_role.id, storageIds=[volume_id]
        ),
    )

    assert response.status_code == 201, response.text


async def test_a_member_with_neither_source_of_authority_is_refused(
    client: AsyncClient, world: World
) -> None:
    """The reverse of the rule above: no membership flag and no role grant means no access.

    A plain member is a member of the group, which is enough to read a *named*
    share and deliberately not enough to read the directory holding their
    colleagues' homes.
    """
    await act_as(client, "s5-root")
    volume_id, _ = await make_storage(
        client, "s5-whole-group", kind="group_share", groupId=world.lab_a.id
    )

    await act_as(client, "s5-dana")
    assert (await client.get(f"{STORAGES}/{volume_id}")).status_code == 404
    listing = await client.get(STORAGES, params={"pageSize": 50})
    assert volume_id not in [row["id"] for row in listing.json()["items"]]

    response = await client.post(
        "/api/v1/instances",
        json=instance_payload(world, "peek", storageIds=[volume_id]),
    )

    assert response.status_code == 404, response.text
    assert error_code(response) == "storage_not_found"


# ---------------------------------------------------------------------------
# Who may place a volume
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("username", ["s5-dana", "s5-boss", "s5-gazer", "s5-erin"])
async def test_only_a_global_admin_may_place_a_volume(
    client: AsyncClient, world: World, username: str
) -> None:
    """Not the group's administrator either.

    Even the group's own directory is created *for* them: the layout is provisioned
    outside the platform, so changing it is not a per-group decision.
    """
    await act_as(client, username)
    response = await client.post(STORAGES, json={"name": "mine", "kind": "global_share"})

    assert response.status_code == 403, response.text
    assert error_code(response) == "permission_denied"


async def test_storage_manage_is_held_only_by_the_global_admin(
    client: AsyncClient, world: World
) -> None:
    """One code for every mutation, and exactly one audience for it."""
    held: dict[str, set[str]] = {}
    for username in ("s5-root", "s5-boss", "s5-dana", "s5-gazer"):
        await act_as(client, username)
        response = await client.get("/api/v1/auth/permissions")
        assert response.status_code == 200, response.text
        held[username] = {
            code for code in response.json()["permissions"] if code.startswith("storage:")
        }

    assert held["s5-root"] == {"storage:read", "storage:manage"}
    assert held["s5-boss"] == {"storage:read"}
    assert held["s5-dana"] == {"storage:read"}
    assert held["s5-gazer"] == {"storage:read"}


async def test_a_group_admin_may_not_change_or_retire_a_volume(
    client: AsyncClient, world: World
) -> None:
    """Reading a volume and owning its lifecycle are different privileges."""
    await act_as(client, "s5-root")
    volume_id, _ = await make_storage(
        client, "scratch", kind="group_share", groupId=world.lab_a.id, dirName="scratch"
    )

    await act_as(client, "s5-boss")
    patch = await client.patch(f"{STORAGES}/{volume_id}", json={"displayName": "renamed"})
    resize = await client.post(f"{STORAGES}/{volume_id}/resize", json={"capacityGb": 99})
    delete = await client.delete(f"{STORAGES}/{volume_id}")

    assert patch.status_code == 403, patch.text
    assert resize.status_code == 403, resize.text
    assert delete.status_code == 403, delete.text


# ---------------------------------------------------------------------------
# The cluster objects: a static volume, and a claim when one can exist
# ---------------------------------------------------------------------------
async def test_a_volume_is_a_static_hostpath_volume_pinned_to_its_claim(
    client: AsyncClient, world: World, gateway: FakeK8sGateway
) -> None:
    """The directory already exists, so there is nothing to provision.

    The claim is pinned to the volume by name for the same reason: with no
    provisioner to satisfy an unpinned claim, it waits forever, and ``Pending``
    then means both "the cluster is slow" and "this will never work".
    """
    await act_as(client, "s5-root")
    volume_id, share = await make_storage(
        client, "scratch", kind="group_share", groupId=world.lab_a.id, dirName="scratch"
    )

    pv = gateway.pvs[f"wl-vol-scratch-{volume_id}"]
    pvc = gateway.pvcs[f"{world.namespace}/wl-vol-scratch-{volume_id}"]
    assert pv.host_path == share["hostPath"]
    assert pv.access_modes == ("ReadWriteMany",)
    assert pv.claim_namespace == world.namespace
    assert pv.claim_name == pvc.name
    # No StorageClass at all: leaving it unset would silently mean "use the
    # cluster default", which is the dynamic provisioner this model does not
    # depend on — and there is not one on the target cluster.
    assert pvc.storage_class == ""
    assert pvc.volume_name == pv.name


async def test_a_users_volume_asks_for_the_narrowest_access_mode(
    client: AsyncClient, world: World, gateway: FakeK8sGateway
) -> None:
    """ReadWriteOnce is enough for several of one user's own pods; shares need RWX."""
    await act_as(client, "s5-root")
    volume_id, home = await make_storage(
        client, "home", kind="user_volume", ownerUserId=world.user_a.id
    )
    share_id, share = await make_storage(
        client, "scratch", kind="group_share", groupId=world.lab_a.id, dirName="scratch"
    )

    assert home["accessMode"] == "ReadWriteOnce"
    assert share["accessMode"] == "ReadWriteMany"
    assert gateway.pvs[f"wl-vol-home-{volume_id}"].access_modes == ("ReadWriteOnce",)
    assert gateway.pvs[f"wl-vol-scratch-{share_id}"].access_modes == ("ReadWriteMany",)


async def test_a_global_share_has_a_volume_but_no_claim(
    client: AsyncClient, world: World, gateway: FakeK8sGateway
) -> None:
    """A claim lives in a namespace; a global share belongs to none.

    So the volume is created unclaimed and rests at ``available`` — a state the
    cluster has no claim-side word for, which is why it has to be its own status
    rather than an optimistic ``pending``.
    """
    await act_as(client, "s5-root")
    volume_id, share = await make_storage(client, "public-data", kind="global_share")

    pv = gateway.pvs[f"wl-vol-public-data-{volume_id}"]
    assert pv.claim_name is None
    assert pv.claim_namespace is None
    assert share["pvcName"] is None
    assert share["k8sNamespace"] is None
    assert share["status"] == "available"
    assert not [key for key in gateway.pvcs if "public-data" in key]


async def test_the_volume_is_submitted_before_its_claim(
    client: AsyncClient, world: World, gateway: FakeK8sGateway
) -> None:
    """A claim pinned by name to a volume that does not exist reports Pending with no reason."""
    await act_as(client, "s5-root")
    await make_storage(
        client, "scratch", kind="group_share", groupId=world.lab_a.id, dirName="scratch"
    )

    submitted = [call[0] for call in gateway.calls]
    assert submitted.index("apply_pv") < submitted.index("apply_pvc")


async def test_placing_a_volume_creates_no_workload(
    client: AsyncClient, world: World, gateway: FakeK8sGateway
) -> None:
    """A volume exists whether or not anything is attached to it — the point of the phase."""
    await act_as(client, "s5-root")
    await make_storage(client, "public-data", kind="global_share")

    assert gateway.deployments == {}
    assert gateway.jobs == {}


# ---------------------------------------------------------------------------
# Mount paths
# ---------------------------------------------------------------------------
async def test_the_first_share_of_a_kind_takes_the_default_mount_path(
    client: AsyncClient, world: World, gateway: FakeK8sGateway
) -> None:
    await act_as(client, "s5-root")
    share_id, _ = await make_storage(
        client, "scratch", kind="group_share", groupId=world.lab_a.id, dirName="scratch"
    )

    await act_as(client, "s5-dana")
    instance_id = await make_instance(client, world, "one", storageIds=[share_id])

    deployment = gateway.deployments[f"{world.namespace}/wl-one-{instance_id}"]
    assert [mount.mount_path for mount in deployment.extra_mounts] == ["/mnt/group_shared"]


async def test_a_second_share_of_a_kind_is_mounted_under_the_first(
    client: AsyncClient, world: World, gateway: FakeK8sGateway
) -> None:
    """Two volumes cannot occupy one directory, so which gets the default path depends on the set.

    That is why the resolved path is *recorded* on the attachment instead of being
    recomputed later, when the set may have changed and the path would move under a
    running pod.
    """
    await act_as(client, "s5-root")
    first, _ = await make_storage(
        client, "alpha", kind="group_share", groupId=world.lab_a.id, dirName="alpha"
    )
    second, _ = await make_storage(
        client, "beta", kind="group_share", groupId=world.lab_a.id, dirName="beta"
    )

    await act_as(client, "s5-dana")
    instance_id = await make_instance(client, world, "two", storageIds=[first, second])

    deployment = gateway.deployments[f"{world.namespace}/wl-two-{instance_id}"]
    assert [mount.mount_path for mount in deployment.extra_mounts] == [
        "/mnt/group_shared",
        "/mnt/group_shared/beta",
    ]


def test_two_volumes_cannot_resolve_to_one_mount_path() -> None:
    """Refused before anything is created, so a bad set never gets half-applied.

    Reachable whenever several shares of one kind carry the same name — which the
    per-workspace name rule permits across workspaces, and which a global share
    (belonging to none) does not constrain at all.
    """
    shares = [
        Storage(id=index, name="same", kind=StorageKind.GROUP_SHARE, host_path=f"/x/{index}")
        for index in (1, 2, 3)
    ]

    assert [path for _, path in resolve_mount_paths(shares[:2])] == [
        "/mnt/group_shared",
        "/mnt/group_shared/same",
    ]
    with pytest.raises(BadRequestError) as raised:
        resolve_mount_paths(shares)
    assert raised.value.code == "storage_mount_conflict"


def test_the_share_mount_roots_are_fixed_by_the_kind() -> None:
    """The two share roots are constants, which is exactly why same-kind shares collide.

    A user's volume instead lands at ``/home/{username}`` — see
    :func:`test_a_user_volume_is_the_users_directory_inside_their_group`.
    """
    share = Storage(id=1, name="scratch", kind=StorageKind.GROUP_SHARE, host_path="/x/1")
    whole_group = Storage(id=2, name="whole", kind=StorageKind.GROUP_SHARE, host_path="/x/2")
    wide = Storage(id=3, name="public", kind=StorageKind.GLOBAL_SHARE, host_path="/x/3")

    assert share.default_mount_path == "/mnt/group_shared"
    assert whole_group.default_mount_path == "/mnt/group_shared"
    assert wide.default_mount_path == "/mnt/global_shared"


# ---------------------------------------------------------------------------
# Visibility
# ---------------------------------------------------------------------------
async def test_a_user_volume_is_visible_only_to_its_owner(
    client: AsyncClient, world: World
) -> None:
    await act_as(client, "s5-root")
    volume_id, _ = await make_storage(
        client, "home", kind="user_volume", ownerUserId=world.user_a.id
    )

    await act_as(client, "s5-dana")
    assert (await client.get(f"{STORAGES}/{volume_id}")).status_code == 200

    await act_as(client, "s5-erin")
    response = await client.get(f"{STORAGES}/{volume_id}")
    assert response.status_code == 404, response.text
    assert error_code(response) == "storage_not_found"


async def test_a_named_group_share_reaches_every_member_of_its_group(
    client: AsyncClient, world: World
) -> None:
    """Sourcing "shared with my group" from the *administered* groups made every share
    invisible to the members it exists for; membership is the right source."""
    await act_as(client, "s5-root")
    volume_id, _ = await make_storage(
        client, "scratch", kind="group_share", groupId=world.lab_a.id, dirName="scratch"
    )

    for username in ("s5-dana", "s5-gazer"):
        await act_as(client, username)
        assert (await client.get(f"{STORAGES}/{volume_id}")).status_code == 200


async def test_another_group_cannot_see_a_group_share(client: AsyncClient, world: World) -> None:
    await act_as(client, "s5-root")
    volume_id, _ = await make_storage(
        client, "scratch", kind="group_share", groupId=world.lab_a.id, dirName="scratch"
    )

    await act_as(client, "s5-erin")
    response = await client.get(f"{STORAGES}/{volume_id}")
    assert response.status_code == 404, response.text
    assert error_code(response) == "storage_not_found"


async def test_everybody_can_see_a_global_share(client: AsyncClient, world: World) -> None:
    await act_as(client, "s5-root")
    volume_id, _ = await make_storage(client, "public-data", kind="global_share")

    for username in ("s5-dana", "s5-erin", "s5-gazer"):
        await act_as(client, username)
        assert (await client.get(f"{STORAGES}/{volume_id}")).status_code == 200


async def test_a_member_sees_their_own_volume_and_the_shares_but_not_the_group_directory(
    client: AsyncClient, world: World
) -> None:
    await act_as(client, "s5-root")
    await make_storage(client, "home", kind="user_volume", ownerUserId=world.user_a.id)
    await make_storage(
        client, "scratch", kind="group_share", groupId=world.lab_a.id, dirName="scratch"
    )
    await make_storage(client, "s5-whole-group", kind="group_share", groupId=world.lab_a.id)
    await make_storage(client, "public-data", kind="global_share")

    await act_as(client, "s5-dana")
    listing = await client.get(STORAGES, params={"pageSize": 50})

    assert listing.status_code == 200, listing.text
    assert sorted(row["name"] for row in listing.json()["items"]) == [
        "home",
        "public-data",
        "scratch",
    ]


async def test_an_observer_sees_the_shares_but_no_homes(client: AsyncClient, world: World) -> None:
    """An observer is still a member of the group — read-only is not "blind"."""
    await act_as(client, "s5-root")
    await make_storage(client, "home", kind="user_volume", ownerUserId=world.user_a.id)
    await make_storage(
        client, "scratch", kind="group_share", groupId=world.lab_a.id, dirName="scratch"
    )
    await make_storage(client, "s5-whole-group", kind="group_share", groupId=world.lab_a.id)

    await act_as(client, "s5-gazer")
    listing = await client.get(STORAGES, params={"pageSize": 50})

    assert sorted(row["name"] for row in listing.json()["items"]) == ["scratch"]


async def test_a_group_admin_sees_the_group_directory_but_not_homes(
    client: AsyncClient, world: World
) -> None:
    await act_as(client, "s5-root")
    await make_storage(client, "home", kind="user_volume", ownerUserId=world.user_a.id)
    await make_storage(client, "s5-whole-group", kind="group_share", groupId=world.lab_a.id)

    await act_as(client, "s5-boss")
    listing = await client.get(STORAGES, params={"pageSize": 50})

    assert sorted(row["name"] for row in listing.json()["items"]) == ["s5-whole-group"]


async def test_an_ordinary_user_is_not_offered_their_own_volume_to_attach(
    client: AsyncClient, world: World
) -> None:
    """The attachable list is the shares only; the home is not a choice."""
    await act_as(client, "s5-root")
    await make_storage(client, "home", kind="user_volume", ownerUserId=world.user_a.id)
    await make_storage(
        client, "scratch", kind="group_share", groupId=world.lab_a.id, dirName="scratch"
    )
    await make_storage(client, "public-data", kind="global_share")

    await act_as(client, "s5-dana")
    response = await client.get(
        f"{STORAGES}/attachable", params={"workspaceId": world.workspace_id}
    )

    assert response.status_code == 200, response.text
    assert sorted(row["name"] for row in response.json()) == ["public-data", "scratch"]


# ---------------------------------------------------------------------------
# Attachment rules
# ---------------------------------------------------------------------------
async def test_a_users_volume_cannot_be_mounted_explicitly(
    client: AsyncClient, world: World
) -> None:
    await act_as(client, "s5-root")
    volume_id, _ = await make_storage(
        client, "home", kind="user_volume", ownerUserId=world.user_a.id
    )

    await act_as(client, "s5-dana")
    response = await client.post(
        "/api/v1/instances",
        json=instance_payload(world, "explicit", storageIds=[volume_id]),
    )

    assert response.status_code == 400, response.text
    assert error_code(response) == "storage_user_volume_implicit"


async def test_a_global_share_cannot_be_mounted_until_it_has_a_claim(
    client: AsyncClient, world: World
) -> None:
    """The boundary the per-namespace claim work has to cross.

    A global share is offered in the picker — everybody can see it — but a claim
    lives in exactly one namespace and this volume has none, so attaching it fails
    before submission rather than producing a container stuck in
    ``ContainerCreating`` with no useful message. Until claims are created per
    namespace, the offer and the ability disagree.
    """
    await act_as(client, "s5-root")
    volume_id, _ = await make_storage(client, "public-data", kind="global_share")

    await act_as(client, "s5-dana")
    response = await client.post(
        "/api/v1/instances",
        json=instance_payload(world, "wide", storageIds=[volume_id]),
    )

    assert response.status_code == 400, response.text
    assert error_code(response) == "storage_claim_missing"


async def test_a_volume_cannot_be_mounted_from_another_namespace(
    client: AsyncClient, world: World, admin_client: AsyncClient
) -> None:
    """Caught before submission because the scheduler's version is invisible.

    A claim lives in one namespace and a pod can only mount claims from its own;
    the symptom on the cluster is a container that never starts and says nothing.
    """
    beta = await admin_client.post(
        "/api/v1/workspaces",
        json={"name": "s5-beta", "groupId": world.lab_b.id, "resourceGroupIds": []},
    )
    assert beta.status_code == 201, beta.text

    await act_as(client, "s5-root")
    far_id, far = await make_storage(
        client, "over-there", kind="group_share", groupId=world.lab_b.id, dirName="over-there"
    )
    assert far["k8sNamespace"] not in (None, world.namespace)

    # Nobody in lab A can see a lab B volume, so the refusal confirms nothing.
    await act_as(client, "s5-dana")
    hidden = await client.post(
        "/api/v1/instances", json=instance_payload(world, "cross", storageIds=[far_id])
    )
    assert hidden.status_code == 404, hidden.text
    assert error_code(hidden) == "storage_not_found"

    # The administrator who *can* see it gets the message that explains the problem
    # instead of a bare "not found".
    await act_as(client, "s5-root")
    response = await client.post(
        "/api/v1/instances",
        json=instance_payload(world, "cross-anyway", storageIds=[far_id]),
    )

    assert response.status_code == 400, response.text
    assert error_code(response) == "storage_claim_missing"


# ---------------------------------------------------------------------------
# Binding: which group, which workspace
# ---------------------------------------------------------------------------
async def test_a_user_volume_must_name_its_owner(client: AsyncClient, world: World) -> None:
    await act_as(client, "s5-root")
    response = await client.post(STORAGES, json={"name": "nobody", "kind": "user_volume"})

    assert response.status_code == 400, response.text
    assert error_code(response) == "storage_requires_owner"


async def test_a_group_share_must_name_its_group(client: AsyncClient, world: World) -> None:
    await act_as(client, "s5-root")
    response = await client.post(STORAGES, json={"name": "nogroup", "kind": "group_share"})

    assert response.status_code == 400, response.text
    assert error_code(response) == "storage_requires_group"


async def test_a_group_with_no_workspace_cannot_own_a_volume(
    client: AsyncClient, world: World
) -> None:
    """Nowhere to put the claim means no pod could ever mount it."""
    await act_as(client, "s5-root")
    response = await client.post(
        STORAGES,
        json={"name": "erin-home", "kind": "user_volume", "ownerUserId": world.user_b.id},
    )

    assert response.status_code == 400, response.text
    assert error_code(response) == "group_has_no_workspace"


async def test_a_group_with_two_workspaces_is_refused_rather_than_guessed_at(
    client: AsyncClient, world: World, admin_client: AsyncClient
) -> None:
    """Silently picking one would decide which namespace gets the data."""
    second = await admin_client.post(
        "/api/v1/workspaces",
        json={"name": "s5-alpha-two", "groupId": world.lab_a.id, "resourceGroupIds": []},
    )
    assert second.status_code == 201, second.text

    await act_as(client, "s5-root")
    response = await client.post(
        STORAGES,
        json={
            "name": "ambiguous",
            "kind": "group_share",
            "groupId": world.lab_a.id,
            "dirName": "x",
        },
    )

    assert response.status_code == 409, response.text
    assert error_code(response) == "group_has_multiple_workspaces"


async def test_a_caller_supplied_workspace_must_be_the_groups_own(
    client: AsyncClient, world: World, admin_client: AsyncClient
) -> None:
    """The workspace is accepted so callers need not be rewritten, but it is not trusted."""
    await act_as(client, "s5-root")
    response = await client.post(
        STORAGES,
        json={
            "name": "elsewhere",
            "kind": "group_share",
            "groupId": world.lab_a.id,
            "dirName": "elsewhere",
            "workspaceId": world.workspace_id,
        },
    )
    assert response.status_code == 201, response.text

    # Lab B's own workspace, asked for on Lab A's behalf, is a mismatch.
    beta = await admin_client.post(
        "/api/v1/workspaces",
        json={"name": "s5-beta", "groupId": world.lab_b.id, "resourceGroupIds": []},
    )
    assert beta.status_code == 201, beta.text

    mismatched = await client.post(
        STORAGES,
        json={
            "name": "mismatch",
            "kind": "user_volume",
            "ownerUserId": world.user_b.id,
            "workspaceId": world.workspace_id,
        },
    )

    assert mismatched.status_code == 400, mismatched.text
    assert error_code(mismatched) == "storage_workspace_mismatch"


async def test_a_name_that_is_not_a_name_is_refused(client: AsyncClient, world: World) -> None:
    await act_as(client, "s5-root")
    response = await client.post(STORAGES, json={"name": "Not A Name", "kind": "global_share"})

    assert response.status_code == 400, response.text
    assert error_code(response) == "invalid_storage_name"


# ---------------------------------------------------------------------------
# Capacity: accounting, not enforcement
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("capacity_gb", [0, 65537])
async def test_capacity_bounds_are_enforced(
    client: AsyncClient, world: World, capacity_gb: int
) -> None:
    await act_as(client, "s5-root")
    response = await client.post(
        STORAGES, json={"name": "sized", "kind": "global_share", "capacityGb": capacity_gb}
    )

    assert response.status_code == 422, response.text


async def test_growing_a_volume_changes_only_the_record(
    client: AsyncClient, world: World, gateway: FakeK8sGateway
) -> None:
    """A statically provisioned claim cannot be expanded at all.

    Verified against the cluster rather than assumed: the API server refuses with
    "only dynamically provisioned pvc can be resized". A hostPath volume's size is
    not enforced by anything either, so this number is what the platform accounts
    for and reports.
    """
    await act_as(client, "s5-root")
    volume_id, _ = await make_storage(
        client,
        "scratch",
        kind="group_share",
        groupId=world.lab_a.id,
        dirName="scratch",
        capacityGb=10,
    )
    submitted = list(gateway.calls)

    response = await client.post(f"{STORAGES}/{volume_id}/resize", json={"capacityGb": 40})

    assert response.status_code == 200, response.text
    assert response.json()["capacityGb"] == 40
    assert gateway.calls == submitted
    assert gateway.pvcs[f"{world.namespace}/wl-vol-scratch-{volume_id}"].size_gb == 10


async def test_a_volume_cannot_shrink(client: AsyncClient, world: World) -> None:
    """Grow-only because this feeds accounting; the honest way back is to retire the volume."""
    await act_as(client, "s5-root")
    volume_id, _ = await make_storage(
        client,
        "scratch",
        kind="group_share",
        groupId=world.lab_a.id,
        dirName="scratch",
        capacityGb=40,
    )

    response = await client.post(f"{STORAGES}/{volume_id}/resize", json={"capacityGb": 10})

    assert response.status_code == 400, response.text
    assert error_code(response) == "storage_shrink_unsupported"
    detail = await client.get(f"{STORAGES}/{volume_id}")
    assert detail.json()["capacityGb"] == 40


async def test_the_cluster_does_not_decide_the_recorded_capacity(
    client: AsyncClient, world: World, gateway: FakeK8sGateway
) -> None:
    """ "The cluster says it is bigger" means somebody edited the cluster.

    Copying it upward would silently inflate what the platform believes it has
    handed out, which is the opposite of what an accounting field is for.
    """
    await act_as(client, "s5-root")
    volume_id, _ = await make_storage(
        client,
        "scratch",
        kind="group_share",
        groupId=world.lab_a.id,
        dirName="scratch",
        capacityGb=10,
    )
    gateway.pv_states[f"wl-vol-scratch-{volume_id}"] = PvInfo(
        name=f"wl-vol-scratch-{volume_id}",
        phase="Bound",
        host_path="/storage/kube-wl/s5-lab-a/scratch",
        capacity_gb=999,
    )
    gateway.pvc_states[f"{world.namespace}/wl-vol-scratch-{volume_id}"] = PvcInfo(
        name=f"wl-vol-scratch-{volume_id}", phase="Bound", capacity_gb=999, storage_class=""
    )

    response = await client.post(f"{STORAGES}/{volume_id}/reconcile")

    assert response.status_code == 200, response.text
    assert response.json()["capacityGb"] == 10
    assert response.json()["status"] == "bound"


# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------
async def test_reconcile_reports_a_waiting_claim_honestly(
    client: AsyncClient, world: World, gateway: FakeK8sGateway
) -> None:
    """Neither "bound" nor "failed": a claim waiting on its first consumer is normal.

    Reporting it as a failure would send an administrator hunting for a problem
    that does not exist, and reporting it as bound would be a lie.
    """
    await act_as(client, "s5-root")
    volume_id, _ = await make_storage(
        client, "scratch", kind="group_share", groupId=world.lab_a.id, dirName="scratch"
    )
    gateway.pvc_states[f"{world.namespace}/wl-vol-scratch-{volume_id}"] = PvcInfo(
        name=f"wl-vol-scratch-{volume_id}",
        phase="Pending",
        message="WaitForFirstConsumer 卷在 Pod 调度前保持 Pending",
    )

    response = await client.post(f"{STORAGES}/{volume_id}/reconcile")

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "pending"
    assert response.json()["statusMessage"] == "WaitForFirstConsumer 卷在 Pod 调度前保持 Pending"


async def test_reconcile_marks_a_vanished_claim_as_failed(
    client: AsyncClient, world: World, gateway: FakeK8sGateway
) -> None:
    """Claiming PENDING forever would be a lie; the volume is not usable."""
    await act_as(client, "s5-root")
    volume_id, _ = await make_storage(
        client, "scratch", kind="group_share", groupId=world.lab_a.id, dirName="scratch"
    )
    del gateway.pvcs[f"{world.namespace}/wl-vol-scratch-{volume_id}"]

    response = await client.post(f"{STORAGES}/{volume_id}/reconcile")

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "failed"
    assert "PersistentVolumeClaim" in response.json()["statusMessage"]


async def test_reconcile_notices_a_volume_pointing_at_another_directory(
    client: AsyncClient, world: World, gateway: FakeK8sGateway
) -> None:
    """The one fact that must not drift: everything downstream is derived from it.

    Checked even though the claim is the object that decides the *status* — the
    disagreement is invisible from the claim alone.
    """
    await act_as(client, "s5-root")
    volume_id, share = await make_storage(
        client, "scratch", kind="group_share", groupId=world.lab_a.id, dirName="scratch"
    )
    gateway.pv_states[f"wl-vol-scratch-{volume_id}"] = PvInfo(
        name=f"wl-vol-scratch-{volume_id}", phase="Bound", host_path="/somewhere/else"
    )

    response = await client.post(f"{STORAGES}/{volume_id}/reconcile")

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "failed"
    assert "/somewhere/else" in response.json()["statusMessage"]
    assert share["hostPath"] in response.json()["statusMessage"]


# ---------------------------------------------------------------------------
# Retirement
# ---------------------------------------------------------------------------
async def test_a_volume_in_use_cannot_be_retired(client: AsyncClient, world: World) -> None:
    """Deleting the claim would orphan the pods still mounting it."""
    await act_as(client, "s5-root")
    volume_id, _ = await make_storage(
        client, "scratch", kind="group_share", groupId=world.lab_a.id, dirName="scratch"
    )

    await act_as(client, "s5-dana")
    instance_id = await make_instance(client, world, "holder", storageIds=[volume_id])

    await act_as(client, "s5-root")
    response = await client.delete(f"{STORAGES}/{volume_id}")

    assert response.status_code == 409, response.text
    assert error_code(response) == "storage_in_use"
    assert response.json()["error"]["detail"]["instanceIds"] == [instance_id]


async def test_retiring_a_volume_deletes_the_cluster_objects_and_keeps_the_row(
    client: AsyncClient, world: World, gateway: FakeK8sGateway
) -> None:
    """A hard delete of the objects, a soft delete of the row.

    The volume must actually stop existing, but the record is what lets an
    administrator answer "what happened to that volume?" afterwards. The directory
    itself is never touched: the reclaim policy is ``Retain``, so this gives up the
    platform's claim on the data rather than the data.
    """
    await act_as(client, "s5-root")
    volume_id, share = await make_storage(
        client, "scratch", kind="group_share", groupId=world.lab_a.id, dirName="scratch"
    )

    response = await client.delete(f"{STORAGES}/{volume_id}")

    assert response.status_code == 200, response.text
    assert f"wl-vol-scratch-{volume_id}" not in gateway.pvs
    assert f"{world.namespace}/wl-vol-scratch-{volume_id}" not in gateway.pvcs
    body = response.json()
    assert body["isActive"] is False
    assert body["status"] == "released"
    assert body["pvcName"] is None
    assert body["k8sNamespace"] is None
    assert body["hostPath"] == share["hostPath"]
    assert "保留" in body["statusMessage"]


async def test_a_retired_volume_can_no_longer_be_mounted(client: AsyncClient, world: World) -> None:
    await act_as(client, "s5-root")
    volume_id, _ = await make_storage(
        client, "scratch", kind="group_share", groupId=world.lab_a.id, dirName="scratch"
    )
    await client.delete(f"{STORAGES}/{volume_id}")

    await act_as(client, "s5-dana")
    response = await client.post(
        "/api/v1/instances",
        json=instance_payload(world, "gone", storageIds=[volume_id]),
    )

    assert response.status_code == 400, response.text
    assert error_code(response) == "storage_inactive"


async def test_a_set_containing_a_retired_volume_is_refused_before_anything_is_created(
    client: AsyncClient, world: World, gateway: FakeK8sGateway
) -> None:
    """Not half-applied: the whole request fails, so no pod is built with a missing mount."""
    await act_as(client, "s5-root")
    live_id, _ = await make_storage(
        client, "live", kind="group_share", groupId=world.lab_a.id, dirName="live"
    )
    dead_id, _ = await make_storage(
        client, "dead", kind="group_share", groupId=world.lab_a.id, dirName="dead"
    )
    await client.delete(f"{STORAGES}/{dead_id}")
    deployments = dict(gateway.deployments)

    await act_as(client, "s5-dana")
    response = await client.post(
        "/api/v1/instances",
        json=instance_payload(world, "mixed", storageIds=[live_id, dead_id]),
    )

    assert response.status_code == 400, response.text
    assert error_code(response) == "storage_inactive"
    assert gateway.deployments == deployments


# ---------------------------------------------------------------------------
# Timeline and usage
# ---------------------------------------------------------------------------
async def test_the_timeline_records_placement_change_and_retirement(
    client: AsyncClient, world: World
) -> None:
    await act_as(client, "s5-root")
    volume_id, _ = await make_storage(
        client, "scratch", kind="group_share", groupId=world.lab_a.id, dirName="scratch"
    )
    await client.post(f"{STORAGES}/{volume_id}/resize", json={"capacityGb": 30})
    await client.delete(f"{STORAGES}/{volume_id}")

    response = await client.get(f"{STORAGES}/{volume_id}/events")

    assert response.status_code == 200, response.text
    kinds = {row["eventType"] for row in response.json()}
    assert {"created", "updated", "deleted"} <= kinds


async def test_the_timeline_of_an_invisible_volume_is_not_readable(
    client: AsyncClient, world: World
) -> None:
    await act_as(client, "s5-root")
    volume_id, _ = await make_storage(
        client, "home", kind="user_volume", ownerUserId=world.user_a.id
    )

    await act_as(client, "s5-erin")
    response = await client.get(f"{STORAGES}/{volume_id}/events")

    assert response.status_code == 404, response.text
    assert error_code(response) == "storage_not_found"


async def test_usage_counts_only_the_volumes_a_user_owns(client: AsyncClient, world: World) -> None:
    """A lab-wide share is visible to every member and owned by none of them.

    Counting it here would tell each of fifty members that they are personally
    consuming its hundred gigabytes.
    """
    await act_as(client, "s5-root")
    await make_storage(
        client, "home", kind="user_volume", ownerUserId=world.user_a.id, capacityGb=7
    )
    await make_storage(
        client,
        "scratch",
        kind="group_share",
        groupId=world.lab_a.id,
        dirName="scratch",
        capacityGb=50,
    )
    await make_storage(client, "public-data", kind="global_share", capacityGb=100)

    await act_as(client, "s5-dana")
    assert (await client.get(f"{STORAGES}/usage")).json() == {"count": 1, "capacityGb": 7}

    await act_as(client, "s5-erin")
    assert (await client.get(f"{STORAGES}/usage")).json() == {"count": 0, "capacityGb": 0}


async def test_usage_stops_counting_a_retired_volume(client: AsyncClient, world: World) -> None:
    await act_as(client, "s5-root")
    volume_id, _ = await make_storage(
        client, "home", kind="user_volume", ownerUserId=world.user_a.id, capacityGb=7
    )
    await client.delete(f"{STORAGES}/{volume_id}")

    await act_as(client, "s5-dana")
    assert (await client.get(f"{STORAGES}/usage")).json() == {"count": 0, "capacityGb": 0}


async def test_a_global_admin_sees_the_installation_total(
    client: AsyncClient, world: World
) -> None:
    """Reporting "0 GB" to the person responsible for the whole installation helps nobody."""
    await act_as(client, "s5-root")
    await make_storage(
        client, "home", kind="user_volume", ownerUserId=world.user_a.id, capacityGb=7
    )
    await make_storage(
        client,
        "scratch",
        kind="group_share",
        groupId=world.lab_a.id,
        dirName="scratch",
        capacityGb=50,
    )
    await make_storage(client, "public-data", kind="global_share", capacityGb=100)

    response = await client.get(f"{STORAGES}/usage")

    assert response.status_code == 200, response.text
    assert response.json() == {"count": 3, "capacityGb": 157}


# ---------------------------------------------------------------------------
# The model that is gone
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "payload",
    [
        {"name": "old", "kind": "dataset"},
        {"name": "old", "kind": "personal_home"},
        {"name": "old", "kind": "personal_data"},
        {"name": "old", "kind": "global_share", "sizeBytes": 100},
        {"name": "old", "kind": "global_share", "storageClass": "standard"},
        {"name": "old", "kind": "global_share", "medium": "ssd"},
    ],
)
async def test_the_old_wire_contract_is_rejected(
    client: AsyncClient, world: World, payload: dict[str, object]
) -> None:
    """Kinds and fields that no longer exist must not be silently accepted.

    ``ApiModel`` forbids unknown fields, so an old client gets a validation error
    rather than a volume whose kind nobody handles.
    """
    await act_as(client, "s5-root")
    response = await client.post(STORAGES, json=payload)

    assert response.status_code == 422, response.text
