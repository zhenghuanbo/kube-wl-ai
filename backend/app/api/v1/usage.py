"""Usage centre endpoints.

Two rules shape this module.

**Every read is scope-filtered, including the aggregates.** It is tempting to
treat "just a total" as harmless, but a total is exactly what a competing lab
wants: ``GET /usage/summary`` for a group administrator must add up their group
and nobody else's. The filter is applied in :mod:`app.services.usage` through the
same ``scope_filter`` every other resource uses, so there is one place to audit
rather than one per endpoint.

**Export is a separate permission.** Reading a number on a page and walking away
with a file of every colleague's daily consumption are different acts. Export is
also the only endpoint here whose response leaves the browser, so it is audited
explicitly — the HTTP middleware only records mutations, and "who downloaded the
usage report" is a question worth being able to answer.
"""

from __future__ import annotations

from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, Response

from app.core.audit import record_audit
from app.core.deps import CurrentUser, DbSession, require
from app.models.enums import UsageKind
from app.schemas.usage import (
    UsageBreakdownDimensionRead,
    UsageBreakdownRead,
    UsageBreakdownRowRead,
    UsageDayRead,
    UsageKindRead,
    UsageMetaRead,
    UsageRollupRequest,
    UsageRollupResultRead,
    UsageSeriesRead,
    UsageSummaryRead,
    totals_to_read,
)
from app.services import usage as usage_service

router = APIRouter(tags=["usage"])

CanRead = Depends(require("usage:read_self"))
#: Exporting is administrative. A group administrator exports their own group;
#: a global administrator exports everything; a normal user reads the page.
CanExport = Depends(require("usage:export"))
#: Recomputing the rollup rewrites rows for every tenant at once, so it is not
#: something a group administrator may trigger for their own group.
CanRollup = Depends(require("usage:manage"))


def _filters(
    *,
    day_from: date | None,
    day_to: date | None,
    user_id: int | None,
    group_id: int | None,
    workspace_id: int | None,
    workload_kind: UsageKind | None,
    gpu_type: str | None,
    default_days: int,
) -> usage_service.UsageFilters:
    start, end = usage_service.resolve_window(day_from, day_to, default_days=default_days)
    return usage_service.UsageFilters(
        day_from=start,
        day_to=end,
        user_id=user_id,
        group_id=group_id,
        workspace_id=workspace_id,
        workload_kind=workload_kind,
        gpu_type=gpu_type,
    )


@router.get("/usage/summary", response_model=UsageSummaryRead)
async def usage_summary(
    session: DbSession,
    actor: CurrentUser,
    _: Annotated[None, CanRead],
    day_from: Annotated[date | None, Query(alias="dayFrom")] = None,
    day_to: Annotated[date | None, Query(alias="dayTo")] = None,
    user_id: Annotated[int | None, Query(alias="userId", ge=1)] = None,
    group_id: Annotated[int | None, Query(alias="groupId", ge=1)] = None,
    workspace_id: Annotated[int | None, Query(alias="workspaceId", ge=1)] = None,
    workload_kind: Annotated[UsageKind | None, Query(alias="workloadKind")] = None,
    gpu_type: Annotated[str | None, Query(alias="gpuType", max_length=32)] = None,
) -> UsageSummaryRead:
    """Headline totals for the window, plus the caller's own share of them."""
    filters = _filters(
        day_from=day_from,
        day_to=day_to,
        user_id=user_id,
        group_id=group_id,
        workspace_id=workspace_id,
        workload_kind=workload_kind,
        gpu_type=gpu_type,
        default_days=30,
    )
    totals = await usage_service.totals(session, actor, filters)
    covered = await usage_service.window_covered(session, filters)

    # An administrator's own share is worth showing beside the group's, but a
    # normal user *is* the self case — repeating the same numbers twice would be
    # noise, so it is omitted when the scope is already just them.
    self_totals = None
    if not usage_service.scope_is_self_only(actor) and user_id is None:
        self_filters = usage_service.UsageFilters(
            day_from=filters.day_from,
            day_to=filters.day_to,
            user_id=actor.id,
            group_id=filters.group_id,
            workspace_id=filters.workspace_id,
            workload_kind=filters.workload_kind,
            gpu_type=filters.gpu_type,
        )
        self_totals = totals_to_read(await usage_service.totals(session, actor, self_filters))

    return UsageSummaryRead(
        day_from=filters.day_from,
        day_to=filters.day_to,
        totals=totals_to_read(totals),
        self_totals=self_totals,
        covered=covered,
    )


@router.get("/usage/series", response_model=UsageSeriesRead)
async def usage_series(
    session: DbSession,
    actor: CurrentUser,
    _: Annotated[None, CanRead],
    day_from: Annotated[date | None, Query(alias="dayFrom")] = None,
    day_to: Annotated[date | None, Query(alias="dayTo")] = None,
    user_id: Annotated[int | None, Query(alias="userId", ge=1)] = None,
    group_id: Annotated[int | None, Query(alias="groupId", ge=1)] = None,
    workspace_id: Annotated[int | None, Query(alias="workspaceId", ge=1)] = None,
    workload_kind: Annotated[UsageKind | None, Query(alias="workloadKind")] = None,
    gpu_type: Annotated[str | None, Query(alias="gpuType", max_length=32)] = None,
) -> UsageSeriesRead:
    """One point per day, with empty days present as zeros."""
    filters = _filters(
        day_from=day_from,
        day_to=day_to,
        user_id=user_id,
        group_id=group_id,
        workspace_id=workspace_id,
        workload_kind=workload_kind,
        gpu_type=gpu_type,
        default_days=30,
    )
    points = await usage_service.daily_series(session, actor, filters)
    return UsageSeriesRead(
        day_from=filters.day_from,
        day_to=filters.day_to,
        points=[
            UsageDayRead(day=point.day, totals=totals_to_read(point.totals)) for point in points
        ],
    )


@router.get("/usage/breakdown", response_model=UsageBreakdownRead)
async def usage_breakdown(
    session: DbSession,
    actor: CurrentUser,
    _: Annotated[None, CanRead],
    by: Annotated[str, Query()] = "user",
    day_from: Annotated[date | None, Query(alias="dayFrom")] = None,
    day_to: Annotated[date | None, Query(alias="dayTo")] = None,
    user_id: Annotated[int | None, Query(alias="userId", ge=1)] = None,
    group_id: Annotated[int | None, Query(alias="groupId", ge=1)] = None,
    workspace_id: Annotated[int | None, Query(alias="workspaceId", ge=1)] = None,
    workload_kind: Annotated[UsageKind | None, Query(alias="workloadKind")] = None,
    gpu_type: Annotated[str | None, Query(alias="gpuType", max_length=32)] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
) -> UsageBreakdownRead:
    """Totals grouped by user, group, workspace, accelerator or workload kind."""
    filters = _filters(
        day_from=day_from,
        day_to=day_to,
        user_id=user_id,
        group_id=group_id,
        workspace_id=workspace_id,
        workload_kind=workload_kind,
        gpu_type=gpu_type,
        default_days=30,
    )
    rows = await usage_service.breakdown(session, actor, filters, by=by, limit=limit)
    return UsageBreakdownRead(
        by=by,
        day_from=filters.day_from,
        day_to=filters.day_to,
        truncated=len(rows) >= limit,
        rows=[
            UsageBreakdownRowRead(
                key=row.key,
                label=row.label,
                entity_id=row.entity_id,
                totals=totals_to_read(row.totals),
                kinds=row.kinds,
            )
            for row in rows
        ],
    )


@router.get("/usage/meta", response_model=UsageMetaRead)
async def usage_meta(_: Annotated[None, CanRead]) -> UsageMetaRead:
    """Filter vocabulary, so the client's controls cannot drift from the API."""
    return UsageMetaRead(
        dimensions=[
            UsageBreakdownDimensionRead(key=key, label=label)
            for key, label in usage_service.BREAKDOWN_DIMENSIONS.items()
        ],
        kinds=[
            UsageKindRead(value=UsageKind.INSTANCE, label="开发实例"),
            UsageKindRead(value=UsageKind.TASK, label="自定义任务"),
        ],
        max_range_days=usage_service.MAX_QUERY_DAYS,
        default_rollup_lookback_days=usage_service.DEFAULT_ROLLUP_LOOKBACK_DAYS,
    )


@router.get("/usage/export")
async def usage_export(
    session: DbSession,
    actor: CurrentUser,
    request: Request,
    _: Annotated[None, CanExport],
    day_from: Annotated[date | None, Query(alias="dayFrom")] = None,
    day_to: Annotated[date | None, Query(alias="dayTo")] = None,
    user_id: Annotated[int | None, Query(alias="userId", ge=1)] = None,
    group_id: Annotated[int | None, Query(alias="groupId", ge=1)] = None,
    workspace_id: Annotated[int | None, Query(alias="workspaceId", ge=1)] = None,
    workload_kind: Annotated[UsageKind | None, Query(alias="workloadKind")] = None,
    gpu_type: Annotated[str | None, Query(alias="gpuType", max_length=32)] = None,
) -> Response:
    """Download the window as CSV, one row per day and workload bucket."""
    filters = _filters(
        day_from=day_from,
        day_to=day_to,
        user_id=user_id,
        group_id=group_id,
        workspace_id=workspace_id,
        workload_kind=workload_kind,
        gpu_type=gpu_type,
        default_days=30,
    )
    body, rows = await usage_service.export_rows(session, actor, filters)

    record_audit(
        session,
        action="usage:export",
        actor=actor,
        object_type="usage",
        object_id=f"{filters.day_from}..{filters.day_to}",
        detail={"rows": rows, "dayFrom": str(filters.day_from), "dayTo": str(filters.day_to)},
        request=request,
    )
    await session.commit()

    filename = f"usage-{filters.day_from}-{filters.day_to}.csv"
    return Response(
        content=body,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/usage/rollup", response_model=UsageRollupResultRead)
async def usage_rollup(
    session: DbSession,
    actor: CurrentUser,
    request: Request,
    _: Annotated[None, CanRollup],
    payload: UsageRollupRequest | None = None,
) -> UsageRollupResultRead:
    """Recompute the daily rollup for a window.

    Always available to a global administrator, and run on a schedule besides.
    Both ranges are optional; omitting them recomputes the default lookback,
    which is what an operator wants after noticing today's number looks wrong.
    """
    body = payload or UsageRollupRequest()
    today = date.today()
    if body.day_from is None and body.day_to is None:
        result = await usage_service.rollup_recent(session)
    else:
        start, end = usage_service.resolve_window(
            body.day_from,
            body.day_to,
            default_days=usage_service.DEFAULT_ROLLUP_LOOKBACK_DAYS,
            max_days=usage_service.MAX_ROLLUP_DAYS,
            today=today,
        )
        result = await usage_service.rollup(session, day_from=start, day_to=end)

    record_audit(
        session,
        action="usage:rollup",
        actor=actor,
        object_type="usage",
        object_id=f"{result.day_from}..{result.day_to}",
        detail={
            "rowsWritten": result.rows_written,
            "gpuSeconds": result.gpu_seconds,
        },
        # Without the request the middleware would add its own coarser
        # `usage:rollup` row for the same call.
        request=request,
    )
    await session.commit()
    return UsageRollupResultRead(
        day_from=result.day_from,
        day_to=result.day_to,
        rows_written=result.rows_written,
        instances_scanned=result.instances_scanned,
        tasks_scanned=result.tasks_scanned,
        runtime_seconds=result.runtime_seconds,
        gpu_seconds=result.gpu_seconds,
        outside_window=result.outside_window,
    )
