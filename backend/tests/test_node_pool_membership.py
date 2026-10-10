"""Node ↔ resource-pool membership.

The model this file pins down: **every node the platform manages belongs to
exactly one pool.** Two things make that true.

``ComputeNode.resource_group_id`` is the authority — a fact the platform owns,
not one inferred from whatever labels a node happened to carry. The requested
model is a partition, and a partition needs a single owner.

``wl-pool`` is that fact, materialised where the scheduler can read it. A single
key with a single value is what makes exclusivity true by construction: a node can
no more be in two pools than a variable can hold two values. It also makes a move
atomic, because re-filing a node is one patch of one key rather than a remove
followed by an add with a window in between.

One pool is nominated the **default**. It is the catch-all that keeps membership
total: a node nobody has claimed still has somewhere to live, so the platform never
has to represent "this machine belongs to no pool", a state in which it would be
unreachable by any workload. Its membership is therefore *derived* — the complement
of every named pool — and cannot be edited directly, because a hand-maintained list
and a computed complement would be two answers to one question.

The tests below are mostly about the *reconciliation* between the database and the
label, because that is where the interesting failures live: the label is stale, the
label names a pool that no longer exists, an operator pre-labelled a node the
platform has never seen, or nobody has nominated a default and there is nowhere to
put anything.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.k8s import FakeK8sGateway, NodeInfo
from app.models.enums import GroupKind
from app.models.group import Group
from app.models.resource import POOL_LABEL, ComputeNode, ResourceGroup
from app.models.user import User
from app.models.workspace import Workspace
from tests import factories

PASSWORD = "correct-horse-battery"
LOGIN = "/api/v1/auth/login"


@dataclass
class World:
    lab_a: Group
    admin: User
    user_a: User
    #: The catch-all, created first so it is the default.
    default_pool_id: int
    #: A named pool: what nodes are deliberately filed into.
    pool_id: int
    spec_id: int
    workspace_id: int


@pytest.fixture
async def world(admin_client: AsyncClient, seeded: AsyncSession, settings: Settings) -> World:
    """A default pool, a named pool, one spec, and a workspace holding both."""
    lab_a = await factories.create_group(seeded, name="lab-a", kind=GroupKind.LAB)
    admin = await factories.create_user(
        seeded, username="root", password=PASSWORD, role="global_admin"
    )
    user_a = await factories.create_user(
        seeded, username="user-a", password=PASSWORD, role="user", group=lab_a
    )
    await seeded.commit()

    async def create_pool(name: str, display_name: str) -> int:
        response = await admin_client.post(
            "/api/v1/resource-groups",
            json={
                "name": name,
                "displayName": display_name,
                "kind": "private",
                "groupId": lab_a.id,
                "networkType": "internal",
            },
        )
        assert response.status_code == 201, response.text
        return response.json()["id"]

    default_pool_id = await create_pool("default-pool", "默认池")
    pool_id = await create_pool("gpu-pool", "GPU 池")

    spec = await admin_client.post(
        "/api/v1/resource-specs",
        json={
            "name": "small",
            "displayName": "小规格",
            "cpuMilli": 2000,
            "memoryMb": 4096,
            "gpuType": "none",
            "gpuCount": 0,
        },
    )
    assert spec.status_code == 201, spec.text
    spec_id = spec.json()["id"]
    for target in (default_pool_id, pool_id):
        attached = await admin_client.put(f"/api/v1/resource-groups/{target}/specs", json=[spec_id])
        assert attached.status_code == 200, attached.text

    workspace = await admin_client.post(
        "/api/v1/workspaces",
        json={
            "name": "alpha",
            "groupId": lab_a.id,
            "resourceGroupIds": [default_pool_id, pool_id],
        },
    )
    assert workspace.status_code == 201, workspace.text
    workspace_id = workspace.json()["id"]

    # Workloads are created by a workspace member, not by the global admin: the
    # admin has no workspace of their own, and launching is scoped to one.
    moved = await admin_client.post(
        f"/api/v1/workspaces/{workspace_id}/members",
        json={"userId": user_a.id, "moveFromExisting": True},
    )
    assert moved.status_code in (200, 201), moved.text

    # A workload mounts its owner's home, and starting one without a registered home
    # is refused — so user-a needs one before any pinning test can run.
    await factories.create_user_volume(
        seeded, user_a, settings=settings, workspace=await seeded.get(Workspace, workspace_id)
    )
    await seeded.commit()

    return World(
        lab_a=lab_a,
        admin=admin,
        user_a=user_a,
        default_pool_id=default_pool_id,
        pool_id=pool_id,
        spec_id=spec_id,
        workspace_id=workspace_id,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
async def act_as(client: AsyncClient, username: str) -> AsyncClient:
    """Re-point a client at another user, the way a second browser would."""
    client.headers.pop("Authorization", None)
    response = await client.post(LOGIN, json={"username": username, "password": PASSWORD})
    assert response.status_code == 200, response.text
    client.headers["Authorization"] = f"Bearer {response.json()['accessToken']}"
    return client


def script_nodes(
    gateway: FakeK8sGateway, *names: str, labels: dict[str, str] | None = None
) -> None:
    """Replace the cluster's node list with plain ready nodes."""
    gateway.nodes = [
        NodeInfo(
            name=name,
            status="ready",
            schedulable=True,
            cpu_total_milli=8000,
            memory_total_mb=16384,
            cpu_allocatable_milli=7900,
            memory_allocatable_mb=15000,
            labels={"kubernetes.io/hostname": name, **(labels or {})},
        )
        for name in names
    ]


def relabel(gateway: FakeK8sGateway, name: str, **labels: str) -> None:
    """Edit a cluster label behind the platform's back."""
    for index, node in enumerate(gateway.nodes):
        if node.name == name:
            gateway.nodes[index] = replace(node, labels={**node.labels, **labels})
            return
    raise AssertionError(f"node {name} is not in the fake cluster")


async def make_pool(admin_client: AsyncClient, name: str, **extra: object) -> dict:
    response = await admin_client.post(
        "/api/v1/resource-groups",
        json={
            "name": name,
            "displayName": name,
            "kind": "private",
            "networkType": "internal",
            **extra,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


async def nodes_by_name(admin_client: AsyncClient) -> dict[str, dict]:
    response = await admin_client.get("/api/v1/nodes")
    assert response.status_code == 200, response.text
    return {node["name"]: node for node in response.json()}


def cluster_label(gateway: FakeK8sGateway, name: str) -> str | None:
    """What the cluster actually holds for a node, rather than what we cached."""
    for node in gateway.nodes:
        if node.name == name:
            return node.labels.get(POOL_LABEL)
    raise AssertionError(f"node {name} is not in the fake cluster")


async def node_id(admin_client: AsyncClient, name: str) -> int:
    return (await nodes_by_name(admin_client))[name]["id"]


async def file_nodes(admin_client: AsyncClient, pool_id: int, *names: str) -> dict:
    ids = [await node_id(admin_client, name) for name in names]
    response = await admin_client.put(f"/api/v1/resource-groups/{pool_id}/nodes", json=ids)
    assert response.status_code == 200, response.text
    return response.json()


# ---------------------------------------------------------------------------
# Which pool is the default
# ---------------------------------------------------------------------------
async def test_the_first_pool_becomes_the_default(admin_client: AsyncClient, world: World) -> None:
    # A pool set with no catch-all cannot hold the invariant that every node has a
    # home, so one is nominated the moment there is a pool to nominate.
    listed = (await admin_client.get("/api/v1/resource-groups")).json()
    defaults = {pool["name"]: pool["isDefault"] for pool in listed["items"]}
    assert defaults == {"default-pool": True, "gpu-pool": False}


async def test_marking_a_pool_default_demotes_the_previous_one(
    admin_client: AsyncClient, world: World
) -> None:
    promoted = await admin_client.patch(
        f"/api/v1/resource-groups/{world.pool_id}", json={"isDefault": True}
    )
    assert promoted.status_code == 200, promoted.text
    assert promoted.json()["isDefault"] is True

    # "The default pool" is a definite description: two of them would make the
    # catch-all ambiguous.
    listed = (await admin_client.get("/api/v1/resource-groups")).json()
    defaults = {pool["name"]: pool["isDefault"] for pool in listed["items"]}
    assert defaults == {"default-pool": False, "gpu-pool": True}


# ---------------------------------------------------------------------------
# The sync derives membership
# ---------------------------------------------------------------------------
async def test_a_new_node_lands_in_the_default_pool_and_is_labelled(
    admin_client: AsyncClient, world: World, gateway: FakeK8sGateway
) -> None:
    script_nodes(gateway, "gpu-01")

    response = await admin_client.post("/api/v1/nodes/sync")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["nodesCreated"] == 1
    assert body["labelsPatched"] == 1
    assert body["nodesUnpooled"] == 0

    node = (await nodes_by_name(admin_client))["gpu-01"]
    assert node["resourceGroupId"] == world.default_pool_id
    assert node["resourceGroupName"] == "默认池"
    # The label is written, not just the row: nodeSelector matches the cluster's
    # copy, so a row without a label would route nothing.
    assert cluster_label(gateway, "gpu-01") == "default-pool"


async def test_a_second_sync_in_a_steady_state_patches_nothing(
    admin_client: AsyncClient, world: World, gateway: FakeK8sGateway
) -> None:
    script_nodes(gateway, "gpu-01", "gpu-02")
    await admin_client.post("/api/v1/nodes/sync")
    assert len([c for c in gateway.calls if c[0] == "patch_node_labels"]) == 2

    again = await admin_client.post("/api/v1/nodes/sync")
    body = again.json()
    # Nothing changed, so nothing is written. Re-patching every node on every
    # sweep would be a stream of pointless writes to the API server — and this
    # runs every couple of minutes.
    assert body["labelsPatched"] == 0
    assert body["nodesUpdated"] == 0
    assert len([c for c in gateway.calls if c[0] == "patch_node_labels"]) == 2


async def test_an_unfiled_node_adopts_its_label(
    admin_client: AsyncClient, world: World, gateway: FakeK8sGateway
) -> None:
    """The onboarding path: an operator pre-labels a batch before we ever see it."""
    script_nodes(gateway, "gpu-07", labels={POOL_LABEL: "gpu-pool"})

    body = (await admin_client.post("/api/v1/nodes/sync")).json()
    assert body["labelsPatched"] == 0  # already correct, so not rewritten
    node = (await nodes_by_name(admin_client))["gpu-07"]
    assert node["resourceGroupId"] == world.pool_id


async def test_the_database_wins_over_a_conflicting_label(
    admin_client: AsyncClient, world: World, gateway: FakeK8sGateway
) -> None:
    script_nodes(gateway, "gpu-01")
    await admin_client.post("/api/v1/nodes/sync")
    assert (await file_nodes(admin_client, world.pool_id, "gpu-01"))["labelsPatched"] == 1
    assert cluster_label(gateway, "gpu-01") == "gpu-pool"

    # Somebody edits the label behind the platform's back. The row is the fact;
    # the label is a copy, and a copy does not get to win.
    relabel(gateway, "gpu-01", **{POOL_LABEL: "default-pool"})
    body = (await admin_client.post("/api/v1/nodes/sync")).json()

    assert body["labelsPatched"] == 1
    assert cluster_label(gateway, "gpu-01") == "gpu-pool"
    assert (await nodes_by_name(admin_client))["gpu-01"]["resourceGroupId"] == world.pool_id


async def test_a_label_naming_a_deleted_pool_falls_back_to_the_default(
    admin_client: AsyncClient, world: World, gateway: FakeK8sGateway
) -> None:
    script_nodes(gateway, "gpu-01", labels={POOL_LABEL: "long-gone"})

    body = (await admin_client.post("/api/v1/nodes/sync")).json()
    assert body["labelsPatched"] == 1
    assert cluster_label(gateway, "gpu-01") == "default-pool"


async def test_without_a_default_pool_a_node_is_reported_unpooled(
    admin_client: AsyncClient, world: World, gateway: FakeK8sGateway, seeded: AsyncSession
) -> None:
    # Clear the flag to reach the misconfigured state deliberately. This is the one
    # situation where the invariant genuinely cannot hold, and the sync has to say
    # so rather than invent a home or, worse, claim success.
    pool = await seeded.get(ResourceGroup, world.default_pool_id)
    assert pool is not None
    pool.is_default = False
    await seeded.commit()
    script_nodes(gateway, "gpu-01")

    body = (await admin_client.post("/api/v1/nodes/sync")).json()
    assert body["nodesUnpooled"] == 1
    assert body["labelsPatched"] == 0
    node = (await nodes_by_name(admin_client))["gpu-01"]
    assert node["resourceGroupId"] is None
    assert node["resourceGroupName"] is None
    # No label either: guessing one would point the scheduler at hardware the
    # platform does not believe it owns.
    assert cluster_label(gateway, "gpu-01") is None


# ---------------------------------------------------------------------------
# Assigning nodes to a pool
# ---------------------------------------------------------------------------
async def test_assigning_a_node_rewrites_its_label(
    admin_client: AsyncClient, world: World, gateway: FakeK8sGateway
) -> None:
    script_nodes(gateway, "gpu-01")
    await admin_client.post("/api/v1/nodes/sync")
    assert cluster_label(gateway, "gpu-01") == "default-pool"

    result = await file_nodes(admin_client, world.pool_id, "gpu-01")

    assert result["labelsPatched"] == 1
    assert cluster_label(gateway, "gpu-01") == "gpu-pool"


async def test_a_node_dropped_from_a_pool_moves_to_the_default(
    admin_client: AsyncClient, world: World, gateway: FakeK8sGateway
) -> None:
    script_nodes(gateway, "gpu-01")
    await admin_client.post("/api/v1/nodes/sync")
    await file_nodes(admin_client, world.pool_id, "gpu-01")

    # Removing it from `gpu-pool` must not leave it unassigned: the partition has
    # to stay total, and "not this pool" is not "nothing may ever run here".
    emptied = await admin_client.put(f"/api/v1/resource-groups/{world.pool_id}/nodes", json=[])

    assert emptied.status_code == 200, emptied.text
    assert emptied.json()["labelsPatched"] == 1
    assert cluster_label(gateway, "gpu-01") == "default-pool"
    assert (await nodes_by_name(admin_client))["gpu-01"]["resourceGroupId"] == (
        world.default_pool_id
    )


async def test_the_default_pools_membership_is_not_directly_editable(
    admin_client: AsyncClient, world: World, gateway: FakeK8sGateway
) -> None:
    script_nodes(gateway, "gpu-01")
    await admin_client.post("/api/v1/nodes/sync")

    # Its membership is the complement of every named pool, so a list for it would
    # be a second source of truth for the same fact.
    response = await admin_client.put(
        f"/api/v1/resource-groups/{world.default_pool_id}/nodes",
        json=[await node_id(admin_client, "gpu-01")],
    )
    assert response.status_code == 400, response.text
    assert response.json()["error"]["code"] == "default_group_nodes_are_derived"


async def test_moving_nodes_needs_a_default_to_move_them_into(
    admin_client: AsyncClient, world: World, gateway: FakeK8sGateway, seeded: AsyncSession
) -> None:
    script_nodes(gateway, "gpu-01")
    await admin_client.post("/api/v1/nodes/sync")
    await file_nodes(admin_client, world.pool_id, "gpu-01")

    pool = await seeded.get(ResourceGroup, world.default_pool_id)
    assert pool is not None
    pool.is_default = False
    await seeded.commit()

    response = await admin_client.put(f"/api/v1/resource-groups/{world.pool_id}/nodes", json=[])
    assert response.status_code == 400, response.text
    assert response.json()["error"]["code"] == "no_default_resource_group"
    # Refused, not half-done: the node is still where it was.
    assert (await nodes_by_name(admin_client))["gpu-01"]["resourceGroupId"] == world.pool_id


async def test_assigning_an_unknown_node_is_rejected(
    admin_client: AsyncClient, world: World
) -> None:
    response = await admin_client.put(f"/api/v1/resource-groups/{world.pool_id}/nodes", json=[9999])
    assert response.status_code == 400, response.text
    assert response.json()["error"]["code"] == "unknown_node"


# ---------------------------------------------------------------------------
# Deleting a pool
# ---------------------------------------------------------------------------
async def test_a_pool_holding_nodes_cannot_be_deleted(
    admin_client: AsyncClient, world: World, gateway: FakeK8sGateway
) -> None:
    # A pool that no workspace has been granted, so the node check is the only
    # thing that can refuse this — otherwise the grant check would answer first
    # and the node guard would never be exercised.
    other = await make_pool(admin_client, "cpu-pool")
    script_nodes(gateway, "gpu-01")
    await admin_client.post("/api/v1/nodes/sync")
    await file_nodes(admin_client, other["id"], "gpu-01")

    # The FK is ON DELETE SET NULL, so allowing this would leave hardware that no
    # workload can reach — invisible as a failure, and permanent.
    response = await admin_client.delete(f"/api/v1/resource-groups/{other['id']}")

    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "resource_group_has_nodes"


async def test_an_empty_pool_can_be_deleted(admin_client: AsyncClient, world: World) -> None:
    other = await make_pool(admin_client, "cpu-pool")
    response = await admin_client.delete(f"/api/v1/resource-groups/{other['id']}")
    assert response.status_code == 200, response.text


async def test_moving_the_nodes_out_makes_a_pool_deletable(
    admin_client: AsyncClient, world: World, gateway: FakeK8sGateway
) -> None:
    """The refusal is a prompt to move the hardware, not a dead end."""
    other = await make_pool(admin_client, "cpu-pool")
    script_nodes(gateway, "gpu-01")
    await admin_client.post("/api/v1/nodes/sync")
    await file_nodes(admin_client, other["id"], "gpu-01")
    await admin_client.put(f"/api/v1/resource-groups/{other['id']}/nodes", json=[])

    response = await admin_client.delete(f"/api/v1/resource-groups/{other['id']}")

    assert response.status_code == 200, response.text
    assert cluster_label(gateway, "gpu-01") == "default-pool"


# ---------------------------------------------------------------------------
# Scheduling reads the pool
# ---------------------------------------------------------------------------
def instance_payload(world: World, **overrides: object) -> dict:
    payload: dict[str, object] = {
        "name": "dev",
        "displayName": "开发环境",
        "resourceGroupId": world.pool_id,
        "resourceSpecId": world.spec_id,
        "image": "registry.local/jupyter:latest",
        "ideType": "jupyter",
    }
    payload.update(overrides)
    return payload


def only_deployment(gateway: FakeK8sGateway):
    """The one Deployment the fake holds — its name is generated, not the input."""
    assert len(gateway.deployments) == 1, gateway.deployments
    return next(iter(gateway.deployments.values()))


def only_job(gateway: FakeK8sGateway):
    assert len(gateway.jobs) == 1, gateway.jobs
    return next(iter(gateway.jobs.values()))


async def test_an_instance_is_pinned_to_its_pool(
    client: AsyncClient, world: World, gateway: FakeK8sGateway
) -> None:
    await act_as(client, "user-a")
    response = await client.post("/api/v1/instances", json=instance_payload(world))
    assert response.status_code == 201, response.text

    # Without this the pool would be a quota with a nice name: the pod could land
    # on any hardware in the cluster.
    assert only_deployment(gateway).node_selector[POOL_LABEL] == "gpu-pool"


async def test_a_pools_own_selector_cannot_override_the_pool_key(
    client: AsyncClient, admin_client: AsyncClient, world: World, gateway: FakeK8sGateway
) -> None:
    """A pool must not be able to select its way out of itself."""
    await admin_client.patch(
        f"/api/v1/resource-groups/{world.pool_id}",
        json={"k8sLabelSelector": {POOL_LABEL: "somewhere-else", "gpu-type": "a100"}},
    )
    await act_as(client, "user-a")
    response = await client.post("/api/v1/instances", json=instance_payload(world))
    assert response.status_code == 201, response.text

    selector = only_deployment(gateway).node_selector
    assert selector[POOL_LABEL] == "gpu-pool"
    # The extra hardware constraint is kept; only the pool key is authoritative.
    assert selector["gpu-type"] == "a100"


async def test_a_task_is_pinned_to_its_pool(
    client: AsyncClient, world: World, gateway: FakeK8sGateway
) -> None:
    await act_as(client, "user-a")
    response = await client.post(
        "/api/v1/tasks",
        json={
            "name": "train",
            "displayName": "训练任务",
            "resourceGroupId": world.pool_id,
            "resourceSpecId": world.spec_id,
            "image": "registry.local/pytorch:2.4",
            "script": "python train.py --epochs 3",
        },
    )
    assert response.status_code == 201, response.text

    assert only_job(gateway).node_selector[POOL_LABEL] == "gpu-pool"


# ---------------------------------------------------------------------------
# The stored fact
# ---------------------------------------------------------------------------
async def test_the_row_and_the_label_end_up_agreeing(
    admin_client: AsyncClient, world: World, gateway: FakeK8sGateway, seeded: AsyncSession
) -> None:
    """The invariant the whole design rests on, checked against the database."""
    script_nodes(gateway, "gpu-01", "gpu-02")
    await admin_client.post("/api/v1/nodes/sync")

    rows = list((await seeded.scalars(select(ComputeNode))).all())
    names = {pool.id: pool.name for pool in (await seeded.scalars(select(ResourceGroup))).all()}
    assert rows, "the sync should have recorded the nodes"
    for row in rows:
        assert row.resource_group_id is not None
        assert cluster_label(gateway, row.name) == names[row.resource_group_id]
