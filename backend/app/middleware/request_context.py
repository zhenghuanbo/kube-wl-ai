"""Request id propagation, context binding and access logging."""

from __future__ import annotations

import time
from typing import Any
from uuid import uuid4

import structlog
from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.context import client_ip_var, request_id_var
from app.core.logging import get_logger

logger = get_logger("app.request")

#: Honoured when a reverse proxy already assigned a correlation id.
REQUEST_ID_HEADER = "X-Request-ID"


def _client_ip(scope: Scope) -> str | None:
    client = scope.get("client")
    if not client:
        return None
    # Respect a proxy's forwarded header only for the left-most hop.
    headers = Headers(scope=scope)
    forwarded = headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return str(client[0])


class RequestContextMiddleware:
    """Assign a request id, bind it to structlog, and log the outcome."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        request_id = headers.get(REQUEST_ID_HEADER) or uuid4().hex
        ip_address = _client_ip(scope)

        # Starlette populates scope["state"] for Request.state; create it here so
        # the downstream dependencies always have somewhere to write.
        state: dict[str, Any] = scope.setdefault("state", {})

        request_token = request_id_var.set(request_id)
        client_token = client_ip_var.set(ip_address)
        structlog.contextvars.bind_contextvars(
            request_id=request_id,
            method=scope.get("method"),
            path=scope.get("path"),
        )

        started = time.perf_counter()
        status_code = 500

        async def send_wrapper(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                MutableHeaders(scope=message)[REQUEST_ID_HEADER] = request_id
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception:
            duration_ms = round((time.perf_counter() - started) * 1000, 2)
            logger.exception(
                "request_failed",
                status_code=status_code,
                duration_ms=duration_ms,
                user_id=state.get("user_id"),
            )
            raise
        else:
            duration_ms = round((time.perf_counter() - started) * 1000, 2)
            logger.info(
                "request_completed",
                status_code=status_code,
                duration_ms=duration_ms,
                user_id=state.get("user_id"),
                ip_address=ip_address,
            )
        finally:
            structlog.contextvars.clear_contextvars()
            request_id_var.reset(request_token)
            client_ip_var.reset(client_token)
