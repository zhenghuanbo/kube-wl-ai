"""Phase 4 acceptance: the application catalogue and custom tasks.

Two features with different shapes share one file because they share a
boundary: an application is a *shape* for an instance, a task is a *one-shot*
version of one, and both must respect the same workspace, quota and visibility
rules. Testing them apart would leave that boundary untested.

The gateway is a fake, so assertions are about what *would* be submitted — the
part the platform is responsible for — not about a live cluster.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.k8s import FakeK8sGateway
from app.k8s.gateway import JobInfo
from app.models.enums import GroupKind, GroupRole, TaskStatus
from app.models.group import Group
from app.models.storage import Storage
from app.models.user import User
from app.models.workspace import Workspace
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
    pool_id: int
    spec_id: int
    workspace_id: int
    #: id of a resource spec that exists but was never attached to the pool
    orphan_spec_id: int
    #: dana's registered home directory — mounted by every workload she starts
    home_volume: Storage


async def act_as(client: AsyncClient, username: str) -> AsyncClient:
    client.headers.pop("Authorization", None)
    response = await client.post(LOGIN, json={"username": username, "password": PASSWORD})
    assert response.status_code == 200, response.text
    client.headers["Authorization"] = f"Bearer {response.json()['accessToken']}"
    return client


async def build_world(admin_client: AsyncClient, seeded: AsyncSession, settings: Settings) -> World:
    lab_a = await factories.create_group(seeded, name="lab-a", kind=GroupKind.LAB)
    lab_b = await factories.create_group(seeded, name="lab-b", kind=GroupKind.LAB)

    global_admin = await factories.create_user(
        seeded, username="root2", password=PASSWORD, role="global_admin"
    )
    admin_a = await factories.create_user(
        seeded,
        username="boss-a",
        password=PASSWORD,
        role="group_admin",
        group=lab_a,
        group_role=GroupRole.GROUP_ADMIN,
    )
    admin_b = await factories.create_user(
        seeded,
        username="boss-b",
        password=PASSWORD,
        role="group_admin",
        group=lab_b,
        group_role=GroupRole.GROUP_ADMIN,
    )
    user_a = await factories.create_user(
        seeded, username="dana", password=PASSWORD, role="user", group=lab_a
    )
    user_b = await factories.create_user(
        seeded, username="erin", password=PASSWORD, role="user", group=lab_b
    )
    observer = await factories.create_user(
        seeded, username="gazer", password=PASSWORD, role="observer", group=lab_a
    )
    await seeded.commit()

    pool = await admin_client.post(
        "/api/v1/resource-groups",
        json={
            "name": "gpu-pool",
            "displayName": "GPU 池",
            "kind": "public",
            "networkType": "internal",
            "maxRuntimeSeconds": 3600,
        },
    )
    assert pool.status_code == 201, pool.text
    pool_id = pool.json()["id"]

    limits = await admin_client.put(
        f"/api/v1/resource-groups/{pool_id}/limits",
        json={
            "maxGpuPerUser": 1,
            "maxCpuCoresPerUser": 4,
            "maxMemoryGbPerUser": 8,
            "maxStorageGbPerUser": 20,
        },
    )
    assert limits.status_code == 200, limits.text

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
    attached = await admin_client.put(f"/api/v1/resource-groups/{pool_id}/specs", json=[spec_id])
    assert attached.status_code == 200, attached.text

    orphan = await admin_client.post(
        "/api/v1/resource-specs",
        json={
            "name": "orphan",
            "displayName": "未挂载规格",
            "cpuMilli": 1000,
            "memoryMb": 1024,
            "gpuType": "none",
            "gpuCount": 0,
        },
    )
    assert orphan.status_code == 201, orphan.text

    workspace = await admin_client.post(
        "/api/v1/workspaces",
        json={"name": "alpha", "groupId": lab_a.id, "resourceGroupIds": [pool_id]},
    )
    assert workspace.status_code == 201, workspace.text
    workspace_id = workspace.json()["id"]

    moved = await admin_client.post(
        f"/api/v1/workspaces/{workspace_id}/members",
        json={"userId": user_a.id, "moveFromExisting": True},
    )
    assert moved.status_code in (200, 201), moved.text

    inactive = await admin_client.post(
        "/api/v1/workspaces",
        json={"name": "beta", "groupId": lab_a.id, "resourceGroupIds": []},
    )
    assert inactive.status_code == 201, inactive.text
    suspended = await admin_client.patch(
        f"/api/v1/workspaces/{inactive.json()['id']}", json={"status": "suspended"}
    )
    assert suspended.status_code == 200, suspended.text

    # Every workload mounts its owner's home directory, and starting one without it
    # is refused (`user_volume_missing`). Dana needs hers registered before any test
    # below can create an instance or a task; Erin deliberately has none.
    home_volume = await factories.create_user_volume(
        seeded, user_a, settings=settings, workspace=await seeded.get(Workspace, workspace_id)
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
        pool_id=pool_id,
        spec_id=spec_id,
        workspace_id=workspace_id,
        orphan_spec_id=orphan.json()["id"],
        home_volume=home_volume,
    )


@pytest.fixture
async def world(admin_client: AsyncClient, seeded: AsyncSession, settings: Settings) -> World:
    return await build_world(admin_client, seeded, settings)


# ---------------------------------------------------------------------------
# Payload helpers
# ---------------------------------------------------------------------------
def app_payload(world: World, **overrides: object) -> dict:
    payload: dict[str, object] = {
        "name": "pytorch",
        "displayName": "PyTorch 2.4",
        "summary": "开箱即用的 PyTorch 环境",
        "readme": "# PyTorch\n\n直接开始训练。",
        "category": "data_science",
        "image": "registry.local/pytorch:2.4",
        "ideType": "jupyter",
        "resourceGroupId": world.pool_id,
        "resourceSpecId": world.spec_id,
        "defaultCpuMilli": 2000,
        "defaultMemoryMb": 4096,
        "visibility": "workspace",
    }
    payload.update(overrides)
    return payload


async def make_application(client: AsyncClient, world: World, **overrides: object) -> dict:
    response = await client.post("/api/v1/applications", json=app_payload(world, **overrides))
    assert response.status_code == 201, response.text
    return response.json()


def task_payload(world: World, **overrides: object) -> dict:
    payload: dict[str, object] = {
        "name": "train",
        "displayName": "训练任务",
        "resourceGroupId": world.pool_id,
        "resourceSpecId": world.spec_id,
        "image": "registry.local/pytorch:2.4",
        "script": "python train.py --epochs 3",
    }
    payload.update(overrides)
    return payload


async def make_task(client: AsyncClient, world: World, **overrides: object) -> dict:
    response = await client.post("/api/v1/tasks", json=task_payload(world, **overrides))
    assert response.status_code == 201, response.text
    return response.json()


def job_name(gateway: FakeK8sGateway) -> str:
    assert len(gateway.jobs) == 1, gateway.jobs
    return next(iter(gateway.jobs))


# ---------------------------------------------------------------------------
# Applications — catalogue and visibility
# ---------------------------------------------------------------------------
async def test_creating_an_application_records_the_launch_defaults(
    client: AsyncClient, world: World
) -> None:
    await act_as(client, "dana")
    app = await make_application(client, world)

    assert app["name"] == "pytorch"
    assert app["image"] == "registry.local/pytorch:2.4"
    assert app["defaultCpuMilli"] == 2000
    assert app["visibility"] == "workspace"
    assert app["isCurated"] is False
    assert app["launchCount"] == 0
    assert app["workspaceId"] == world.workspace_id


async def test_an_application_name_is_unique_within_the_group(
    client: AsyncClient, world: World
) -> None:
    """Two labs may both publish "pytorch"; one lab may not publish it twice."""
    await act_as(client, "dana")
    await make_application(client, world)

    duplicate = await client.post("/api/v1/applications", json=app_payload(world))

    assert duplicate.status_code == 409
    assert duplicate.json()["error"]["code"] == "application_name_taken"


async def test_a_workspace_visible_application_is_hidden_from_another_lab(
    client: AsyncClient, world: World
) -> None:
    await act_as(client, "dana")
    await make_application(client, world)

    await act_as(client, "erin")
    listing = await client.get("/api/v1/applications")

    assert listing.status_code == 200
    assert listing.json()["items"] == []


async def test_a_group_visible_application_reaches_the_whole_lab(
    client: AsyncClient, world: World
) -> None:
    await act_as(client, "dana")
    await make_application(client, world, visibility="group")

    await act_as(client, "boss-a")
    listing = await client.get("/api/v1/applications")

    assert [item["name"] for item in listing.json()["items"]] == ["pytorch"]


async def test_a_global_application_reaches_every_lab(client: AsyncClient, world: World) -> None:
    await act_as(client, "dana")
    await make_application(client, world, visibility="global")

    await act_as(client, "erin")
    listing = await client.get("/api/v1/applications")

    assert [item["name"] for item in listing.json()["items"]] == ["pytorch"]


async def test_an_out_of_visibility_application_is_a_404_not_a_403(
    client: AsyncClient, world: World
) -> None:
    """A 403 would confirm that another lab's private application exists."""
    await act_as(client, "dana")
    app = await make_application(client, world, visibility="private")

    await act_as(client, "erin")
    response = await client.get(f"/api/v1/applications/{app['id']}")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "application_not_found"


async def test_an_observer_can_read_but_not_create(client: AsyncClient, world: World) -> None:
    await act_as(client, "dana")
    await make_application(client, world, visibility="global")

    await act_as(client, "gazer")
    assert (await client.get("/api/v1/applications")).status_code == 200
    denied = await client.post("/api/v1/applications", json=app_payload(world, name="other"))

    assert denied.status_code == 403


async def test_a_colleague_may_see_but_not_edit_someone_elses_application(
    client: AsyncClient, world: World, admin_client: AsyncClient, seeded: AsyncSession
) -> None:
    """Sharing a definition is not the same as handing over the right to change it."""
    peer = await factories.create_user(
        seeded, username="peer", password=PASSWORD, role="user", group=world.lab_a
    )
    await seeded.commit()
    joined = await admin_client.post(
        f"/api/v1/workspaces/{world.workspace_id}/members", json={"userId": peer.id}
    )
    assert joined.status_code in (200, 201), joined.text

    await act_as(client, "dana")
    app = await make_application(client, world, visibility="workspace")

    # A peer in the same workspace can find it...
    await act_as(client, "peer")
    assert (await client.get(f"/api/v1/applications/{app['id']}")).status_code == 200
    # ...but not rewrite it.
    denied = await client.patch(f"/api/v1/applications/{app['id']}", json={"displayName": "改过了"})
    assert denied.status_code == 404

    # The author can.
    await act_as(client, "dana")
    allowed = await client.patch(
        f"/api/v1/applications/{app['id']}", json={"displayName": "PyTorch 2.5"}
    )
    assert allowed.status_code == 200
    assert allowed.json()["displayName"] == "PyTorch 2.5"


async def test_a_spec_that_is_not_in_the_pool_cannot_be_a_default(
    client: AsyncClient, world: World
) -> None:
    """An application that cannot be launched is worse than one that will not save."""
    await act_as(client, "dana")
    response = await client.post(
        "/api/v1/applications",
        json=app_payload(world, name="broken", resourceSpecId=world.orphan_spec_id),
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "resource_spec_not_in_group"


async def test_a_half_specified_resource_default_is_rejected(
    client: AsyncClient, world: World
) -> None:
    await act_as(client, "dana")
    response = await client.post(
        "/api/v1/applications",
        json=app_payload(world, name="half", resourceSpecId=None),
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "incomplete_resource_defaults"


async def test_retiring_an_application_keeps_the_row(client: AsyncClient, world: World) -> None:
    await act_as(client, "dana")
    app = await make_application(client, world)

    removed = await client.delete(f"/api/v1/applications/{app['id']}")

    assert removed.status_code == 200
    assert removed.json()["isActive"] is False
    listing = await client.get("/api/v1/applications")
    assert listing.json()["items"] == []


# ---------------------------------------------------------------------------
# Applications — launching
# ---------------------------------------------------------------------------
async def test_launching_an_application_creates_a_real_instance(
    client: AsyncClient, world: World, gateway: FakeK8sGateway
) -> None:
    await act_as(client, "dana")
    app = await make_application(client, world)

    launched = await client.post(
        f"/api/v1/applications/{app['id']}/launch", json={"name": "my-dev"}
    )

    assert launched.status_code == 201, launched.text
    instance = launched.json()
    assert instance["name"] == "my-dev"
    assert instance["image"] == "registry.local/pytorch:2.4"
    assert instance["ideType"] == "jupyter"
    assert instance["cpuMilli"] == 2000
    assert instance["workspaceId"] == world.workspace_id
    # The cluster objects are the ordinary instance set, not something new.
    assert instance["k8sDeployment"]
    assert instance["sshNodePort"]


async def test_launching_increments_the_counter_only_on_success(
    client: AsyncClient, world: World
) -> None:
    await act_as(client, "dana")
    app = await make_application(client, world)

    await client.post(f"/api/v1/applications/{app['id']}/launch", json={"name": "one"})
    after = await client.get(f"/api/v1/applications/{app['id']}")
    assert after.json()["launchCount"] == 1

    # A name clash fails, so the figure must not move.
    await client.post(f"/api/v1/applications/{app['id']}/launch", json={"name": "one"})
    again = await client.get(f"/api/v1/applications/{app['id']}")
    assert again.json()["launchCount"] == 1


async def test_launching_an_application_without_defaults_asks_for_a_choice(
    client: AsyncClient, world: World
) -> None:
    await act_as(client, "dana")
    app = await make_application(
        client, world, name="bare", resourceGroupId=None, resourceSpecId=None
    )

    response = await client.post(f"/api/v1/applications/{app['id']}/launch", json={})

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "resource_choice_required"


async def test_launching_an_application_into_another_lab_workspace_is_refused(
    client: AsyncClient, world: World, gateway: FakeK8sGateway
) -> None:
    """Visibility is about reading the catalogue, not about where work may run."""
    await act_as(client, "dana")
    app = await make_application(client, world, visibility="global")

    await act_as(client, "erin")
    other = await client.get(f"/api/v1/applications/{app['id']}")
    assert other.status_code == 200

    # Erin has no workspace of her own, so she must name one — and she names one
    # belonging to lab A, which she may not use.
    before = len(gateway.deployments)
    response = await client.post(
        f"/api/v1/applications/{app['id']}/launch",
        json={"workspaceId": world.workspace_id},
    )

    assert response.status_code in (403, 404)
    assert len(gateway.deployments) == before


async def test_launching_a_retired_application_is_refused(
    client: AsyncClient, world: World
) -> None:
    await act_as(client, "dana")
    app = await make_application(client, world)
    await client.delete(f"/api/v1/applications/{app['id']}")

    # Still visible to its author, who may want to re-enable it.
    response = await client.post(f"/api/v1/applications/{app['id']}/launch", json={})

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "application_inactive"


# ---------------------------------------------------------------------------
# Tasks — submission
# ---------------------------------------------------------------------------
async def test_a_task_mounts_the_owners_home_and_any_opted_in_shares(
    client: AsyncClient, world: World, gateway: FakeK8sGateway, admin_client: AsyncClient
) -> None:
    """A task's mounts are the home plus exactly what it asked for — nothing else.

    The home is mounted at ``/home/{username}`` and named in ``HOME``; a share is
    *not* mounted unless the submitter names it. A task that silently saw a
    colleague's shared directory would be a surprise rather than a convenience.

    A *global* share is used rather than a group share because this world's lab has
    two workspaces, and a group share is refused when its group's workspace is
    ambiguous (``group_has_multiple_workspaces``) — a rule about where the claim
    lives, unrelated to what is being tested here.
    """
    await act_as(client, "root2")
    share = await admin_client.post(
        "/api/v1/storages",
        json={
            "name": "task-share",
            "displayName": "任务共享",
            "kind": "global_share",
            "capacityGb": 10,
        },
    )
    assert share.status_code == 201, share.text
    share_id = share.json()["id"]

    # Without a choice: only the home.
    await act_as(client, "dana")
    await make_task(client, world, name="bare")
    spec = gateway.jobs[job_name(gateway)]
    assert spec.mount_path == "/home/dana"
    assert spec.pvc_name == world.home_volume.pvc_name
    assert spec.env["HOME"] == "/home/dana"
    assert spec.extra_mounts == ()

    gateway.jobs.clear()
    await make_task(client, world, name="mounted", storageIds=[share_id])

    spec = gateway.jobs[job_name(gateway)]
    assert spec.mount_path == "/home/dana"
    assert len(spec.extra_mounts) == 1
    assert spec.extra_mounts[0].mount_path == "/mnt/global_shared"
    assert spec.extra_mounts[0].pvc_name is not None
    assert spec.extra_mounts[0].read_only is False


async def test_submitting_a_task_creates_a_job_with_the_users_shell(
    client: AsyncClient, world: World, gateway: FakeK8sGateway
) -> None:
    """The script is the argument to a shell the user chose, with no interpolation."""
    await act_as(client, "dana")
    task = await make_task(client, world)

    assert task["status"] == "running"
    spec = gateway.jobs[job_name(gateway)]
    assert spec.command == ("/bin/sh", "-lc")
    assert spec.args == ("python train.py --epochs 3",)
    assert spec.backoff_limit == 0
    assert spec.labels["wl-task"] == "train"
    assert spec.env["WL_TASK_ID"] == str(task["id"])


async def test_a_task_job_name_cannot_collide_with_an_instance_deployment(
    client: AsyncClient, world: World, gateway: FakeK8sGateway
) -> None:
    await act_as(client, "dana")
    task = await make_task(client, world, name="run")

    assert task["k8sJobName"].startswith("wl-job-")
    assert task["k8sJobName"] not in gateway.deployments


async def test_a_task_is_pinned_by_its_pools_selector(
    client: AsyncClient, world: World, admin_client: AsyncClient, gateway: FakeK8sGateway
) -> None:
    """Scheduling follows the pool, exactly as an instance does.

    Two constraints travel together and they are not interchangeable. ``wl-pool``
    is the partition — written by the node sync, owned by the platform, and the
    reason a workload cannot land on another pool's hardware. The pool's own
    selector is an *extra* hardware filter on top of it. Note the second
    assertion: a pool cannot use its selector to point somewhere other than
    itself, because a partition with an escape hatch is not a partition.
    """
    updated = await admin_client.patch(
        f"/api/v1/resource-groups/{world.pool_id}",
        json={"k8sLabelSelector": {"gpu-type": "a100", "wl-pool": "somewhere-else"}},
    )
    assert updated.status_code == 200, updated.text

    await act_as(client, "dana")
    await make_task(client, world)

    assert gateway.jobs[job_name(gateway)].node_selector == {
        "gpu-type": "a100",
        "wl-pool": "gpu-pool",
    }


async def test_the_runtime_cap_is_enforced_by_kubernetes(
    client: AsyncClient, world: World, gateway: FakeK8sGateway
) -> None:
    """A platform-side timer would not survive the backend being down."""
    await act_as(client, "dana")
    task = await make_task(client, world, maxRuntimeSeconds=600)

    assert task["maxRuntimeSeconds"] == 600
    assert gateway.jobs[job_name(gateway)].active_deadline_seconds == 600


async def test_a_runtime_beyond_the_pool_ceiling_is_rejected(
    client: AsyncClient, world: World
) -> None:
    await act_as(client, "dana")
    response = await client.post("/api/v1/tasks", json=task_payload(world, maxRuntimeSeconds=7200))

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "runtime_exceeds_pool_limit"


async def test_a_task_may_not_use_a_pool_its_workspace_lacks(
    client: AsyncClient, world: World
) -> None:
    await act_as(client, "dana")
    response = await client.post(
        "/api/v1/tasks", json=task_payload(world, name="bad", resourceGroupId=world.pool_id + 999)
    )

    assert response.status_code == 404


async def test_a_task_may_not_use_a_spec_outside_its_pool(
    client: AsyncClient, world: World
) -> None:
    await act_as(client, "dana")
    response = await client.post(
        "/api/v1/tasks",
        json=task_payload(world, name="bad-spec", resourceSpecId=world.orphan_spec_id),
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "resource_spec_not_in_group"


async def test_a_task_name_is_unique_within_the_workspace(
    client: AsyncClient, world: World
) -> None:
    await act_as(client, "dana")
    await make_task(client, world)

    duplicate = await client.post("/api/v1/tasks", json=task_payload(world))

    assert duplicate.status_code == 409
    assert duplicate.json()["error"]["code"] == "task_name_taken"


async def test_a_script_beyond_the_size_cap_is_refused(client: AsyncClient, world: World) -> None:
    await act_as(client, "dana")
    response = await client.post(
        "/api/v1/tasks", json=task_payload(world, script="x" * (64 * 1024 + 1))
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "script_too_large"


async def test_a_task_consumes_the_same_workspace_quota_as_an_instance(
    client: AsyncClient, world: World
) -> None:
    """The namespace ResourceQuota cannot tell the two apart, so neither may we."""
    await act_as(client, "dana")
    # 4000m ceiling; two instances already use all of it.
    for name in ("one", "two"):
        response = await client.post(
            "/api/v1/instances",
            json={
                "name": name,
                "resourceGroupId": world.pool_id,
                "resourceSpecId": world.spec_id,
                "image": "registry.local/base:1",
            },
        )
        assert response.status_code == 201, response.text

    blocked = await client.post("/api/v1/tasks", json=task_payload(world, name="late"))

    assert blocked.status_code == 409
    assert blocked.json()["error"]["detail"]["resource"] == "cpu"


async def test_stopping_an_instance_frees_room_for_a_task(
    client: AsyncClient, world: World, gateway: FakeK8sGateway
) -> None:
    """Compute freed by a stop is available to *any* workload, because it is one allowance.

    Kubernetes counts a Pod's requests regardless of what owns it, and a stopped
    instance's Deployment holds no Pod at all — so the room it gives back is real room,
    and a task may take it. The second task below then shows the ceiling is still
    enforced on the new occupancy rather than having been opened permanently.
    """
    await act_as(client, "dana")
    for name in ("one", "two"):
        created = await client.post(
            "/api/v1/instances",
            json={
                "name": name,
                "resourceGroupId": world.pool_id,
                "resourceSpecId": world.spec_id,
                "image": "registry.local/base:1",
            },
        )
        assert created.status_code == 201, created.text
        if name == "one":
            instance_id = created.json()["id"]

    assert (await client.post(f"/api/v1/instances/{instance_id}/stop")).status_code == 200

    now_fits = await client.post("/api/v1/tasks", json=task_payload(world, name="late"))

    assert now_fits.status_code == 201, now_fits.text

    # The freed 2 cores are gone again, so the next task is refused on CPU.
    blocked = await client.post("/api/v1/tasks", json=task_payload(world, name="later"))

    assert blocked.status_code == 409, blocked.text
    assert blocked.json()["error"]["detail"]["resource"] == "cpu"


async def test_a_task_cannot_run_in_a_suspended_workspace(
    client: AsyncClient, world: World, admin_client: AsyncClient
) -> None:
    other = await admin_client.post(
        "/api/v1/workspaces", json={"name": "gamma", "groupId": world.lab_a.id}
    )
    assert other.status_code == 201, other.text
    workspace_id = other.json()["id"]
    assert (
        await admin_client.patch(f"/api/v1/workspaces/{workspace_id}", json={"status": "suspended"})
    ).status_code == 200
    moved = await admin_client.post(
        f"/api/v1/workspaces/{workspace_id}/members",
        json={"userId": world.user_a.id, "moveFromExisting": True},
    )
    assert moved.status_code in (200, 201), moved.text

    await act_as(client, "dana")
    response = await client.post("/api/v1/tasks", json=task_payload(world, name="nowhere"))

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "workspace_not_ready"


# ---------------------------------------------------------------------------
# Tasks — reconciliation
# ---------------------------------------------------------------------------
async def test_a_task_promotes_to_succeeded_and_records_its_exit_code(
    client: AsyncClient, world: World, gateway: FakeK8sGateway
) -> None:
    await act_as(client, "dana")
    task = await make_task(client, world)
    key = job_name(gateway)
    gateway.job_states[key] = JobInfo(
        name=task["k8sJobName"], state="Complete", succeeded=1, exit_code=0
    )

    response = await client.post(f"/api/v1/tasks/{task['id']}/reconcile")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "succeeded"
    assert body["exitCode"] == 0
    assert body["finishedAt"]
    assert body["durationSeconds"] is not None


async def test_a_failed_task_reports_its_exit_code(
    client: AsyncClient, world: World, gateway: FakeK8sGateway
) -> None:
    await act_as(client, "dana")
    task = await make_task(client, world)
    gateway.job_states[job_name(gateway)] = JobInfo(
        name=task["k8sJobName"],
        state="Failed",
        failed=1,
        exit_code=2,
        message="BackoffLimitExceeded",
    )

    body = (await client.post(f"/api/v1/tasks/{task['id']}/reconcile")).json()

    assert body["status"] == "failed"
    assert body["exitCode"] == 2
    assert "2" in body["statusMessage"]


async def test_a_task_killed_for_exceeding_its_deadline_is_a_timeout(
    client: AsyncClient, world: World, gateway: FakeK8sGateway
) -> None:
    """A timeout is a different problem from a non-zero exit, so it reads differently."""
    await act_as(client, "dana")
    task = await make_task(client, world, maxRuntimeSeconds=600)
    gateway.job_states[job_name(gateway)] = JobInfo(
        name=task["k8sJobName"], state="Failed", failed=1, deadline_exceeded=True
    )

    body = (await client.post(f"/api/v1/tasks/{task['id']}/reconcile")).json()

    assert body["status"] == "timeout"
    assert "600" in body["statusMessage"]


async def test_a_finished_task_releases_its_quota(
    client: AsyncClient, world: World, gateway: FakeK8sGateway
) -> None:
    """Unlike a stopped instance: a finished job will never consume anything again."""
    await act_as(client, "dana")
    first = await make_task(client, world, name="one")
    gateway.job_states[job_name(gateway)] = JobInfo(
        name=first["k8sJobName"], state="Complete", succeeded=1
    )
    await client.post(f"/api/v1/tasks/{first['id']}/reconcile")

    # 4000m ceiling, 2000m per task: the second would fail if the first held on.
    second = await client.post("/api/v1/tasks", json=task_payload(world, name="two"))

    assert second.status_code == 201, second.text


async def test_a_terminal_task_is_not_reconciled_again(
    client: AsyncClient, world: World, gateway: FakeK8sGateway
) -> None:
    await act_as(client, "dana")
    task = await make_task(client, world)
    gateway.job_states[job_name(gateway)] = JobInfo(
        name=task["k8sJobName"], state="Complete", succeeded=1
    )
    await client.post(f"/api/v1/tasks/{task['id']}/reconcile")

    # The job vanishes afterwards, which must not reopen a settled task.
    gateway.jobs.clear()
    gateway.job_states.clear()
    body = (await client.post(f"/api/v1/tasks/{task['id']}/reconcile")).json()

    assert body["status"] == "succeeded"


async def test_a_vanished_job_that_never_succeeded_is_a_failure(
    client: AsyncClient, world: World, gateway: FakeK8sGateway
) -> None:
    """TTL expiry is not success. Reporting otherwise would credit work never done."""
    await act_as(client, "dana")
    task = await make_task(client, world, ttlSecondsAfterFinished=60)

    gateway.jobs.clear()
    body = (await client.post(f"/api/v1/tasks/{task['id']}/reconcile")).json()

    assert body["status"] == "failed"
    assert "不存在" in body["statusMessage"]


# ---------------------------------------------------------------------------
# Tasks — cancel, delete, logs, timeline
# ---------------------------------------------------------------------------
async def test_cancelling_a_task_deletes_the_job_and_keeps_the_row(
    client: AsyncClient, world: World, gateway: FakeK8sGateway
) -> None:
    await act_as(client, "dana")
    task = await make_task(client, world)

    cancelled = await client.post(f"/api/v1/tasks/{task['id']}/cancel")

    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"
    assert gateway.jobs == {}
    still_there = await client.get(f"/api/v1/tasks/{task['id']}")
    assert still_there.status_code == 200


async def test_a_finished_task_cannot_be_cancelled(
    client: AsyncClient, world: World, gateway: FakeK8sGateway
) -> None:
    await act_as(client, "dana")
    task = await make_task(client, world)
    gateway.job_states[job_name(gateway)] = JobInfo(
        name=task["k8sJobName"], state="Complete", succeeded=1
    )
    await client.post(f"/api/v1/tasks/{task['id']}/reconcile")

    response = await client.post(f"/api/v1/tasks/{task['id']}/cancel")

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "task_already_finished"


async def test_deleting_a_task_leaves_the_users_storage_alone(
    client: AsyncClient, world: World, gateway: FakeK8sGateway
) -> None:
    """A task has no scratch volume of its own any more — and must not take the home.

    This is the sharp edge of the change: the task mounts the owner's registered
    home claim, so a delete that reclaimed "the task's volume" would destroy the
    user's data. The PV is ``Retain``, but a deleted claim leaves it ``Released``
    and unrecoverable.
    """
    await act_as(client, "dana")
    task = await make_task(client, world)

    # Nothing is created in the storage layer when a task starts: the home claim
    # already exists, and the task asks for no shares.
    assert gateway.pvcs == {}, "a task must not create a claim"

    response = await client.delete(f"/api/v1/tasks/{task['id']}")

    assert response.status_code == 204
    # No claim was deleted, and in particular the home claim is untouched.
    assert world.home_volume.pvc_name is not None
    assert gateway.jobs == {}
    assert (await client.get(f"/api/v1/tasks/{task['id']}")).json()["isActive"] is False


async def test_task_logs_come_from_the_pod(
    client: AsyncClient, world: World, gateway: FakeK8sGateway
) -> None:
    await act_as(client, "dana")
    task = await make_task(client, world)
    key = job_name(gateway)
    gateway.job_states[key] = JobInfo(
        name=task["k8sJobName"], state="Running", active=1, pod_name="wl-job-train-1-abcde"
    )
    await client.post(f"/api/v1/tasks/{task['id']}/reconcile")
    gateway.pod_logs[f"{task['workspaceName']}/wl-job-train-1-abcde"] = "epoch 1\nepoch 2\n"

    response = await client.get(f"/api/v1/tasks/{task['id']}/logs")

    assert response.status_code == 200
    assert "epoch 2" in response.json()["logs"]


async def test_logs_of_a_task_with_no_pod_say_so_plainly(
    client: AsyncClient, world: World, gateway: FakeK8sGateway
) -> None:
    """A 400 naming the reason beats an empty 200 that looks like an empty run.

    ``task_no_pod`` is a distinct code so the UI can say "no container yet"
    rather than showing a blank console, which is indistinguishable from a run
    that produced no output.
    """
    await act_as(client, "dana")
    task = await make_task(client, world)
    gateway.job_states[job_name(gateway)] = JobInfo(
        name=task["k8sJobName"], state="Complete", succeeded=1
    )
    await client.post(f"/api/v1/tasks/{task['id']}/reconcile")

    response = await client.get(f"/api/v1/tasks/{task['id']}/logs")

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "task_no_pod"


async def test_the_task_timeline_records_the_lifecycle(
    client: AsyncClient, world: World, gateway: FakeK8sGateway
) -> None:
    await act_as(client, "dana")
    task = await make_task(client, world)
    gateway.job_states[job_name(gateway)] = JobInfo(
        name=task["k8sJobName"], state="Complete", succeeded=1
    )
    await client.post(f"/api/v1/tasks/{task['id']}/reconcile")

    events = (await client.get(f"/api/v1/tasks/{task['id']}/events")).json()

    kinds = [event["eventType"] for event in events]
    assert "created" in kinds
    assert "status_changed" in kinds
    # Newest first, and the actor is recorded for the human-initiated ones.
    assert events[0]["toStatus"] == "succeeded"
    assert any(event["actorUsername"] == "dana" for event in events)


async def test_the_application_timeline_records_creation_and_retirement(
    client: AsyncClient, world: World
) -> None:
    await act_as(client, "dana")
    app = await make_application(client, world)
    await client.delete(f"/api/v1/applications/{app['id']}")

    events = (await client.get(f"/api/v1/applications/{app['id']}/events")).json()

    assert [event["eventType"] for event in events] == ["deleted", "created"]


# ---------------------------------------------------------------------------
# Tenancy
# ---------------------------------------------------------------------------
async def test_a_task_in_another_lab_workspace_is_invisible(
    client: AsyncClient, world: World
) -> None:
    await act_as(client, "dana")
    task = await make_task(client, world)

    await act_as(client, "erin")
    assert (await client.get("/api/v1/tasks")).json()["items"] == []
    assert (await client.get(f"/api/v1/tasks/{task['id']}")).status_code == 404


async def test_a_colleague_in_the_same_workspace_sees_the_task(
    client: AsyncClient, world: World, admin_client: AsyncClient
) -> None:
    await act_as(client, "dana")
    await make_task(client, world)

    await act_as(client, "boss-a")
    listing = await client.get("/api/v1/tasks")

    assert [item["name"] for item in listing.json()["items"]] == ["train"]


async def test_an_observer_cannot_submit_a_task(client: AsyncClient, world: World) -> None:
    await act_as(client, "gazer")
    response = await client.post("/api/v1/tasks", json=task_payload(world))

    assert response.status_code == 403


async def test_a_normal_user_cannot_submit_for_someone_in_another_workspace(
    client: AsyncClient, world: World
) -> None:
    await act_as(client, "dana")
    response = await client.post(
        "/api/v1/tasks", json=task_payload(world, ownerUserId=world.user_b.id)
    )

    assert response.status_code in (400, 403, 404)
    assert response.json()["error"]["code"] in (
        "owner_not_in_workspace",
        "unknown_user",
        "owner_required",
    )


async def test_the_task_list_filters_by_status_and_search(
    client: AsyncClient, world: World, gateway: FakeK8sGateway
) -> None:
    await act_as(client, "dana")
    first = await make_task(client, world, name="alpha")
    await make_task(client, world, name="beta")
    gateway.job_states[f"{first['workspaceName']}/{first['k8sJobName']}"] = JobInfo(
        name=first["k8sJobName"], state="Complete", succeeded=1
    )
    await client.post(f"/api/v1/tasks/{first['id']}/reconcile")

    succeeded = await client.get("/api/v1/tasks", params={"status": "succeeded"})
    assert [item["name"] for item in succeeded.json()["items"]] == ["alpha"]

    searched = await client.get("/api/v1/tasks", params={"search": "bet"})
    assert [item["name"] for item in searched.json()["items"]] == ["beta"]


async def test_a_group_admin_may_edit_a_members_application(
    client: AsyncClient, world: World
) -> None:
    await act_as(client, "dana")
    app = await make_application(client, world, visibility="group")

    await act_as(client, "boss-a")
    response = await client.patch(
        f"/api/v1/applications/{app['id']}", json={"summary": "由管理员补充说明"}
    )

    assert response.status_code == 200
    assert response.json()["summary"] == "由管理员补充说明"


async def test_a_cluster_failure_leaves_an_error_row(
    client: AsyncClient, world: World, gateway: FakeK8sGateway
) -> None:
    """The row is the only record that a half-submitted job exists."""
    from app.k8s import K8sError

    def explode(spec: object) -> None:
        raise K8sError("模拟的下发失败")

    gateway.apply_job = explode  # type: ignore[method-assign]

    await act_as(client, "dana")
    response = await client.post("/api/v1/tasks", json=task_payload(world))

    assert response.status_code == 502
    listing = await client.get("/api/v1/tasks", params={"includeInactive": True})
    rows = [item for item in listing.json()["items"] if item["name"] == "train"]
    assert rows and rows[0]["status"] == "error"


async def test_task_status_enum_covers_every_terminal_state() -> None:
    """A terminal status the quota rule does not know about would leak quota."""
    from app.models.enums import TERMINAL_TASK_STATUSES

    assert TaskStatus.SUCCEEDED in TERMINAL_TASK_STATUSES
    assert TaskStatus.FAILED in TERMINAL_TASK_STATUSES
    assert TaskStatus.TIMEOUT in TERMINAL_TASK_STATUSES
    assert TaskStatus.CANCELLED in TERMINAL_TASK_STATUSES
    assert TaskStatus.RUNNING not in TERMINAL_TASK_STATUSES
    assert TaskStatus.PENDING not in TERMINAL_TASK_STATUSES


# ---------------------------------------------------------------------------
# Wire-format consistency
# ---------------------------------------------------------------------------
def test_every_multi_word_query_parameter_is_camel_case() -> None:
    """Query parameters must be camelCase, like every request body.

    This is not cosmetic. FastAPI matches query parameters by name and ignores
    anything it does not recognise, so a parameter declared as
    ``include_inactive`` while the frontend sends ``includeInactive`` does not
    error — the filter is simply dropped. That happened for a whole phase, and
    the only symptom was a list that quietly returned too much.

    Single-word parameters (``page``, ``search``, ``status``) have nothing to
    camel-case, and the auth endpoints use OAuth's fixed names.
    """
    from app.main import create_app

    app = create_app(_settings_for_schema())
    schema = app.openapi()

    offenders: list[str] = []
    allowed = {
        # OAuth/OIDC fixed spellings and pre-existing deliberate exceptions.
        "grant_type",
        "client_id",
        "code",
        "state",
    }
    for path, operations in schema["paths"].items():
        for method, operation in operations.items():
            if method not in {"get", "post", "put", "patch", "delete"}:
                continue
            for parameter in operation.get("parameters", []):
                if parameter.get("in") != "query":
                    continue
                name = parameter["name"]
                if "_" in name and name not in allowed:
                    offenders.append(f"{method.upper()} {path}: {name}")

    assert offenders == [], f"snake_case query parameters: {offenders}"


def _settings_for_schema() -> object:
    """Settings good enough to build the schema, without touching a database."""
    from app.core.config import Settings

    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        app_env="development",
        secret_key="schema-build-key-with-enough-entropy-0123",  # type: ignore[arg-type]
        database_url="sqlite+aiosqlite:///:memory:",
        auth_providers=["local"],
    )
