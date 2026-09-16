"""Reading the audit trail.

Writing has existed since Phase 1 — every mutating request, every denial, every
failed login. This module is the other half: without it the trail is evidence
nobody can produce.

The query is a *read of a security record*, so two things matter more than the
usual listing concerns:

1. **Nothing is writable here.** There is no delete, no update, no "clear". An
   audit trail that its own administrators can edit is not an audit trail.

2. **The row filter is the data scope, not a UI filter.** A group administrator
   holds ``audit:read`` and sees the actions of their own group's members. A
   global administrator sees everything, including rows with no actor at all.
   That last part is not an accident of the scope rule — it is the whole reason
   anonymous failures are only visible to a global administrator, and it is
   asserted in the acceptance tests.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import Select, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import policy
from app.models.audit import AuditLog
from app.models.user import User
from app.services.workload_common import Page

#: Hard ceiling on an export. An administrator can ask for a year of trail, and
#: a year of trail is tens of megabytes of JSON detail columns. Past this the
#: answer is "narrow the range", not a longer wait.
MAX_EXPORT_ROWS = 50_000

#: The statuses the middleware writes. Kept here so the filter is validated
#: against something rather than accepting any string and returning nothing.
AUDIT_STATUSES = ("success", "denied", "error")


@dataclass(frozen=True, slots=True)
class AuditFilters:
    search: str | None = None
    action: str | None = None
    status: str | None = None
    object_type: str | None = None
    object_id: str | None = None
    actor_username: str | None = None
    request_id: str | None = None
    occurred_from: datetime | None = None
    occurred_to: datetime | None = None


def _base_query(session: AsyncSession, actor: User, filters: AuditFilters) -> Select[Any]:
    """The scoped, filtered statement. Every listing path goes through here.

    Building the statement in one place is what keeps the export from drifting
    away from the listing: an export is a listing without a page bound, and if it
    were assembled separately it would eventually be assembled *differently* —
    which on this table means handing somebody rows they cannot see on screen.
    """
    stmt = select(AuditLog)
    stmt = policy.scope_filter(stmt, AuditLog, actor)

    if filters.action:
        # Prefix match: `instance:` selects every instance action, which is how
        # an operator asks "what has been done to this resource type".
        stmt = stmt.where(AuditLog.action.like(f"{filters.action}%"))
    if filters.status:
        stmt = stmt.where(AuditLog.status == filters.status)
    if filters.object_type:
        stmt = stmt.where(AuditLog.object_type == filters.object_type)
    if filters.object_id:
        stmt = stmt.where(AuditLog.object_id == filters.object_id)
    if filters.actor_username:
        stmt = stmt.where(AuditLog.actor_username == filters.actor_username)
    if filters.request_id:
        stmt = stmt.where(AuditLog.request_id == filters.request_id)
    if filters.occurred_from:
        stmt = stmt.where(AuditLog.created_at >= filters.occurred_from)
    if filters.occurred_to:
        stmt = stmt.where(AuditLog.created_at <= filters.occurred_to)
    if filters.search:
        needle = f"%{filters.search}%"
        stmt = stmt.where(
            or_(
                AuditLog.actor_username.ilike(needle),
                AuditLog.action.ilike(needle),
                AuditLog.object_type.ilike(needle),
                AuditLog.object_id.ilike(needle),
                AuditLog.error_message.ilike(needle),
            )
        )
    return stmt


async def list_audit_logs(
    session: AsyncSession,
    actor: User,
    *,
    page: int = 1,
    page_size: int = 20,
    filters: AuditFilters | None = None,
) -> Page[AuditLog]:
    """One page of the trail, newest first.

    Newest first is the only sensible default for a record whose most
    interesting row is almost always the one just written.
    """
    filters = filters or AuditFilters()
    stmt = _base_query(session, actor, filters)

    total = await session.scalar(select(func.count()).select_from(stmt.order_by(None).subquery()))
    rows = await session.scalars(
        stmt.order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    return Page(items=list(rows), total=int(total or 0))


async def export_rows(
    session: AsyncSession,
    actor: User,
    *,
    filters: AuditFilters | None = None,
    limit: int | None = None,
) -> tuple[list[AuditLog], bool]:
    """Rows for CSV export, plus whether the ceiling truncated them.

    The truncation flag is returned rather than logged because the caller writes
    it into the file's first lines: a CSV that silently stops halfway through the
    period it claims to cover is worse than no CSV, and it is exactly the file
    somebody attaches to an incident report.
    """
    # Read at call time rather than captured as a default argument, so an
    # operator (or a test) can tune the ceiling without editing the signature.
    ceiling = MAX_EXPORT_ROWS if limit is None else limit
    filters = filters or AuditFilters()
    stmt = (
        _base_query(session, actor, filters)
        .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
        .limit(ceiling + 1)
    )
    rows = list(await session.scalars(stmt))
    truncated = len(rows) > ceiling
    return rows[:ceiling], truncated


#: Column order for the export. `detail` is JSON-encoded because it is a nested
#: object; flattening it would either lose fields or invent a schema.
EXPORT_COLUMNS = (
    "id",
    "created_at",
    "actor_username",
    "action",
    "object_type",
    "object_id",
    "status",
    "ip_address",
    "request_id",
    "error_message",
    "detail",
)


def to_csv(rows: list[AuditLog], *, truncated: bool = False) -> str:
    """Serialise rows as CSV, with a UTF-8 BOM.

    The BOM is for Excel, which otherwise reads the Chinese columns as mojibake.
    Every other consumer ignores it.
    """
    import json

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(EXPORT_COLUMNS)
    for row in rows:
        writer.writerow(
            [
                row.id,
                row.created_at.isoformat() if row.created_at else "",
                row.actor_username,
                row.action,
                row.object_type or "",
                row.object_id or "",
                row.status,
                row.ip_address or "",
                row.request_id or "",
                row.error_message or "",
                json.dumps(row.detail, ensure_ascii=False) if row.detail else "",
            ]
        )
    if truncated:
        writer.writerow([])
        writer.writerow([f"# 结果被截断：仅包含最近 {len(rows)} 条，请缩小时间范围后重新导出"])
    return "\ufeff" + buffer.getvalue()


async def distinct_values(
    session: AsyncSession, actor: User, column: Any, *, limit: int = 200
) -> list[str]:
    """The values a filter dropdown should offer, within the caller's scope.

    Built from scoped rows rather than from a fixed list, so a group
    administrator is not offered the name of an action nobody in their group has
    ever performed — which would tell them that action exists elsewhere.
    """
    stmt = (
        policy.scope_filter(select(column), AuditLog, actor)
        .where(column.is_not(None))
        .distinct()
        .order_by(column)
        .limit(limit)
    )
    return [str(value) for value in await session.scalars(stmt) if value is not None]
