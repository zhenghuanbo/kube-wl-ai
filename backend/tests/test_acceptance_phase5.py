"""Phase 5 acceptance: persistent storage volumes.

The point of this phase is that a volume stops being a property of an instance
and becomes something with its own identity. Almost every test here is about
that separation:

* a volume exists before anything mounts it, and survives every instance that
  did;
* a read-only dataset is read-only *at the mount*, not merely in the UI, because
  the guarantee is worthless if a training job can write through it;
* a volume cannot be deleted while an instance is still holding it;
* capacity grows and never shrinks, matching what Kubernetes will actually do.

Every cluster interaction goes through ``FakeK8sGateway``, so these assertions
are about what the platform *submits* — which is the part it is responsible for.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.k8s import FakeK8sGateway
from app.k8s.gateway import PvcInfo
from app.models.enums import GroupKind, GroupRole, StorageKind, StorageStatus
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
    user_a: User
    user_b: User
    observer: User
    pool_id: int
    spec_id: int
    workspace_id: int


async def act_as(client: AsyncClient, username: str) -> AsyncClient:
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

    workspace = await admin_client.post(
        "/api/v1/workspaces",
        json={"name": "s5-alpha", "groupId": lab_a.id, "resourceGroupIds": [pool_id]},
    )
    assert workspace.status_code == 201, workspace.text
    workspace_id = workspace.json()["id"]

    moved = await admin_client.post(
        f"/api/v1/workspaces/{workspace_id}/members",
        json={"userId": user_a.id, "moveFromExisting": True},
    )
    assert moved.status_code in (200, 201), moved.text
    moved_observer = await admin_client.post(
        f"/api/v1/workspaces/{workspace_id}/members",
        json={"userId": observer.id, "moveFromExisting": True},
    )
    assert moved_observer.status_code in (200, 201), moved_observer.text
    # The lab admin administers this workspace and is a member of it, which is
    # what lets them publish a dataset *into* it without naming it explicitly.
    moved_admin = await admin_client.post(
        f"/api/v1/workspaces/{workspace_id}/members",
        json={"userId": admin_a.id, "moveFromExisting": True},
    )
    assert moved_admin.status_code in (200, 201), moved_admin.text

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
    )


@pytest.fixture
async def world(admin_client: AsyncClient, seeded: AsyncSession) -> World:
    return await build_world(admin_client, seeded)


def instance_payload(world: World, name: str, **overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "name": name,
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
    payload: dict[str, object] = {"name": name, "capacityGb": 10, "kind": "personal_data"}
    payload.update(overrides)
    response = await client.post("/api/v1/storages", json=payload)
    assert response.status_code == 201, response.text
    body = response.json()
    return int(body["id"]), body


async def make_instance(client: AsyncClient, world: World, name: str, **overrides: object) -> int:
    response = await client.post(
        "/api/v1/instances", json=instance_payload(world, name, **overrides)
    )
    assert response.status_code == 201, response.text
    return int(response.json()["id"])


# ---------------------------------------------------------------------------
# Creating a volume
# ---------------------------------------------------------------------------
async def test_a_personal_volume_is_provisioned_before_anything_mounts_it(
    client: AsyncClient, world: World, gateway: FakeK8sGateway
) -> None:
    await act_as(client, "s5-dana")
    storage_id, body = await make_storage(client, "scratch")

    assert body["status"] == StorageStatus.BOUND.value
    assert body["kind"] == StorageKind.PERSONAL_DATA.value
    assert body["ownerUsername"] == "s5-dana"

    # The claim exists in the workspace namespace with no instance in sight.
    assert len(gateway.pvcs) == 1
    spec = next(iter(gateway.pvcs.values()))
    assert spec.size_gb == 10
    assert spec.labels["wl-storage-id"] == str(storage_id)


async def test_the_claim_name_does_not_depend_on_an_instance(
    client: AsyncClient, world: World, gateway: FakeK8sGateway
) -> None:
    """A volume must keep its cluster identity across instances.

    If the PVC name were derived from whichever instance happened to mount it,
    deleting that instance would orphan the data.
    """
    await act_as(client, "s5-dana")
    _, body = await make_storage(client, "keepme")
    pvc_name = body["pvcName"]

    instance_id = await make_instance(client, world, "one", storageIds=[body["id"]])
    assert body["pvcName"] == pvc_name

    await client.delete(f"/api/v1/instances/{instance_id}")
    still_there = await client.get(f"/api/v1/storages/{body['id']}")
    assert still_there.status_code == 200
    assert still_there.json()["pvcName"] == pvc_name
    assert f"{body['k8sNamespace']}/{pvc_name}" in gateway.pvcs


async def test_a_duplicate_name_in_one_workspace_is_a_clean_conflict(
    client: AsyncClient, world: World
) -> None:
    """The unique constraint is the arbiter, so it must not surface as a 500.

    A pre-flight check cannot be trusted: two concurrent requests for the same
    name both pass it, and only one can win the insert.
    """
    await act_as(client, "s5-dana")
    await make_storage(client, "samename")

    response = await client.post(
        "/api/v1/storages", json={"name": "samename", "kind": "personal_data", "capacityGb": 5}
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "storage_name_taken"


async def test_a_second_personal_home_is_refused(client: AsyncClient, world: World) -> None:
    """One home per person.

    Two would reintroduce the ambiguity that makes a container disposable:
    nobody could say which one holds the code.
    """
    await act_as(client, "s5-dana")
    await make_storage(client, "home", kind="personal_home")

    second = await client.post(
        "/api/v1/storages", json={"name": "home2", "kind": "personal_home", "capacityGb": 5}
    )
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "storage_home_exists"


async def test_capacity_bounds_are_enforced(client: AsyncClient, world: World) -> None:
    await act_as(client, "s5-dana")

    zero = await client.post(
        "/api/v1/storages", json={"name": "zero", "capacityGb": 0, "kind": "personal_data"}
    )
    assert zero.status_code == 422

    huge = await client.post(
        "/api/v1/storages",
        json={"name": "huge", "capacityGb": 999999, "kind": "personal_data"},
    )
    assert huge.status_code == 422


async def test_a_bad_name_is_refused(client: AsyncClient, world: World) -> None:
    await act_as(client, "s5-dana")
    response = await client.post(
        "/api/v1/storages",
        json={"name": "Bad Name!", "capacityGb": 5, "kind": "personal_data"},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_storage_name"


# ---------------------------------------------------------------------------
# Datasets are curated, and read-only
# ---------------------------------------------------------------------------
async def test_a_user_cannot_publish_a_dataset(client: AsyncClient, world: World) -> None:
    """Publishing a dataset is an administrative act.

    The read-only guarantee is the whole value of the label, so who may apply it
    has to be restricted.
    """
    await act_as(client, "s5-dana")
    response = await client.post(
        "/api/v1/storages",
        json={
            "name": "fake-dataset",
            "kind": "dataset",
            "capacityGb": 10,
            "source": "https://example.edu/data",
        },
    )
    assert response.status_code == 403


async def test_a_dataset_requires_provenance(client: AsyncClient, world: World) -> None:
    """Without a source, the next person cannot judge whether to trust it."""
    await act_as(client, "s5-boss")
    response = await client.post(
        "/api/v1/storages",
        json={"name": "mystery", "kind": "dataset", "capacityGb": 10},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "dataset_source_required"


async def test_a_dataset_is_read_only_in_the_pod_spec(
    client: AsyncClient, world: World, gateway: FakeK8sGateway
) -> None:
    """Read-only must reach the container, not stop at the API response."""
    await act_as(client, "s5-boss")
    _, dataset = await make_storage(
        client,
        "imagenet",
        kind="dataset",
        capacityGb=100,
        source="https://example.edu/imagenet",
    )

    await act_as(client, "s5-boss")
    await make_instance(client, world, "reads-data", storageIds=[dataset["id"]])

    deployment = next(iter(gateway.deployments.values()))
    mounts = deployment.extra_mounts
    assert len(mounts) == 1
    assert mounts[0].read_only is True
    assert mounts[0].mount_path == "/mnt/dataset"
    assert mounts[0].pvc_name == dataset["pvcName"]


async def test_a_dataset_cannot_be_made_writable(client: AsyncClient, world: World) -> None:
    await act_as(client, "s5-boss")
    storage_id, _ = await make_storage(
        client, "frozen", kind="dataset", source="https://example.edu/x"
    )

    response = await client.patch(f"/api/v1/storages/{storage_id}", json={"isReadOnly": False})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "dataset_always_read_only"


async def test_a_personal_volume_is_writable(
    client: AsyncClient, world: World, gateway: FakeK8sGateway
) -> None:
    await act_as(client, "s5-dana")
    _, storage = await make_storage(client, "writable")
    await make_instance(client, world, "writes-data", storageIds=[storage["id"]])

    deployment = next(iter(gateway.deployments.values()))
    assert deployment.extra_mounts[0].read_only is False


# ---------------------------------------------------------------------------
# Visibility
# ---------------------------------------------------------------------------
async def test_a_colleague_cannot_see_a_personal_volume(client: AsyncClient, world: World) -> None:
    await act_as(client, "s5-dana")
    storage_id, _ = await make_storage(client, "private")

    await act_as(client, "s5-gazer")
    # 404 rather than 403: a 403 would confirm the volume exists.
    assert (await client.get(f"/api/v1/storages/{storage_id}")).status_code == 404


async def test_another_lab_cannot_see_a_group_share(client: AsyncClient, world: World) -> None:
    await act_as(client, "s5-boss")
    storage_id, _ = await make_storage(client, "shared", kind="group_share")

    await act_as(client, "s5-erin")
    assert (await client.get(f"/api/v1/storages/{storage_id}")).status_code == 404


async def test_an_observer_cannot_create_storage(client: AsyncClient, world: World) -> None:
    await act_as(client, "s5-gazer")
    response = await client.post(
        "/api/v1/storages", json={"name": "nope", "capacityGb": 5, "kind": "personal_data"}
    )
    assert response.status_code == 403


# ---------------------------------------------------------------------------
# Attaching to an instance
# ---------------------------------------------------------------------------
async def test_an_attached_volume_does_not_double_charge_quota(
    client: AsyncClient, world: World
) -> None:
    """A volume is charged once, when it is created.

    Counting the same gigabyte again on every instance that mounts it would make
    a workspace hit its ceiling for no reason.
    """
    await act_as(client, "s5-dana")
    _, storage = await make_storage(client, "bulk", capacityGb=10)

    # The instance's own scratch is 5 GB; the attached 10 GB must not be added.
    instance_id = await make_instance(client, world, "mounted", storageIds=[storage["id"]])
    detail = await client.get(f"/api/v1/instances/{instance_id}")
    assert detail.json()["storageGb"] == 5


async def test_a_cross_workspace_volume_is_refused(
    client: AsyncClient, world: World, admin_client: AsyncClient
) -> None:
    """A claim lives in one namespace; a pod in another simply cannot see it.

    Caught before submission rather than left to the scheduler, because the
    symptom would otherwise be a container stuck in ``ContainerCreating`` with
    no useful message.
    """
    beta = await admin_client.post(
        "/api/v1/workspaces",
        json={"name": "s5-beta", "groupId": world.lab_b.id, "resourceGroupIds": []},
    )
    assert beta.status_code == 201, beta.text
    beta_id = beta.json()["id"]

    # A global administrator can see and create in both workspaces, so they are
    # the only caller who can even attempt this.
    far = await admin_client.post(
        "/api/v1/storages",
        json={
            "name": "over-there",
            "kind": "personal_data",
            "capacityGb": 5,
            "workspaceId": beta_id,
        },
    )
    assert far.status_code == 201, far.text
    far_id = far.json()["id"]

    await act_as(client, "s5-dana")
    dana = await client.post(
        "/api/v1/instances",
        json=instance_payload(world, "cross", workspaceId=world.workspace_id, storageIds=[far_id]),
    )
    # Dana cannot even see a volume belonging to another lab, so she gets a 404
    # rather than a message confirming it exists.
    assert dana.status_code == 404
    assert dana.json()["error"]["code"] == "storage_not_found"

    # An administrator *can* see it, so they get the message that actually
    # explains the problem instead of a bare "not found".
    await act_as(admin_client, "s5-root")
    root = await admin_client.post(
        "/api/v1/instances",
        json=instance_payload(
            world,
            "cross2",
            workspaceId=world.workspace_id,
            # An administrator creating on someone else's behalf must name them.
            ownerUserId=world.user_a.id,
            storageIds=[far_id],
        ),
    )
    assert root.status_code == 400, root.text
    assert root.json()["error"]["code"] == "storage_cross_workspace"


async def test_a_home_volume_cannot_be_mounted_explicitly(
    client: AsyncClient, world: World
) -> None:
    await act_as(client, "s5-dana")
    _, home = await make_storage(client, "myhome", kind="personal_home")

    response = await client.post(
        "/api/v1/instances", json=instance_payload(world, "explicit", storageIds=[home["id"]])
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "storage_home_implicit"


async def test_an_inactive_volume_cannot_be_mounted(client: AsyncClient, world: World) -> None:
    await act_as(client, "s5-dana")
    storage_id, _ = await make_storage(client, "retired")
    await client.delete(f"/api/v1/storages/{storage_id}")

    response = await client.post(
        "/api/v1/instances", json=instance_payload(world, "gone", storageIds=[storage_id])
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "storage_inactive"


async def test_the_attachable_list_excludes_homes(client: AsyncClient, world: World) -> None:
    await act_as(client, "s5-dana")
    await make_storage(client, "adata")
    await make_storage(client, "ahome", kind="personal_home")

    response = await client.get(
        "/api/v1/storages/attachable", params={"workspaceId": world.workspace_id}
    )
    assert response.status_code == 200
    names = [row["name"] for row in response.json()]
    assert "adata" in names
    assert "ahome" not in names


# ---------------------------------------------------------------------------
# Resizing
# ---------------------------------------------------------------------------
async def test_a_volume_can_grow(
    client: AsyncClient, world: World, gateway: FakeK8sGateway
) -> None:
    await act_as(client, "s5-dana")
    storage_id, _ = await make_storage(client, "grow", capacityGb=10)

    response = await client.post(f"/api/v1/storages/{storage_id}/resize", json={"capacityGb": 50})
    assert response.status_code == 200, response.text
    assert response.json()["capacityGb"] == 50
    assert next(iter(gateway.pvcs.values())).size_gb == 50


async def test_a_volume_cannot_shrink(client: AsyncClient, world: World) -> None:
    """Kubernetes cannot shrink a claim, so accepting it would desynchronise."""
    await act_as(client, "s5-dana")
    storage_id, _ = await make_storage(client, "shrink", capacityGb=50)

    response = await client.post(f"/api/v1/storages/{storage_id}/resize", json={"capacityGb": 10})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "storage_shrink_unsupported"


async def test_a_failed_resize_leaves_the_recorded_size_alone(
    client: AsyncClient, world: World, gateway: FakeK8sGateway, admin_client: AsyncClient
) -> None:
    """If the cluster did not grow, the row must not claim it did."""
    await act_as(client, "s5-dana")
    storage_id, _ = await make_storage(client, "noshrink", capacityGb=10)

    # Make the cluster refuse: report the claim as already larger than the
    # requested target would be a shrink, which the fake gateway rejects.
    spec = next(iter(gateway.pvcs.values()))
    gateway.pvcs[f"{spec.namespace}/{spec.name}"] = spec.__class__(
        name=spec.name,
        namespace=spec.namespace,
        size_gb=100,
        storage_class=spec.storage_class,
        labels=spec.labels,
    )

    response = await client.post(f"/api/v1/storages/{storage_id}/resize", json={"capacityGb": 20})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "storage_resize_failed"

    after = await client.get(f"/api/v1/storages/{storage_id}")
    # Unchanged, and *not* marked FAILED: the volume is still bound and still
    # usable at 10 GB. A failed operation is not a failed volume, and reporting
    # it as one would send the user chasing a problem that does not exist.
    assert after.json()["capacityGb"] == 10
    assert after.json()["status"] == StorageStatus.BOUND.value


# ---------------------------------------------------------------------------
# Deletion
# ---------------------------------------------------------------------------
async def test_a_volume_in_use_cannot_be_deleted(client: AsyncClient, world: World) -> None:
    """Deleting the claim would orphan the pods still mounting it."""
    await act_as(client, "s5-dana")
    storage_id, _ = await make_storage(client, "inuse")
    instance_id = await make_instance(client, world, "holder", storageIds=[storage_id])

    response = await client.delete(f"/api/v1/storages/{storage_id}")
    assert response.status_code == 409
    body = response.json()
    assert body["error"]["code"] == "storage_in_use"
    assert instance_id in body["error"]["detail"]["instanceIds"]


async def test_a_volume_can_be_deleted_once_its_instance_is_gone(
    client: AsyncClient, world: World, gateway: FakeK8sGateway
) -> None:
    await act_as(client, "s5-dana")
    storage_id, body = await make_storage(client, "freed")
    instance_id = await make_instance(client, world, "temp", storageIds=[storage_id])

    await client.delete(f"/api/v1/instances/{instance_id}")

    response = await client.delete(f"/api/v1/storages/{storage_id}")
    assert response.status_code == 200, response.text
    assert response.json()["status"] == StorageStatus.RELEASED.value
    assert response.json()["isActive"] is False
    assert body["pvcName"] not in gateway.pvcs


async def test_a_deleted_volume_keeps_its_record(client: AsyncClient, world: World) -> None:
    """Capacity must be released; the history must not be."""
    await act_as(client, "s5-dana")
    storage_id, _ = await make_storage(client, "history")
    await client.delete(f"/api/v1/storages/{storage_id}")

    listed = await client.get("/api/v1/storages", params={"includeInactive": True})
    assert any(row["id"] == storage_id for row in listed.json()["items"])

    default = await client.get("/api/v1/storages")
    assert all(row["id"] != storage_id for row in default.json()["items"])


# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------
async def test_reconcile_reports_a_waiting_volume_honestly(
    client: AsyncClient, world: World, gateway: FakeK8sGateway
) -> None:
    """`WaitForFirstConsumer` volumes sit Pending until a pod schedules.

    That is normal, and the reason belongs in `statusMessage` rather than being
    reported as a failure.
    """
    await act_as(client, "s5-dana")
    storage_id, _ = await make_storage(client, "waiting")

    spec = next(iter(gateway.pvcs.values()))
    gateway.pvc_states[f"{spec.namespace}/{spec.name}"] = PvcInfo(
        name=spec.name, phase="Pending", message="waiting for first consumer"
    )

    response = await client.post(f"/api/v1/storages/{storage_id}/reconcile")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == StorageStatus.PENDING.value
    assert body["statusMessage"] == "waiting for first consumer"


async def test_reconcile_marks_a_vanished_claim_as_failed(
    client: AsyncClient, world: World, gateway: FakeK8sGateway
) -> None:
    """Claim deleted outside the platform: saying Bound forever would be a lie."""
    await act_as(client, "s5-dana")
    storage_id, _ = await make_storage(client, "vanished")

    gateway.pvcs.clear()
    response = await client.post(f"/api/v1/storages/{storage_id}/reconcile")
    assert response.status_code == 200
    assert response.json()["status"] == StorageStatus.FAILED.value


async def test_the_cluster_is_the_authority_on_size(
    client: AsyncClient, world: World, gateway: FakeK8sGateway
) -> None:
    """If the volume is larger than requested, record the real number."""
    await act_as(client, "s5-dana")
    storage_id, _ = await make_storage(client, "bigger", capacityGb=10)

    spec = next(iter(gateway.pvcs.values()))
    gateway.pvc_states[f"{spec.namespace}/{spec.name}"] = PvcInfo(
        name=spec.name, phase="Bound", capacity_gb=64
    )

    response = await client.post(f"/api/v1/storages/{storage_id}/reconcile")
    assert response.json()["capacityGb"] == 64


# ---------------------------------------------------------------------------
# Timeline and totals
# ---------------------------------------------------------------------------
async def test_the_timeline_records_creation_and_deletion(
    client: AsyncClient, world: World
) -> None:
    await act_as(client, "s5-dana")
    storage_id, _ = await make_storage(client, "timed")
    await client.delete(f"/api/v1/storages/{storage_id}")

    events = await client.get(f"/api/v1/storages/{storage_id}/events")
    assert events.status_code == 200
    kinds = [row["eventType"] for row in events.json()]
    assert "created" in kinds
    assert "deleted" in kinds
    assert all(row["actorUsername"] == "s5-dana" for row in events.json())


async def test_the_timeline_of_an_invisible_volume_is_not_readable(
    client: AsyncClient, world: World
) -> None:
    await act_as(client, "s5-dana")
    storage_id, _ = await make_storage(client, "secret")

    await act_as(client, "s5-gazer")
    assert (await client.get(f"/api/v1/storages/{storage_id}/events")).status_code == 404


async def test_usage_totals_count_only_active_volumes(client: AsyncClient, world: World) -> None:
    await act_as(client, "s5-dana")
    await make_storage(client, "u1", capacityGb=10)
    retired, _ = await make_storage(client, "u2", capacityGb=20)
    await client.delete(f"/api/v1/storages/{retired}")

    response = await client.get("/api/v1/storages/usage")
    assert response.status_code == 200
    assert response.json()["count"] == 1
    assert response.json()["capacityGb"] == 10


async def test_usage_does_not_charge_a_user_for_a_shared_dataset(
    client: AsyncClient, world: World
) -> None:
    """Seeing a volume and being charged for it are different things.

    A lab-wide dataset is visible to every member and owned by none of them. If
    usage counted visibility, each of fifty members would be told they were
    personally consuming its hundred gigabytes.
    """
    await act_as(client, "s5-boss")
    await make_storage(
        client, "lab-data", kind="dataset", capacityGb=100, source="https://example.edu/shared"
    )

    await act_as(client, "s5-dana")
    await make_storage(client, "mine", capacityGb=7)

    # Dana can see the dataset...
    listed = await client.get("/api/v1/storages")
    assert any(row["kind"] == "dataset" for row in listed.json()["items"])

    # ...but is only charged for her own volume.
    usage = await client.get("/api/v1/storages/usage")
    assert usage.json()["count"] == 1
    assert usage.json()["capacityGb"] == 7


async def test_a_global_admin_sees_the_installation_total(
    client: AsyncClient, world: World
) -> None:
    """A global administrator has no meaningful personal total.

    Reporting "0 GB" to the person responsible for the whole installation would
    be technically true and completely useless, so they get the site total.
    """
    await act_as(client, "s5-dana")
    await make_storage(client, "for-total", capacityGb=30)

    await act_as(client, "s5-root")
    usage = await client.get("/api/v1/storages/usage")
    assert usage.status_code == 200
    assert usage.json()["capacityGb"] == 30
