"""Domain-level audit helper.

The middleware in :mod:`app.middleware.audit` records what it can infer from the
HTTP exchange. When a service knows more — which object was touched, what
changed — it should record that explicitly through :func:`record_audit` and pass
the request so the middleware does not add a second, coarser row.
"""

from __future__ import annotations

from typing import Any

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import client_ip_var, get_request_id
from app.models.audit import AuditLog
from app.models.user import User

#: ``success`` | ``denied`` | ``error``
AuditStatus = str


def record_audit(
    session: AsyncSession,
    *,
    action: str,
    actor: User | None = None,
    object_type: str | None = None,
    object_id: str | int | None = None,
    status: AuditStatus = "success",
    detail: dict[str, Any] | None = None,
    error_message: str | None = None,
    request: Request | None = None,
    actor_username: str | None = None,
) -> AuditLog:
    """Stage an audit entry on the caller's session.

    It is committed by the same transaction as the change it describes, so the
    trail can never claim an action that was rolled back. Pass ``request`` so the
    HTTP-level middleware skips this request.
    """
    entry = AuditLog(
        actor_user_id=actor.id if actor else None,
        # Denormalised so the trail survives deletion of the user.
        actor_username=(actor.username if actor else actor_username) or "anonymous",
        action=action[:96],
        object_type=object_type,
        object_id=str(object_id) if object_id is not None else None,
        status=status,
        detail=detail or {},
        ip_address=(client_ip_var.get() or "")[:64] or None,
        request_id=(get_request_id() or "")[:64] or None,
        error_message=error_message,
    )
    session.add(entry)

    if request is not None:
        request.state.audit_recorded = True

    return entry
