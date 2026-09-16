"""Recording and reading the workload timeline.

Deliberately tiny. The value is in *always* recording, not in clever queries: a
status change that nobody wrote down is the one that gets asked about three
weeks later, and by then Kubernetes has forgotten it.
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import WorkloadEventType, WorkloadKind
from app.models.user import User
from app.models.workload_event import WorkloadEvent


async def record(
    session: AsyncSession,
    *,
    kind: WorkloadKind,
    workload_id: int,
    event_type: WorkloadEventType,
    message: str | None = None,
    from_status: str | None = None,
    to_status: str | None = None,
    actor: User | None = None,
) -> WorkloadEvent:
    """Append one event. Never raises for a missing actor.

    ``actor=None`` is meaningful: it marks something the reconciler or the
    cluster did, and that distinction is most of what makes a timeline readable.
    """
    event = WorkloadEvent(
        workload_kind=kind,
        workload_id=workload_id,
        event_type=event_type,
        message=message[:512] if message else None,
        from_status=from_status,
        to_status=to_status,
        actor_user_id=actor.id if actor is not None else None,
        actor_username=actor.username if actor is not None else None,
    )
    session.add(event)
    return event


async def record_status(
    session: AsyncSession,
    *,
    kind: WorkloadKind,
    workload_id: int,
    from_status: str | None,
    to_status: str | None,
    message: str | None = None,
    actor: User | None = None,
) -> WorkloadEvent | None:
    """Append a status change, unless nothing actually changed.

    A reconciler runs every minute; recording "still running" each time would
    bury the two entries that matter under a thousand that do not.
    """
    if from_status == to_status:
        return None
    return await record(
        session,
        kind=kind,
        workload_id=workload_id,
        event_type=WorkloadEventType.STATUS_CHANGED,
        from_status=from_status,
        to_status=to_status,
        message=message,
        actor=actor,
    )


async def list_events(
    session: AsyncSession,
    *,
    kind: WorkloadKind,
    workload_id: int,
    limit: int = 100,
) -> Sequence[WorkloadEvent]:
    """Newest first, which is the order a timeline is read in."""
    return (
        (
            await session.execute(
                select(WorkloadEvent)
                .where(
                    WorkloadEvent.workload_kind == kind,
                    WorkloadEvent.workload_id == workload_id,
                )
                .order_by(WorkloadEvent.created_at.desc(), WorkloadEvent.id.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
