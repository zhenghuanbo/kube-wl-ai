"""Phase 2 acceptance: workspaces, resource pools and quotas.

The behaviour under test is the tenancy boundary itself: a workspace is one
Kubernetes namespace, a user belongs to exactly one of them, and a pool of
hardware only becomes usable once it is granted.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.k8s import (
    SERVICES_PER_INSTANCE,
    FakeK8sGateway,
    InstanceObjects,
    build_instance_objects,
    set_gateway,
)
from app.models.enums import GroupKind, GroupRole, IdeType
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
        observer=observer,
    )


async def act_as(client: AsyncClient, username: str) -> AsyncClient:
    client.headers.pop("Authorization", None)
    response = await client.post(LOGIN, json={"username": username, "password": PASSWORD})
    assert response.status_code == 200, response.text
    client.headers["Authorization"] = f"Bearer {response.json()['accessToken']}"
    return client


async def make_pool(
    client: AsyncClient,
    *,
    name: str,
    kind: str = "private",
    group_id: int | None = None,
    cpu_cores: int = 8,
    memory_gb: int = 16,
    max_gpu_per_user: int = 2,
) -> dict:
    """Create a resource pool with one CPU spec and its per-user fairness limits."""
    created = await client.post(
        "/api/v1/resource-groups",
        json={
            "name": name,
            "displayName": name,
            "kind": kind,
            "groupId": group_id,
            "networkType": "internal",
            "maxRuntimeSeconds": 86400,
        },
    )
    assert created.status_code == 201, created.text
    pool = created.json()

    limited = await client.put(
        f"/api/v1/resource-groups/{pool['id']}/limits",
        json={
            "maxGpuPerUser": max_gpu_per_user,
            "maxCpuCoresPerUser": cpu_cores,
            "maxMemoryGbPerUser": memory_gb,
            "maxStorageGbPerUser": 100,
        },
    )
    assert limited.status_code == 200, limited.text

    spec = await client.post(
        "/api/v1/resource-specs",
        json={
            "name": f"{name}-cpu",
            "displayName": f"{name} CPU",
            "cpuMilli": 4000,
            "memoryMb": 8192,
            "gpuType": "none",
            "gpuCount": 0,
        },
    )
    assert spec.status_code == 201, spec.text
    attached = await client.put(
        f"/api/v1/resource-groups/{pool['id']}/specs", json=[spec.json()["id"]]
    )
    assert attached.status_code == 200, attached.text
    return pool


# ---------------------------------------------------------------------------
# Resource pools
# ---------------------------------------------------------------------------
async def test_pool_creation_seeds_a_limits_row(admin_client: AsyncClient, world: World) -> None:
    """An absent limits row would mean "unlimited" — the wrong default."""
    created = await admin_client.post(
        "/api/v1/resource-groups",
        json={"name": "a100-pool", "displayName": "A100 池", "kind": "public"},
    )

    assert created.status_code == 201, created.text
    body = created.json()
    assert body["limits"] is not None
    assert body["limits"]["maxGpuPerUser"] == 2
    # Cores and GB, the units an administrator types, rather than the milli-cores
    # and mebibytes Kubernetes is written in.
    assert body["limits"]["maxCpuCoresPerUser"] == 8
    assert body["limits"]["maxMemoryGbPerUser"] == 16


async def test_resource_group_catalogue_is_readable_by_a_plain_user(
    client: AsyncClient, admin_client: AsyncClient, world: World
) -> None:
    """A user must be able to see what they may ask for; the *grant* is the gate."""
    await make_pool(admin_client, name="public-pool", kind="public")

    await act_as(client, "user-a")
    response = await client.get("/api/v1/resource-groups")

    assert response.status_code == 200
    assert [item["name"] for item in response.json()["items"]] == ["public-pool"]


async def test_group_admin_cannot_create_a_pool(client: AsyncClient, world: World) -> None:
    await act_as(client, "admin-a")

    response = await client.post("/api/v1/resource-groups", json={"name": "rogue-pool"})

    assert response.status_code == 403


async def test_pool_in_use_cannot_be_deleted(
    admin_client: AsyncClient, world: World, gateway: FakeK8sGateway
) -> None:
    pool = await make_pool(admin_client, name="busy-pool", kind="public")

    created = await admin_client.post(
        "/api/v1/workspaces",
        json={
            "name": "ws-a",
            "groupId": world.lab_a.id,
            "resourceGroupIds": [pool["id"]],
        },
    )
    assert created.status_code == 201, created.text

    removed = await admin_client.delete(f"/api/v1/resource-groups/{pool['id']}")

    assert removed.status_code == 409
    assert removed.json()["error"]["code"] == "resource_group_in_use"


async def test_specs_can_be_searched_by_name_and_display_name(
    admin_client: AsyncClient, world: World
) -> None:
    """
    Every list endpoint takes `search`, and this one used to be the exception.

    An unknown query parameter is dropped rather than rejected, so `?search=`
    returned the whole list with status 200. Two smoke scripts read the first row
    of that list believing they had selected their own spec, and quietly ran
    against a CPU-only one — the failure surfaced several assertions later, as a
    GPU-accounting identity that did not hold.
    """
    for name, display in (("searchable-a100", "可搜索 A100"), ("searchable-cpu", "可搜索 CPU")):
        created = await admin_client.post(
            "/api/v1/resource-specs",
            json={
                "name": name,
                "displayName": display,
                "cpuMilli": 1000,
                "memoryMb": 1024,
                "gpuType": "a100" if "a100" in name else "none",
                "gpuCount": 1 if "a100" in name else 0,
            },
        )
        assert created.status_code == 201, created.text

    by_name = await admin_client.get("/api/v1/resource-specs", params={"search": "searchable-a100"})
    assert by_name.status_code == 200
    assert [item["name"] for item in by_name.json()] == ["searchable-a100"]

    # The display name is searched too, which is what a person would type.
    by_display = await admin_client.get("/api/v1/resource-specs", params={"search": "可搜索 CPU"})
    assert [item["name"] for item in by_display.json()] == ["searchable-cpu"]

    unmatched = await admin_client.get(
        "/api/v1/resource-specs", params={"search": "nothing-matches-this"}
    )
    assert unmatched.json() == []


async def test_gpu_spec_requires_a_model(admin_client: AsyncClient, world: World) -> None:
    response = await admin_client.post(
        "/api/v1/resource-specs",
        json={
            "name": "broken",
            "cpuMilli": 1000,
            "memoryMb": 2048,
            "gpuType": "none",
            "gpuCount": 1,
        },
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "gpu_type_required"


async def test_gpu_model_without_a_count_is_rejected(
    admin_client: AsyncClient, world: World
) -> None:
    """ "0×A100" would look like a reservation while reserving nothing."""
    response = await admin_client.post(
        "/api/v1/resource-specs",
        json={"name": "odd", "cpuMilli": 1000, "memoryMb": 2048, "gpuType": "a100", "gpuCount": 0},
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "gpu_type_without_count"


async def test_only_one_runtime_limit_is_the_default(
    admin_client: AsyncClient, world: World
) -> None:
    first = await admin_client.post(
        "/api/v1/runtime-limits",
        json={"name": "short", "maxRuntimeSeconds": 14400, "isDefault": True},
    )
    assert first.status_code == 201

    second = await admin_client.post(
        "/api/v1/runtime-limits",
        json={"name": "long", "maxRuntimeSeconds": 604800, "isDefault": True},
    )
    assert second.status_code == 201
    assert second.json()["isDefault"] is True

    listed = (await admin_client.get("/api/v1/runtime-limits")).json()
    defaults = [item for item in listed if item["isDefault"]]
    assert len(defaults) == 1
    assert defaults[0]["name"] == "long"


# ---------------------------------------------------------------------------
# Workspace provisioning
# ---------------------------------------------------------------------------
async def test_creating_a_workspace_provisions_the_namespace(
    admin_client: AsyncClient, world: World, gateway: FakeK8sGateway
) -> None:
    pool = await make_pool(admin_client, name="lab-a-pool", group_id=world.lab_a.id)

    response = await admin_client.post(
        "/api/v1/workspaces",
        json={
            "name": "alice",
            "displayName": "Alice 的工作空间",
            "groupId": world.lab_a.id,
            "resourceGroupIds": [pool["id"]],
        },
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["k8sNamespace"] == "wl-lab-a-alice"
    assert body["status"] == "active"

    assert set(gateway.namespaces) == {"wl-lab-a-alice"}
    spec = gateway.namespaces["wl-lab-a-alice"]
    assert spec.labels["wl-managed"] == "true"
    assert spec.labels["wl-group"] == "lab-a"
    assert spec.labels["wl-workspace"] == "alice"

    quota = gateway.quotas["wl-lab-a-alice"]
    assert quota.hard["requests.cpu"] == "8"
    assert quota.hard["requests.memory"] == "16384Mi"
    # The object counts come from the workspace's quota profile, not from the pool:
    # `sum(max_concurrent_instances)` was only a per-workspace ceiling while a
    # workspace had exactly one user. These are the configured defaults.
    assert quota.hard["count/deployments.apps"] == "200"
    assert quota.hard["count/jobs.batch"] == "200"
    assert quota.hard["count/services"] == "400"
    assert quota.hard["pods"] == "500"
    limits = gateway.limit_ranges["wl-lab-a-alice"]
    assert limits.default_cpu == "4"
    # This pool offers exactly one spec, so the ceiling equals the default. The case
    # where they must differ has its own test below.
    assert limits.max_cpu == "4"
    assert gateway.network_policies["wl-lab-a-alice"].value == "internal"


async def test_the_limit_range_ceiling_covers_the_biggest_spec_not_the_smallest(
    admin_client: AsyncClient, world: World, gateway: FakeK8sGateway
) -> None:
    """A ``max`` taken from the smallest spec makes every bigger spec unschedulable.

    ``wl-limits`` is admission control, so a spec above the ceiling is not a slow
    start: the pod is refused before it exists. The instance then sits on "等待容器
    启动" with no pod anywhere to show for it — which is exactly how a pool offering
    both 4cpu and 8cpu could only ever run 4cpu.

    The two dimensions are deliberately crossed: the largest CPU and the largest memory
    belong to *different* specs, so code that picked one "smallest row" (or one
    "largest row") and used it for both would fail the memory assertions here.
    """
    pool = await make_pool(admin_client, name="two-tier", group_id=world.lab_a.id)
    listed = await admin_client.get(f"/api/v1/resource-groups/{pool['id']}")
    assert listed.status_code == 200, listed.text
    existing = listed.json()["specs"]
    # `make_pool` attached one 4cpu/8192Mi spec.
    assert [(spec["cpuMilli"], spec["memoryMb"]) for spec in existing] == [(4000, 8192)]

    crossed = await admin_client.post(
        "/api/v1/resource-specs",
        json={
            "name": "big-cpu-small-memory",
            "displayName": "大 CPU 小内存",
            "cpuMilli": 8000,
            "memoryMb": 4096,
            "gpuType": "none",
            "gpuCount": 0,
        },
    )
    assert crossed.status_code == 201, crossed.text
    attached = await admin_client.put(
        f"/api/v1/resource-groups/{pool['id']}/specs",
        json=[*[spec["id"] for spec in existing], crossed.json()["id"]],
    )
    assert attached.status_code == 200, attached.text

    response = await admin_client.post(
        "/api/v1/workspaces",
        json={
            "name": "two-tier",
            "groupId": world.lab_a.id,
            "resourceGroupIds": [pool["id"]],
        },
    )
    assert response.status_code == 201, response.text

    limits = gateway.limit_ranges["wl-lab-a-two-tier"]
    # Defaults stay the smallest shape in each dimension: a pod that declares nothing
    # must not become a shape the user never asked for.
    assert limits.default_cpu == "4"
    assert limits.default_request_cpu == "4"
    assert limits.default_memory == "4096Mi"
    assert limits.default_request_memory == "4096Mi"
    # Ceilings are the largest in each dimension, and they come from two different
    # specs: 8 cpu from the spec added above, 8192Mi from the one `make_pool` created.
    assert limits.max_cpu == "8"
    assert limits.max_memory == "8192Mi"


async def test_workspace_without_a_pool_gets_a_usable_but_empty_namespace(
    admin_client: AsyncClient, world: World, gateway: FakeK8sGateway
) -> None:
    """A workspace must exist before it is granted anything, but schedule nothing."""
    response = await admin_client.post(
        "/api/v1/workspaces", json={"name": "bare", "groupId": world.lab_a.id}
    )

    assert response.status_code == 201, response.text
    assert response.json()["status"] == "active"
    # Fail closed: an absent quota would read to Kubernetes as "unlimited".
    # Storage is pinned here too, not only pods. `pods: 0` stops workloads but says
    # nothing about PersistentVolumeClaims, and a workspace with no granted pool is
    # the one case where the per-volume ceiling cannot help either: with no
    # ResourceGroupLimit to read, `_refuse_oversized_user_volume` logs
    # `storage_capacity_unchecked` and lets the volume through. The namespace quota
    # is then the only thing between that workspace and unbounded storage.
    assert gateway.quotas["wl-lab-a-bare"].hard == {"pods": "0", "requests.storage": "0Gi"}


async def test_workspace_names_are_unique_within_a_group(
    admin_client: AsyncClient, world: World, gateway: FakeK8sGateway
) -> None:
    first = await admin_client.post(
        "/api/v1/workspaces", json={"name": "dup", "groupId": world.lab_a.id}
    )
    assert first.status_code == 201

    second = await admin_client.post(
        "/api/v1/workspaces", json={"name": "dup", "groupId": world.lab_a.id}
    )

    assert second.status_code == 409
    assert second.json()["error"]["code"] == "workspace_name_taken"


async def test_same_name_in_two_groups_yields_distinct_namespaces(
    admin_client: AsyncClient, world: World, gateway: FakeK8sGateway
) -> None:
    first = await admin_client.post(
        "/api/v1/workspaces", json={"name": "shared", "groupId": world.lab_a.id}
    )
    second = await admin_client.post(
        "/api/v1/workspaces", json={"name": "shared", "groupId": world.lab_b.id}
    )

    assert first.status_code == 201 and second.status_code == 201
    assert first.json()["k8sNamespace"] == "wl-lab-a-shared"
    assert second.json()["k8sNamespace"] == "wl-lab-b-shared"


async def test_provisioning_failure_is_recorded_not_swallowed(
    admin_client: AsyncClient, world: World, gateway: FakeK8sGateway
) -> None:
    gateway.fail_on["ensure_namespace"] = "cluster unreachable"

    response = await admin_client.post(
        "/api/v1/workspaces", json={"name": "doomed", "groupId": world.lab_a.id}
    )

    # The failure surfaces; the row stays behind in `error` so an operator can
    # see and retry it rather than losing the intent.
    assert response.status_code >= 500

    listed = (await admin_client.get("/api/v1/workspaces?search=doomed")).json()
    assert listed["meta"]["total"] == 1
    assert listed["items"][0]["status"] == "error"
    assert "cluster unreachable" in listed["items"][0]["statusMessage"]


async def test_reconcile_reapplies_cluster_state(
    admin_client: AsyncClient, world: World, gateway: FakeK8sGateway
) -> None:
    pool = await make_pool(admin_client, name="recon-pool", kind="public")
    created = await admin_client.post(
        "/api/v1/workspaces",
        json={"name": "recon", "groupId": world.lab_a.id, "resourceGroupIds": [pool["id"]]},
    )
    workspace_id = created.json()["id"]

    gateway.calls.clear()
    # Simulate an operator deleting the quota out of band.
    gateway.quotas.pop("wl-lab-a-recon", None)

    response = await admin_client.post(f"/api/v1/workspaces/{workspace_id}/reconcile")

    assert response.status_code == 200, response.text
    assert "wl-lab-a-recon" in gateway.quotas
    assert response.json()["applied"] is True


# ---------------------------------------------------------------------------
# The one-user-one-workspace rule
# ---------------------------------------------------------------------------
async def test_a_user_cannot_silently_be_moved_between_workspaces(
    admin_client: AsyncClient, world: World, gateway: FakeK8sGateway
) -> None:
    first = (
        await admin_client.post(
            "/api/v1/workspaces", json={"name": "one", "groupId": world.lab_a.id}
        )
    ).json()
    second = (
        await admin_client.post(
            "/api/v1/workspaces", json={"name": "two", "groupId": world.lab_a.id}
        )
    ).json()

    assigned = await admin_client.post(
        f"/api/v1/workspaces/{first['id']}/members", json={"userId": world.user_a.id}
    )
    assert assigned.status_code == 201, assigned.text

    # Moving them would relocate their files and running workloads.
    refused = await admin_client.post(
        f"/api/v1/workspaces/{second['id']}/members", json={"userId": world.user_a.id}
    )
    assert refused.status_code == 409
    assert refused.json()["error"]["code"] == "user_already_has_workspace"

    moved = await admin_client.post(
        f"/api/v1/workspaces/{second['id']}/members",
        json={"userId": world.user_a.id, "moveFromExisting": True},
    )
    assert moved.status_code == 201, moved.text

    members = (await admin_client.get(f"/api/v1/workspaces/{first['id']}/members")).json()
    assert members == []
    assert (
        await admin_client.get("/api/v1/workspaces/mine", headers={"X-No-Op": "1"})
    ).status_code == 200


async def test_a_user_sees_only_their_own_workspace(
    client: AsyncClient, admin_client: AsyncClient, world: World
) -> None:
    workspace = (
        await admin_client.post(
            "/api/v1/workspaces", json={"name": "mine", "groupId": world.lab_a.id}
        )
    ).json()
    await admin_client.post(
        f"/api/v1/workspaces/{workspace['id']}/members", json={"userId": world.user_a.id}
    )

    await act_as(client, "user-a")
    mine = await client.get("/api/v1/workspaces/mine")

    assert mine.status_code == 200
    assert mine.json()["name"] == "mine"

    listed = (await client.get("/api/v1/workspaces")).json()
    assert [item["name"] for item in listed["items"]] == ["mine"]


async def test_a_user_without_a_workspace_gets_null_not_an_error(
    client: AsyncClient, world: World
) -> None:
    await act_as(client, "user-a")

    response = await client.get("/api/v1/workspaces/mine")

    assert response.status_code == 200
    assert response.json() is None


async def test_removing_the_last_member_is_refused(admin_client: AsyncClient, world: World) -> None:
    workspace = (
        await admin_client.post(
            "/api/v1/workspaces", json={"name": "solo", "groupId": world.lab_a.id}
        )
    ).json()
    await admin_client.post(
        f"/api/v1/workspaces/{workspace['id']}/members", json={"userId": world.user_a.id}
    )

    response = await admin_client.delete(
        f"/api/v1/workspaces/{workspace['id']}/members/{world.user_a.id}"
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "last_workspace_member"


# ---------------------------------------------------------------------------
# Resource grants
# ---------------------------------------------------------------------------
async def test_a_private_pool_of_another_group_is_refused(
    admin_client: AsyncClient, world: World, gateway: FakeK8sGateway
) -> None:
    other_pool = await make_pool(admin_client, name="lab-b-only", group_id=world.lab_b.id)
    workspace = (
        await admin_client.post(
            "/api/v1/workspaces", json={"name": "lab-a-ws", "groupId": world.lab_a.id}
        )
    ).json()

    response = await admin_client.post(
        f"/api/v1/workspaces/{workspace['id']}/resources",
        json={"resourceGroupId": other_pool["id"]},
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "resource_group_not_shared"


async def test_a_public_pool_may_be_granted_across_groups(
    admin_client: AsyncClient, world: World, gateway: FakeK8sGateway
) -> None:
    shared = await make_pool(admin_client, name="shared-pool", kind="public")
    workspace = (
        await admin_client.post(
            "/api/v1/workspaces", json={"name": "consumer", "groupId": world.lab_a.id}
        )
    ).json()

    response = await admin_client.post(
        f"/api/v1/workspaces/{workspace['id']}/resources",
        json={"resourceGroupId": shared["id"]},
    )

    assert response.status_code == 201, response.text
    # Granting must immediately re-apply the quota, or the user could not use
    # what they were just given.
    assert "wl-lab-a-consumer" in gateway.quotas
    assert gateway.quotas["wl-lab-a-consumer"].hard["requests.cpu"] == "8"


async def test_revoking_a_grant_shrinks_the_quota(
    admin_client: AsyncClient, world: World, gateway: FakeK8sGateway
) -> None:
    pool = await make_pool(admin_client, name="revoke-pool", kind="public")
    workspace = (
        await admin_client.post(
            "/api/v1/workspaces",
            json={"name": "revoker", "groupId": world.lab_a.id, "resourceGroupIds": [pool["id"]]},
        )
    ).json()

    response = await admin_client.delete(
        f"/api/v1/workspaces/{workspace['id']}/resources/{pool['id']}"
    )

    assert response.status_code == 200, response.text
    quota = gateway.quotas["wl-lab-a-revoker"]
    # The quota is rewritten with zero capacity rather than deleted: the
    # namespace must stop accepting pods immediately.
    assert quota.hard.get("requests.cpu", "0") == "0"


async def test_granting_the_same_pool_twice_conflicts(
    admin_client: AsyncClient, world: World, gateway: FakeK8sGateway
) -> None:
    pool = await make_pool(admin_client, name="dup-pool", kind="public")
    workspace = (
        await admin_client.post(
            "/api/v1/workspaces",
            json={"name": "dupgrant", "groupId": world.lab_a.id, "resourceGroupIds": [pool["id"]]},
        )
    ).json()

    response = await admin_client.post(
        f"/api/v1/workspaces/{workspace['id']}/resources",
        json={"resourceGroupId": pool["id"]},
    )

    assert response.status_code == 409


async def test_quota_is_the_sum_of_the_granted_pools(
    admin_client: AsyncClient, world: World
) -> None:
    first = await make_pool(admin_client, name="pool-one", kind="public", cpu_cores=4, memory_gb=8)
    second = await make_pool(admin_client, name="pool-two", kind="public", cpu_cores=2, memory_gb=4)

    workspace = (
        await admin_client.post(
            "/api/v1/workspaces",
            json={
                "name": "aggregate",
                "groupId": world.lab_a.id,
                "resourceGroupIds": [first["id"], second["id"]],
            },
        )
    ).json()

    quotas = (
        await admin_client.get(f"/api/v1/quotas?scopeType=workspace&scopeId={workspace['id']}")
    ).json()
    by_resource = {row["resource"]: row["limitValue"] for row in quotas}

    assert by_resource["cpu"] == 6000
    assert by_resource["memory"] == 12288
    assert by_resource["storage"] == 200
    # No `instance` row: how many instances a person may hold is a ceiling on their
    # quota profile, not something summed from the pools they can reach.
    assert "instance" not in by_resource


# ---------------------------------------------------------------------------
# The quota Kubernetes is actually given
#
# These assert on the ``ResourceQuota`` the platform applies, not on the
# platform's own record of the limits. The two are different objects, and the
# difference is exactly where a real bug lived: the platform's record said
# "instance: 2" and looked right, while the namespace quota it produced allowed
# two *Services* — which one instance with both an IDE and SSH consumes, leaving
# a two-instance workspace unable to run a second instance.
# ---------------------------------------------------------------------------
def _fullest_instance() -> InstanceObjects:
    """The instance that declares the most objects: an IDE *and* SSH."""
    return build_instance_objects(
        instance_id=1,
        instance_name="fullest",
        owner_username="probe",
        namespace="wl-probe",
        workspace_labels={"wl-managed": "true"},
        image="library/ubuntu-dev:24.04",
        ide_type=IdeType.JUPYTER,
        cpu_milli=1000,
        memory_mb=1024,
        gpu_type="none",
        gpu_count=0,
        home_pvc_name="wl-vol-probe-1",
        home_mount_path="/home/probe",
        ssh_enabled=True,
        authorized_keys="ssh-ed25519 AAAAprobe probe@wl\n",
        node_selector={},
        tolerations=(),
    )


def _services_declared(objects: InstanceObjects) -> int:
    return sum(1 for spec in (objects.ssh_service, objects.ide_service) if spec is not None)


async def _make_pool_granting_no_storage(admin_client: AsyncClient, name: str) -> dict:
    """A pool that grants no storage at all, which ``make_pool`` cannot express."""
    created = await admin_client.post(
        "/api/v1/resource-groups",
        json={
            "name": name,
            "displayName": name,
            "kind": "public",
            "networkType": "internal",
            "maxRuntimeSeconds": 86400,
        },
    )
    assert created.status_code == 201, created.text
    pool = created.json()
    limited = await admin_client.put(
        f"/api/v1/resource-groups/{pool['id']}/limits",
        json={
            "maxGpuPerUser": 0,
            "maxCpuCoresPerUser": 8,
            "maxMemoryGbPerUser": 16,
            "maxStorageGbPerUser": 0,
        },
    )
    assert limited.status_code == 200, limited.text
    return pool


async def _workspace_on(
    admin_client: AsyncClient, world: World, *, name: str, pool_ids: list[int]
) -> dict:
    response = await admin_client.post(
        "/api/v1/workspaces",
        json={"name": name, "groupId": world.lab_a.id, "resourceGroupIds": pool_ids},
    )
    assert response.status_code == 201, response.text
    return response.json()


async def test_the_service_allowance_covers_the_services_an_instance_creates(
    admin_client: AsyncClient, world: World, gateway: FakeK8sGateway
) -> None:
    """The service allowance must cover the instances the deployment allowance permits.

    Anchored to what :func:`build_instance_objects` actually declares rather than to
    the number two, so giving an instance a third Service fails here instead of in a
    workspace that cannot start its second instance. Both allowances now come from
    the workspace's quota profile, whose defaults keep the 2:1 ratio — an
    administrator who edits one without the other breaks this relationship, which is
    exactly what the assertion is for.
    """
    pool = await make_pool(admin_client, name="svc-pool", kind="public")
    workspace = await _workspace_on(admin_client, world, name="svc-ws", pool_ids=[pool["id"]])

    hard = gateway.quotas[workspace["k8sNamespace"]].hard
    per_instance = _services_declared(_fullest_instance())

    assert per_instance == SERVICES_PER_INSTANCE
    assert int(hard["count/services"]) >= per_instance * int(hard["count/deployments.apps"])
    assert int(hard["count/services"]) >= 4


async def test_attaching_a_workspace_quota_profile_moves_its_object_counts(
    admin_client: AsyncClient, world: World, gateway: FakeK8sGateway
) -> None:
    """The namespace object counts are *derived*, so attaching a template must apply it.

    Clearing the attachment has to go back to the default rather than to nothing:
    ``None`` means "follow the default", not "no ceiling".
    """
    pool = await make_pool(admin_client, name="profile-pool", kind="public")
    workspace = await _workspace_on(admin_client, world, name="profiled", pool_ids=[pool["id"]])

    created = await admin_client.post(
        "/api/v1/quota-profiles",
        json={
            "kind": "workspace",
            "name": "small-space",
            "maxPods": 40,
            "maxDeployments": 20,
            "maxServices": 40,
            "maxJobs": 20,
        },
    )
    assert created.status_code == 201, created.text

    attached = await admin_client.put(
        f"/api/v1/workspaces/{workspace['id']}/quota-profile",
        json={"profileId": created.json()["id"]},
    )
    assert attached.status_code == 200, attached.text
    assert attached.json()["quotaProfileId"] == created.json()["id"]

    hard = gateway.quotas[workspace["k8sNamespace"]].hard
    assert hard["pods"] == "40"
    assert hard["count/deployments.apps"] == "20"
    assert hard["count/services"] == "40"
    assert hard["count/jobs.batch"] == "20"
    # Compute still comes from the granted pools at this point: only the object
    # counts are profile-driven yet, and this records that boundary rather than
    # letting it drift silently.
    assert hard["requests.cpu"] == "8"
    assert hard["requests.memory"] == "16384Mi"

    cleared = await admin_client.put(
        f"/api/v1/workspaces/{workspace['id']}/quota-profile", json={"profileId": None}
    )
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["quotaProfileId"] is None
    assert gateway.quotas[workspace["k8sNamespace"]].hard["pods"] == "500"


async def test_storage_is_bounded_even_when_no_pool_grants_any(
    admin_client: AsyncClient, world: World, gateway: FakeK8sGateway
) -> None:
    """An absent limit is *unlimited* to Kubernetes, which is not a policy.

    The namespace quota is the only place a group's storage is bounded in
    aggregate: ``max_storage_gb_per_user`` is checked when a volume is registered
    and says nothing about the total. Leaving the key out when nothing was
    granted therefore made the group's storage unbounded.
    """
    pool = await _make_pool_granting_no_storage(admin_client, "nostorage-pool")
    workspace = await _workspace_on(admin_client, world, name="nostorage-ws", pool_ids=[pool["id"]])

    hard = gateway.quotas[workspace["k8sNamespace"]].hard
    assert hard["requests.storage"] == "0Gi"


async def test_storage_is_the_sum_of_what_the_pools_grant(
    admin_client: AsyncClient, world: World, gateway: FakeK8sGateway
) -> None:
    first = await make_pool(admin_client, name="storage-one", kind="public")
    second = await make_pool(admin_client, name="storage-two", kind="public")
    workspace = await _workspace_on(
        admin_client, world, name="storage-ws", pool_ids=[first["id"], second["id"]]
    )

    hard = gateway.quotas[workspace["k8sNamespace"]].hard
    assert hard["requests.storage"] == "200Gi"


async def test_the_claim_allowance_is_not_guessed_from_instances(
    admin_client: AsyncClient, world: World, gateway: FakeK8sGateway
) -> None:
    """Nothing an instance or a task does creates a claim, so nothing sizes one.

    The old formula allowed one claim per instance plus one per task. An instance
    creates none — its home is the owner's *volume*, which outlives it — and a
    task's scratch claims were removed. The claims that do land in the namespace
    (one per registered volume, and one per global share per namespace) bear no
    relation to how many instances may run, so a number derived from instances
    was wrong in both directions: it failed volume registration once the group
    outgrew it, while never actually bounding anything by claims. Bytes are what
    is bounded, by ``requests.storage``.
    """
    pool = await make_pool(admin_client, name="claim-pool", kind="public")
    workspace = await _workspace_on(admin_client, world, name="claim-ws", pool_ids=[pool["id"]])

    hard = gateway.quotas[workspace["k8sNamespace"]].hard
    assert "count/persistentvolumeclaims" not in hard


# ---------------------------------------------------------------------------
# Isolation
# ---------------------------------------------------------------------------
async def test_group_admin_sees_only_their_own_groups_workspaces(
    client: AsyncClient, admin_client: AsyncClient, world: World
) -> None:
    await admin_client.post("/api/v1/workspaces", json={"name": "a-ws", "groupId": world.lab_a.id})
    await admin_client.post("/api/v1/workspaces", json={"name": "b-ws", "groupId": world.lab_b.id})

    await act_as(client, "admin-a")
    listed = (await client.get("/api/v1/workspaces")).json()

    assert [item["name"] for item in listed["items"]] == ["a-ws"]


async def test_cross_tenant_workspace_detail_is_not_found(
    client: AsyncClient, admin_client: AsyncClient, world: World
) -> None:
    workspace = (
        await admin_client.post(
            "/api/v1/workspaces", json={"name": "b-only", "groupId": world.lab_b.id}
        )
    ).json()

    await act_as(client, "admin-a")
    response = await client.get(f"/api/v1/workspaces/{workspace['id']}")

    # 404, not 403: workspace ids are sequential and must not be probeable.
    assert response.status_code == 404


async def test_a_plain_user_cannot_create_a_workspace(client: AsyncClient, world: World) -> None:
    await act_as(client, "user-a")

    response = await client.post(
        "/api/v1/workspaces", json={"name": "nope", "groupId": world.lab_a.id}
    )

    assert response.status_code == 403


async def test_group_admin_defaults_to_their_own_group(
    client: AsyncClient, world: World, gateway: FakeK8sGateway
) -> None:
    """Omitting groupId is the one-click path for a lab administrator."""
    await act_as(client, "admin-a")

    response = await client.post("/api/v1/workspaces", json={"name": "implicit"})

    assert response.status_code == 201, response.text
    assert response.json()["k8sNamespace"] == "wl-lab-a-implicit"


async def test_group_admin_cannot_create_a_workspace_in_another_group(
    client: AsyncClient, world: World
) -> None:
    await act_as(client, "admin-a")

    response = await client.post(
        "/api/v1/workspaces", json={"name": "sneaky", "groupId": world.lab_b.id}
    )

    assert response.status_code == 403


# ---------------------------------------------------------------------------
# Deletion
# ---------------------------------------------------------------------------
async def test_deleting_a_workspace_removes_the_namespace_and_keeps_the_row(
    admin_client: AsyncClient, world: World, gateway: FakeK8sGateway
) -> None:
    workspace = (
        await admin_client.post(
            "/api/v1/workspaces", json={"name": "doomed", "groupId": world.lab_a.id}
        )
    ).json()
    assert "wl-lab-a-doomed" in gateway.namespaces

    response = await admin_client.delete(f"/api/v1/workspaces/{workspace['id']}")

    assert response.status_code == 200, response.text
    assert "wl-lab-a-doomed" not in gateway.namespaces

    # The row survives for audit and usage history, but is out of every listing.
    listed = (await admin_client.get("/api/v1/workspaces?search=doomed")).json()
    assert listed["meta"]["total"] == 0

    all_rows = (await admin_client.get("/api/v1/workspaces?search=doomed&isActive=false")).json()
    assert all_rows["meta"]["total"] == 1


async def test_group_admin_cannot_delete_a_workspace(
    client: AsyncClient, admin_client: AsyncClient, world: World
) -> None:
    """Destructive and irreversible — kept to global administrators."""
    workspace = (
        await admin_client.post(
            "/api/v1/workspaces", json={"name": "keep", "groupId": world.lab_a.id}
        )
    ).json()

    await act_as(client, "admin-a")
    response = await client.delete(f"/api/v1/workspaces/{workspace['id']}")

    assert response.status_code == 403


# ---------------------------------------------------------------------------
# Node inventory
# ---------------------------------------------------------------------------
async def test_node_sync_records_nodes_and_their_accelerators(
    admin_client: AsyncClient, world: World, gateway: FakeK8sGateway
) -> None:
    from app.k8s import NodeInfo

    gateway.nodes = [
        NodeInfo(
            name="gpu-node-1",
            status="ready",
            schedulable=True,
            cpu_total_milli=64000,
            memory_total_mb=262144,
            cpu_allocatable_milli=63000,
            memory_allocatable_mb=250000,
            labels={
                "kubernetes.io/hostname": "gpu-node-1",
                "nvidia.com/gpu.product": "NVIDIA-A100-SXM4-40GB",
            },
            gpu_devices=((0, "a100", 40960, "GPU-0"), (1, "a100", 40960, "GPU-1")),
        ),
        NodeInfo(
            name="cpu-node-1",
            status="ready",
            schedulable=True,
            cpu_total_milli=16000,
            memory_total_mb=65536,
            cpu_allocatable_milli=15500,
            memory_allocatable_mb=60000,
            labels={"kubernetes.io/hostname": "cpu-node-1"},
        ),
    ]

    response = await admin_client.post("/api/v1/nodes/sync")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["nodesSeen"] == 2
    assert body["nodesCreated"] == 2
    assert body["devicesSeen"] == 2

    nodes = (await admin_client.get("/api/v1/nodes")).json()
    by_name = {node["name"]: node for node in nodes}
    assert by_name["gpu-node-1"]["gpuCount"] == 2
    assert by_name["gpu-node-1"]["gpuModels"] == ["a100"]
    assert by_name["gpu-node-1"]["cpuAllocatableMilli"] == 63000
    assert by_name["cpu-node-1"]["gpuCount"] == 0

    # Syncing again updates rather than duplicates — and reports *no* updates,
    # because nothing about the nodes changed. The counter is a change count, not
    # a "rows I looked at" count; the old version incremented it for every node it
    # visited, which made it useless for spotting drift.
    again = await admin_client.post("/api/v1/nodes/sync")
    assert again.json()["nodesCreated"] == 0
    assert again.json()["nodesUpdated"] == 0

    # A node that really did change is counted.
    gateway.nodes[1] = NodeInfo(
        name="cpu-node-1",
        status="not_ready",
        schedulable=False,
        cpu_total_milli=16000,
        memory_total_mb=65536,
        cpu_allocatable_milli=15500,
        memory_allocatable_mb=60000,
        labels={"kubernetes.io/hostname": "cpu-node-1"},
    )
    changed = await admin_client.post("/api/v1/nodes/sync")
    assert changed.json()["nodesUpdated"] == 1


async def test_a_disappeared_node_stops_looking_schedulable(
    admin_client: AsyncClient, world: World, gateway: FakeK8sGateway
) -> None:
    from app.k8s import NodeInfo

    gateway.nodes = [
        NodeInfo("gone", "ready", True, 8000, 16384, 8000, 16384, {}),
    ]
    await admin_client.post("/api/v1/nodes/sync")

    gateway.nodes = []
    await admin_client.post("/api/v1/nodes/sync")

    nodes = (await admin_client.get("/api/v1/nodes")).json()
    assert nodes[0]["status"] == "unknown"
    assert nodes[0]["schedulable"] is False


async def test_a_plain_user_cannot_read_the_node_inventory(
    client: AsyncClient, world: World
) -> None:
    await act_as(client, "user-a")

    assert (await client.get("/api/v1/nodes")).status_code == 403
    assert (await client.post("/api/v1/nodes/sync")).status_code == 403


# ---------------------------------------------------------------------------
# Workspace quota isolation
# ---------------------------------------------------------------------------
async def test_a_user_cannot_read_another_workspaces_quota(
    client: AsyncClient, admin_client: AsyncClient, world: World
) -> None:
    workspace = (
        await admin_client.post(
            "/api/v1/workspaces", json={"name": "private-ws", "groupId": world.lab_a.id}
        )
    ).json()

    await act_as(client, "user-a")
    response = await client.get(f"/api/v1/quotas?scopeType=workspace&scopeId={workspace['id']}")

    assert response.status_code == 404


async def test_a_user_can_read_their_own_quota(
    client: AsyncClient, admin_client: AsyncClient, world: World, gateway: FakeK8sGateway
) -> None:
    pool = await make_pool(admin_client, name="own-quota", kind="public")
    workspace = (
        await admin_client.post(
            "/api/v1/workspaces",
            json={
                "name": "own",
                "groupId": world.lab_a.id,
                "resourceGroupIds": [pool["id"]],
            },
        )
    ).json()
    await admin_client.post(
        f"/api/v1/workspaces/{workspace['id']}/members", json={"userId": world.user_a.id}
    )

    await act_as(client, "user-a")
    response = await client.get(f"/api/v1/quotas?scopeType=workspace&scopeId={workspace['id']}")

    assert response.status_code == 200, response.text
    # No `instance` row any more: holding N instances is a per-user ceiling on a
    # quota profile, not a workspace quota. What the workspace has is its compute
    # and storage ceiling.
    assert {row["resource"] for row in response.json()} >= {"cpu", "memory", "storage"}


async def test_adjusting_a_quota_never_resets_consumption(
    admin_client: AsyncClient, world: World, gateway: FakeK8sGateway, session_factory
) -> None:
    """Otherwise a user could escape their quota by asking for an edit."""
    from sqlalchemy import select

    from app.models.enums import QuotaScope
    from app.models.resource import Quota

    pool = await make_pool(admin_client, name="counter-pool", kind="public")
    workspace = (
        await admin_client.post(
            "/api/v1/workspaces",
            json={
                "name": "counter",
                "groupId": world.lab_a.id,
                "resourceGroupIds": [pool["id"]],
            },
        )
    ).json()

    # Pretend the user has already consumed most of their allowance.
    async with session_factory() as session:
        quota = await session.scalar(
            select(Quota).where(
                Quota.scope_type == QuotaScope.WORKSPACE,
                Quota.scope_id == workspace["id"],
                Quota.resource == "cpu",
            )
        )
        assert quota is not None
        quota.used_value = 6000
        await session.commit()
        quota_id = quota.id

    response = await admin_client.patch(f"/api/v1/quotas/{quota_id}", json={"limitValue": 16000})

    assert response.status_code == 200, response.text
    assert response.json()["limitValue"] == 16000
    assert response.json()["usedValue"] == 6000

    async with session_factory() as session:
        stored = await session.scalar(select(Quota).where(Quota.id == quota_id))
        assert stored is not None
        assert stored.used_value == 6000


async def test_group_admin_cannot_adjust_a_quota(
    client: AsyncClient, admin_client: AsyncClient, world: World, session_factory
) -> None:
    from app.models.enums import QuotaScope
    from app.models.resource import Quota

    async with session_factory() as session:
        quota = Quota(
            scope_type=QuotaScope.GROUP,
            scope_id=world.lab_a.id,
            resource="gpu:a100",
            limit_value=4,
            unit="card",
        )
        session.add(quota)
        await session.commit()
        quota_id = quota.id

    await act_as(client, "admin-a")
    response = await client.patch(f"/api/v1/quotas/{quota_id}", json={"limitValue": 64})

    assert response.status_code == 403


@pytest.fixture(autouse=True)
def _always_use_the_fake_gateway(gateway: FakeK8sGateway) -> None:
    """Every test in this module talks to the fake, never a cluster."""
    set_gateway(gateway)
