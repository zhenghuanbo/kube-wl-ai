"""The sweep must survive the session that produced it.

Every test here is the same failure seen from a different side. A sweep reads the
cluster correctly, writes the answer into a session, and then the session goes
away — and a session that is *closed* rolls back whatever was not committed. The
periodic sweep therefore observed everything and persisted nothing: instance
statuses stayed ``creating`` forever, ``pod_name`` stayed NULL (which is how you
can tell nobody ever looked), and the runtime cap in ``expire_overdue`` was never
enforced even once. Nothing raised, so nothing looked broken.

It stayed hidden because the HTTP path commits for itself: ``get_session`` commits
after the handler returns, so ``POST /instances/{id}/reconcile`` worked perfectly,
and every pre-existing reconciliation test went through that door.
``test_acceptance_phase3.py::test_reconcile_promotes_a_ready_pod_to_running`` is
exactly that test, and it passed the whole time.

So the assertions below read the row back through a **new** session. Reading it back
through the same one proves nothing at all: the identity map hands back the object
that was just modified whether or not a single byte reached the database.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.db.session import get_session_factory
from app.k8s import FakeK8sGateway, K8sError
from app.k8s.gateway import PodInfo, WorkloadSpec
from app.k8s.naming import MANAGED_LABEL, MANAGED_LABEL_SELECTOR, MANAGED_LABEL_VALUE
from app.models.enums import InstanceStatus
from app.models.instance import Instance
from app.services import instances as instance_service
from app.services import reconcile as reconcile_service
from app.workers import sweeper
from tests.test_acceptance_phase3 import World, act_as, build_world, make_instance, spawn_pod

LAB_A_NAMESPACE = "wl-lab-a-alpha"


@pytest.fixture
async def world(admin_client: httpx.AsyncClient, seeded: AsyncSession, settings: Settings) -> World:
    """Phase 3's world: a lab with a granted pool and a home volume for dana.

    Redeclared rather than shared, because ``world`` is a fixture *inside*
    ``test_acceptance_phase3.py`` and pytest does not export fixtures across
    modules. See ``tests/test_ide_proxy.py``, which does the same.
    """
    return await build_world(admin_client, seeded, settings)


def _deployment(gateway: FakeK8sGateway, namespace: str = LAB_A_NAMESPACE) -> str:
    """The deployment the world's instance produced."""
    return next(
        key.split("/", 1)[1] for key in gateway.deployments if key.startswith(f"{namespace}/")
    )


async def _instance_with_a_ready_pod(
    client: httpx.AsyncClient, world: World, gateway: FakeK8sGateway
) -> int:
    """Create an instance the way a user does, then bring its pod up.

    Creation through the API is deliberate: it is the only way the row gets the
    ``deployment`` reference a sweep needs, and it leaves the row in ``creating``,
    which is the state every assertion below is about.
    """
    await act_as(client, "dana")
    body = await make_instance(client, world)
    spawn_pod(gateway, namespace=LAB_A_NAMESPACE, deployment=_deployment(gateway))
    return int(body["id"])


# ---------------------------------------------------------------------------
# The sweep persists what it observed
# ---------------------------------------------------------------------------
async def test_a_sweep_persists_what_it_observed(
    client: httpx.AsyncClient,
    world: World,
    gateway: FakeK8sGateway,
    reload_from_db: Any,
) -> None:
    """After ``sweep_all`` returns, the answer is in the database.

    This is the production path — the Celery task and the in-process reconciler
    both call exactly this and nothing else — and it used to leave nothing behind.
    """
    instance_id = await _instance_with_a_ready_pod(client, world, gateway)

    counters = await reconcile_service.sweep_all(gateway)

    # Exactly one instance moved (creating -> running) and nothing expired. The
    # counters count *changes*, which is what lets every caller stay silent on an
    # uneventful sweep instead of printing the same line every interval forever.
    assert counters == {"instances": 1, "expired": 0, "tasks": 0}

    row = await reload_from_db(Instance, instance_id)
    assert row.status is InstanceStatus.RUNNING, "the sweep saw a Ready pod and kept it to itself"
    assert row.pod_name == f"{_deployment(gateway)}-abc12"
    assert row.node_name == "gpu-01"
    assert row.last_seen_at is not None, "last_seen_at is the only sign that anyone looked at all"


async def test_the_session_shape_that_caused_the_bug_now_persists(
    client: httpx.AsyncClient,
    world: World,
    gateway: FakeK8sGateway,
    reload_from_db: Any,
) -> None:
    """``async with factory() as session:`` — the exact shape that swallowed it.

    Nothing commits *after* the block exits, so if the commit is not inside
    ``reconcile_all`` then by the time the assertion runs the transaction has
    already been rolled back and the row still says ``creating``. This is the
    regression test for the original bug, and it fails on the old code.
    """
    instance_id = await _instance_with_a_ready_pod(client, world, gateway)

    factory = get_session_factory()
    async with factory() as session:
        await instance_service.reconcile_all(session, gateway)

    row = await reload_from_db(Instance, instance_id)
    assert row.status is InstanceStatus.RUNNING
    assert row.pod_name is not None


async def test_an_instance_past_its_cap_is_stopped_and_the_stop_persists(
    client: httpx.AsyncClient,
    world: World,
    gateway: FakeK8sGateway,
    session_factory: async_sessionmaker[AsyncSession],
    reload_from_db: Any,
) -> None:
    """The runtime cap had never once taken effect.

    Worse than a status that never advanced: the deployment really was scaled to
    zero in the cluster while the row still said ``running``, so the next sweep saw
    no pods, concluded the instance was still coming up, and the instance could
    neither stop nor expire.
    """
    instance_id = await _instance_with_a_ready_pod(client, world, gateway)
    deployment = f"{LAB_A_NAMESPACE}/{_deployment(gateway)}"

    async with session_factory() as session:
        row = await session.get(Instance, instance_id)
        assert row is not None
        row.status = InstanceStatus.RUNNING
        row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await session.commit()

    await reconcile_service.sweep_all(gateway)

    row = await reload_from_db(Instance, instance_id)
    assert row.status is InstanceStatus.STOPPED
    assert row.status_message == "已达最长运行时长，已自动停止"
    assert gateway.deployments[deployment].replicas == 0, "the cap was reported but not applied"


# ---------------------------------------------------------------------------
# One listing per namespace, not one per instance
# ---------------------------------------------------------------------------
def _deployments(gateway: FakeK8sGateway, namespace: str = LAB_A_NAMESPACE) -> list[str]:
    """Every Deployment the fake holds in ``namespace``."""
    return sorted(
        key.split("/", 1)[1] for key in gateway.deployments if key.startswith(f"{namespace}/")
    )


def _listing_calls(gateway: FakeK8sGateway) -> list[tuple[str, str]]:
    """Every ``list_pods`` argument pair the fake has seen."""
    return [(args[0], args[1]) for name, args in gateway.calls if name == "list_pods"]


async def test_a_namespace_is_listed_once_however_many_instances_it_holds(
    client: httpx.AsyncClient,
    world: World,
    gateway: FakeK8sGateway,
    reload_from_db: Any,
) -> None:
    """Two instances in one workspace cost **one** API server round trip, not two.

    This is the whole point of the change. The old sweep asked the cluster once per
    instance, so its cost grew with how much was running; asking once per namespace
    makes it grow with how many namespaces are in use instead.

    The status assertions are what stop this being a test of a call count. A sweep
    that listed once but handed the same pod to both rows — or to neither — would
    satisfy a bare "one call" assertion and be badly wrong, so both rows have to come
    back ``running`` each naming its *own* pod.
    """
    first_id = await _instance_with_a_ready_pod(client, world, gateway)
    first_deployment = _deployment(gateway)

    body = await make_instance(client, world, name="dev2")
    second_id = int(body["id"])
    second_deployment = next(name for name in _deployments(gateway) if name != first_deployment)
    spawn_pod(gateway, namespace=LAB_A_NAMESPACE, deployment=second_deployment)

    gateway.calls.clear()
    counters = await reconcile_service.sweep_all(gateway)

    assert _listing_calls(gateway) == [(LAB_A_NAMESPACE, MANAGED_LABEL_SELECTOR)], (
        "the sweep must ask for the namespace's managed pods, not for each instance's"
    )
    assert counters["instances"] == 2, "two instances moved, so two must be counted"

    for instance_id, deployment in ((first_id, first_deployment), (second_id, second_deployment)):
        row = await reload_from_db(Instance, instance_id)
        assert row.status is InstanceStatus.RUNNING, f"{deployment} was not observed"
        assert row.pod_name == f"{deployment}-abc12", f"{deployment} got another row's pod"


async def test_a_managed_pod_that_belongs_to_no_instance_is_ignored(
    client: httpx.AsyncClient,
    world: World,
    gateway: FakeK8sGateway,
    reload_from_db: Any,
) -> None:
    """Task pods live in the same namespace and carry the same managed label.

    A task's Job pods have ``wl-managed=true`` but no ``wl-instance``, because they
    belong to a row in another table. A listing wide enough to cover a namespace
    therefore returns them, and the sweep has to drop them rather than attribute
    them to whichever instance happens to be nearby — an instance with no pod of its
    own must stay ``creating`` even while the namespace is busy running tasks.
    """
    await act_as(client, "dana")
    body = await make_instance(client, world)
    instance_id = int(body["id"])

    # A managed workload that is not an instance: labels without `wl-instance`, and
    # a pod whose name resolves back to it.
    gateway.deployments[f"{LAB_A_NAMESPACE}/wl-task-7"] = WorkloadSpec(
        name="wl-task-7",
        namespace=LAB_A_NAMESPACE,
        image="registry.local/pytorch:latest",
        mount_path="/home/dana",
        labels={MANAGED_LABEL: MANAGED_LABEL_VALUE},
    )
    spawn_pod(gateway, namespace=LAB_A_NAMESPACE, deployment="wl-task-7")

    await reconcile_service.sweep_all(gateway)

    row = await reload_from_db(Instance, instance_id)
    assert row.pod_name is None, "the sweep gave a task's pod to an instance"
    assert row.status is InstanceStatus.CREATING
    assert row.status_message == "等待容器启动"


async def test_a_listing_that_fails_does_not_invent_a_status(
    client: httpx.AsyncClient,
    world: World,
    gateway: FakeK8sGateway,
    monkeypatch: pytest.MonkeyPatch,
    reload_from_db: Any,
) -> None:
    """A failed *query* is not an observation, and must not be recorded as one.

    The row was ``running`` a moment ago; a cluster the platform cannot reach is far
    weaker evidence that it stopped than the pod itself was that it started. So the
    status is left exactly as it was and only the message changes — and the message
    still has to be committed, or the page shows a stale reason forever.
    """
    instance_id = await _instance_with_a_ready_pod(client, world, gateway)
    await reconcile_service.sweep_all(gateway)
    assert (await reload_from_db(Instance, instance_id)).status is InstanceStatus.RUNNING

    def boom(namespace: str, label_selector: str) -> list[PodInfo]:
        raise K8sError("the API server went away")

    monkeypatch.setattr(gateway, "list_pods", boom)

    await reconcile_service.sweep_all(gateway)

    row = await reload_from_db(Instance, instance_id)
    assert row.status is InstanceStatus.RUNNING, "an unreachable cluster became an error status"
    assert row.status_message == "状态查询失败：the API server went away"


# ---------------------------------------------------------------------------
# The reconciler survives its own failures
# ---------------------------------------------------------------------------
async def test_a_failed_sweep_is_contained(
    gateway: FakeK8sGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cluster error returns nothing; it does not escape into the loop."""

    async def boom(_: FakeK8sGateway) -> dict[str, int]:
        raise K8sError("the API server went away")

    monkeypatch.setattr(sweeper, "sweep_all", boom)

    assert await sweeper.sweep_once(gateway) == {}


async def test_the_loop_keeps_ticking_after_a_failure(
    gateway: FakeK8sGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The loop is the only thing that would notice the next failure.

    So a tick that raises must not be the last one. Two failures in a row is the
    assertion, because a single one says nothing about what follows it.
    """
    ticks = 0

    async def boom(_: FakeK8sGateway) -> dict[str, int]:
        nonlocal ticks
        ticks += 1
        raise RuntimeError("transient")

    monkeypatch.setattr(sweeper, "sweep_all", boom)

    task = asyncio.create_task(sweeper.run_reconciler(0.01, gateway))
    try:
        for _ in range(500):
            if ticks >= 2:
                break
            await asyncio.sleep(0.01)
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    assert ticks >= 2, "one failed sweep ended the reconciler"


# ---------------------------------------------------------------------------
# The reconciler is opt-out, and the opt-outs are real
# ---------------------------------------------------------------------------
def test_the_reconciler_is_not_started_without_a_cluster(settings: Settings) -> None:
    """No cluster means nothing to observe.

    Sweeping against the fake would not merely be pointless: it would write
    invented statuses into real rows.
    """
    assert sweeper.start_reconciler(settings) is None


def test_the_reconciler_can_be_turned_off(settings: Settings) -> None:
    """``RECONCILE_INTERVAL_SECONDS=0`` leaves the Celery beat task as the sweeper."""
    off = settings.model_copy(update={"k8s_enabled": True, "reconcile_interval_seconds": 0.0})
    assert sweeper.start_reconciler(off) is None


async def test_the_reconciler_starts_when_asked(app: Any, settings: Settings) -> None:
    """The positive case.

    Without it the two tests above would still pass if the gate never opened at
    all — which is the failure mode that put us here: a mechanism nobody had ever
    seen run.
    """
    on = settings.model_copy(update={"k8s_enabled": True, "reconcile_interval_seconds": 30.0})

    task = sweeper.start_reconciler(on)

    assert task is not None, "a cluster plus a positive interval must start a reconciler"
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task
