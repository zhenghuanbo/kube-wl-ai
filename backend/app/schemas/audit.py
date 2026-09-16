"""Audit-trail read models."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import Field

from app.models.audit import AuditLog
from app.schemas.base import ApiModel


class AuditLogRead(ApiModel):
    """One audit row.

    ``actorUsername`` is read from the denormalised column rather than from the
    relationship: it is the value recorded *at the time*, and a user renamed
    since then must not retroactively rewrite history.
    """

    id: int
    created_at: datetime
    actor_user_id: int | None = None
    actor_username: str
    action: str
    object_type: str | None = None
    object_id: str | None = None
    status: str
    detail: dict[str, Any] | None = None
    ip_address: str | None = None
    request_id: str | None = None
    error_message: str | None = None

    @classmethod
    def from_row(cls, row: AuditLog) -> AuditLogRead:
        return cls(
            id=row.id,
            created_at=row.created_at,
            actor_user_id=row.actor_user_id,
            actor_username=row.actor_username,
            action=row.action,
            object_type=row.object_type,
            object_id=row.object_id,
            status=row.status,
            detail=row.detail,
            ip_address=row.ip_address,
            request_id=row.request_id,
            error_message=row.error_message,
        )


class AuditActionSummary(ApiModel):
    """A filter option: an action code and how many rows it has."""

    action: str
    count: int


class AuditMeta(ApiModel):
    """What the filter controls should offer.

    Every list is built within the caller's scope, so a group administrator is
    not shown a value that only exists in somebody else's history.
    """

    actions: list[str]
    object_types: list[str]
    statuses: list[str] = Field(default_factory=lambda: ["success", "denied", "error"])
