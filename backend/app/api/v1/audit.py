"""Audit-trail endpoints (plan §12, Phase 8).

Read-only by construction: there is no POST, PUT, PATCH or DELETE in this
module, and there is not meant to be one. An audit trail whose own
administrators can edit it answers a different question than the one it exists
to answer.

`audit:read` is held by group administrators and above. What each of them may
*see* is decided by the data scope (`policy.SCOPE_RULES[AuditLog]`), not by this
module — a group administrator's rows stop at their own group's members, and
rows with no actor (failed logins, unauthenticated probes) belong to no group, so
only a global administrator sees them.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response

from app.core.deps import CurrentUser, DbSession, require
from app.models.audit import AuditLog
from app.schemas.audit import AuditLogRead, AuditMeta
from app.schemas.common import Page, PageMeta
from app.services import audit as audit_service
from app.services.audit import AuditFilters

router = APIRouter(prefix="/audit-logs", tags=["audit"])

CanRead = Annotated[object, Depends(require("audit:read"))]


def _filters(
    search: str | None,
    action: str | None,
    status: str | None,
    object_type: str | None,
    object_id: str | None,
    actor_username: str | None,
    request_id: str | None,
    occurred_from: datetime | None,
    occurred_to: datetime | None,
) -> AuditFilters:
    return AuditFilters(
        search=search,
        action=action,
        status=status,
        object_type=object_type,
        object_id=object_id,
        actor_username=actor_username,
        request_id=request_id,
        occurred_from=occurred_from,
        occurred_to=occurred_to,
    )


@router.get(
    "",
    response_model=Page[AuditLogRead],
    summary="List audit entries visible to the caller",
    dependencies=[Depends(require("audit:read"))],
)
async def list_audit_logs(
    session: DbSession,
    actor: CurrentUser,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=200, alias="pageSize")] = 20,
    search: Annotated[str | None, Query(max_length=150)] = None,
    action: Annotated[str | None, Query(max_length=96)] = None,
    status: Annotated[str | None, Query(max_length=16)] = None,
    object_type: Annotated[str | None, Query(max_length=64, alias="objectType")] = None,
    object_id: Annotated[str | None, Query(max_length=64, alias="objectId")] = None,
    actor_username: Annotated[str | None, Query(max_length=150, alias="actorUsername")] = None,
    request_id: Annotated[str | None, Query(max_length=64, alias="requestId")] = None,
    occurred_from: Annotated[datetime | None, Query(alias="occurredFrom")] = None,
    occurred_to: Annotated[datetime | None, Query(alias="occurredTo")] = None,
) -> Page[AuditLogRead]:
    """Newest first, always.

    `requestId` is the filter that turns this from a browsing screen into a
    diagnostic one: every log line the application emitted while handling a
    request carries the same id, so one value ties the audit row to the trace.
    """
    result = await audit_service.list_audit_logs(
        session,
        actor,
        page=page,
        page_size=page_size,
        filters=_filters(
            search,
            action,
            status,
            object_type,
            object_id,
            actor_username,
            request_id,
            occurred_from,
            occurred_to,
        ),
    )
    pages = (result.total + page_size - 1) // page_size if page_size else 0
    return Page[AuditLogRead](
        items=[AuditLogRead.from_row(row) for row in result.items],
        meta=PageMeta(page=page, page_size=page_size, total=result.total, pages=pages),
    )


@router.get(
    "/meta",
    response_model=AuditMeta,
    summary="Filter options available within the caller's scope",
    dependencies=[Depends(require("audit:read"))],
)
async def audit_meta(session: DbSession, actor: CurrentUser) -> AuditMeta:
    """The values the filter controls should offer.

    Derived from scoped rows rather than from the permission catalogue: offering
    an action that nobody in the caller's scope has performed would disclose that
    the action exists elsewhere.
    """
    return AuditMeta(
        actions=await audit_service.distinct_values(session, actor, AuditLog.action),
        object_types=await audit_service.distinct_values(session, actor, AuditLog.object_type),
    )


@router.get(
    "/export",
    summary="Export audit entries as CSV",
    dependencies=[Depends(require("audit:read"))],
    response_class=Response,
)
async def export_audit_logs(
    session: DbSession,
    actor: CurrentUser,
    search: Annotated[str | None, Query(max_length=150)] = None,
    action: Annotated[str | None, Query(max_length=96)] = None,
    status: Annotated[str | None, Query(max_length=16)] = None,
    object_type: Annotated[str | None, Query(max_length=64, alias="objectType")] = None,
    object_id: Annotated[str | None, Query(max_length=64, alias="objectId")] = None,
    actor_username: Annotated[str | None, Query(max_length=150, alias="actorUsername")] = None,
    request_id: Annotated[str | None, Query(max_length=64, alias="requestId")] = None,
    occurred_from: Annotated[datetime | None, Query(alias="occurredFrom")] = None,
    occurred_to: Annotated[datetime | None, Query(alias="occurredTo")] = None,
) -> Response:
    """The same rows as the listing, without the page bound, as CSV.

    It reuses `AuditFilters` and therefore the same scoped statement as the
    listing. That is not a convenience: an export assembled separately from the
    listing eventually gets assembled *differently*, and on this table the
    difference is handing somebody rows they cannot see on screen.

    When the row ceiling truncates the result, the file says so in its own last
    line. A CSV that stops early while claiming a date range is the one artefact
    most likely to end up in an incident report unexamined.
    """
    rows, truncated = await audit_service.export_rows(
        session,
        actor,
        filters=_filters(
            search,
            action,
            status,
            object_type,
            object_id,
            actor_username,
            request_id,
            occurred_from,
            occurred_to,
        ),
    )
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return Response(
        content=audit_service.to_csv(rows, truncated=truncated),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="audit-{stamp}.csv"'},
    )
