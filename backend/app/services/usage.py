"""The usage centre: what the cluster actually gave each person, per day.

**This module does not bill anybody.** It reports consumption so a lab can see
where its share went and an administrator can size the next purchase. There are
no prices, no balances and no invoices here, and adding them would be a different
feature with different correctness requirements — a report that is a rounding
crumb off is a nuisance; an invoice that is a rounding crumb off is a dispute.

What is counted
---------------

*Reserved* time, not busy time. An instance holding an A100 for eight hours
consumed eight GPU-hours whether the kernel was pegged or idle: the card was
unavailable to everyone else for those eight hours, and that is the number a
cluster owner can actually plan with. Measuring how *hard* the card worked is
Prometheus's job and belongs to the monitoring integration.

Only time inside a live interval counts. An instance that never started
contributes nothing, and a stopped one stops contributing the moment it stopped
— so a job left running over the weekend shows up, which is usually the finding
people are looking for.

How a day is bounded
--------------------

Everything is UTC, start to finish. A local-time rollup would double-count or
drop an hour twice a year, and the bug would appear in exactly one month's report
and be blamed on the data.

A workload that spans midnight is **split** at the boundary rather than assigned
to the day it started. This is the entire reason the table is daily: a training
run from 22:00 to 02:00 is two hours on each day, and a month boundary cuts every
long-running environment in half. Assigning the whole span to the start day would
make "how much did we use yesterday?" wrong every morning.

Recomputing is always safe
--------------------------

A rollup for a day range deletes that range and rewrites it from the source
records. There is no incremental counter to drift, which is what makes it
legitimate to recompute the current day every few minutes while it is still
growing, and to backfill a whole quarter after fixing a bug.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from sqlalchemy import Select, delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import policy
from app.core.errors import BadRequestError
from app.models.enums import UsageKind
from app.models.group import Group
from app.models.instance import Instance
from app.models.task import Task
from app.models.usage import UsageDaily
from app.models.user import User
from app.models.workspace import Workspace

#: A rollup window is bounded so a mistyped backfill cannot scan the whole
#: table. Two years is a full history for a cluster this size; beyond that the
#: request is a mistake rather than a plan.
MAX_ROLLUP_DAYS = 731

#: A query window, likewise bounded. Charts read a year at most, and an
#: unbounded `GROUP BY` over every day since the cluster was installed is the
#: query that takes the database down during a demo.
MAX_QUERY_DAYS = 731

#: How far back the scheduled rollup reaches by default. Three days, not one:
#: a workload can be stopped, reconciled or corrected slightly after midnight,
#: and re-running the previous days costs nothing while a late correction that
#: never lands is a permanent lie in the report.
DEFAULT_ROLLUP_LOOKBACK_DAYS = 3


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class RollupResult:
    """What one rollup pass did. Counters, so an operator can see it worked."""

    day_from: date
    day_to: date
    rows_written: int = 0
    instances_scanned: int = 0
    tasks_scanned: int = 0
    runtime_seconds: int = 0
    gpu_seconds: int = 0
    #: Workloads whose interval was entirely before the window, so contributed
    #: nothing. Reported because "scanned 4000, used 12" is otherwise alarming.
    outside_window: int = 0


@dataclass(slots=True)
class UsageTotals:
    """Aggregated measures, with display units derived rather than stored."""

    runtime_seconds: int = 0
    gpu_seconds: int = 0
    cpu_milli_seconds: int = 0
    memory_mb_seconds: int = 0
    workload_count: int = 0
    #: Number of distinct people/workspaces the totals cover — the difference
    #: between "the lab used 900 GPU·h" and "900 GPU·h across 30 people".
    user_count: int = 0
    workspace_count: int = 0

    @property
    def gpu_hours(self) -> float:
        return round(self.gpu_seconds / 3600, 2)

    @property
    def cpu_core_hours(self) -> float:
        return round(self.cpu_milli_seconds / 1000 / 3600, 2)

    @property
    def memory_gb_hours(self) -> float:
        return round(self.memory_mb_seconds / 1024 / 3600, 2)


@dataclass(slots=True)
class DailyPoint:
    """One day's totals, for a time series."""

    day: date
    totals: UsageTotals


@dataclass(slots=True)
class BreakdownRow:
    """One bucket of a grouped report."""

    key: str
    label: str
    #: Present only for the dimensions that can be filtered back to. A GPU model
    #: breakdown has no id, and inventing one would be a lie.
    entity_id: int | None
    totals: UsageTotals
    #: Workload kinds inside this bucket. A bucket that mixes instances and
    #: tasks is normal; one that is only tasks is worth noticing.
    kinds: dict[str, int] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _utc_day_start(day: date) -> datetime:
    return datetime.combine(day, time.min, tzinfo=UTC)


def _as_utc(value: datetime) -> datetime:
    """Normalise a stored timestamp to an aware UTC datetime.

    SQLite hands back naive datetimes even for ``DateTime(timezone=True)``
    columns, and comparing a naive value with an aware one raises. The tests run
    on SQLite and production runs on PostgreSQL, so this cannot be left to the
    driver to get right.
    """
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def resolve_window(
    day_from: date | None,
    day_to: date | None,
    *,
    default_days: int,
    max_days: int = MAX_QUERY_DAYS,
    today: date | None = None,
) -> tuple[date, date]:
    """Fill in and validate a day range.

    Rejects an inverted range instead of silently swapping it: a client that
    asked for March-to-January has a bug, and quietly returning January-to-March
    hides it until somebody reads a chart that is subtly the wrong way round.
    """
    today = today or datetime.now(UTC).date()
    end = day_to or today
    start = day_from or (end - timedelta(days=default_days - 1))
    if start > end:
        raise BadRequestError("开始日期不能晚于结束日期", code="usage_invalid_range")
    if (end - start).days + 1 > max_days:
        raise BadRequestError(f"查询区间不能超过 {max_days} 天", code="usage_range_too_large")
    return start, end


def _slices(
    start: datetime,
    end: datetime,
    window_from: datetime,
    window_to: datetime,
) -> Iterable[tuple[date, int]]:
    """Split a live interval into ``(day, seconds)`` pieces inside the window.

    Walks day boundaries from the interval's own start rather than from the
    window's, because the first slice is usually partial: a workload that began
    at 22:30 contributes 5400 seconds to that day, not 86400.
    """
    begin = max(start, window_from)
    finish = min(end, window_to)
    if finish <= begin:
        return

    cursor = begin
    while cursor < finish:
        day = cursor.date()
        boundary = min(_utc_day_start(day) + timedelta(days=1), finish)
        seconds = int((boundary - cursor).total_seconds())
        if seconds > 0:
            yield day, seconds
        cursor = boundary


@dataclass(slots=True)
class _Accumulator:
    """Mutable bucket, keyed by the table's unique grain."""

    runtime_seconds: int = 0
    gpu_seconds: int = 0
    cpu_milli_seconds: int = 0
    memory_mb_seconds: int = 0
    workload_count: int = 0


def _accumulate(
    buckets: dict[tuple[Any, ...], _Accumulator],
    key: tuple[Any, ...],
    *,
    seconds: int,
    cpu_milli: int,
    memory_mb: int,
    gpu_count: int,
) -> None:
    bucket = buckets.get(key)
    if bucket is None:
        bucket = _Accumulator()
        buckets[key] = bucket
    bucket.runtime_seconds += seconds
    bucket.gpu_seconds += gpu_count * seconds
    bucket.cpu_milli_seconds += cpu_milli * seconds
    bucket.memory_mb_seconds += memory_mb * seconds
    bucket.workload_count += 1


# ---------------------------------------------------------------------------
# Rollup
# ---------------------------------------------------------------------------
async def rollup(
    session: AsyncSession,
    *,
    day_from: date,
    day_to: date,
    now: datetime | None = None,
) -> RollupResult:
    """Recompute ``[day_from, day_to]`` from the instance and task records.

    The source of truth is deliberately the platform's own records rather than a
    metrics system. What the scheduler handed out is known exactly, needs no
    monitoring stack to be reachable, and cannot be argued with; a metrics-backed
    figure is a better *measurement* and a worse *record*. Prometheus-backed
    accounting is therefore an addition to make later, not a replacement.
    """
    now = now or datetime.now(UTC)
    result = RollupResult(day_from=day_from, day_to=day_to)

    if day_from > day_to:
        raise BadRequestError("开始日期不能晚于结束日期", code="usage_invalid_range")
    if (day_to - day_from).days + 1 > MAX_ROLLUP_DAYS:
        raise BadRequestError(
            f"重算区间不能超过 {MAX_ROLLUP_DAYS} 天", code="usage_range_too_large"
        )

    window_from = _utc_day_start(day_from)
    # Half-open at the end so a workload stopping exactly at midnight belongs to
    # the day that is ending, not to both.
    window_to = _utc_day_start(day_to) + timedelta(days=1)

    # The workspace's group is snapshotted onto each row, so it is read here
    # rather than joined at query time. A workspace moved between groups must not
    # retroactively move last quarter's usage.
    workspace_rows = (await session.execute(select(Workspace.id, Workspace.group_id))).all()
    workspace_groups: dict[int, int] = {int(row[0]): int(row[1]) for row in workspace_rows}

    buckets: dict[tuple[Any, ...], _Accumulator] = {}

    instances = (
        await session.scalars(select(Instance).where(Instance.started_at.is_not(None)))
    ).all()
    for instance in instances:
        started = _as_utc(instance.started_at)  # type: ignore[arg-type]
        # A running instance has no stop time; it is still consuming, so it runs
        # up to now. That is what makes today's row provisional and growing.
        ended = _as_utc(instance.stopped_at) if instance.stopped_at else now
        if ended <= started:
            result.outside_window += 1
            continue
        group_id = workspace_groups.get(instance.workspace_id)
        if group_id is None:
            # The workspace was deleted, taking its usage rows with it via the
            # cascade; there is nothing to attribute this to.
            result.outside_window += 1
            continue
        result.instances_scanned += 1
        contributed = False
        for day, seconds in _slices(started, ended, window_from, window_to):
            contributed = True
            _accumulate(
                buckets,
                (
                    day,
                    instance.owner_user_id,
                    group_id,
                    instance.workspace_id,
                    UsageKind.INSTANCE.value,
                    instance.gpu_type or "none",
                ),
                seconds=seconds,
                cpu_milli=instance.cpu_milli,
                memory_mb=instance.memory_mb,
                gpu_count=instance.gpu_count,
            )
            result.runtime_seconds += seconds
            result.gpu_seconds += instance.gpu_count * seconds
        if not contributed:
            result.outside_window += 1

    tasks = (await session.scalars(select(Task).where(Task.started_at.is_not(None)))).all()
    for task in tasks:
        started = _as_utc(task.started_at)  # type: ignore[arg-type]
        ended = _as_utc(task.finished_at) if task.finished_at else now
        if ended <= started:
            result.outside_window += 1
            continue
        group_id = workspace_groups.get(task.workspace_id)
        if group_id is None:
            result.outside_window += 1
            continue
        result.tasks_scanned += 1
        contributed = False
        for day, seconds in _slices(started, ended, window_from, window_to):
            contributed = True
            _accumulate(
                buckets,
                (
                    day,
                    task.owner_user_id,
                    group_id,
                    task.workspace_id,
                    UsageKind.TASK.value,
                    task.gpu_type or "none",
                ),
                seconds=seconds,
                cpu_milli=task.cpu_milli,
                memory_mb=task.memory_mb,
                gpu_count=task.gpu_count,
            )
            result.runtime_seconds += seconds
            result.gpu_seconds += task.gpu_count * seconds
        if not contributed:
            result.outside_window += 1

    # Replace, do not merge. Deleting only the window's own days means a rerun
    # cannot double a total, and a day that has become empty (because a workload
    # record was corrected or removed) correctly disappears.
    await session.execute(
        delete(UsageDaily).where(UsageDaily.day >= day_from, UsageDaily.day <= day_to)
    )
    for key, bucket in buckets.items():
        day, user_id, group_id, workspace_id, kind, gpu_type = key
        session.add(
            UsageDaily(
                day=day,
                user_id=user_id,
                group_id=group_id,
                workspace_id=workspace_id,
                workload_kind=kind,
                gpu_type=gpu_type,
                runtime_seconds=bucket.runtime_seconds,
                gpu_seconds=bucket.gpu_seconds,
                cpu_milli_seconds=bucket.cpu_milli_seconds,
                memory_mb_seconds=bucket.memory_mb_seconds,
                workload_count=bucket.workload_count,
            )
        )
    result.rows_written = len(buckets)
    await session.flush()
    return result


async def rollup_recent(
    session: AsyncSession,
    *,
    lookback_days: int = DEFAULT_ROLLUP_LOOKBACK_DAYS,
    now: datetime | None = None,
) -> RollupResult:
    """Recompute the last few days — what the scheduled job runs."""
    now = now or datetime.now(UTC)
    today = now.date()
    return await rollup(
        session,
        day_from=today - timedelta(days=lookback_days - 1),
        day_to=today,
        now=now,
    )


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class UsageFilters:
    """The narrowing a caller asked for, on top of their data scope."""

    day_from: date
    day_to: date
    user_id: int | None = None
    group_id: int | None = None
    workspace_id: int | None = None
    workload_kind: UsageKind | None = None
    gpu_type: str | None = None


def _base(actor: User, filters: UsageFilters) -> Select[Any]:
    """Scope-filtered, range-filtered statement over the fact table.

    ``scope_filter`` is not optional. Without it a group administrator's report
    would include every other lab's consumption, and the row-level filter is the
    only thing standing between the two.
    """
    stmt = select(UsageDaily).where(
        UsageDaily.day >= filters.day_from, UsageDaily.day <= filters.day_to
    )
    stmt = policy.scope_filter(stmt, UsageDaily, actor)

    if filters.user_id is not None:
        stmt = stmt.where(UsageDaily.user_id == filters.user_id)
    if filters.group_id is not None:
        stmt = stmt.where(UsageDaily.group_id == filters.group_id)
    if filters.workspace_id is not None:
        stmt = stmt.where(UsageDaily.workspace_id == filters.workspace_id)
    if filters.workload_kind is not None:
        stmt = stmt.where(UsageDaily.workload_kind == filters.workload_kind)
    if filters.gpu_type is not None:
        stmt = stmt.where(UsageDaily.gpu_type == filters.gpu_type)
    return stmt


#: Columns summed for every total. Kept in one place so the totals endpoint, the
#: time series and the grouped report cannot disagree about what they measure.
_MEASURES = (
    func.coalesce(func.sum(UsageDaily.runtime_seconds), 0),
    func.coalesce(func.sum(UsageDaily.gpu_seconds), 0),
    func.coalesce(func.sum(UsageDaily.cpu_milli_seconds), 0),
    func.coalesce(func.sum(UsageDaily.memory_mb_seconds), 0),
    func.coalesce(func.sum(UsageDaily.workload_count), 0),
    func.count(func.distinct(UsageDaily.user_id)),
    func.count(func.distinct(UsageDaily.workspace_id)),
)


def _totals_from_row(row: Sequence[Any] | None) -> UsageTotals:
    if row is None:
        return UsageTotals()
    return UsageTotals(
        runtime_seconds=int(row[0]),
        gpu_seconds=int(row[1]),
        cpu_milli_seconds=int(row[2]),
        memory_mb_seconds=int(row[3]),
        workload_count=int(row[4]),
        user_count=int(row[5]),
        workspace_count=int(row[6]),
    )


async def totals(session: AsyncSession, actor: User, filters: UsageFilters) -> UsageTotals:
    row = (await session.execute(_base(actor, filters).with_only_columns(*_MEASURES))).one()
    return _totals_from_row(row)


def scope_is_self_only(actor: User) -> bool:
    """Whether this actor's scope reduces to their own rows.

    Used to decide whether showing "your share" beside the total would just be
    the same figures twice. It asks the policy engine rather than inspecting
    roles, so it stays correct if the scope rules change.
    """
    scope = policy.resolve_scope(actor)
    return not scope.all_access and not scope.group_ids


async def window_covered(session: AsyncSession, filters: UsageFilters) -> bool:
    """Whether the rollup has produced any row at all inside this window.

    Deliberately **not** scope-filtered. The question is "has the job run?",
    which is a property of the table rather than of the caller's data. A user
    with no consumption in a fully-computed window should see zeros and trust
    them; an all-zero *uncomputed* window is a different situation, and the UI
    says so instead of drawing a flat line.
    """
    exists = await session.scalar(
        select(UsageDaily.id)
        .where(UsageDaily.day >= filters.day_from, UsageDaily.day <= filters.day_to)
        .limit(1)
    )
    return exists is not None


async def daily_series(
    session: AsyncSession, actor: User, filters: UsageFilters
) -> list[DailyPoint]:
    """One point per day, **including days with no usage**.

    Gaps matter. A chart that silently omits empty days draws a straight line
    across a week when the cluster was idle, which reads as "steady usage" —
    the opposite of the truth. Filling the zeros here means every consumer of
    this endpoint gets the honest picture without having to remember to.
    """
    stmt = (
        _base(actor, filters).with_only_columns(UsageDaily.day, *_MEASURES).group_by(UsageDaily.day)
    )
    rows = (await session.execute(stmt.order_by(UsageDaily.day))).all()

    by_day = {row[0]: _totals_from_row(row[1:]) for row in rows}
    series: list[DailyPoint] = []
    cursor = filters.day_from
    while cursor <= filters.day_to:
        series.append(DailyPoint(day=cursor, totals=by_day.get(cursor, UsageTotals())))
        cursor += timedelta(days=1)
    return series


#: Dimensions whose bucket key is a value in its own right rather than a row id.
#: A GPU model breakdown has no id to link back to, and inventing one would
#: invite the frontend to build a drill-down that cannot exist.
_KEYLESS_DIMENSIONS = frozenset({"gpu_type", "workload_kind"})

#: Dimensions the grouped report understands.
BREAKDOWN_DIMENSIONS: dict[str, str] = {
    "user": "按用户",
    "group": "按用户组",
    "workspace": "按工作空间",
    "gpu_type": "按加速卡型号",
    "workload_kind": "按负载类型",
}


async def breakdown(
    session: AsyncSession,
    actor: User,
    filters: UsageFilters,
    *,
    by: str,
    limit: int = 50,
) -> list[BreakdownRow]:
    """Group the totals by one dimension, largest first.

    Every dimension is resolved to a *label* here rather than in the frontend:
    the caller may legitimately not be allowed to read the user directory, but
    they are allowed to know that somebody consumed 400 GPU-hours. Sending a
    bare id would render as "#7" and send them looking for a lookup they cannot
    perform.

    Names for records that have since been deleted are reported as such instead
    of being dropped — usage that happened must not vanish from a report because
    the account was closed.
    """
    if by not in BREAKDOWN_DIMENSIONS:
        raise BadRequestError(f"不支持的分组维度：{by}", code="usage_unknown_dimension")
    limit = max(1, min(limit, 500))

    column = {
        "user": UsageDaily.user_id,
        "group": UsageDaily.group_id,
        "workspace": UsageDaily.workspace_id,
        "gpu_type": UsageDaily.gpu_type,
        "workload_kind": UsageDaily.workload_kind,
    }[by]

    stmt = (
        _base(actor, filters)
        .with_only_columns(column, *_MEASURES)
        .group_by(column)
        # Ordered by GPU-seconds rather than by name: the interesting bucket is
        # the one at the top, and alphabetical order buries it.
        .order_by(func.coalesce(func.sum(UsageDaily.gpu_seconds), 0).desc())
        .limit(limit)
    )
    rows = (await session.execute(stmt)).all()

    labels = await _labels_for(session, by, [row[0] for row in rows])
    kind_counts = await _kind_counts(session, actor, filters, by, column, [row[0] for row in rows])

    result: list[BreakdownRow] = []
    for row in rows:
        key = row[0]
        entity_id = None if by in _KEYLESS_DIMENSIONS else int(key)
        result.append(
            BreakdownRow(
                key=str(key),
                label=labels.get(str(key), str(key)),
                entity_id=entity_id,
                totals=_totals_from_row(row[1:]),
                kinds=kind_counts.get(str(key), {}),
            )
        )
    return result


async def _labels_for(session: AsyncSession, by: str, keys: Sequence[Any]) -> dict[str, str]:
    """Resolve bucket keys to human labels in one query per dimension."""
    if by == "gpu_type":
        # The value *is* the label; "none" is the only one worth spelling out.
        return {str(key): ("未使用加速卡" if key == "none" else str(key)) for key in keys}
    if by == "workload_kind":
        return {
            UsageKind.INSTANCE.value: "开发实例",
            UsageKind.TASK.value: "自定义任务",
        }

    ids = [int(key) for key in keys]
    if not ids:
        return {}

    if by == "user":
        rows = (
            await session.execute(
                select(User.id, User.username, User.full_name).where(User.id.in_(ids))
            )
        ).all()
        return {str(row[0]): (row[2] or row[1] or f"用户 #{row[0]}") for row in rows}
    if by == "group":
        rows = (
            await session.execute(
                select(Group.id, Group.name, Group.display_name).where(Group.id.in_(ids))
            )
        ).all()
        return {str(row[0]): (row[2] or row[1] or f"用户组 #{row[0]}") for row in rows}

    rows = (
        await session.execute(
            select(Workspace.id, Workspace.name, Workspace.display_name).where(
                Workspace.id.in_(ids)
            )
        )
    ).all()
    return {str(row[0]): (row[2] or row[1] or f"工作空间 #{row[0]}") for row in rows}


async def _kind_counts(
    session: AsyncSession,
    actor: User,
    filters: UsageFilters,
    by: str,
    column: Any,
    keys: Sequence[Any],
) -> dict[str, dict[str, int]]:
    """Per-bucket split between instances and tasks.

    A second grouped query rather than a python-side sum, because the first query
    is already limited to the top N buckets and the split has to describe those
    same buckets.
    """
    if by == "workload_kind" or not keys:
        return {}
    stmt = (
        _base(actor, filters)
        .where(column.in_(keys))
        .with_only_columns(
            column,
            UsageDaily.workload_kind,
            func.coalesce(func.sum(UsageDaily.gpu_seconds), 0),
        )
        .group_by(column, UsageDaily.workload_kind)
    )
    out: dict[str, dict[str, int]] = {}
    for key, kind, gpu_seconds in (await session.execute(stmt)).all():
        out.setdefault(str(key), {})[str(kind)] = int(gpu_seconds)
    return out


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------
def _csv_text(rows: Sequence[Sequence[Any]]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        [
            "日期",
            "用户",
            "用户组",
            "工作空间",
            "负载类型",
            "加速卡",
            "运行时长(小时)",
            "GPU卡时",
            "CPU核时",
            "内存GB时",
            "负载数",
        ]
    )
    writer.writerows(rows)
    return buffer.getvalue()


async def export_rows(session: AsyncSession, actor: User, filters: UsageFilters) -> tuple[str, int]:
    """Render the range as CSV, one row per (day, user, workspace, kind, gpu).

    Every row of the underlying grain is exported rather than a pre-aggregated
    summary, because the point of an export is that the recipient can pivot it
    their own way — and a summary that has already collapsed a dimension cannot
    be un-collapsed.

    The BOM is deliberate: Excel on Windows reads a UTF-8 CSV without it as the
    system code page, and a report full of mojibake gets sent back as "broken".
    """
    stmt = _base(actor, filters).order_by(
        UsageDaily.day, UsageDaily.user_id, UsageDaily.workspace_id
    )
    facts = (await session.scalars(stmt)).all()

    labels = await _labels_for(session, "user", sorted({fact.user_id for fact in facts}))
    group_labels = await _labels_for(session, "group", sorted({fact.group_id for fact in facts}))
    workspace_labels = await _labels_for(
        session, "workspace", sorted({fact.workspace_id for fact in facts})
    )

    rows: list[list[Any]] = []
    for fact in facts:
        rows.append(
            [
                fact.day.isoformat(),
                labels.get(str(fact.user_id), f"用户 #{fact.user_id}"),
                group_labels.get(str(fact.group_id), f"用户组 #{fact.group_id}"),
                workspace_labels.get(str(fact.workspace_id), f"工作空间 #{fact.workspace_id}"),
                "开发实例" if fact.workload_kind == UsageKind.INSTANCE else "自定义任务",
                "未使用加速卡" if fact.gpu_type == "none" else fact.gpu_type,
                round(fact.runtime_seconds / 3600, 3),
                round(fact.gpu_seconds / 3600, 3),
                round(fact.cpu_milli_seconds / 1000 / 3600, 3),
                round(fact.memory_mb_seconds / 1024 / 3600, 3),
                fact.workload_count,
            ]
        )
    return "\ufeff" + _csv_text(rows), len(rows)
