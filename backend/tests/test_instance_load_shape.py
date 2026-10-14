"""Loading an instance must not walk the whole object graph.

``Instance`` eagerly joins four relationships, and each of those entities eagerly
loads collections of its own — a user's group links, role links, SSH keys, API tokens
and workspace memberships; a workspace's members and resource grants; a resource
group's specs and limits. Left alone the cascade is **72 statements for a single
row**, and it was paid by every sweep tick and by every request that lists or fetches
an instance (``GET /instances`` measured 170 ms against 1.1 ms for ``/healthz``).

The counters below are the regression guard. They are a ceiling rather than an exact
number on purpose: adding a column to the select must not fail here, while
reintroducing the cascade — which takes the count from 2 to 73 — must. Measured
ceilings on the narrowed code are 2 statements for a listing and 3 for a sweep.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from sqlalchemy import event, select
from sqlalchemy.exc import InvalidRequestError

from app.core.config import Settings
from app.db.session import get_engine, get_session_factory
from app.k8s import FakeK8sGateway
from app.models.instance import Instance
from app.models.user import User
from app.schemas.instance import InstanceDetail
from app.services import instances as instance_service
from tests.test_acceptance_phase3 import World, act_as, build_world, make_instance, spawn_pod

LAB_A_NAMESPACE = "wl-lab-a-alpha"

#: A listing costs 2 and a sweep 3 on the narrowed code; the cascade costs 72 and up.
MAX_STATEMENTS = 8


@pytest.fixture
async def world(admin_client: httpx.AsyncClient, seeded: Any, settings: Settings) -> World:
    """Phase 3's world; redeclared because pytest fixtures do not cross modules."""
    return await build_world(admin_client, seeded, settings)


@pytest.fixture
def statements() -> Any:
    """Collect the SQL the test emits, off the process-wide engine."""
    seen: list[str] = []

    def _record(conn: Any, cursor: Any, statement: str, *_: Any) -> None:
        seen.append(" ".join(statement.split())[:60])

    engine = get_engine()
    event.listen(engine.sync_engine, "before_cursor_execute", _record)
    try:
        yield seen
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", _record)


def _deployment(gateway: FakeK8sGateway, namespace: str = LAB_A_NAMESPACE) -> str:
    return next(
        key.split("/", 1)[1] for key in gateway.deployments if key.startswith(f"{namespace}/")
    )


async def _one_instance(client: httpx.AsyncClient, world: World) -> int:
    await act_as(client, "dana")
    return int((await make_instance(client, world))["id"])


# ---------------------------------------------------------------------------
# The cascade must stay out
# ---------------------------------------------------------------------------
async def test_listing_instances_does_not_walk_the_object_graph(
    client: httpx.AsyncClient, world: World, admin: User, statements: list[str]
) -> None:
    """``GET /instances`` loads rows, not the graph behind them."""
    await _one_instance(client, world)
    factory = get_session_factory()

    async with factory() as session:
        actor = await session.get(User, admin.id)
        assert actor is not None
        statements.clear()
        page = await instance_service.list_instances(session, actor, limit=20)

    assert page.total == 1
    assert len(statements) <= MAX_STATEMENTS, (
        f"listing one instance emitted {len(statements)} statements — "
        "the eager-load cascade is back. See _READ_LOAD in app/services/instances.py"
    )


async def test_a_sweep_does_not_walk_the_object_graph(
    client: httpx.AsyncClient, world: World, gateway: FakeK8sGateway, statements: list[str]
) -> None:
    """A periodic sweep runs every few seconds; it cannot afford the graph either."""
    await _one_instance(client, world)
    spawn_pod(gateway, namespace=LAB_A_NAMESPACE, deployment=_deployment(gateway))
    factory = get_session_factory()

    async with factory() as session:
        statements.clear()
        await instance_service.reconcile_all(session, gateway)

    assert len(statements) <= MAX_STATEMENTS, (
        f"one sweep emitted {len(statements)} statements — "
        "the eager-load cascade is back. See _SWEEP_LOAD in app/services/instances.py"
    )


# ---------------------------------------------------------------------------
# ...without refusing anything the serialiser actually reads
# ---------------------------------------------------------------------------
async def test_the_read_shape_still_carries_what_the_detail_page_shows(
    client: httpx.AsyncClient, world: World, admin: User
) -> None:
    """The three relationships the summary needs must survive the narrowing.

    ``InstanceDetail`` inherits ``InstanceSummary.from_instance``, so this covers the
    detail endpoint too. Over-narrowing is the failure mode this test exists for: the
    cascade tests above would happily pass on a query that loads nothing at all.
    """
    instance_id = await _one_instance(client, world)
    factory = get_session_factory()

    async with factory() as session:
        actor = await session.get(User, admin.id)
        assert actor is not None
        instance = await instance_service.get_instance(session, actor, instance_id)

    detail = InstanceDetail.from_instance(instance)
    assert detail.workspace_name == LAB_A_NAMESPACE
    assert detail.owner_username == "dana"
    assert detail.resource_group_name, "the pool's display name comes from resource_group"
    assert detail.status is not None


async def test_the_sweep_shape_refuses_an_attribute_it_never_declared(
    client: httpx.AsyncClient, world: World
) -> None:
    """Reading past the declaration fails loudly instead of returning ``None``.

    ``raiseload`` is what makes the narrowing safe to extend: a future caller that
    starts reading the owner inside a sweep gets an error naming the attribute, not a
    ``None`` that turns into a wrong answer three frames later.
    """
    instance_id = await _one_instance(client, world)

    async with get_session_factory()() as session:
        row = (
            await session.scalars(
                select(Instance)
                .where(Instance.id == instance_id)
                .options(*instance_service._SWEEP_LOAD)
            )
        ).one()

        assert row.workspace.k8s_namespace == LAB_A_NAMESPACE, "the one thing a sweep reads"
        with pytest.raises(InvalidRequestError):
            _ = row.owner
        with pytest.raises(InvalidRequestError):
            _ = row.resource_spec
