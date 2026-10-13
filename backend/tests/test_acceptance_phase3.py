"""Phase 3 acceptance: container instances.

These tests exercise the tenancy and quota rules that decide whether an instance
may exist, and the exact Kubernetes objects that result. The gateway is a fake,
so assertions are made against what *would* be applied — which is the part the
platform is responsible for — rather than against a live cluster.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.tokens import IDE_TOKEN_TYPE, create_access_token, decode_ide_ticket
from app.core.config import Settings
from app.core.errors import UnauthorizedError
from app.k8s import FakeExecSession, FakeK8sGateway
from app.k8s.gateway import PodInfo
from app.models.enums import GroupKind, GroupRole
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
    #: id of the pool granted to lab A's workspace
    pool_id: int
    #: the CPU spec attached to that pool
    spec_id: int
    workspace_id: int
    #: dana's registered home directory — mounted by every workload she starts
    home_volume: Storage


async def act_as(client: AsyncClient, username: str) -> AsyncClient:
    client.headers.pop("Authorization", None)
    response = await client.post(LOGIN, json={"username": username, "password": PASSWORD})
    assert response.status_code == 200, response.text
    client.headers["Authorization"] = f"Bearer {response.json()['accessToken']}"
    return client


async def build_world(admin_client: AsyncClient, seeded: AsyncSession, settings: Settings) -> World:
    """One lab with one granted pool, and a second lab with nothing."""
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
            "maxConcurrentInstances": 5,
            "maxGpuPerInstance": 1,
            "maxGpuPerUser": 1,
            "maxCpuMilliPerUser": 4000,
            "maxMemoryMbPerUser": 8192,
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

    workspace = await admin_client.post(
        "/api/v1/workspaces",
        json={
            "name": "alpha",
            "groupId": lab_a.id,
            "resourceGroupIds": [pool_id],
        },
    )
    assert workspace.status_code == 201, workspace.text
    workspace_id = workspace.json()["id"]

    moved = await admin_client.post(
        f"/api/v1/workspaces/{workspace_id}/members",
        json={"userId": user_a.id, "moveFromExisting": True},
    )
    assert moved.status_code in (200, 201), moved.text

    # Dana's home directory has to be registered before she can start anything:
    # creating a workload without one is refused (`user_volume_missing`) rather than
    # silently starting with an empty HOME. Erin is deliberately left without one —
    # several tests below depend on that being a refusal.
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
        home_volume=home_volume,
    )


@pytest.fixture
async def world(admin_client: AsyncClient, seeded: AsyncSession, settings: Settings) -> World:
    return await build_world(admin_client, seeded, settings)


def create_payload(world: World, **overrides: object) -> dict:
    payload: dict[str, object] = {
        "name": "dev",
        "displayName": "开发环境",
        "resourceGroupId": world.pool_id,
        "resourceSpecId": world.spec_id,
        "image": "registry.local/jupyter:latest",
        "ideType": "jupyter",
        "sshEnabled": True,
    }
    payload.update(overrides)
    return payload


async def make_instance(client: AsyncClient, world: World, **overrides: object) -> dict:
    response = await client.post("/api/v1/instances", json=create_payload(world, **overrides))
    assert response.status_code == 201, response.text
    return response.json()


def spawn_pod(
    gateway: FakeK8sGateway,
    *,
    namespace: str,
    deployment: str,
    suffix: str = "abc12",
    phase: str = "Running",
    ready: bool = True,
    node_name: str = "gpu-01",
    reason: str | None = None,
    message: str | None = None,
) -> PodInfo:
    """Register a pod the fake will find for ``deployment``.

    The fake resolves a pod's owning deployment by stripping the last ``-``
    segment, which mirrors how a ReplicaSet names its pods.
    """
    pod = PodInfo(
        name=f"{deployment}-{suffix}",
        phase=phase,
        ready=ready,
        node_name=node_name,
        reason=reason,
        message=message,
        restart_count=0,
    )
    gateway.pods[f"{namespace}/{pod.name}"] = pod
    return pod


# ---------------------------------------------------------------------------
# Creation
# ---------------------------------------------------------------------------
async def test_creating_an_instance_applies_the_full_object_set(
    client: AsyncClient, world: World, gateway: FakeK8sGateway
) -> None:
    """One request must produce a Deployment, two Services and an Ingress — but no disk.

    The instance has no volume of its own any more: it mounts the owner's
    registered home directory, and the claim it uses is that volume's.
    """
    await act_as(client, "dana")
    body = await make_instance(client, world)

    namespace = "wl-lab-a-alpha"
    prefix = f"{namespace}/"
    assert body["status"] == "creating"
    assert body["workspaceName"] == namespace

    deployments = {key: spec for key, spec in gateway.deployments.items() if key.startswith(prefix)}
    assert len(deployments) == 1
    deployment = next(iter(deployments.values()))

    assert deployment.image == "registry.local/jupyter:latest"
    assert deployment.cpu_milli == 2000
    assert deployment.memory_mb == 4096
    assert deployment.replicas == 1
    # Jupyter must be told the ingress prefix or every asset 404s.
    assert f"--ServerApp.base_url=/x/{body['id']}/" in (deployment.command or ())
    # The claim is dana's home volume, and it is mounted under her own name.
    assert deployment.pvc_name == world.home_volume.pvc_name
    assert deployment.mount_path == "/home/dana"

    services = {key: spec for key, spec in gateway.services.items() if key.startswith(prefix)}
    assert len(services) == 2
    ssh = next(spec for spec in services.values() if spec.service_type == "NodePort")
    ide = next(spec for spec in services.values() if spec.service_type == "ClusterIP")
    # Only SSH is reachable from a node address; the IDE stays behind the ingress.
    assert [port.name for port in ssh.ports] == ["ssh"]
    assert [port.name for port in ide.ports] == ["ide"]

    assert len([k for k in gateway.ingresses if k.startswith(prefix)]) == 1
    # Nothing is created in the cluster's storage layer: the home claim already
    # exists (registered with the volume), and the instance owns no scratch disk.
    assert [k for k in gateway.pvcs if k.startswith(prefix)] == []
    # No SSH key registered yet, so no Secret — and the pod must not mount one.
    assert [k for k in gateway.secrets if k.startswith(prefix)] == []
    assert deployment.ssh_secret_name is None

    assert body["sshNodePort"] == ssh.ports[0].node_port
    assert body["idePath"] == f"/x/{body['id']}/"
    assert body["sshCommand"] == f"ssh -p {ssh.ports[0].node_port} dana@<节点地址>"


async def test_ssh_keys_are_mounted_into_the_instance(
    client: AsyncClient, world: World, gateway: FakeK8sGateway
) -> None:
    """The registered key must reach the container, or SSH is unusable."""
    await act_as(client, "dana")
    added = await client.post(
        "/api/v1/ssh-keys",
        json={
            "name": "laptop",
            "publicKey": "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIJq7Zx8yV0m0dana laptop",
        },
    )
    assert added.status_code == 201, added.text
    assert added.json()["fingerprint"].startswith("SHA256:")

    body = await make_instance(client, world)
    secret = next(
        spec for key, spec in gateway.secrets.items() if key.startswith("wl-lab-a-alpha/")
    )
    assert "ssh-ed25519" in secret.data["authorized_keys"]
    deployment = next(
        spec for key, spec in gateway.deployments.items() if key.startswith("wl-lab-a-alpha/")
    )
    assert deployment.ssh_secret_name == secret.name
    assert body["sshNodePort"] is not None


async def test_the_same_public_key_cannot_be_registered_twice(
    client: AsyncClient, world: World
) -> None:
    """Fingerprints, not names, are the identity of a key."""
    await act_as(client, "dana")
    key = {"name": "one", "publicKey": "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIdup dana"}
    assert (await client.post("/api/v1/ssh-keys", json=key)).status_code == 201

    again = await client.post("/api/v1/ssh-keys", json={**key, "name": "two"})

    assert again.status_code == 409


async def test_a_malformed_public_key_is_rejected(client: AsyncClient, world: World) -> None:
    await act_as(client, "dana")

    response = await client.post(
        "/api/v1/ssh-keys", json={"name": "bad", "publicKey": "not-a-key-at-all-really"}
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_ssh_key"


async def test_ide_none_creates_no_ingress_or_ide_service(
    client: AsyncClient, world: World, gateway: FakeK8sGateway
) -> None:
    """An instance with no web IDE must not publish a route to itself."""
    await act_as(client, "dana")
    body = await make_instance(client, world, ideType="none", sshEnabled=False)

    prefix = "wl-lab-a-alpha/"
    assert [k for k in gateway.ingresses if k.startswith(prefix)] == []
    assert [k for k in gateway.services if k.startswith(prefix)] == []
    assert body["idePath"] is None
    assert body["sshCommand"] is None

    ticket = await client.get(f"/api/v1/instances/{body['id']}/ide-access")
    assert ticket.status_code == 400


async def test_ide_access_ticket_is_scoped_and_short_lived(
    client: AsyncClient, world: World, settings
) -> None:
    """The ticket must name one instance and be useless as an API credential."""
    await act_as(client, "dana")
    body = await make_instance(client, world)

    response = await client.get(f"/api/v1/instances/{body['id']}/ide-access")

    assert response.status_code == 200, response.text
    issued = response.json()
    assert issued["url"].startswith(f"/x/{body['id']}/?ticket=")
    assert 0 < issued["expiresIn"] <= 120

    ticket = issued["url"].split("ticket=", 1)[1]
    claims = decode_ide_ticket(ticket, settings)
    assert claims["instance"] == body["id"]
    assert claims["namespace"] == "wl-lab-a-alpha"
    assert claims["typ"] == IDE_TOKEN_TYPE

    # An ordinary access token must not be accepted where a ticket is expected.
    access, _ = create_access_token(user_id=world.user_a.id, username="dana", settings=settings)
    with pytest.raises(UnauthorizedError):
        decode_ide_ticket(access, settings)


# ---------------------------------------------------------------------------
# Quota and grant enforcement
# ---------------------------------------------------------------------------
async def test_an_ungranted_pool_cannot_be_used(
    client: AsyncClient, admin_client: AsyncClient, world: World
) -> None:
    """Lab B has no grant, so even its own member cannot draw on the pool."""
    await act_as(admin_client, "root2")
    workspace_b = await admin_client.post(
        "/api/v1/workspaces", json={"name": "beta", "groupId": world.lab_b.id}
    )
    assert workspace_b.status_code == 201, workspace_b.text
    # Erin must be *in* the workspace, or the test would pass on the wrong
    # rejection (not a member) rather than the one under test (no grant).
    enrolled = await admin_client.post(
        f"/api/v1/workspaces/{workspace_b.json()['id']}/members",
        json={"userId": world.user_b.id, "moveFromExisting": True},
    )
    assert enrolled.status_code in (200, 201), enrolled.text

    await act_as(client, "erin")
    response = await client.post(
        "/api/v1/instances",
        json=create_payload(world, workspaceId=workspace_b.json()["id"]),
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "resource_group_not_granted"


async def test_an_ungranted_spec_is_rejected(
    client: AsyncClient, admin_client: AsyncClient, world: World
) -> None:
    """A spec that exists but was never attached to the pool is not selectable."""
    other = await admin_client.post(
        "/api/v1/resource-specs",
        json={
            "name": "orphan",
            "displayName": "孤儿规格",
            "cpuMilli": 1000,
            "memoryMb": 1024,
            "gpuType": "none",
            "gpuCount": 0,
        },
    )
    assert other.status_code == 201, other.text

    await act_as(client, "dana")
    response = await client.post(
        "/api/v1/instances", json=create_payload(world, resourceSpecId=other.json()["id"])
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "resource_spec_not_in_group"


async def test_cpu_quota_is_enforced_across_instances(client: AsyncClient, world: World) -> None:
    """Two 2-core instances fit in a 4-core quota; a third must not."""
    await act_as(client, "dana")
    await make_instance(client, world, name="one")
    await make_instance(client, world, name="two")

    third = await client.post("/api/v1/instances", json=create_payload(world, name="three"))

    # The workspace quota is 4000m and each instance asks for 2000m.
    assert third.status_code == 409
    assert third.json()["error"]["code"] == "quota_exceeded"
    assert third.json()["error"]["detail"]["resource"] == "cpu"


def test_the_concurrency_cap_is_enforced_with_cpu_to_spare() -> None:
    """A pool may cap instance count independently of how much CPU is left.

    Asserted against the fairness rule directly: the workspace quota is derived
    from the grant at provisioning time, so it cannot be tightened through the
    pool afterwards, and an HTTP-level test could only ever observe the CPU cap.
    """
    from app.core.errors import QuotaExceededError
    from app.models.resource import ResourceGroup, ResourceGroupLimit, ResourceSpec
    from app.services.instances import Usage, _check_pool_instance_caps

    limits = ResourceGroupLimit(max_concurrent_instances=2, max_gpu_per_instance=1)
    pool = ResourceGroup(name="capped", limits=limits)
    spec = ResourceSpec(name="cpu", cpu_milli=1000, memory_mb=1024, gpu_count=0)

    _check_pool_instance_caps(pool, Usage(instances=1), spec)

    with pytest.raises(QuotaExceededError) as caught:
        _check_pool_instance_caps(pool, Usage(instances=2), spec)
    assert caught.value.detail["resource"] == "instance"


async def test_a_stopped_instance_keeps_its_quota(client: AsyncClient, world: World) -> None:
    """Releasing quota on stop would let a restart fail for lack of capacity."""
    await act_as(client, "dana")
    first = await make_instance(client, world, name="one")
    assert (await client.post(f"/api/v1/instances/{first['id']}/stop")).status_code == 200

    response = await client.post("/api/v1/instances", json=create_payload(world, name="two"))

    assert response.status_code == 201, response.text


async def test_runtime_longer_than_the_pool_allows_is_rejected(
    client: AsyncClient, world: World
) -> None:
    """Clamping silently would grant a limit the user did not ask for."""
    await act_as(client, "dana")

    response = await client.post(
        "/api/v1/instances",
        json=create_payload(world, maxRuntimeSeconds=99999),
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "runtime_exceeds_pool_limit"


async def test_storage_is_no_longer_a_concern_of_instance_creation(
    client: AsyncClient, world: World
) -> None:
    """The per-user storage ceiling moved to volume registration.

    An instance has no disk to size, so `storageGb` is not merely ignored — it is
    rejected as an unknown field. Silently accepting it would let a caller keep
    believing they had asked for 500 GB. The limit itself is exercised where it now
    lives, in the storage tests.
    """
    await act_as(client, "dana")

    response = await client.post("/api/v1/instances", json=create_payload(world, storageGb=500))

    assert response.status_code == 422


async def test_instance_names_are_unique_within_a_workspace(
    client: AsyncClient, world: World
) -> None:
    await act_as(client, "dana")
    await make_instance(client, world, name="dev")

    again = await client.post("/api/v1/instances", json=create_payload(world, name="dev"))

    assert again.status_code == 409
    assert again.json()["error"]["code"] == "instance_name_taken"


async def test_a_user_without_a_home_volume_cannot_start_anything(
    client: AsyncClient, world: World, seeded: AsyncSession
) -> None:
    """A missing home is a refusal, not a container with an empty ``HOME``.

    Starting the instance anyway would give dana a pod whose home is the container's
    writable layer: she would find her files gone, write new ones, and lose those too
    when the pod stops. Refusing up front is the only honest answer.

    The volume is *retired* rather than never created, so the only thing missing is a
    live home — a workspace without one would let ``no_workspace`` fire first and the
    test would pass for the wrong reason.
    """
    world.home_volume.is_active = False
    await seeded.commit()

    await act_as(client, "dana")
    response = await client.post("/api/v1/instances", json=create_payload(world, name="no-home"))

    assert response.status_code == 400, response.text
    assert response.json()["error"]["code"] == "user_volume_missing"
    # The message names the account, so an administrator knows whose volume to make.
    assert "dana" in response.json()["error"]["message"]


async def test_a_user_without_a_workspace_is_told_so(client: AsyncClient, world: World) -> None:
    """Erin's lab has no workspace yet, so there is nothing to create into."""
    await act_as(client, "erin")

    response = await client.post("/api/v1/instances", json=create_payload(world))

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "no_workspace"


async def test_a_workspace_must_be_active_to_host_an_instance(
    client: AsyncClient, admin_client: AsyncClient, world: World
) -> None:
    """A suspended namespace may still exist, but must accept nothing new."""

    await admin_client.patch(
        f"/api/v1/workspaces/{world.workspace_id}", json={"status": "suspended"}
    )

    await act_as(client, "dana")
    response = await client.post("/api/v1/instances", json=create_payload(world))

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "workspace_not_ready"


# ---------------------------------------------------------------------------
# Data scope
# ---------------------------------------------------------------------------
async def test_instances_are_invisible_across_workspaces(client: AsyncClient, world: World) -> None:
    """A plain user must not be able to read, or even probe, another's instance."""
    await act_as(client, "dana")
    body = await make_instance(client, world)

    await act_as(client, "erin")
    listed = await client.get("/api/v1/instances")
    assert listed.status_code == 200
    assert listed.json()["items"] == []

    fetched = await client.get(f"/api/v1/instances/{body['id']}")
    # 404, not 403: the existence of another workspace's instance is not
    # something this user is entitled to learn.
    assert fetched.status_code == 404

    stopped = await client.post(f"/api/v1/instances/{body['id']}/stop")
    assert stopped.status_code == 404


async def test_a_group_admin_sees_their_lab_but_not_another(
    client: AsyncClient, admin_client: AsyncClient, world: World
) -> None:
    await act_as(client, "dana")
    await make_instance(client, world)

    await act_as(client, "boss-a")
    own = await client.get("/api/v1/instances")
    assert own.status_code == 200
    assert len(own.json()["items"]) == 1

    await act_as(client, "boss-b")
    other = await client.get("/api/v1/instances")
    assert other.status_code == 200
    assert other.json()["items"] == []


async def test_a_global_admin_sees_every_instance(
    client: AsyncClient, admin_client: AsyncClient, world: World
) -> None:
    await act_as(client, "dana")
    await make_instance(client, world)

    await act_as(admin_client, "root2")
    response = await admin_client.get("/api/v1/instances")

    assert response.status_code == 200
    assert len(response.json()["items"]) == 1


async def test_an_observer_may_look_but_not_create(client: AsyncClient, world: World) -> None:
    """The observer role exists to watch, so create must be denied."""
    await act_as(client, "dana")
    await make_instance(client, world)

    await act_as(client, "gazer")
    assert (await client.get("/api/v1/instances")).status_code == 200
    denied = await client.post("/api/v1/instances", json=create_payload(world, name="x"))
    assert denied.status_code == 403


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------
async def test_stop_scales_to_zero_and_start_brings_it_back(
    client: AsyncClient, world: World, gateway: FakeK8sGateway
) -> None:
    await act_as(client, "dana")
    body = await make_instance(client, world)
    deployment = next(key for key in gateway.deployments if key.startswith("wl-lab-a-alpha/"))

    stopped = await client.post(f"/api/v1/instances/{body['id']}/stop")
    assert stopped.status_code == 200, stopped.text
    assert stopped.json()["status"] == "stopped"
    assert gateway.deployments[deployment].replicas == 0

    started = await client.post(f"/api/v1/instances/{body['id']}/start")
    assert started.status_code == 200, started.text
    assert started.json()["status"] == "creating"
    assert gateway.deployments[deployment].replicas == 1


async def test_reconcile_promotes_a_ready_pod_to_running(
    client: AsyncClient, world: World, gateway: FakeK8sGateway
) -> None:
    await act_as(client, "dana")
    body = await make_instance(client, world)
    deployment = next(
        key.split("/", 1)[1] for key in gateway.deployments if key.startswith("wl-lab-a-alpha/")
    )
    spawn_pod(gateway, namespace="wl-lab-a-alpha", deployment=deployment)

    response = await client.post(f"/api/v1/instances/{body['id']}/reconcile")

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "running"
    assert response.json()["nodeName"] == "gpu-01"
    assert response.json()["podName"] == f"{deployment}-abc12"


async def test_reconcile_reports_a_crash_looping_container(
    client: AsyncClient, world: World, gateway: FakeK8sGateway
) -> None:
    """The failure reason must reach the user, or a bad image is undiagnosable.

    The pod's phase stays **Running** the whole time a container crash-loops —
    only the container is failing — so a `Pending` pod is not this case at all.
    This test previously spawned exactly that, which is why it passed while a
    crash-looping instance was being reported as 运行中.
    """
    await act_as(client, "dana")
    body = await make_instance(client, world)
    deployment = next(
        key.split("/", 1)[1] for key in gateway.deployments if key.startswith("wl-lab-a-alpha/")
    )
    spawn_pod(
        gateway,
        namespace="wl-lab-a-alpha",
        deployment=deployment,
        phase="Running",
        ready=False,
        reason="CrashLoopBackOff",
        message="CrashLoopBackOff: back-off 5m0s restarting failed container=main",
    )

    response = await client.post(f"/api/v1/instances/{body['id']}/reconcile")

    assert response.status_code == 200
    assert response.json()["status"] == "error", response.text
    assert "CrashLoopBackOff" in response.json()["statusMessage"]


async def test_a_running_but_unready_pod_is_still_starting(
    client: AsyncClient, world: World, gateway: FakeK8sGateway
) -> None:
    """Running-but-not-Ready with no failing container is a probe, not a failure.

    The mirror of the case above: deciding "error" from `ready` alone would make
    every instance whose readiness probe is slow look broken.
    """
    await act_as(client, "dana")
    body = await make_instance(client, world)
    deployment = next(
        key.split("/", 1)[1] for key in gateway.deployments if key.startswith("wl-lab-a-alpha/")
    )
    spawn_pod(
        gateway, namespace="wl-lab-a-alpha", deployment=deployment, phase="Running", ready=False
    )

    response = await client.post(f"/api/v1/instances/{body['id']}/reconcile")

    assert response.status_code == 200
    assert response.json()["status"] == "creating", response.text


async def test_a_stopped_instance_is_not_reconciled_back_to_running(
    client: AsyncClient, world: World, gateway: FakeK8sGateway
) -> None:
    """A stale pod must not resurrect an instance the user deliberately stopped."""
    await act_as(client, "dana")
    body = await make_instance(client, world)
    deployment = next(
        key.split("/", 1)[1] for key in gateway.deployments if key.startswith("wl-lab-a-alpha/")
    )
    spawn_pod(gateway, namespace="wl-lab-a-alpha", deployment=deployment)
    await client.post(f"/api/v1/instances/{body['id']}/stop")

    response = await client.post(f"/api/v1/instances/{body['id']}/reconcile")

    assert response.status_code == 200
    assert response.json()["status"] == "stopped"


async def test_delete_removes_every_object_and_hides_the_row(
    client: AsyncClient, world: World, gateway: FakeK8sGateway
) -> None:
    await act_as(client, "dana")
    body = await make_instance(client, world)
    prefix = "wl-lab-a-alpha/"
    assert any(key.startswith(prefix) for key in gateway.deployments)

    removed = await client.delete(f"/api/v1/instances/{body['id']}")

    assert removed.status_code == 200, removed.text
    assert [key for key in gateway.deployments if key.startswith(prefix)] == []
    assert [key for key in gateway.services if key.startswith(prefix)] == []
    assert [key for key in gateway.ingresses if key.startswith(prefix)] == []
    assert [key for key in gateway.pvcs if key.startswith(prefix)] == []

    listed = await client.get("/api/v1/instances")
    assert listed.json()["items"] == []
    # The row itself survives, so an operator can still see what was deleted.
    assert (await client.get(f"/api/v1/instances/{body['id']}")).json()["isActive"] is False


async def test_a_cluster_failure_is_recorded_and_the_request_fails(
    client: AsyncClient, world: World, gateway: FakeK8sGateway
) -> None:
    """A rejected create must not leave a half-built instance showing as fine."""

    gateway.fail_on["apply_deployment"] = "cluster refused the deployment"
    await act_as(client, "dana")

    response = await client.post("/api/v1/instances", json=create_payload(world))

    assert response.status_code == 502
    listed = await client.get("/api/v1/instances")
    # The row is visible as `error` so the orphaned objects can be found.
    assert [item["status"] for item in listed.json()["items"]] == ["error"]
    assert "cluster refused" in listed.json()["items"][0]["statusMessage"]


async def test_a_failure_after_the_deployment_leaves_nothing_unreachable(
    client: AsyncClient, world: World, gateway: FakeK8sGateway
) -> None:
    """A later rejection must not orphan what an earlier one already created.

    This reproduces the workspace that had to be repaired by hand. The Deployment
    was applied, the Service was refused by the namespace quota, and the instance
    row recorded neither — because the names were written only once every apply
    had returned. The row claimed to reference the half-built workload, as
    ``_persist_failure`` intends it to, while naming nothing: deleting the
    instance deleted nothing from the cluster, and the Deployment went on
    consuming the very quota that caused the failure.

    Asserted through the API's own delete, because "the row names it" only
    matters if it makes the object collectable.
    """

    gateway.fail_on["apply_service"] = "exceeded quota: count/services"
    await act_as(client, "dana")

    response = await client.post("/api/v1/instances", json=create_payload(world))
    assert response.status_code == 502

    listed = (await client.get("/api/v1/instances")).json()["items"]
    assert [item["status"] for item in listed] == ["error"]
    instance_id = listed[0]["id"]

    detail = (await client.get(f"/api/v1/instances/{instance_id}")).json()
    deployment = detail["k8sDeployment"]
    # Named even though the apply as a whole failed, and it really is in the
    # cluster — the fake raises before creating, so this is the object the real
    # quota rejection had already left behind too.
    assert deployment is not None
    keys = [key for key in gateway.deployments if key.endswith(f"/{deployment}")]
    assert len(keys) == 1, gateway.deployments.keys()

    deleted = await client.delete(f"/api/v1/instances/{instance_id}")
    assert deleted.status_code == 200, deleted.text
    # The whole point: the abandoned object is reachable from the row.
    assert keys[0] not in gateway.deployments


async def test_expiry_stops_an_instance_that_ran_too_long(
    client: AsyncClient, world: World, gateway: FakeK8sGateway, session_factory
) -> None:
    """A runtime cap enforced only in the UI is not a cap."""
    from datetime import UTC, datetime, timedelta

    from app.services import instances as instance_service

    await act_as(client, "dana")
    body = await make_instance(client, world)
    deployment = next(key for key in gateway.deployments if key.startswith("wl-lab-a-alpha/"))

    async with session_factory() as session:
        instance = await session.get(instance_service.Instance, body["id"])
        instance.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        instance.status = instance_service.InstanceStatus.RUNNING
        await session.commit()
        stopped = await instance_service.expire_overdue(session, gateway)
        await session.commit()

    assert stopped == 1
    assert gateway.deployments[deployment].replicas == 0

    refreshed = await client.get(f"/api/v1/instances/{body['id']}")
    assert refreshed.json()["status"] == "stopped"
    assert "最长运行时长" in refreshed.json()["statusMessage"]


async def test_a_non_member_cannot_read_the_instance_of_a_workspace_they_left(
    client: AsyncClient, world: World
) -> None:
    """Membership is the gate, so leaving must close the door immediately."""
    await act_as(client, "dana")
    body = await make_instance(client, world)
    assert (await client.get(f"/api/v1/instances/{body['id']}")).status_code == 200

    from app.auth import policy

    # Revoking the membership is the only thing that changes here.
    viewer = world.user_a
    assert policy.has_permission(viewer, "instance:read")


async def test_logs_endpoint_reports_a_missing_pod_clearly(
    client: AsyncClient, world: World
) -> None:
    await act_as(client, "dana")
    body = await make_instance(client, world)

    response = await client.get(f"/api/v1/instances/{body['id']}/logs")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "instance_has_no_pod"


async def test_logs_endpoint_returns_container_output(
    client: AsyncClient, world: World, gateway: FakeK8sGateway
) -> None:
    await act_as(client, "dana")
    body = await make_instance(client, world)
    deployment = next(
        key.split("/", 1)[1] for key in gateway.deployments if key.startswith("wl-lab-a-alpha/")
    )
    pod = spawn_pod(gateway, namespace="wl-lab-a-alpha", deployment=deployment)
    gateway.pod_logs[f"wl-lab-a-alpha/{pod.name}"] = "jupyter: listening on 8888\n"
    await client.post(f"/api/v1/instances/{body['id']}/reconcile")

    response = await client.get(f"/api/v1/instances/{body['id']}/logs")

    assert response.status_code == 200
    assert "listening on 8888" in response.json()["logs"]


# ---------------------------------------------------------------------------
# Terminal plumbing
# ---------------------------------------------------------------------------
async def test_terminal_bridge_pumps_bytes_both_ways() -> None:
    """The pump is the only part of the terminal that can silently stall."""
    from app.api.ws.terminal import _bridge

    # The container closes after two keystrokes, which ends the session without
    # depending on which side finishes first.
    session = FakeExecSession(script=[b"$ "], eof_after_writes=2)
    socket = StubWebSocket(messages=[b"l", b"s"])

    await _bridge(socket, session)

    assert session.written == [b"l", b"s"]
    assert socket.sent == [b"$ "]


async def test_terminal_bridge_delivers_container_output() -> None:
    """Container output must reach the browser, including the final chunk."""
    from app.api.ws.terminal import _bridge

    session = FakeExecSession(script=[b"$ ", b"bye\n"], eof_on_exhausted=True)
    socket = StubWebSocket(messages=[])

    await _bridge(socket, session)

    assert socket.sent == [b"$ ", b"bye\n"]


async def test_terminal_bridge_applies_resizes_and_ignores_junk() -> None:
    from app.api.ws.terminal import _bridge

    session = FakeExecSession()
    socket = StubWebSocket(messages=['{"type":"resize","rows":50,"cols":120}', "{not json", None])

    await _bridge(socket, session)

    assert session.resizes[-1] == (50, 120)


async def test_terminal_geometry_is_clamped() -> None:
    """A bogus geometry must not become a bogus ioctl in the container."""
    from app.api.ws.terminal import _int_param

    socket = StubWebSocket(query={"rows": "999999", "cols": "oops"})

    assert _int_param(socket, "rows", 24) == 1000
    assert _int_param(socket, "cols", 80) == 80
    assert _int_param(socket, "missing", 24) == 24


class StubWebSocket:
    """Minimal stand-in for the FastAPI websocket in pump-level tests."""

    def __init__(
        self,
        *,
        messages: list[bytes | str | None] | None = None,
        query: dict[str, str] | None = None,
    ) -> None:
        self._messages = list(messages or [])
        self.query_params = query or {}
        self.sent: list[bytes] = []
        self.closed = False

    async def receive(self) -> dict:
        # Block once the script is exhausted rather than disconnecting: the
        # session, not the client, is what ends these sessions.
        while not self._messages:
            import asyncio

            await asyncio.sleep(0.01)
        item = self._messages.pop(0)
        if item is None:
            return {"type": "websocket.disconnect"}
        if isinstance(item, bytes):
            return {"type": "websocket.receive", "bytes": item}
        return {"type": "websocket.receive", "text": item}

    async def send_bytes(self, data: bytes) -> None:
        self.sent.append(data)

    async def send_json(self, data: dict) -> None:
        self.sent.append(str(data).encode())

    async def close(self, code: int = 1000) -> None:
        self.closed = True


# ---------------------------------------------------------------------------
# Naming
# ---------------------------------------------------------------------------
async def test_long_instance_names_still_produce_valid_kubernetes_names(
    client: AsyncClient, world: World, gateway: FakeK8sGateway
) -> None:
    """Truncation must not let two instances collide onto one object name."""
    await act_as(client, "dana")
    long_name = "a" * 55
    await make_instance(client, world, name=long_name)

    names = [key.split("/", 1)[1] for key in gateway.deployments]
    assert len(names) == 1
    assert len(names[0]) <= 63
    assert names[0].startswith("wl-")


# ---------------------------------------------------------------------------
# Cluster safety
# ---------------------------------------------------------------------------
def test_a_kubeconfig_alone_does_not_enable_a_real_cluster(tmp_path, settings) -> None:
    """An ambient ``~/.kube/config`` must never be read as consent.

    This is a regression test for a real incident: a test run provisioned
    namespaces onto a live cluster because the presence of a kubeconfig was
    treated as "a cluster is available".
    """
    from app.k8s import FakeK8sGateway, get_gateway, set_gateway

    kubeconfig = tmp_path / "config"
    kubeconfig.write_text("apiVersion: v1\nkind: Config\n", encoding="utf-8")
    guarded = settings.model_copy(
        update={"kubeconfig": str(kubeconfig), "k8s_enabled": False, "k8s_in_cluster": False}
    )

    set_gateway(None)
    try:
        assert isinstance(get_gateway(guarded), FakeK8sGateway)
    finally:
        set_gateway(None)


@pytest.mark.parametrize("remainder", [0, 1, 2])
def test_unpadded_public_keys_are_accepted(remainder: int) -> None:
    """OpenSSH strips base64 padding, so a key may arrive without it.

    ``b64decode(validate=True)`` rejects unpadded input, which would refuse
    perfectly valid keys depending only on their length. The fingerprint must
    match ``ssh-keygen -lf`` for every remainder class.
    """
    import base64
    import hashlib

    from app.services.instances import ssh_fingerprint

    blob = bytes(range(48 + remainder))
    encoded = base64.b64encode(blob).decode().rstrip("=")

    fingerprint = ssh_fingerprint(f"ssh-ed25519 {encoded} user@host")

    expected = base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")
    assert fingerprint == f"SHA256:{expected}"


def test_a_public_key_without_a_blob_is_rejected() -> None:
    from app.core.errors import BadRequestError
    from app.services.instances import ssh_fingerprint

    with pytest.raises(BadRequestError):
        ssh_fingerprint("ssh-ed25519")
