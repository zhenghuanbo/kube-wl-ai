"""Phase 6 acceptance: the usage centre.

The centre of gravity here is the accounting itself, because an accounting bug is
invisible: a number that is 8% too low still renders, still charts, and is only
discovered when somebody reconciles a purchase against it a year later. So most
of these tests pin exact integers on hand-built intervals rather than trusting a
total to "look about right".

The second theme is the data scope. A usage report is an unusually tempting
thing to get wrong, because every endpoint returns a *sum* — and a sum that leaks
is not a row you can point at, it is a number that quietly includes another lab.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import (
    GroupRole,
    IdeType,
    InstanceStatus,
    TaskStatus,
    UsageKind,
    WorkspaceStatus,
)
from app.models.group import Group
from app.models.instance import Instance
from app.models.task import Task
from app.models.usage import UsageDaily
from app.models.user import User
from app.models.workspace import Workspace, WorkspaceMember
from app.services import usage as usage_service
from tests import factories

PASSWORD = "phase6-password"
SUMMARY = "/api/v1/usage/summary"
SERIES = "/api/v1/usage/series"
BREAKDOWN = "/api/v1/usage/breakdown"
EXPORT = "/api/v1/usage/export"
ROLLUP = "/api/v1/usage/rollup"

#: A fixed "now" so nothing depends on when the suite runs. Midday, so a
#: workload started hours earlier is unambiguously inside the same day.
NOW = datetime(2026, 3, 10, 12, 0, tzinfo=UTC)


async def act_as(client: AsyncClient, username: str) -> AsyncClient:
    client.headers.pop("Authorization", None)
    response = await client.post(
        "/api/v1/auth/login", json={"username": username, "password": PASSWORD}
    )
    assert response.status_code == 200, response.text
    client.headers["Authorization"] = f"Bearer {response.json()['accessToken']}"
    return client


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
class World:
    lab_a: Group
    lab_b: Group
    user_a: User
    user_b: User
    admin_a: User
    observer: User
    workspace_a: int
    workspace_b: int


async def build_world(session: AsyncSession) -> World:
    """Two labs, two people each, one workspace apiece. No HTTP — this phase is
    about the fact table, and driving everything through the API would make the
    tests slow enough that the interval cases would be cut."""
    lab_a = await factories.create_group(session, name="lab-a", display_name="自然语言处理")
    lab_b = await factories.create_group(session, name="lab-b", display_name="计算机视觉")

    user_a = await factories.create_user(
        session, username="dana", password=PASSWORD, role="user", group=lab_a, full_name="戴娜"
    )
    user_b = await factories.create_user(
        session, username="erin", password=PASSWORD, role="user", group=lab_b, full_name="艾琳"
    )
    admin_a = await factories.create_user(
        session,
        username="boss-a",
        password=PASSWORD,
        role="group_admin",
        group=lab_a,
        group_role=GroupRole.GROUP_ADMIN,
        full_name="A 组管理员",
    )
    observer = await factories.create_user(
        session, username="gazer", password=PASSWORD, role="observer", group=lab_a
    )
    await factories.create_user(session, username="root2", password=PASSWORD, role="global_admin")

    workspace_a = Workspace(
        name="alpha",
        display_name="A 组工作空间",
        group_id=lab_a.id,
        k8s_namespace="wl-lab-a-alpha",
        status=WorkspaceStatus.ACTIVE,
    )
    workspace_b = Workspace(
        name="beta",
        display_name="B 组工作空间",
        group_id=lab_b.id,
        k8s_namespace="wl-lab-b-beta",
        status=WorkspaceStatus.ACTIVE,
    )
    session.add_all([workspace_a, workspace_b])
    await session.flush()

    for user, workspace in ((user_a, workspace_a), (admin_a, workspace_a), (user_b, workspace_b)):
        session.add(WorkspaceMember(workspace_id=workspace.id, user_id=user.id))
    await session.flush()
    await session.commit()

    world = World()
    world.lab_a, world.lab_b = lab_a, lab_b
    world.user_a, world.user_b = user_a, user_b
    world.admin_a, world.observer = admin_a, observer
    world.workspace_a, world.workspace_b = workspace_a.id, workspace_b.id
    return world


@pytest.fixture
async def world(seeded: AsyncSession) -> World:
    return await build_world(seeded)


async def add_instance(
    session: AsyncSession,
    *,
    workspace_id: int,
    owner_user_id: int,
    started_at: datetime,
    stopped_at: datetime | None = None,
    gpu_count: int = 0,
    gpu_type: str = "none",
    cpu_milli: int = 1000,
    memory_mb: int = 1024,
    name: str = "inst",
) -> Instance:
    instance = Instance(
        name=name,
        display_name=name,
        workspace_id=workspace_id,
        owner_user_id=owner_user_id,
        cpu_milli=cpu_milli,
        memory_mb=memory_mb,
        gpu_type=gpu_type,
        gpu_count=gpu_count,
        image="registry.local/pytorch:2.4",
        ide_type=IdeType.NONE,
        status=InstanceStatus.STOPPED,
        started_at=started_at,
        stopped_at=stopped_at,
    )
    session.add(instance)
    await session.flush()
    return instance


async def add_task(
    session: AsyncSession,
    *,
    workspace_id: int,
    group_id: int,
    owner_user_id: int,
    started_at: datetime,
    finished_at: datetime | None = None,
    gpu_count: int = 0,
    gpu_type: str = "none",
    cpu_milli: int = 1000,
    memory_mb: int = 1024,
    name: str = "job",
) -> Task:
    task = Task(
        name=name,
        workspace_id=workspace_id,
        # NOT NULL on `tasks` (unlike `instances`, which derives it at rollup
        # time) — the column snapshots the workspace's group at submission.
        group_id=group_id,
        owner_user_id=owner_user_id,
        cpu_milli=cpu_milli,
        memory_mb=memory_mb,
        gpu_type=gpu_type,
        gpu_count=gpu_count,
        image="registry.local/pytorch:2.4",
        script="echo hi",
        status=TaskStatus.SUCCEEDED,
        started_at=started_at,
        finished_at=finished_at,
    )
    session.add(task)
    await session.flush()
    return task


async def rollup(session: AsyncSession, start: date, end: date) -> usage_service.RollupResult:
    result = await usage_service.rollup(session, day_from=start, day_to=end, now=NOW)
    await session.commit()
    return result


async def rows(session: AsyncSession) -> list[UsageDaily]:
    stmt = select(UsageDaily).order_by(
        UsageDaily.day, UsageDaily.user_id, UsageDaily.workload_kind, UsageDaily.gpu_type
    )
    return list((await session.scalars(stmt)).all())


@pytest.fixture
async def admin_client(client: AsyncClient, seeded: AsyncSession, world: World) -> AsyncClient:
    return await act_as(client, "root2")


# ---------------------------------------------------------------------------
# The interval arithmetic
# ---------------------------------------------------------------------------
async def test_a_workload_inside_one_day_lands_on_that_day(
    seeded: AsyncSession, world: World
) -> None:
    await add_instance(
        seeded,
        workspace_id=world.workspace_a,
        owner_user_id=world.user_a.id,
        started_at=datetime(2026, 3, 9, 9, 0, tzinfo=UTC),
        stopped_at=datetime(2026, 3, 9, 11, 30, tzinfo=UTC),
        gpu_count=2,
        gpu_type="A100",
        cpu_milli=4000,
        memory_mb=8192,
    )
    await seeded.commit()

    await rollup(seeded, date(2026, 3, 9), date(2026, 3, 9))
    facts = await rows(seeded)

    assert len(facts) == 1
    fact = facts[0]
    assert fact.day == date(2026, 3, 9)
    assert fact.runtime_seconds == 9000  # 2.5 hours
    assert fact.gpu_seconds == 18000  # 2 cards × 2.5 h
    assert fact.cpu_milli_seconds == 4000 * 9000
    assert fact.memory_mb_seconds == 8192 * 9000
    assert fact.workload_count == 1
    assert fact.workload_kind == UsageKind.INSTANCE
    assert fact.gpu_type == "A100"


async def test_a_workload_across_midnight_is_split_at_the_boundary(
    seeded: AsyncSession, world: World
) -> None:
    """The whole reason the table is daily. 22:00–02:00 is two hours each side."""
    await add_instance(
        seeded,
        workspace_id=world.workspace_a,
        owner_user_id=world.user_a.id,
        started_at=datetime(2026, 3, 9, 22, 0, tzinfo=UTC),
        stopped_at=datetime(2026, 3, 10, 2, 0, tzinfo=UTC),
        gpu_count=1,
        gpu_type="V100",
    )
    await seeded.commit()

    await rollup(seeded, date(2026, 3, 9), date(2026, 3, 10))
    facts = await rows(seeded)

    assert [f.day for f in facts] == [date(2026, 3, 9), date(2026, 3, 10)]
    assert [f.runtime_seconds for f in facts] == [7200, 7200]
    assert [f.gpu_seconds for f in facts] == [7200, 7200]
    # Nothing is created or lost at the seam.
    assert sum(f.runtime_seconds for f in facts) == 4 * 3600


async def test_a_multi_day_workload_fills_the_days_between(
    seeded: AsyncSession, world: World
) -> None:
    """Interior days are whole; only the first and last are partial."""
    await add_instance(
        seeded,
        workspace_id=world.workspace_a,
        owner_user_id=world.user_a.id,
        started_at=datetime(2026, 3, 9, 6, 0, tzinfo=UTC),
        stopped_at=datetime(2026, 3, 12, 6, 0, tzinfo=UTC),
        gpu_count=1,
        gpu_type="H100",
    )
    await seeded.commit()

    await rollup(seeded, date(2026, 3, 9), date(2026, 3, 12))
    facts = await rows(seeded)

    assert [f.day for f in facts] == [date(2026, 3, d) for d in (9, 10, 11, 12)]
    assert [f.runtime_seconds for f in facts] == [64800, 86400, 86400, 21600]
    assert sum(f.runtime_seconds for f in facts) == 3 * 86400


async def test_a_still_running_workload_counts_up_to_now(
    seeded: AsyncSession, world: World
) -> None:
    """Today's row is provisional and grows — that is expected, not a bug."""
    await add_instance(
        seeded,
        workspace_id=world.workspace_a,
        owner_user_id=world.user_a.id,
        started_at=datetime(2026, 3, 10, 10, 0, tzinfo=UTC),
        stopped_at=None,
        gpu_count=4,
        gpu_type="A100",
    )
    await seeded.commit()

    await rollup(seeded, date(2026, 3, 10), date(2026, 3, 10))
    facts = await rows(seeded)

    assert len(facts) == 1
    assert facts[0].runtime_seconds == 7200  # 10:00 → 12:00
    assert facts[0].gpu_seconds == 4 * 7200


async def test_a_never_started_workload_contributes_nothing(
    seeded: AsyncSession, world: World
) -> None:
    """An instance that was requested but never scheduled holds no hardware."""
    instance = Instance(
        name="never",
        display_name="never",
        workspace_id=world.workspace_a,
        owner_user_id=world.user_a.id,
        cpu_milli=1000,
        memory_mb=1024,
        gpu_type="A100",
        gpu_count=8,
        image="registry.local/x:1",
        ide_type=IdeType.NONE,
        status=InstanceStatus.PENDING,
        started_at=None,
        stopped_at=None,
    )
    seeded.add(instance)
    await seeded.commit()

    result = await rollup(seeded, date(2026, 3, 9), date(2026, 3, 10))
    assert await rows(seeded) == []
    assert result.rows_written == 0


async def test_a_zero_length_interval_is_not_counted(seeded: AsyncSession, world: World) -> None:
    """A workload that started and stopped in the same instant consumed nothing.

    Not a hypothetical: a task whose Job fails on admission has both timestamps
    set to the same reconcile pass, and counting it as one second each time the
    sweep runs would invent usage out of a scheduling failure.
    """
    moment = datetime(2026, 3, 9, 9, 0, tzinfo=UTC)
    await add_task(
        seeded,
        workspace_id=world.workspace_a,
        group_id=world.lab_a.id,
        owner_user_id=world.user_a.id,
        started_at=moment,
        finished_at=moment,
        gpu_count=1,
        gpu_type="A100",
    )
    await seeded.commit()

    result = await rollup(seeded, date(2026, 3, 9), date(2026, 3, 9))
    assert await rows(seeded) == []
    assert result.outside_window == 1


async def test_workloads_outside_the_window_are_reported_but_not_counted(
    seeded: AsyncSession, world: World
) -> None:
    await add_instance(
        seeded,
        workspace_id=world.workspace_a,
        owner_user_id=world.user_a.id,
        started_at=datetime(2026, 1, 1, 0, 0, tzinfo=UTC),
        stopped_at=datetime(2026, 1, 2, 0, 0, tzinfo=UTC),
        gpu_count=1,
    )
    await seeded.commit()

    result = await rollup(seeded, date(2026, 3, 9), date(2026, 3, 9))
    assert await rows(seeded) == []
    assert result.outside_window == 1
    assert result.instances_scanned == 1


async def test_two_workloads_on_the_same_day_share_one_row(
    seeded: AsyncSession, world: World
) -> None:
    """Same day, user, group, workspace, kind and accelerator → one row, summed.

    One row per workload would make the table grow with every container ever run
    and turn every report into a `GROUP BY` at read time.
    """
    for hour in (9, 14):
        await add_instance(
            seeded,
            workspace_id=world.workspace_a,
            owner_user_id=world.user_a.id,
            started_at=datetime(2026, 3, 9, hour, 0, tzinfo=UTC),
            stopped_at=datetime(2026, 3, 9, hour + 1, 0, tzinfo=UTC),
            gpu_count=1,
            gpu_type="A100",
            name=f"inst-{hour}",
        )
    await seeded.commit()

    await rollup(seeded, date(2026, 3, 9), date(2026, 3, 9))
    facts = await rows(seeded)

    assert len(facts) == 1
    assert facts[0].workload_count == 2
    assert facts[0].runtime_seconds == 7200
    assert facts[0].gpu_seconds == 7200


async def test_different_accelerators_stay_separate(seeded: AsyncSession, world: World) -> None:
    """Because "how much A100 did we use" is the question that gets asked."""
    for gpu_type in ("A100", "V100", "none"):
        await add_instance(
            seeded,
            workspace_id=world.workspace_a,
            owner_user_id=world.user_a.id,
            started_at=datetime(2026, 3, 9, 9, 0, tzinfo=UTC),
            stopped_at=datetime(2026, 3, 9, 10, 0, tzinfo=UTC),
            gpu_count=0 if gpu_type == "none" else 1,
            gpu_type=gpu_type,
            name=f"inst-{gpu_type}",
        )
    await seeded.commit()

    await rollup(seeded, date(2026, 3, 9), date(2026, 3, 9))
    facts = await rows(seeded)

    assert {f.gpu_type for f in facts} == {"A100", "V100", "none"}
    assert all(f.runtime_seconds == 3600 for f in facts)
    assert {f.gpu_type: f.gpu_seconds for f in facts} == {"A100": 3600, "V100": 3600, "none": 0}


async def test_instances_and_tasks_are_counted_separately(
    seeded: AsyncSession, world: World
) -> None:
    await add_instance(
        seeded,
        workspace_id=world.workspace_a,
        owner_user_id=world.user_a.id,
        started_at=datetime(2026, 3, 9, 9, 0, tzinfo=UTC),
        stopped_at=datetime(2026, 3, 9, 10, 0, tzinfo=UTC),
        gpu_count=1,
        gpu_type="A100",
    )
    await add_task(
        seeded,
        workspace_id=world.workspace_a,
        group_id=world.lab_a.id,
        owner_user_id=world.user_a.id,
        started_at=datetime(2026, 3, 9, 9, 0, tzinfo=UTC),
        finished_at=datetime(2026, 3, 9, 12, 0, tzinfo=UTC),
        gpu_count=2,
        gpu_type="A100",
    )
    await seeded.commit()

    await rollup(seeded, date(2026, 3, 9), date(2026, 3, 9))
    facts = await rows(seeded)

    by_kind = {f.workload_kind: f for f in facts}
    assert set(by_kind) == {UsageKind.INSTANCE, UsageKind.TASK}
    assert by_kind[UsageKind.INSTANCE].gpu_seconds == 3600
    assert by_kind[UsageKind.TASK].gpu_seconds == 2 * 10800


async def test_recomputing_a_day_does_not_double_it(seeded: AsyncSession, world: World) -> None:
    """The property that makes it safe to run every twenty minutes."""
    await add_instance(
        seeded,
        workspace_id=world.workspace_a,
        owner_user_id=world.user_a.id,
        started_at=datetime(2026, 3, 9, 9, 0, tzinfo=UTC),
        stopped_at=datetime(2026, 3, 9, 10, 0, tzinfo=UTC),
        gpu_count=1,
        gpu_type="A100",
    )
    await seeded.commit()

    await rollup(seeded, date(2026, 3, 9), date(2026, 3, 9))
    first = (await rows(seeded))[0].gpu_seconds
    await rollup(seeded, date(2026, 3, 9), date(2026, 3, 9))
    await rollup(seeded, date(2026, 3, 9), date(2026, 3, 9))
    second = (await rows(seeded))[0].gpu_seconds

    assert first == second == 3600
    assert len(await rows(seeded)) == 1


async def test_a_corrected_workload_replaces_its_old_row(
    seeded: AsyncSession, world: World
) -> None:
    """Replacement, not accumulation: a stopped time that turns out to be wrong
    must be able to reduce a total, or a fix could never be applied."""
    instance = await add_instance(
        seeded,
        workspace_id=world.workspace_a,
        owner_user_id=world.user_a.id,
        started_at=datetime(2026, 3, 9, 9, 0, tzinfo=UTC),
        stopped_at=datetime(2026, 3, 9, 12, 0, tzinfo=UTC),
        gpu_count=1,
    )
    await seeded.commit()
    await rollup(seeded, date(2026, 3, 9), date(2026, 3, 9))
    assert (await rows(seeded))[0].gpu_seconds == 3 * 3600

    instance.stopped_at = datetime(2026, 3, 9, 10, 0, tzinfo=UTC)
    await seeded.commit()
    await rollup(seeded, date(2026, 3, 9), date(2026, 3, 9))
    assert (await rows(seeded))[0].gpu_seconds == 3600


async def test_a_deleted_workload_still_counts(seeded: AsyncSession, world: World) -> None:
    """`is_active=False` removed it from the listings, not from history.

    Deleting an instance must not retroactively make the cluster look idle; the
    hardware was held either way.
    """
    instance = await add_instance(
        seeded,
        workspace_id=world.workspace_a,
        owner_user_id=world.user_a.id,
        started_at=datetime(2026, 3, 9, 9, 0, tzinfo=UTC),
        stopped_at=datetime(2026, 3, 9, 10, 0, tzinfo=UTC),
        gpu_count=1,
    )
    instance.is_active = False
    await seeded.commit()

    await rollup(seeded, date(2026, 3, 9), date(2026, 3, 9))
    assert (await rows(seeded))[0].gpu_seconds == 3600


async def test_the_group_on_a_row_is_a_snapshot(seeded: AsyncSession, world: World) -> None:
    """Moving a workspace must not move last quarter's usage with it."""
    await add_instance(
        seeded,
        workspace_id=world.workspace_a,
        owner_user_id=world.user_a.id,
        started_at=datetime(2026, 3, 9, 9, 0, tzinfo=UTC),
        stopped_at=datetime(2026, 3, 9, 10, 0, tzinfo=UTC),
        gpu_count=1,
    )
    await seeded.commit()
    await rollup(seeded, date(2026, 3, 9), date(2026, 3, 9))
    assert (await rows(seeded))[0].group_id == world.lab_a.id

    workspace = await seeded.get(Workspace, world.workspace_a)
    assert workspace is not None
    workspace.group_id = world.lab_b.id
    await seeded.commit()

    # The already-written row keeps the old group; a recompute of a *new* window
    # picks up the new one, which is the honest reading of each.
    assert (await rows(seeded))[0].group_id == world.lab_a.id


async def test_the_rollup_window_is_bounded(seeded: AsyncSession, world: World) -> None:
    from app.core.errors import BadRequestError

    with pytest.raises(BadRequestError) as excinfo:
        await usage_service.rollup(
            seeded, day_from=date(2000, 1, 1), day_to=date(2026, 1, 1), now=NOW
        )
    assert excinfo.value.code == "usage_range_too_large"


# ---------------------------------------------------------------------------
# The data scope
# ---------------------------------------------------------------------------
async def test_a_user_sees_only_their_own_usage(
    seeded: AsyncSession, world: World, client: AsyncClient
) -> None:
    await add_instance(
        seeded,
        workspace_id=world.workspace_a,
        owner_user_id=world.user_a.id,
        started_at=datetime(2026, 3, 9, 9, 0, tzinfo=UTC),
        stopped_at=datetime(2026, 3, 9, 10, 0, tzinfo=UTC),
        gpu_count=1,
        gpu_type="A100",
    )
    await add_instance(
        seeded,
        workspace_id=world.workspace_b,
        owner_user_id=world.user_b.id,
        started_at=datetime(2026, 3, 9, 9, 0, tzinfo=UTC),
        stopped_at=datetime(2026, 3, 9, 20, 0, tzinfo=UTC),
        gpu_count=4,
        gpu_type="H100",
        name="inst-b",
    )
    await seeded.commit()
    await rollup(seeded, date(2026, 3, 9), date(2026, 3, 9))

    await act_as(client, "dana")
    body = (
        await client.get(SUMMARY, params={"dayFrom": "2026-03-09", "dayTo": "2026-03-09"})
    ).json()

    assert body["totals"]["gpuSeconds"] == 3600
    assert body["totals"]["userCount"] == 1
    # A normal user is the "self" case, so the second panel would be a duplicate.
    assert body["selfTotals"] is None


async def test_a_colleague_in_the_same_workspace_still_cannot_see_your_usage(
    seeded: AsyncSession, world: World, client: AsyncClient
) -> None:
    """The deliberate asymmetry with every other resource.

    Being in the same namespace is reason enough to see each other's containers.
    It is not reason enough to see each other's share of the cluster, and the
    scope rule omits `member_workspace` precisely to keep the two apart.
    """
    await add_instance(
        seeded,
        workspace_id=world.workspace_a,
        owner_user_id=world.user_a.id,
        started_at=datetime(2026, 3, 9, 9, 0, tzinfo=UTC),
        stopped_at=datetime(2026, 3, 9, 10, 0, tzinfo=UTC),
        gpu_count=1,
        gpu_type="A100",
    )
    await seeded.commit()
    await rollup(seeded, date(2026, 3, 9), date(2026, 3, 9))

    # The observer is a member of lab-a and of the same workspace through the
    # group, but dana's consumption is not theirs to read.
    await act_as(client, "gazer")
    body = (
        await client.get(SUMMARY, params={"dayFrom": "2026-03-09", "dayTo": "2026-03-09"})
    ).json()
    assert body["totals"]["gpuSeconds"] == 0
    assert body["totals"]["userCount"] == 0


async def test_a_group_admin_sees_their_group_and_not_the_other_lab(
    seeded: AsyncSession, world: World, client: AsyncClient
) -> None:
    await add_instance(
        seeded,
        workspace_id=world.workspace_a,
        owner_user_id=world.user_a.id,
        started_at=datetime(2026, 3, 9, 9, 0, tzinfo=UTC),
        stopped_at=datetime(2026, 3, 9, 10, 0, tzinfo=UTC),
        gpu_count=1,
        gpu_type="A100",
    )
    await add_instance(
        seeded,
        workspace_id=world.workspace_b,
        owner_user_id=world.user_b.id,
        started_at=datetime(2026, 3, 9, 9, 0, tzinfo=UTC),
        stopped_at=datetime(2026, 3, 9, 20, 0, tzinfo=UTC),
        gpu_count=4,
        gpu_type="H100",
        name="inst-b",
    )
    await seeded.commit()
    await rollup(seeded, date(2026, 3, 9), date(2026, 3, 9))

    await act_as(client, "boss-a")
    body = (
        await client.get(SUMMARY, params={"dayFrom": "2026-03-09", "dayTo": "2026-03-09"})
    ).json()

    # Their own group's hour, and not lab-b's 44 hours.
    assert body["totals"]["gpuSeconds"] == 3600

    by_group = (
        await client.get(
            BREAKDOWN, params={"by": "group", "dayFrom": "2026-03-09", "dayTo": "2026-03-09"}
        )
    ).json()
    assert {row["label"] for row in by_group["rows"]} == {"自然语言处理"}


async def test_a_global_admin_sees_everything_and_their_own_share(
    seeded: AsyncSession, world: World, admin_client: AsyncClient
) -> None:
    await add_instance(
        seeded,
        workspace_id=world.workspace_a,
        owner_user_id=world.user_a.id,
        started_at=datetime(2026, 3, 9, 9, 0, tzinfo=UTC),
        stopped_at=datetime(2026, 3, 9, 10, 0, tzinfo=UTC),
        gpu_count=1,
        gpu_type="A100",
    )
    await add_instance(
        seeded,
        workspace_id=world.workspace_b,
        owner_user_id=world.user_b.id,
        started_at=datetime(2026, 3, 9, 9, 0, tzinfo=UTC),
        stopped_at=datetime(2026, 3, 9, 20, 0, tzinfo=UTC),
        gpu_count=4,
        gpu_type="H100",
        name="inst-b",
    )
    await seeded.commit()
    await rollup(seeded, date(2026, 3, 9), date(2026, 3, 9))

    body = (
        await admin_client.get(SUMMARY, params={"dayFrom": "2026-03-09", "dayTo": "2026-03-09"})
    ).json()

    assert body["totals"]["gpuSeconds"] == 3600 + 4 * 39600
    assert body["totals"]["userCount"] == 2
    # The administrator consumed nothing themselves, and can see that they did not.
    assert body["selfTotals"] is not None
    assert body["selfTotals"]["gpuSeconds"] == 0


async def test_a_group_admin_exporting_cannot_widen_their_own_scope(
    seeded: AsyncSession, world: World, client: AsyncClient
) -> None:
    """The export applies the same filter as the page.

    A CSV is where a scope bug does the most damage and is noticed least, so this
    asserts the file's *contents*, not just its status code.
    """
    await add_instance(
        seeded,
        workspace_id=world.workspace_a,
        owner_user_id=world.user_a.id,
        started_at=datetime(2026, 3, 9, 9, 0, tzinfo=UTC),
        stopped_at=datetime(2026, 3, 9, 10, 0, tzinfo=UTC),
        gpu_count=1,
        gpu_type="A100",
    )
    await add_instance(
        seeded,
        workspace_id=world.workspace_b,
        owner_user_id=world.user_b.id,
        started_at=datetime(2026, 3, 9, 9, 0, tzinfo=UTC),
        stopped_at=datetime(2026, 3, 9, 20, 0, tzinfo=UTC),
        gpu_count=4,
        gpu_type="H100",
        name="inst-b",
    )
    await seeded.commit()
    await rollup(seeded, date(2026, 3, 9), date(2026, 3, 9))

    await act_as(client, "boss-a")
    response = await client.get(EXPORT, params={"dayFrom": "2026-03-09", "dayTo": "2026-03-09"})
    assert response.status_code == 200, response.text
    text = response.content.decode("utf-8-sig")
    assert "戴娜" in text
    assert "艾琳" not in text
    assert "H100" not in text
    assert "A100" in text


# ---------------------------------------------------------------------------
# The HTTP surface
# ---------------------------------------------------------------------------
async def test_query_parameters_are_camel_case(
    seeded: AsyncSession, world: World, admin_client: AsyncClient
) -> None:
    """The snake_case spelling is silently ignored, so a typo reads as "no data".

    This is the failure mode that has already bitten three phases: the request
    returns 200 with an empty result, and nothing says the filter was dropped.
    """
    await add_instance(
        seeded,
        workspace_id=world.workspace_a,
        owner_user_id=world.user_a.id,
        started_at=datetime(2026, 3, 9, 9, 0, tzinfo=UTC),
        stopped_at=datetime(2026, 3, 9, 10, 0, tzinfo=UTC),
        gpu_count=1,
        gpu_type="A100",
    )
    await seeded.commit()
    await rollup(seeded, date(2026, 3, 9), date(2026, 3, 9))

    camel = (
        await admin_client.get(SUMMARY, params={"dayFrom": "2026-03-09", "dayTo": "2026-03-09"})
    ).json()
    assert camel["dayFrom"] == "2026-03-09"
    assert camel["totals"]["gpuSeconds"] == 3600

    # The snake_case key is not an alias, so the default 30-day window applies
    # and the explicit range is ignored — visible in the echoed dayFrom.
    snake = (
        await admin_client.get(SUMMARY, params={"day_from": "2026-03-09", "day_to": "2026-03-09"})
    ).json()
    assert snake["dayFrom"] != "2026-03-09"


async def test_an_empty_day_is_present_as_a_zero(
    seeded: AsyncSession, world: World, admin_client: AsyncClient
) -> None:
    """Gaps must be visible. A line drawn straight across an idle week reads as
    steady usage, which is exactly wrong."""
    await add_instance(
        seeded,
        workspace_id=world.workspace_a,
        owner_user_id=world.user_a.id,
        started_at=datetime(2026, 3, 9, 9, 0, tzinfo=UTC),
        stopped_at=datetime(2026, 3, 9, 10, 0, tzinfo=UTC),
        gpu_count=1,
        gpu_type="A100",
    )
    await seeded.commit()
    await rollup(seeded, date(2026, 3, 9), date(2026, 3, 10))

    body = (
        await admin_client.get(SERIES, params={"dayFrom": "2026-03-09", "dayTo": "2026-03-11"})
    ).json()

    assert [point["day"] for point in body["points"]] == [
        "2026-03-09",
        "2026-03-10",
        "2026-03-11",
    ]
    assert [point["totals"]["gpuSeconds"] for point in body["points"]] == [3600, 0, 0]


async def test_the_breakdown_labels_what_it_groups(
    seeded: AsyncSession, world: World, admin_client: AsyncClient
) -> None:
    await add_instance(
        seeded,
        workspace_id=world.workspace_a,
        owner_user_id=world.user_a.id,
        started_at=datetime(2026, 3, 9, 9, 0, tzinfo=UTC),
        stopped_at=datetime(2026, 3, 9, 10, 0, tzinfo=UTC),
        gpu_count=1,
        gpu_type="A100",
    )
    await seeded.commit()
    await rollup(seeded, date(2026, 3, 9), date(2026, 3, 9))

    params = {"dayFrom": "2026-03-09", "dayTo": "2026-03-09"}
    people = (await admin_client.get(BREAKDOWN, params={**params, "by": "user"})).json()
    assert people["rows"][0]["label"] == "戴娜"
    assert people["rows"][0]["entityId"] == world.user_a.id
    assert people["rows"][0]["totals"]["gpuHours"] == 1.0
    # The instance/task split comes along for every dimension but its own.
    assert people["rows"][0]["kinds"] == {"instance": 3600}

    labs = (await admin_client.get(BREAKDOWN, params={**params, "by": "group"})).json()
    assert labs["rows"][0]["label"] == "自然语言处理"

    spaces = (await admin_client.get(BREAKDOWN, params={**params, "by": "workspace"})).json()
    assert spaces["rows"][0]["label"] == "A 组工作空间"

    models = (await admin_client.get(BREAKDOWN, params={**params, "by": "gpu_type"})).json()
    assert models["rows"][0]["label"] == "A100"
    # Nothing to drill into, so no id is invented.
    assert models["rows"][0]["entityId"] is None
    # The instance/task split is still useful here — "how much A100 time was
    # interactive jobs?" is a real question. It is suppressed only when the
    # dimension *is* the workload kind, where it would say the same thing twice.
    assert models["rows"][0]["kinds"] == {"instance": 3600}

    kinds = (await admin_client.get(BREAKDOWN, params={**params, "by": "workload_kind"})).json()
    assert kinds["rows"][0]["label"] == "开发实例"


async def test_an_unknown_breakdown_dimension_is_refused(
    seeded: AsyncSession, world: World, admin_client: AsyncClient
) -> None:
    response = await admin_client.get(BREAKDOWN, params={"by": "phase_of_the_moon"})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "usage_unknown_dimension"


async def test_an_inverted_range_is_refused_rather_than_swapped(
    seeded: AsyncSession, world: World, admin_client: AsyncClient
) -> None:
    response = await admin_client.get(
        SUMMARY, params={"dayFrom": "2026-03-10", "dayTo": "2026-03-01"}
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "usage_invalid_range"


async def test_an_absurd_range_is_refused(
    seeded: AsyncSession, world: World, admin_client: AsyncClient
) -> None:
    response = await admin_client.get(
        SUMMARY, params={"dayFrom": "1999-01-01", "dayTo": "2026-03-01"}
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "usage_range_too_large"


async def test_covered_distinguishes_no_usage_from_no_rollup(
    seeded: AsyncSession, world: World, admin_client: AsyncClient
) -> None:
    before = (
        await admin_client.get(SUMMARY, params={"dayFrom": "2026-03-09", "dayTo": "2026-03-09"})
    ).json()
    assert before["covered"] is False
    assert before["totals"]["gpuSeconds"] == 0

    await add_instance(
        seeded,
        workspace_id=world.workspace_a,
        owner_user_id=world.user_a.id,
        started_at=datetime(2026, 3, 9, 9, 0, tzinfo=UTC),
        stopped_at=datetime(2026, 3, 9, 10, 0, tzinfo=UTC),
        gpu_count=1,
    )
    await seeded.commit()
    await rollup(seeded, date(2026, 3, 9), date(2026, 3, 9))

    after = (
        await admin_client.get(SUMMARY, params={"dayFrom": "2026-03-09", "dayTo": "2026-03-09"})
    ).json()
    assert after["covered"] is True


async def test_filters_narrow_the_totals(
    seeded: AsyncSession, world: World, admin_client: AsyncClient
) -> None:
    await add_instance(
        seeded,
        workspace_id=world.workspace_a,
        owner_user_id=world.user_a.id,
        started_at=datetime(2026, 3, 9, 9, 0, tzinfo=UTC),
        stopped_at=datetime(2026, 3, 9, 10, 0, tzinfo=UTC),
        gpu_count=1,
        gpu_type="A100",
    )
    await add_task(
        seeded,
        workspace_id=world.workspace_a,
        group_id=world.lab_a.id,
        owner_user_id=world.user_a.id,
        started_at=datetime(2026, 3, 9, 9, 0, tzinfo=UTC),
        finished_at=datetime(2026, 3, 9, 11, 0, tzinfo=UTC),
        gpu_count=8,
        gpu_type="H100",
    )
    await seeded.commit()
    await rollup(seeded, date(2026, 3, 9), date(2026, 3, 9))

    base = {"dayFrom": "2026-03-09", "dayTo": "2026-03-09"}

    only_tasks = (await admin_client.get(SUMMARY, params={**base, "workloadKind": "task"})).json()
    assert only_tasks["totals"]["gpuSeconds"] == 8 * 7200

    only_a100 = (await admin_client.get(SUMMARY, params={**base, "gpuType": "A100"})).json()
    assert only_a100["totals"]["gpuSeconds"] == 3600

    by_workspace = (
        await admin_client.get(SUMMARY, params={**base, "workspaceId": world.workspace_b})
    ).json()
    assert by_workspace["totals"]["gpuSeconds"] == 0


# ---------------------------------------------------------------------------
# Permissions
# ---------------------------------------------------------------------------
async def test_export_needs_its_own_permission(
    seeded: AsyncSession, world: World, client: AsyncClient
) -> None:
    """A normal user reads the page; they do not walk away with the file."""
    await act_as(client, "dana")
    response = await client.get(EXPORT)
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "permission_denied"


async def test_a_group_admin_may_export_their_group(
    seeded: AsyncSession, world: World, client: AsyncClient
) -> None:
    await add_instance(
        seeded,
        workspace_id=world.workspace_a,
        owner_user_id=world.user_a.id,
        started_at=datetime(2026, 3, 9, 9, 0, tzinfo=UTC),
        stopped_at=datetime(2026, 3, 9, 10, 0, tzinfo=UTC),
        gpu_count=1,
        gpu_type="A100",
    )
    await seeded.commit()
    await rollup(seeded, date(2026, 3, 9), date(2026, 3, 9))

    await act_as(client, "boss-a")
    response = await client.get(EXPORT, params={"dayFrom": "2026-03-09", "dayTo": "2026-03-09"})

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert "attachment; filename=" in response.headers["content-disposition"]
    # The BOM is what stops Excel mangling the Chinese headers.
    assert response.content.startswith(b"\xef\xbb\xbf")
    body = response.content.decode("utf-8-sig")
    assert "GPU卡时" in body
    assert "1.0" in body


async def test_export_is_recorded_in_the_audit_trail(
    seeded: AsyncSession, world: World, client: AsyncClient, audit_rows: Any
) -> None:
    """The middleware only records mutations, so the handler records this one.

    "Who downloaded the usage report" is a question an institution asks.
    """
    await act_as(client, "boss-a")
    await client.get(EXPORT, params={"dayFrom": "2026-03-09", "dayTo": "2026-03-09"})

    entries = await audit_rows(action="usage:export")
    assert len(entries) == 1
    assert entries[0].actor_username == "boss-a"
    assert entries[0].status == "success"


async def test_rollup_is_global_admin_only(
    seeded: AsyncSession, world: World, client: AsyncClient
) -> None:
    """Rewriting every tenant's rows has no group-scoped version."""
    await act_as(client, "boss-a")
    response = await client.post(ROLLUP, json={})
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "permission_denied"


async def test_a_global_admin_can_trigger_the_rollup(
    seeded: AsyncSession, world: World, admin_client: AsyncClient, audit_rows: Any
) -> None:
    await add_instance(
        seeded,
        workspace_id=world.workspace_a,
        owner_user_id=world.user_a.id,
        started_at=datetime.now(UTC) - timedelta(hours=2),
        stopped_at=datetime.now(UTC) - timedelta(hours=1),
        gpu_count=1,
        gpu_type="A100",
    )
    await seeded.commit()

    response = await admin_client.post(ROLLUP, json={})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["rowsWritten"] >= 1
    assert body["instancesScanned"] == 1
    assert body["gpuSeconds"] == 3600

    entries = await audit_rows(action="usage:rollup")
    assert len(entries) == 1
    assert entries[0].status == "success"


async def test_the_rollup_endpoint_accepts_an_explicit_window(
    seeded: AsyncSession, world: World, admin_client: AsyncClient
) -> None:
    await add_instance(
        seeded,
        workspace_id=world.workspace_a,
        owner_user_id=world.user_a.id,
        started_at=datetime(2026, 3, 9, 9, 0, tzinfo=UTC),
        stopped_at=datetime(2026, 3, 9, 10, 0, tzinfo=UTC),
        gpu_count=1,
    )
    await seeded.commit()

    response = await admin_client.post(
        ROLLUP, json={"dayFrom": "2026-03-09", "dayTo": "2026-03-09"}
    )
    assert response.status_code == 200, response.text
    assert response.json()["dayFrom"] == "2026-03-09"
    assert response.json()["gpuSeconds"] == 3600


async def test_an_observer_may_read_their_own_usage(
    seeded: AsyncSession, world: World, client: AsyncClient
) -> None:
    """Read-only access is still access — the observer role exists to look."""
    await act_as(client, "gazer")
    assert (await client.get(SUMMARY)).status_code == 200
    assert (await client.get(SERIES)).status_code == 200
    assert (await client.get("/api/v1/usage/meta")).status_code == 200


async def test_the_meta_endpoint_matches_the_service(
    seeded: AsyncSession, world: World, admin_client: AsyncClient
) -> None:
    body = (await admin_client.get("/api/v1/usage/meta")).json()
    assert [d["key"] for d in body["dimensions"]] == list(usage_service.BREAKDOWN_DIMENSIONS)
    assert [k["value"] for k in body["kinds"]] == ["instance", "task"]
    assert body["maxRangeDays"] == usage_service.MAX_QUERY_DAYS
    assert body["defaultRollupLookbackDays"] == usage_service.DEFAULT_ROLLUP_LOOKBACK_DAYS


async def test_an_unknown_query_field_is_refused_not_ignored(
    seeded: AsyncSession, world: World, admin_client: AsyncClient
) -> None:
    """`usage:rollup` takes a body; a typo in it must not silently do nothing."""
    response = await admin_client.post(ROLLUP, json={"dayFromm": "2026-03-09"})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_failed"


async def test_a_deleted_user_still_appears_in_the_report(
    seeded: AsyncSession, world: World, admin_client: AsyncClient
) -> None:
    """Usage that happened must not disappear because the account was closed."""
    await add_instance(
        seeded,
        workspace_id=world.workspace_a,
        owner_user_id=world.user_a.id,
        started_at=datetime(2026, 3, 9, 9, 0, tzinfo=UTC),
        stopped_at=datetime(2026, 3, 9, 10, 0, tzinfo=UTC),
        gpu_count=1,
        gpu_type="A100",
    )
    await seeded.commit()
    await rollup(seeded, date(2026, 3, 9), date(2026, 3, 9))

    # The account is deactivated; the facts must survive it.
    user = await seeded.get(User, world.user_a.id)
    assert user is not None
    user.full_name = ""
    user.username = "dana-removed"
    await seeded.commit()

    body = (
        await admin_client.get(
            BREAKDOWN, params={"by": "user", "dayFrom": "2026-03-09", "dayTo": "2026-03-09"}
        )
    ).json()
    assert body["rows"][0]["label"] == "dana-removed"
    assert body["rows"][0]["totals"]["gpuSeconds"] == 3600


async def test_the_timezone_used_is_utc_end_to_end(
    seeded: AsyncSession, world: World, admin_client: AsyncClient
) -> None:
    """A local-time rollup would double-count an hour twice a year.

    An interval that is entirely one UTC day must not leak into the neighbouring
    day, whatever the server's local timezone happens to be.
    """
    await add_instance(
        seeded,
        workspace_id=world.workspace_a,
        owner_user_id=world.user_a.id,
        started_at=datetime(2026, 3, 9, 0, 30, tzinfo=UTC),
        stopped_at=datetime(2026, 3, 9, 23, 30, tzinfo=UTC),
        gpu_count=1,
    )
    await seeded.commit()
    await rollup(seeded, date(2026, 3, 8), date(2026, 3, 10))

    facts = await rows(seeded)
    assert [f.day for f in facts] == [date(2026, 3, 9)]
    assert facts[0].runtime_seconds == 23 * 3600
