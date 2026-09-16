"""Audit middleware.

Records every mutating API request and every denied attempt. This is the HTTP
level net: it guarantees an entry even for endpoints whose author forgot to log
anything. Services that need richer detail can call
:func:`app.core.audit.record_audit`, which marks the request as already audited
so no duplicate row is written.

Audit writes must never break a request, so failures are logged and swallowed.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qsl

from sqlalchemy.ext.asyncio import async_sessionmaker
from starlette.datastructures import Headers
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.context import client_ip_var, request_id_var
from app.core.logging import get_logger
from app.db.session import get_session_factory
from app.models.audit import AuditLog

logger = get_logger("app.audit")

#: Methods that change state and therefore always produce an audit entry.
AUDITED_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

#: Responses recorded regardless of method: denied actions are always
#: interesting.
AUDITED_STATUS = frozenset({403})

#: Authentication failures are recorded only on auth routes, so a frontend
#: holding an expired token does not flood the trail.
AUDITED_AUTH_STATUS = frozenset({401, 429})

#: Responses that represent a refusal rather than a failure.
REJECTED_STATUS = frozenset({401, 403, 429})

#: Paths that would only add noise.
SKIP_PATHS = frozenset({"/healthz", "/readyz", "/metrics", "/docs", "/redoc", "/openapi.json"})

_VERB_BY_METHOD = {
    "POST": "create",
    "PUT": "update",
    "PATCH": "update",
    "DELETE": "delete",
}

#: Paths whose derived name would be misleading; keyed by ``(method, shape)``.
ACTION_OVERRIDES: dict[tuple[str, str], str] = {
    ("POST", "auth/login"): "auth:login",
    ("POST", "auth/refresh"): "auth:refresh",
    ("POST", "auth/logout"): "auth:logout",
    ("POST", "users/{}/roles"): "user:grant_role",
    ("DELETE", "users/{}/roles"): "user:revoke_role",
    ("POST", "users/{}/password"): "user:reset_password",
    ("POST", "groups/{}/members"): "group:add_member",
    ("DELETE", "groups/{}/members/{}"): "group:remove_member",
    ("POST", "groups/sync"): "group:sync_directory",
}

#: URL segments are plural, the domain vocabulary is singular. Success paths are
#: audited by the service layer with names like ``user:create``; canonicalising
#: here makes the middleware's fallback for failures use the same vocabulary, so
#: one operation is always one action string regardless of its outcome. The
#: outcome itself lives in :attr:`AuditLog.status`.
RESOURCE_ALIASES: dict[str, str] = {
    "users": "user",
    "groups": "group",
    "roles": "role",
    "permissions": "permission",
}


def canonical_resource(resource: str | None) -> str:
    """Map a URL segment onto the domain noun used in audit actions."""
    if not resource:
        return "api"
    return RESOURCE_ALIASES.get(resource, resource)


#: Query parameters that must never be written to the audit trail.
_SENSITIVE_QUERY_KEYS = frozenset({"token", "password", "secret", "refresh_token", "code"})


def describe_request(path: str, api_prefix: str) -> tuple[str | None, str | None, str]:
    """Derive ``(resource, object_id, shape)`` from a request path.

    ``/api/v1/users/12/roles`` → ``("users", "12", "users/{}/roles")``
    """
    relative = path
    if api_prefix and relative.startswith(api_prefix):
        relative = relative[len(api_prefix) :]

    parts = [part for part in relative.split("/") if part]
    if not parts:
        return None, None, ""

    resource = parts[0]
    object_id = parts[1] if len(parts) > 1 and parts[1].isdigit() else None
    shape = "/".join("{}" if part.isdigit() else part for part in parts)
    return resource, object_id, shape


def _safe_query(scope: Scope) -> dict[str, str]:
    raw = scope.get("query_string", b"").decode("latin-1")
    if not raw:
        return {}
    return {
        key: ("***" if key in _SENSITIVE_QUERY_KEYS else value)
        for key, value in parse_qsl(raw, keep_blank_values=False)
    }


def _should_audit(method: str, path: str, status_code: int, api_prefix: str) -> bool:
    if method in AUDITED_METHODS:
        return True
    if status_code in AUDITED_STATUS:
        return True
    if status_code in AUDITED_AUTH_STATUS:
        relative = path[len(api_prefix) :] if path.startswith(api_prefix) else path
        return relative.startswith("/auth/")
    return False


class AuditMiddleware:
    """Persist an :class:`AuditLog` row for mutating and denied requests."""

    def __init__(self, app: ASGIApp, *, api_prefix: str = "/api/v1") -> None:
        self.app = app
        self.api_prefix = api_prefix

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        method = scope.get("method", "GET")

        if path in SKIP_PATHS or not path.startswith(self.api_prefix):
            await self.app(scope, receive, send)
            return

        state: dict[str, Any] = scope.setdefault("state", {})
        status_code = 500

        async def send_wrapper(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
            await send(message)

        await self.app(scope, receive, send_wrapper)

        if state.get("audit_recorded"):
            return
        if not _should_audit(method, path, status_code, self.api_prefix):
            return

        await self._write(
            scope=scope,
            state=state,
            method=method,
            path=path,
            status_code=status_code,
        )

    async def _write(
        self,
        *,
        scope: Scope,
        state: dict[str, Any],
        method: str,
        path: str,
        status_code: int,
    ) -> None:
        resource, object_id, shape = describe_request(path, self.api_prefix)
        verb = _VERB_BY_METHOD.get(method, method.lower())

        override = ACTION_OVERRIDES.get((method, shape))
        if override is not None:
            action = override
        elif status_code == 403:
            # A permission denial is not an attempted operation on a resource —
            # it is the only case where the caller never got that far.
            action = "access:denied"
        else:
            action = f"{canonical_resource(resource)}:{verb}"

        if status_code in REJECTED_STATUS:
            audit_status = "denied"
        elif status_code >= 400:
            audit_status = "error"
        else:
            audit_status = "success"

        entry = AuditLog(
            actor_user_id=state.get("user_id"),
            actor_username=state.get("username") or "anonymous",
            action=action[:96],
            object_type=resource,
            object_id=object_id,
            status=audit_status,
            detail={
                "method": method,
                "path": shape or path,
                "status_code": status_code,
                "query": _safe_query(scope),
                "user_agent": (Headers(scope=scope).get("user-agent") or "")[:255],
            },
            ip_address=(client_ip_var.get() or "")[:64] or None,
            request_id=(request_id_var.get() or "")[:64] or None,
            error_message=state.get("error_message"),
        )

        try:
            factory: async_sessionmaker[Any] = get_session_factory()
            async with factory() as session:
                session.add(entry)
                await session.commit()
        except Exception:
            # Never let an audit failure affect the response the client already
            # received — but make it loud.
            logger.exception(
                "audit_write_failed",
                action=action,
                path=path,
                actor=entry.actor_username,
            )
